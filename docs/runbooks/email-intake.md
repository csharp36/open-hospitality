# Runbook — emailed night-audit intake (OH-23)

What to do to turn emailed night audits on, and what to expect afterwards.

Design: [`../design/2026-09-21-oh23-emailed-intake-design.md`](../design/2026-09-21-oh23-emailed-intake-design.md).
Worker: [`../../cloudflare/email-intake/`](../../cloudflare/email-intake/).

**The shape of it.** A PMS mails its night audit to a per-property address at
`intake.mandati.ai`. Cloudflare Email Routing hands every such message to the
`oh-email-intake` Worker, which signs the raw bytes and POSTs them to
`POST /api/intake/email`. The app checks the signature, resolves the address to
a property, checks the sender, and runs each attachment through the same
ingestion gate an operator's upload goes through. Anything the app does not
take, the worker mails to a fallback mailbox.

> **The fallback mailbox is an ops mailbox, and it holds unredacted mail.**
> Everything that reaches it is a night-audit report that never passed the
> redaction gate: guest names, folio detail, and whatever card digits the PMS
> put in its own export. Give it to the people who already handle raw PMS
> exports, on an account with 2FA, and do not point it at a shared alias, a
> ticketing system, or anyone's personal inbox.

---

## Setup

### 1. Enable Email Routing on `intake.mandati.ai`

`mandati.ai` is already on Cloudflare nameservers and its MX already points at
Email Routing. The subdomain is separate and starts off: in the dashboard,
**Email → Email Routing → Settings → Custom addresses / subdomains**, add
`intake.mandati.ai`. Cloudflare adds the subdomain's own MX records when it is
enabled.

Check the records landed before going on:

```sh
dig +short MX intake.mandati.ai
# expect route1.mx.cloudflare.net, route2..., route3...
```

Enabling a subdomain writes more than those three. Cloudflare also adds a
`cf-bounce` MX for the subdomain's bounce handling, an SPF TXT record, and the
DKIM and DMARC records Email Routing needs. Leave all of them; deleting the
SPF or DKIM record does not stop mail arriving, it just degrades the
`Authentication-Results` header the sender policy is built on, and the symptom
shows up much later as `sender_rejected`.

Whatever subdomain you choose here must be the same string as
`USALI_EMAIL_INTAKE_DOMAIN` in `scripts/cloud/deploy_app.sh` —
`intake_api._resolve_address` compares the envelope recipient's domain against
it, and a message to the right local part at any other domain resolves
nothing.

### 2. Verify the fallback destination

**Email → Email Routing → Destination addresses → Add.** Cloudflare mails a
confirmation link to it; a destination that has not been confirmed cannot be
forwarded to. Confirm it, and re-read the warning above about what that mailbox
will hold.

**Do not skip this, and do not leave the address blank later.** A forward to an
unset or unverified destination *rejects*, and that rejection escapes the
worker's `email()` handler — which means the message is not quietly dropped,
it is **failed back to the sender**, who sees a bounce or a retry. A bounce to
a PMS's automated sender is exactly what D-OH23.1 refuses to produce, so a
broken fallback is worse than no fallback: it turns the safety net into the
one behavior the design rules out. The deploy workflow refuses to run if
`EMAIL_INTAKE_FALLBACK` is empty, but it cannot tell whether the address was
ever confirmed.

Note the address down — it is the `EMAIL_INTAKE_FALLBACK` GitHub secret in
step 5.

### 3. Create the Workers-scoped API token

The existing `CLOUDFLARE_API_TOKEN` repo secret is scoped to Pages and cannot
deploy a Worker. Create a **second** token
(**My Profile → API Tokens → Create Token → Custom token**):

| scope | permission |
| --- | --- |
| Account → Workers Scripts | Edit |
| Zone → Email Routing Rules | Edit (zone `mandati.ai`) |

**Issue it on the same Cloudflare account as the `CLOUDFLARE_ACCOUNT_ID` repo
secret.** The workflow passes that account id to wrangler alongside this
token; a token minted on a different account (easy to do with more than one in
the dashboard's account switcher) authenticates fine and then fails on the
account id, or — worse — succeeds against the wrong account and deploys a
worker nobody is looking for.

Store it as the repo secret `CLOUDFLARE_WORKERS_API_TOKEN`. Leave the Pages
token alone — two narrow tokens, so the deploy that runs on every marketing
change cannot replace the code that handles every property's mail.

### 4. Mint the intake secret and put it in both places

One value, two homes. Generate it once:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

**GitHub** (repo → Settings → Secrets → Actions): `EMAIL_INTAKE_SECRET`.
`deploy-email-intake.yml` pushes it into the worker as `INTAKE_SECRET`.

**GCP Secret Manager**, where `deploy_app.sh` mounts it as
`USALI_EMAIL_INTAKE_SECRET`:

```sh
printf '%s' '<the value>' | gcloud secrets create usali-email-intake-secret \
  --project "<project>" --data-file=- --replication-policy=automatic
# on a rotation, instead:
printf '%s' '<the value>' | gcloud secrets versions add usali-email-intake-secret \
  --project "<project>" --data-file=-
# and let the app's service account read it:
gcloud secrets add-iam-policy-binding usali-email-intake-secret \
  --project "<project>" --member "serviceAccount:<APP_SA>" \
  --role roles/secretmanager.secretAccessor
```

**`printf '%s'`, not `echo`, and it matters on this side only.** `wrangler
secret put` trims trailing whitespace off what it reads from stdin; Secret
Manager stores exactly the bytes it is given. So a newline added to *both*
sides cancels out, and a newline added *here alone* — which is what `echo`
does — leaves the app signing with a key one byte longer than the worker's.
Every message then 401s, and nothing anywhere prints a reason.

Create the secret **before** the next `deploy_app.sh` run — the `--set-secrets`
line that mounts it is what fails if it is missing. It also has to exist before
the day any environment sets `USALI_ENV=prod`:
`config._refuse_dev_secrets_in_prod` refuses the committed dev default only
under that flag, and `deploy_app.sh` deliberately does not set it (its own
comment at `COMMON_ENV` says why). So today nothing in the app itself will
complain — see the comment beside `USALI_EMAIL_INTAKE_SECRET` in that script.

### 5. Add the two GitHub secrets and deploy the worker

`EMAIL_INTAKE_SECRET` (step 4) and `EMAIL_INTAKE_FALLBACK` (step 2), then
dispatch **Actions → Deploy email intake worker → Run workflow**. It refuses to
run if either secret is empty, runs the tests, puts both worker secrets, and
uploads the worker.

The job declares `environment: email-intake`. GitHub creates that environment
on the first run; if you want a second pair of eyes on anything that can
rewrite mail handling, add required reviewers to it (repo → Settings →
Environments → email-intake) and every future dispatch will wait for approval.

### 6. Redeploy the app — **before** the routing rule

`scripts/cloud/deploy_app.sh` now carries `USALI_EMAIL_INTAKE_DOMAIN` and
mounts `USALI_EMAIL_INTAKE_SECRET`. Until it has run, the app is verifying
signatures against the committed dev default and resolving addresses at
`intake.example.test`.

**This is why it comes before the catch-all rule.** Turn routing on first and
every message in the gap is signed with the real secret, refused by an app
still holding the dev default, and forwarded — unredacted — into the ops
mailbox. Nothing is lost, but somebody has to re-deliver a night's reports by
hand, and the fallback mailbox fills with exactly the content it exists to
minimize.

While you are here, check `cloudflare/email-intake/wrangler.toml`'s
`INTAKE_URL` is the app host you just deployed. A wrong origin has no other
symptom: the worker signs correctly, gets a non-2xx or a connection error from
a host that is not the app, and forwards every message to the fallback mailbox
unredacted. The intake events page stays empty, which reads like "no mail
arrived" rather than "mail arrived and went somewhere else".

### 7. Point the catch-all rule at the worker

Only now, with the worker deployed and the app expecting the real secret:
**Email → Email Routing → Routing rules → Catch-all address** for
`intake.mandati.ai` → action **Send to a Worker** → `oh-email-intake`. Enable
it.

The catch-all is deliberate: addresses are minted per property in the app and
Email Routing never needs to learn about them. Mail to a local part nothing
answers to still reaches the worker, gets posted, and comes back
`unknown_address` with nothing stored.

### 8. Send a real test message — and save its header here

Mint an intake address on a property's page, then mail a night-audit export to
it **from a domain that signs with DKIM** (any Gmail or Google Workspace
account does). Then open the property page and read the intake events: a
successful one reads `ingested`, with an entry per attachment.

Then do the part that only a human can do. Cloudflare's
`Authentication-Results` header is the whole of the sender policy's evidence,
and until a real message arrives nobody has seen the exact text this account's
receiver writes. Pull it out of the event row's `auth_result` and **paste one
real example into this file**, below:

```
Authentication-Results: <paste the real header from the first test message>
```

<!-- Left empty on purpose. D-OH23.4 records, as an ASSUMPTION rather than a
     measurement, that Cloudflare's SPF comment echoes the MAIL FROM address
     and the connecting IP but NOT the HELO string — `intake.sender_allowed`
     is written against that shape. This step is the first time anyone sees
     the real thing; an example pasted here is what lets the next person
     check the assumption instead of re-deriving it. -->

Work through the refusals too, so the failure modes are seen once on purpose
rather than first at 3am:

- **`sender_rejected`.** This one needs setting up first: an address with no
  `sender_domains` allowlist accepts any DKIM- or SPF-authenticated sender, so
  there is nothing to reject. Set the allowlist on the property page's intake
  address (the same place the address itself is shown) to the PMS's sending
  domain, then mail from anywhere else.
- **`revoked_address`.** Rotate the address on the property page, then mail to
  the old one. It is recorded, not dropped — that row is the evidence a PMS is
  still sending somewhere stale.
- **`duplicate`.** Send the same message twice.

---

## What to expect in normal running

**A re-sent corrupt report makes a new failure every night.** Deduplication
matches only batches that reached `transformed`. A report that fails to parse
leaves a `failed` batch, which the dedupe read does not see, so a PMS that
keeps re-sending the same broken export produces a fresh failed batch and a
fresh error record each night rather than one `duplicate`. That is the
intended trace — the noise is the signal that a source needs fixing — but it
means a persistent parse failure shows up as a growing pile, not one row.

**Committed batches with no event row are possible.** The event is written
last, on purpose. An exception between the final attachment's commit and the
event commit answers 5xx for a message the app actually ingested: the worker
forwards it to the fallback mailbox, and if a human re-delivers it the
attachments hash the same and it is recorded as `duplicate`. What is lost is
provenance — which message carried that batch — not data. If a batch ever
appears on a property with no matching intake event, this is the explanation
to check first.

**A captured request can be replayed for up to 600 s.** The timestamp window
is ±300 s, and nothing stores seen signatures, so anyone who captures a signed
POST can repost it for ten minutes and have it processed again. Accepted, with
eyes open (D-OH23.2): the attachments deduplicate by content hash so nothing is
staged twice, but the event rows do not, so a replay leaves duplicate event
rows. A run of identical events seconds apart is what that looks like.

**The rate limit is per instance, not per service.** The limiter is in-process
at 120 messages a minute (`server.py`), and Cloud Run runs up to
`--max-instances` of them — 2 on the demo — so the real ceiling is that
multiple: about 240/min today, and it moves whenever `--max-instances` does.
Do not read a 429 as "the service is at 120".

**The sender policy reads free text.** `X-Intake-Auth` is Cloudflare's
`Authentication-Results` header, which is prose a receiver formats, not a
structured verdict. `intake.sender_allowed` strips comments and requires a
`dkim=pass` or `spf=pass` *aligned* with the envelope sender, which bounds the
damage — but it does not remove the dependency. D-OH23.4 records the
assumption this rests on. **If `X-Intake-Auth` is ever sourced from a receiver
other than Cloudflare, this policy has to be replaced by verifying DKIM over
the raw message in-process** — do not just repoint the header.

---

## Rotating the intake secret

The worker and the app must hold the same value, and there is a window between
the two writes when they will not.

1. Add the new value to GCP Secret Manager — `printf '%s' | gcloud secrets
   versions add`, never `echo`, for the reason in step 4 — and redeploy the
   app. The app now expects the new secret.
2. Update the `EMAIL_INTAKE_SECRET` GitHub secret and dispatch **Deploy email
   intake worker**.

In between, messages 401 and the worker forwards them to the fallback mailbox.
Nothing is lost; someone has to re-deliver them by hand. Rotate outside the
window when the PMSs send — most deliver in the small hours.

## When mail stops arriving

In order, cheapest first:

1. **Is it in the fallback mailbox?** Then the worker ran and the app refused
   or was unreachable. `wrangler tail --name oh-email-intake` shows the
   worker's own line: a status code, never message content.
2. **Nothing in the fallback mailbox either?** Then the message never reached
   the worker. Check the catch-all rule is enabled and still points at
   `oh-email-intake`, and that the subdomain's MX records are intact.
3. **Everything 401s?** The two copies of the secret have drifted. See the
   rotation section — most often a trailing newline on the GCP copy, which
   `wrangler secret put` would have trimmed off the worker's.
   Also check `wrangler.toml`'s `INTAKE_URL` still names the app: a worker
   pointed at the wrong origin looks identical from here.
4. **`unknown_address` on the property page?** The address was rotated or
   belongs to another property. The current one is on the property page.
5. **`sender_rejected`?** Read the event's `auth_result`. Either the sender's
   domain failed both SPF and DKIM alignment, or the address has a
   `sender_domains` allowlist that no longer matches where the PMS mails from.
