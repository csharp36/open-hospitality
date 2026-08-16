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
CLOUDSQL="${PROJECT}:${REGION}:${SQL_INSTANCE}"
BUCKET="${PROJECT}-usali-demo-photos"
APP_SA="usali-app@${PROJECT}.iam.gserviceaccount.com"
SHA="$(git rev-parse --short HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/usali-app:${SHA}"

echo "== [1/4] image build + push"
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
JOB_SECRETS="USALI_DB_PASSWORD=usali-db-password:latest,${SHARED_SECRETS}"
APP_SECRETS="USALI_DB_PASSWORD=usali-app-db-password:latest,${SHARED_SECRETS}"

echo "== [2/4] migrate-seed job (migrate BEFORE deploy)"
gcloud run jobs deploy usali-migrate-seed \
  --image "${IMAGE}" \
  --command "/app/scripts/cloud/job.sh" \
  --set-cloudsql-instances "${CLOUDSQL}" \
  --set-env-vars "${COMMON_ENV}" \
  --set-secrets "${JOB_SECRETS}" \
  --service-account "${APP_SA}" \
  --memory 2Gi --cpu 2 --task-timeout 25m --max-retries 0 \
  --project "${PROJECT}" --region "${REGION}"

echo "== [3/4] executing the job"
gcloud run jobs execute usali-migrate-seed --wait \
  --project "${PROJECT}" --region "${REGION}"

echo "== [4/4] the serving revision"
gcloud run deploy usali-app \
  --image "${IMAGE}" \
  --allow-unauthenticated \
  --service-account "${APP_SA}" \
  --set-cloudsql-instances "${CLOUDSQL}" \
  --set-env-vars "${COMMON_ENV},USALI_DB_USER=usali_app,USALI_OIDC_ISSUER=https://${AUTH_HOST}/realms/usali,USALI_OIDC_JWKS_URL=${AUTH_URL}/realms/usali/protocol/openid-connect/certs" \
  --set-secrets "${APP_SECRETS}" \
  --memory 2Gi --cpu 2 --cpu-boost \
  --min-instances 0 --max-instances 2 --concurrency 40 \
  --port 8080 \
  --project "${PROJECT}" --region "${REGION}"

URL="$(gcloud run services describe usali-app --project "${PROJECT}" \
  --region "${REGION}" --format='value(status.url)')"
echo
echo "Deployed: ${URL}"
echo "Smoke:    scripts/cloud/smoke_cloud.sh ${PROJECT}"
