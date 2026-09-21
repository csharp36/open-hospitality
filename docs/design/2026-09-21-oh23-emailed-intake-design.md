# OH-23 — Emailed night-audit intake (design)

**Date:** 2026-09-21. **Status:** DECIDED — the three shaping decisions are
the owner's (2026-09-21): ingress is a **Cloudflare Email Worker posting to
an HTTPS webhook**; each property gets a **random-token address**; the
**redaction gate (OH-32, PR #141) shipped first** and this intake inherits
it. Implementation plan:
[`../plans/2026-09-21-oh23-emailed-intake.md`](../plans/2026-09-21-oh23-emailed-intake.md).

Roadmap: `.github/roadmap.yml` OH-23 (`planned`); `docs/ROADMAP.md` Tier 0
row 4, "Depends on #2" (now shipped). Four of the seven target PMSs deliver
their night audit by scheduled email; until this exists each is a daily
manual upload.

## 1. What exists that this builds on

- **The gate.** `ingestion.process_document_bytes(session, data, name, *,
  processed_dir, failed_dir)` processes an upload in memory and files only a
  redacted words artifact or an error record. An email endpoint hands it
  bytes and never writes a file. Error text passes `mask_pans`.
- **The night-audit upload** (`night_audit_api.py::upload_night_audit_report`)
  validates BEFORE staging: the report's detected property must equal the
  URL's property; the report type must be in `REQUIRED_REPORTS[pms_source]`;
  the business date must equal `NightAuditState.current_business_date`,
  else "use the Upload page for backfills". Packs go through `_ingest_pack`,
  which validates every recognized section the same way.
- **"Landed" is a coverage row.** `record_coverage` inside `_process_section`
  writes `ingestion_coverage`, and `night_audit._report_landed` reads it, so
  anything that goes through the pipeline shows up on the night-audit page
  without extra wiring.
- **Tenancy.** Operator routes get an org-bound session from the OIDC token
  (`require_active_org`). The one non-OIDC surface today, the kiosk device
  token, is pinned to the founding org (`server.py`: "no OIDC claim to
  resolve an org from"). An email has no token either; the address must
  carry the org. `invites.validate` is the precedent for a lookup that runs
  before any org is known: an org-independent table read on the unbound
  base factory (`resolve_org_id` does the same for aliases).
- **Infrastructure.** `mandati.ai` MX already points at Cloudflare Email
  Routing (`route1-3.mx.cloudflare.net`, SPF `include:_spf.mx.cloudflare.net`);
  the nameservers are Cloudflare; the marketing deploy runs `wrangler` with
  `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` (scoped to Pages). The
  app is Cloud Run; there is no scheduler or worker process. `notifications.py`
  has a `Notifier` seam (console / SMTP).
- **Data posture** (`2026-08-16-data-posture-progressive-onboarding-design.md`)
  already sketched this layer: same boundary redaction as D8.4, SPF/DKIM
  verified, optional per-property sender allowlist, and an expectation model
  (missing / duplicate / wrong-property / unparseable).

## 2. Decisions

**D-OH23.1 — Ingress is a Cloudflare Email Worker that forwards the raw
message to `POST /api/intake/email`.** Email Routing on the subdomain
`intake.<domain>` (Cloudflare supports Email Routing subdomains; the MX for
the subdomain is Cloudflare's, added when the subdomain is enabled) with a
catch-all rule → the worker. The worker does nothing clever: it reads
`message.raw`, refuses anything over the size limit, and POSTs the bytes as
`message/rfc822` with five headers: `X-Intake-Timestamp` (unix seconds),
`X-Intake-Signature` (`sha256=` + HMAC-SHA256 over
`timestamp + "\n" + body` with the shared secret), `X-Intake-To` (the
envelope recipient, `message.to`), `X-Intake-From` (the envelope sender,
`message.from`), and `X-Intake-Auth` (Cloudflare's `Authentication-Results`
header, which carries the SPF/DKIM/DMARC verdicts). Envelope values, not
MIME `To:`/`From:`, decide routing and sender policy: the MIME headers are
author-controlled. On a non-2xx response or a network error the worker
**forwards the message unchanged to `FALLBACK_ADDRESS`** (an ops mailbox
verified in Email Routing) so nothing is lost when the app is down; it
never `setReject`s a report, because a bounce to a PMS is silent. Code
lives in `cloudflare/email-intake/` (`wrangler.toml`, `src/worker.js`, a
`node --test` for the signing function), deployed by a `workflow_dispatch`
workflow mirroring `deploy-marketing.yml`. Rejected: forward-to-mailbox +
polling (needs a scheduler and mailbox credentials); third-party inbound
parse (a vendor and an MX move for a job the existing routing does).

**D-OH23.2 — The webhook authenticates with an HMAC and a timestamp
window; it is not an operator route.** `USALI_EMAIL_INTAKE_SECRET` (a dev
default refused in prod by `_refuse_dev_secrets_in_prod`, like the other
secrets) signs `timestamp + "\n" + body`; the app verifies with
`hmac.compare_digest` and refuses a timestamp more than 300 s from now.
Failures are 401 with no detail. A `RateLimiter` (as `/api/preview`) caps
the route. The body is capped by `email_intake_max_bytes` (default 25 MB,
Cloudflare's message limit) and each attachment by `_MAX_UPLOAD_BYTES`.

**D-OH23.3 — Each property gets a random-token address held in an
org-independent table, and the address resolves the org.**
`property_intake_address`: `address_id`, `local_part` (unique; `na-` + 26
lowercase base32 chars from 16 random bytes: 128 bits, unguessable),
`org_id` (FK organization), `property_id` (FK property), `created_at`,
`revoked_at` (nullable), `sender_domains` (nullable JSON list). It is NOT
`OrgScoped`, on the `invite` precedent: the lookup by local part runs on
the unbound base factory before any org is known, then the request opens
`OrgBoundSessionFactory(base_factory, row.org_id)` and everything else,
including the event row and the ingest, happens inside that binding. The
local part is stored in clear because the property page must display it
(unlike an invite token, which is shown once); its secrecy is the
capability to inject a report for that property, which is why rotation
exists and why sender checks are a second layer, not the first. Operator
reads and writes of this table filter `org_id == principal.org_id`
explicitly (the ORM read wall covers `OrgScoped` classes only) AND resolve
the property through the org-bound `Property` lookup, which is walled.
Pinned by a two-tenant test. At most one un-revoked address exists per
property, enforced by the partial unique index
`uq_property_intake_address_active` on `(org_id, property_id) WHERE
revoked_at IS NULL` (review decision, 2026-09-21); rotate sets
`revoked_at` on the old row and inserts the new one in one transaction,
or the insert is refused.

**D-OH23.4 — Sender policy: authenticated or allowlisted, never
unauthenticated.** From `X-Intake-Auth`, require `dkim=pass` or `spf=pass`
for the envelope sender's domain; if the address has `sender_domains`, the
envelope sender's domain must also be in it. A pass must be ALIGNED with the
envelope sender, not merely present (review decision, 2026-09-21): a `dkim=pass`
counts only when its `header.d` equals the envelope-from domain or is a parent
of it on a label boundary (DMARC relaxed alignment), an `spf=pass` counts only
when its `smtp.mailfrom` domain equals it exactly, a pass carrying no such
property counts for nothing, and the `sender_domains` allowlist stays an exact
match — without that binding, anyone holding a valid signature for a domain of
their own could post a message under any envelope sender.

WHAT THIS RESTS ON (review decision, 2026-09-21). The verdict is parsed out of
a free-text header that the RECEIVER formats, and receivers interpolate
attacker-influenced text into it: a MAIL FROM local part of
`a) dkim=pass header.d=victim.test (b`, echoed into the SPF comment, closes the
comment, writes a forged aligned result and reopens a comment the trailing
paren balances. Two structural rules answer it, both fail-closed: a `method=`
token may only appear as the first `key=value` of its `;`-delimited clause (a
forged result brings no separator of its own), and the envelope sender's local
part must be an RFC 5322 dot-atom, which excludes every character — `(`, `)`,
`;`, space, `"` — a breakout needs. Property values must also match a strict
domain/address grammar before they are compared.

That bounds the exposure; it does not remove the dependency. A receiver that
echoed an attacker-chosen string CONTAINING `;` — the HELO, say — into its
comment could still manufacture a well-formed result. The assumption this
design rests on is that Cloudflare's Authentication-Results comment echoes the
MAIL FROM address and the connecting IP, not the HELO string. That is an
ASSUMPTION, recorded here rather than measured. The trigger to revisit: if
`X-Intake-Auth` is ever sourced from a receiver other than Cloudflare's Email
Routing, or Cloudflare's comment format changes to include the HELO, the
free-text policy must be replaced by verifying DKIM over the raw message
in-process rather than trusting a header about it. A message failing either is
recorded (`sender_rejected`) and not opened for attachments. Rejected: no
sender check (the address is a capability but the PMS vendor's sending
domain is a cheap second factor); DMARC-required (many PMS senders have no
DMARC policy).

**D-OH23.5 — Validation before staging mirrors the upload endpoint,
minus the business-date refusal.** For every attachment that is a PDF or
XLSX by magic bytes: detect (single) or split and detect by section title
(pack), exactly as `upload_night_audit_report` / `_ingest_pack` do, and
refuse the attachment (`wrong_property`) if any recognized section resolves
to a different property than the address's. The report-type-in-required
check is kept (`not_a_night_audit_report`). The business-date check is NOT
applied: an email can arrive late, and a backfill by email is the point.
The shared validator moves out of `night_audit_api.py` into `intake.py` so
both callers run the same code. Two consequences of that move (review
decision, 2026-09-21): the upload's PACK path now runs the report-type check
too, which it did not before — it is symmetric with the single-report path and
is a no-op for every pack reachable today — and the property and report-type
checks now run over every recognized section before any business-date check,
where they used to interleave section by section, so a pack that fails both
ways names the wrong-property section rather than the wrong-date one.

**D-OH23.6 — Duplicates are skipped by content hash before processing.**
PMS schedulers re-send. An attachment whose sha256 already has a
`transformed` `IngestBatch` in this org is recorded as `duplicate` and not
processed. (The gate's design left hash-refusal out of the upload path as a
UX decision; for email it is the expected case.)

**D-OH23.7 — Every message is an event row; the event log is the
expectation model's first surface.** `email_intake_event` (`OrgScoped`,
`org_wall`): `event_id`, `address_id`, `property_id`, `received_at`,
`envelope_from`, `subject` (through `mask_pans`), `auth_result` (the raw
`X-Intake-Auth`, capped), `outcome` (`ingested | partial | duplicate |
no_attachment | sender_rejected | wrong_property | not_a_night_audit_report
| unreadable | failed | revoked_address`), `attachments` (JSON:
`[{name, sha256, bytes, outcome, batch_id?, error?}]`, errors through
`mask_pans`), `message_id` (the MIME `Message-ID`, capped). Message bodies
are never stored. A message to an unknown local part has no org and is not
recorded; the response tells the worker, which logs it. A message to a
revoked address is recorded under the address's org (`revoked_address`) so
the operator can see the PMS still sends to the old address.

**D-OH23.8 — The property page shows the address, rotates it, and lists
recent events.** `PropertyConfigPage` gains a section "Night-audit email":
the address (`<local_part>@<email_intake_domain>`, domain served by the
API from settings, never hard-coded in the SPA), a copy button, "Rotate
address" (confirm; the old row gets `revoked_at`, a new row is created),
the sender-domain allowlist as a comma-separated field, and the last 20
events (received, from, outcome, attachment count). Endpoints under
`/api/properties/{property_id}/intake-address` (GET, POST create, POST
rotate, PUT allowlist) and `/intake-events` (GET), gated like the other
property-config writes (`require_grants(ORG_ADMIN, PROPERTY_GM)` +
`_require_onboardable_property`) and reads. The address is created on
demand (POST), not at property creation: a property that uploads by hand
should not carry an unused capability.

**D-OH23.9 — Failure handling: the message is never lost, the batch is
the trace, alerting is OH-26.** A processing failure records the failed
batch and the error record (the gate), marks the attachment `failed` in
the event, and returns 200 to the worker (the message was received and
disposed). Only auth, size and 5xx-class problems return non-2xx, which is
what makes the worker forward to the fallback mailbox. No email is sent
from this feature; "you have not received tonight's report" is OH-26's
notification, fed by these events.

## 3. Data flow

```
PMS ──SMTP──> Cloudflare Email Routing (intake.<domain>, catch-all)
        ──> Email Worker: size check, HMAC(ts, raw), POST message/rfc822
        ──> POST /api/intake/email
              1. verify signature + timestamp window, size caps, rate limit
              2. local part of X-Intake-To -> property_intake_address (unbound lookup)
                   none -> 200 {outcome: unknown_address}; revoked -> event(revoked_address)
              3. OrgBoundSessionFactory(base, row.org_id)
              4. sender policy (X-Intake-Auth, sender_domains) -> event(sender_rejected) or continue
              5. parse MIME; attachments by magic bytes; none -> event(no_attachment)
              6. per attachment: sha256 dedupe -> duplicate
                               | validate_for_property -> wrong_property / not_a_night_audit_report / unreadable
                               | process_document_bytes -> ingested (batch ids) / failed (masked error)
              7. event row; 200 {outcome, attachments}
        worker: non-2xx or error -> message.forward(FALLBACK_ADDRESS)
```

## 4. Tenancy and security notes

- Two new tables, two different shapes: `property_intake_address` is
  org-independent (joins `invite`, `otp_challenge` in the migration test's
  `_L1_ORG_INDEPENDENT`, and `test_tables_registered`);
  `email_intake_event` is `OrgScoped` with `ENABLE`/`FORCE ROW LEVEL
  SECURITY` and the `org_wall` policy (the `l5a0orgsettings` template),
  and joins the RLS inventory in `test_l2_rls_wall`. The alembic head
  literal in `test_l4_org_grants` moves. Four hand-maintained lists.
- The webhook is the second non-OIDC surface. Unlike the kiosk it is
  multi-org by construction, because the address row carries the org.
- The secret is one value in two places (GitHub secret → `wrangler secret
  put`; GCP Secret Manager → the app's env), rotated together.
- The worker forwards raw mail to the fallback mailbox on failure: that
  mailbox holds unredacted reports and is outside the gate. It is an ops
  mailbox, named in the runbook as such.

## 5. Tests that carry the design

- `tests/test_intake_email.py` (TestClient, synthetic RFC822 built with
  `email.message.EmailMessage`, the committed samples as attachments):
  bad signature / stale timestamp → 401 and nothing written; unknown
  address → 200 `unknown_address`, no event; sender rejected; no
  attachment; Opera flash for HISJ → `ingested`, one batch, a coverage row
  (the night-audit page's "landed"); the mock pack for STDEMO → `ingested`
  with two batches; the same message again → `duplicate`; an attachment for
  another property → `wrong_property` and nothing staged; a HotelKey
  four-file night (three XLSX + one PDF) → `ingested` ×4; a corrupt PDF →
  `failed`, error record, 200; a revoked address → `revoked_address` event.
  After every case, the gate's inventory (`_assert_no_file_carries`).
- Two-tenant: an address belonging to org 2's property ingests into org
  2 and org 1 sees neither the batch nor the event (`two_tenant_world`).
- Property endpoints: create, rotate (old revoked, new active, old mail →
  `revoked_address`), allowlist, events list; a GM confined to another
  property is refused.
- Validator parity: `night_audit_validation.validate_for_property` is called by both the
  upload endpoint and the webhook; the existing night-audit refusal tests
  keep passing unchanged.
- Worker: `node --test` on the signing function against a vector the
  Python side also asserts (same secret, timestamp and body → same hex),
  so the two implementations are pinned to each other.
- Frontend: the section renders the address and events, rotate confirms
  and refreshes (vitest; visible `<label htmlFor>` per the repo's a11y
  rule).

## 6. Out of scope, named

- Alerting on a missing or failed report (OH-26 consumes the event log).
- Marking a slot from an email that arrives for a day other than the
  night-audit state's current date: coverage is recorded; the roll logic
  is untouched.
- IMAP/Gmail polling; inbound parse vendors.
- Multi-org kiosk (the other non-OIDC surface) stays founding-org-bound.
- Cloudflare account configuration (enabling Email Routing on the
  subdomain, the catch-all rule, the verified fallback destination, a
  Workers-scoped API token) is a runbook, `docs/runbooks/email-intake.md`,
  not code; the deploy workflow assumes it is done.
