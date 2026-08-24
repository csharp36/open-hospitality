#!/usr/bin/env bash
# K4: build + ship the app — MIGRATE BEFORE DEPLOY (the repo standard):
#   1. build the K1 image (linux/amd64), push to Artifact Registry
#   2. deploy + EXECUTE the migrate-seed job (alembic -> fictitious
#      seed; sentinel-guarded, re-runs no-op)
#   3. deploy the serving revision (scale-to-zero, capped)
# Secrets ride --set-secrets only; the DB URL is composed in-container
# by env.sh from the password secret + the Cloud SQL socket path.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT}" || "${PROJECT}" == "(unset)" ]]; then
  echo "usage: scripts/cloud/deploy_app.sh <project-id>" >&2
  exit 2
fi
REGION="${REGION:-us-west1}"
SQL_INSTANCE="${SQL_INSTANCE:-usali-demo}"
AR_REPO="${AR_REPO:-usali}"
AUTH_HOST="${AUTH_HOST:-auth.example.com}"
# The public app host — what /signup invite links point at (D-B4). The
# serving app builds its own absolute URLs from request headers
# (--proxy-headers), but the invite CLI has no request context, so the
# invite job needs this explicitly. Placeholder here; the deploy workflow
# supplies the real host (DEMO_APP_HOST=demo.mandati.ai).
APP_HOST="${APP_HOST:-app.example.com}"
# Email delivery (B2). Self-serve signup is only reachable by a stranger if the
# invite link and the verification code can actually be sent, so this is what
# turns POST /api/signup/request from a 502 into a working front door.
#
# Vendor-neutral on purpose: SendGrid, Mailgun, Postmark, SES and a self-hosted
# MTA all speak submission, so choosing one is these three values plus a secret,
# not a code change. Leave SMTP_HOST empty and the deploy keeps the console
# notifier it has always used -- nothing breaks, but nothing is delivered
# either, and /api/signup/request answers 502 by design rather than minting
# invites nobody can reach.
SMTP_HOST="${SMTP_HOST:-}"
SMTP_PORT="${SMTP_PORT:-587}"
SMTP_USERNAME="${SMTP_USERNAME:-apikey}"
# The From address. Must be one the relay is authorised to send for (SPF/DKIM),
# or the mail is accepted here and silently dropped or junked downstream.
SMTP_FROM="${SMTP_FROM:-Open Hospitality <no-reply@${APP_HOST}>}"
CLOUDSQL="${PROJECT}:${REGION}:${SQL_INSTANCE}"
BUCKET="${PROJECT}-usali-demo-photos"
APP_SA="usali-app@${PROJECT}.iam.gserviceaccount.com"
SHA="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/usali-app:${SHA}"

echo "== [1/5] image build + push"
# Refresh the baked release notes from the FULL history of the ref being
# deployed (the deploy workflow checks out with fetch-depth 0). A snapshot is
# committed for local/CI builds without git; this keeps the deployed bundle's
# notes current with what actually shipped. The frontend build stage copies
# frontend/, so the regenerated file rides into the image.
if command -v node >/dev/null 2>&1; then
  node scripts/gen-release-notes.mjs
else
  echo "WARNING: node not found — shipping the committed release-notes snapshot"
fi
# The SPA's OIDC authority is baked at build time (frontend/src/auth/oidc.ts
# reads import.meta.env.VITE_OIDC_AUTHORITY; the Dockerfile ARG only carries a
# placeholder default). Feed it the SAME ${AUTH_HOST} the backend issuer uses
# below (step 4) so the frontend and API agree on the Keycloak realm — without
# this the deployed SPA points at the Dockerfile's placeholder host and login
# breaks.
docker build --platform linux/amd64 \
  --build-arg "VITE_OIDC_AUTHORITY=https://${AUTH_HOST}/realms/usali" \
  --build-arg "VITE_BUILD_SHA=${SHA}" \
  -t "${IMAGE}" .
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker push "${IMAGE}"

AUTH_URL="$(gcloud run services describe usali-auth --project "${PROJECT}" \
  --region "${REGION}" --format='value(status.url)')"

# Shared by the job and the service: same field key, same HPKE key,
# same bucket — the job WRITES what the service must READ.
COMMON_ENV="USALI_PII_HPKE_KEY_ID=cloud-demo-1"
COMMON_ENV+=",CLOUDSQL_INSTANCE=${CLOUDSQL}"
COMMON_ENV+=",USALI_PHOTO_STORE_GCS_BUCKET=${BUCKET}"
COMMON_ENV+=",USALI_BIOMETRIC_MATCHING_ENABLED=true"
COMMON_ENV+=",USALI_CRM_PROVIDER=delphi"
COMMON_ENV+=",USALI_KC_ADMIN_BASE_URL=${AUTH_URL}"
# Both the serving revision (signup request + OTP) and the invite job send mail,
# so the notifier config is shared. `notifier_from_settings` REFUSES to build an
# smtp notifier without a host and a From, which is why this is all-or-nothing:
# a half-set SMTP_* fails the revision at startup instead of at the first owner
# who asks for a link.
if [[ -n "${SMTP_HOST}" ]]; then
  COMMON_ENV+=",USALI_NOTIFIER=smtp"
  COMMON_ENV+=",USALI_SMTP_HOST=${SMTP_HOST}"
  COMMON_ENV+=",USALI_SMTP_PORT=${SMTP_PORT}"
  COMMON_ENV+=",USALI_SMTP_USERNAME=${SMTP_USERNAME}"
  COMMON_ENV+=",USALI_SMTP_FROM=${SMTP_FROM}"
  SMTP_SECRET=",USALI_SMTP_PASSWORD=usali-smtp-password:latest"
else
  echo "NOTE: SMTP_HOST unset -- deploying with the console notifier."
  echo "      Self-serve signup will 502 and invite links will only reach the logs."
  SMTP_SECRET=""
fi
SHARED_SECRETS="USALI_PII_HPKE_PRIVATE_KEY=usali-hpke-private-key:latest"
SHARED_SECRETS+=",USALI_FIELD_ENCRYPTION_KEY=usali-field-encryption-key:latest"
SHARED_SECRETS+=",USALI_KC_ADMIN_CLIENT_SECRET=usali-admin-client-secret:latest"
# The DB identity is where the job and the service part ways (L2): the
# job (alembic + seed) KEEPS the owner role — migrations own the schema,
# and FORCE RLS still confines the owner at query time — while the
# SERVING revision connects as the RLS-bound app role (usali_app: no
# BYPASSRLS, not the table owner), so the DB wall applies to every
# tenant-facing request. env.sh composes the same URL shape from
# USALI_DB_USER (owner default) + USALI_DB_PASSWORD.
JOB_SECRETS="USALI_DB_PASSWORD=usali-db-password:latest,${SHARED_SECRETS}${SMTP_SECRET}"
APP_SECRETS="USALI_DB_PASSWORD=usali-app-db-password:latest,${SHARED_SECRETS}${SMTP_SECRET}"
# D-B7: the SERVING revision (not the job) opens the provisioner session
# for signup /complete, so it alone mounts the provisioner password — a
# strong secret in place of config.py's dev default.
APP_SECRETS+=",USALI_PROVISIONER_DB_PASSWORD=usali-provisioner-db-password:latest"

echo "== [2/5] migrate-seed job (migrate BEFORE deploy)"
gcloud run jobs deploy usali-migrate-seed \
  --image "${IMAGE}" \
  --command "/app/scripts/cloud/job.sh" \
  --set-cloudsql-instances "${CLOUDSQL}" \
  --set-env-vars "${COMMON_ENV}" \
  --set-secrets "${JOB_SECRETS}" \
  --service-account "${APP_SA}" \
  --memory 2Gi --cpu 2 --task-timeout 25m --max-retries 0 \
  --project "${PROJECT}" --region "${REGION}"

echo "== [3/5] executing the job"
gcloud run jobs execute usali-migrate-seed --wait \
  --project "${PROJECT}" --region "${REGION}"

echo "== [4/5] the serving revision"
gcloud run deploy usali-app \
  --image "${IMAGE}" \
  --allow-unauthenticated \
  --service-account "${APP_SA}" \
  --set-cloudsql-instances "${CLOUDSQL}" \
  --set-env-vars "${COMMON_ENV},USALI_DB_USER=usali_app,USALI_PUBLIC_BASE_URL=https://${APP_HOST},USALI_OIDC_ISSUER=https://${AUTH_HOST}/realms/usali,USALI_OIDC_JWKS_URL=${AUTH_URL}/realms/usali/protocol/openid-connect/certs" \
  --set-secrets "${APP_SECRETS}" \
  --memory 2Gi --cpu 2 --cpu-boost \
  --min-instances 0 --max-instances 2 --concurrency 40 \
  --port 8080 \
  --project "${PROJECT}" --region "${REGION}"

echo "== [5/5] invite job (operator-triggered; DEFINED here, not executed)"
# Invite origination stays the `usali invite` CLI (D-B4 — no platform-admin
# HTTP surface), wrapped in a job so it reaches Cloud SQL. Repinned to this
# deploy's ${IMAGE} so links never come from a stale build. The owner role
# (job default) can insert the invite row; USALI_PUBLIC_BASE_URL sets the
# link host; USALI_INVITE_EMAIL is a placeholder overridden per execution:
#   gcloud run jobs execute usali-invite \
#     --update-env-vars USALI_INVITE_EMAIL=owner@hotel.com --wait ...
gcloud run jobs deploy usali-invite \
  --image "${IMAGE}" \
  --command "/app/scripts/cloud/invite_job.sh" \
  --set-cloudsql-instances "${CLOUDSQL}" \
  --set-env-vars "${COMMON_ENV},USALI_PUBLIC_BASE_URL=https://${APP_HOST},USALI_INVITE_EMAIL=placeholder@example.com" \
  --set-secrets "${JOB_SECRETS}" \
  --service-account "${APP_SA}" \
  --max-retries 0 \
  --project "${PROJECT}" --region "${REGION}"

URL="$(gcloud run services describe usali-app --project "${PROJECT}" \
  --region "${REGION}" --format='value(status.url)')"
echo
echo "Deployed: ${URL}"
echo "Invite:   gcloud run jobs execute usali-invite --update-env-vars USALI_INVITE_EMAIL=<owner-email> --wait --project ${PROJECT} --region ${REGION}"
echo "Smoke:    scripts/cloud/smoke_cloud.sh ${PROJECT}"
