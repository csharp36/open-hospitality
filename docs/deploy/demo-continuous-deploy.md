# Continuous deploy to the demo (`demo.mandati.ai`)

How a commit on the public repo's `main` reaches the live demo, and the
**one-time** setup that makes the `Deploy demo` GitHub Action work.

## Model

The container image is the deployable artifact, built from this repo. The
`Deploy demo` workflow (`.github/workflows/deploy-demo.yml`) is a thin wrapper
around the existing `scripts/cloud/deploy_app.sh`, which already enforces
ADR-008's ordering invariant:

1. build the image (`linux/amd64`) and push it to Artifact Registry, tagged
   with the commit SHA;
2. **deploy + execute the migrate-seed Cloud Run job** — `alembic upgrade head`
   then the idempotent synthetic-year seed (backfills new tables such as
   `property_stat_config` and `ingestion_coverage`, no-ops existing rows);
3. ship the scale-to-zero serving revision.

The workflow is **`workflow_dispatch` only** — a manual, maintainer-gated
action. Fork pull requests cannot run it, so no outside contributor can reach
the deploy credential. The private `usali-engine` repo is **not** in this path;
promoting a build no longer requires the manual public→private sync.

**To promote a build:** Actions → *Deploy demo* → *Run workflow* → pick the
branch/tag (defaults to `main`) → *Run*. That ref's commit is what deploys.

Auth is **Workload Identity Federation** — no static JSON key lives in this
repo. The federated identity may impersonate only the deploy service account,
and the WIF provider is bound to this one repository.

---

## One-time setup

Run these once, as a project owner, with `gcloud` pointed at the demo project.
Fill in the four values at the top; the rest is copy-paste.

```bash
PROJECT=your-demo-project-id          # e.g. mandati-demo
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
REPO=csharp36/open-hospitality        # owner/name of THIS repo
DEPLOY_SA=usali-deployer              # new service account (short id)
APP_SA=usali-app                      # EXISTING runtime SA the services run as
AR_REPO=usali                         # Artifact Registry repo (deploy_app.sh default)
DEPLOY_SA_EMAIL="${DEPLOY_SA}@${PROJECT}.iam.gserviceaccount.com"
APP_SA_EMAIL="${APP_SA}@${PROJECT}.iam.gserviceaccount.com"
```

### 1. The deploy service account (least privilege)

```bash
gcloud iam service-accounts create "$DEPLOY_SA" \
  --project "$PROJECT" --display-name "GitHub Actions demo deployer"

# Ship Cloud Run revisions and jobs.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:${DEPLOY_SA_EMAIL}" --role roles/run.admin

# Deploy services/jobs that RUN AS the app SA (actAs). Scope to that one SA,
# not the project.
gcloud iam service-accounts add-iam-policy-binding "$APP_SA_EMAIL" \
  --project "$PROJECT" \
  --member "serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role roles/iam.serviceAccountUser

# Push the image to Artifact Registry.
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --project "$PROJECT" --location us-west1 \
  --member "serviceAccount:${DEPLOY_SA_EMAIL}" --role roles/artifactregistry.writer
```

> The deploy SA does **not** need `cloudsql.client` or `secretmanager.secretAccessor`.
> Those are used by the *running* job/service as the app SA (`$APP_SA_EMAIL`),
> which already has them since the demo runs today. The deployer only *wires*
> the connection and secret references; it never reads them.

### 2. Workload Identity Federation pool + provider

```bash
gcloud iam workload-identity-pools create github \
  --project "$PROJECT" --location global --display-name "GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-oidc \
  --project "$PROJECT" --location global --workload-identity-pool github \
  --display-name "github.com" \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition "assertion.repository == '${REPO}'"   # confused-deputy guard: only THIS repo
```

Let the federated principals from this repo impersonate the deploy SA:

```bash
POOL="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA_EMAIL" \
  --project "$PROJECT" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/${POOL}/attribute.repository/${REPO}"
```

Print the provider resource name for the repo variable in step 3:

```bash
echo "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/github-oidc"
echo "$DEPLOY_SA_EMAIL"
```

### 3. GitHub repo configuration

**Settings → Secrets and variables → Actions → Variables** (these are
non-secret; use *Variables*, not *Secrets*):

| Variable | Required | Value |
|---|---|---|
| `GCP_PROJECT` | yes | the demo project id |
| `GCP_WIF_PROVIDER` | yes | the `projects/…/providers/github-oidc` string from step 2 |
| `GCP_DEPLOY_SA` | yes | `$DEPLOY_SA_EMAIL` |
| `DEMO_AUTH_HOST` | **yes, for this demo** | `auth.mandati.ai` — the public `deploy_app.sh` defaults `AUTH_HOST` to the `auth.example.com` placeholder (open-core scrubbing). This host is baked into the SPA's OIDC authority **and** the backend issuer, so if it is unset the deployed site points at the placeholder realm and login breaks. |
| `DEMO_APP_HOST` | **yes, for self-service signup** | `demo.mandati.ai` — the public host the `/signup` invite links point at. The serving app derives its own absolute URLs from request headers, but the `usali invite` CLI (run by the `usali-invite` job) has no request context, so it needs this explicitly; unset → invite links point at the `app.example.com` placeholder. |
| `DEMO_SMTP_HOST` | **yes, for a public front door** | e.g. `smtp.sendgrid.net`. Unset → the deploy keeps the console notifier and prints a NOTE saying so: `POST /api/signup/request` answers 502, and invite links and verification codes reach only the Cloud Run logs. That is fine for an operator-run pilot and useless for anyone you send `/try` to. See *Email delivery* below. |
| `DEMO_SMTP_PORT` | no | overrides the `587` (STARTTLS submission) default |
| `DEMO_SMTP_USERNAME` | no | overrides the `apikey` default. Leave the password secret unset **and** this empty only for a relay that authorises by network, not credentials. |
| `DEMO_SMTP_FROM` | no | overrides `Open Hospitality <no-reply@$APP_HOST>`. Must be an address the relay is authorised to send for. |
| `GCP_REGION` | no | overrides the `us-west1` default |
| `GCP_SQL_INSTANCE` | no | overrides the `usali-demo` default |
| `GCP_AR_REPO` | no | overrides the `usali` default |

#### Email delivery

Self-serve signup is only reachable by someone you have never met if the invite
link and the verification code can actually be sent. Both go out through the
`Notifier` seam, which ships two adapters: `console` (logs, sends nothing) and
`smtp`.

The SMTP adapter is deliberately vendor-neutral — SendGrid, Mailgun, Postmark,
SES and a self-hosted MTA all speak submission — so choosing one is these
variables plus one secret, never a code change:

```bash
printf '%s' "$SMTP_API_KEY" | gcloud secrets create usali-smtp-password \
  --data-file=- --project "$PROJECT"
gcloud secrets add-iam-policy-binding usali-smtp-password \
  --member "serviceAccount:usali-app@${PROJECT}.iam.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor --project "$PROJECT"
```

Two things fail loudly rather than quietly, on purpose:

- Selecting `smtp` without a host or a From address raises when the notifier is
  **built**, so a half-set deploy dies at startup instead of on the first owner
  who asks for a link.
- There is still no SMS vendor. `SmtpNotifier.send_sms` raises rather than drop
  a code the caller believes was delivered, which is why the signup OTP goes to
  the invited email address.

Verify delivery after a deploy by requesting a link for an address you control:

```bash
curl -sS -X POST "https://${DEMO_APP_HOST}/api/signup/request" \
  -H 'content-type: application/json' -d '{"email":"you@example.com"}' -i | head -1
```

`202` means the mail was handed to the relay; `502` means the send failed and
the invite was revoked rather than left pending as a credential nobody can
reach. A `202` is **not** proof the message arrived — check the inbox.

**Settings → Environments → New environment → `demo`** (matches the workflow's
`environment:`). Optionally add *Required reviewers* / a *Wait timer* here to
gate the live demo behind a manual approval — the workflow runs fine without
them.

### 4. First run

Actions → *Deploy demo* → *Run workflow* (from `main`). Watch the three phases
in the log, then smoke it: `scripts/cloud/smoke_cloud.sh <project>`.

---

## Minting self-service invites

Invites stay CLI-only (D-B4 — no platform-admin HTTP surface). Each deploy
defines an operator-triggered `usali-invite` Cloud Run job (repinned to the
deploy's image). To invite an owner and get their signup link:

```bash
gcloud run jobs execute usali-invite \
  --update-env-vars USALI_INVITE_EMAIL=owner@hotel.com --wait \
  --region us-west1 --project <project>
# then read the printed /signup?token= link from the execution logs:
gcloud logging read \
  'resource.type="cloud_run_job" resource.labels.job_name="usali-invite"' \
  --project <project> --limit 10 --order desc --format 'value(textPayload)' \
  | grep '/signup?token='
```

In the console notifier (B1), the SMS OTP the owner then requests is **logged,
not sent** — read it from the serving service's logs to complete a facilitated
demo (`… service_name="usali-app" AND textPayload:"SMS to="`). A real SMS
vendor is the B2 follow-up.

## Provisioner role (D-B7) — existing-environment migration

`bootstrap.sh` now creates the least-privilege `usali_provisioner` role +
`usali-provisioner-db-password` secret, and the serving revision mounts that
secret (replacing config's dev default). `ensure_sql_user` is describe-or-create
and will **not** rotate a role that already exists — so a demo where the role
was created out-of-band (e.g. a hotfix with a placeholder password) needs a
one-time rotation **before** the next deploy, or the serving app's provisioner
session will fail to authenticate:

```bash
scripts/cloud/bootstrap.sh <project>   # creates the secret (role already exists → unchanged)
gcloud sql users set-password usali_provisioner --instance usali-demo \
  --password "$(gcloud secrets versions access latest \
      --secret usali-provisioner-db-password --project <project>)" \
  --project <project>
# then: Actions → Deploy demo → Run workflow
```

A **fresh** environment needs none of this — `bootstrap.sh` creates the role
straight from the generated secret.

## Notes

- **`demo.mandati.ai` is a persistent Cloud Run domain mapping** — set once,
  inherited by every new revision, so it is not a per-deploy concern.
- **The build runs on the Actions runner** (`ubuntu-latest`, native amd64), so
  no QEMU emulation. It includes `uv sync --extra face` and the sha-pinned face
  model fetch, so first builds are a few minutes; layer caching amortises it.
- **Rollback** is a redeploy of an earlier commit: *Run workflow* from that
  tag/SHA. The seed is idempotent, so re-running is safe.
- **Tightening later:** if you ever want zero deploy credentials reachable from
  the public repo, split this into a push-only build here plus a deploy runner
  elsewhere (the "public builds only" option). The current setup keeps the
  privileged path behind maintainer-only `workflow_dispatch` + WIF + the `demo`
  environment.
