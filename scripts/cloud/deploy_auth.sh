#!/usr/bin/env bash
# K3: build + deploy the auth service (Keycloak) and map its hostname.
#   1. generate the CLOUD realm (credentials stripped — make_cloud_realm)
#   2. build the image from a TEMP context (official KC + realm only)
#   3. push to Artifact Registry, `gcloud run services replace` the spec
#   4. wait for OIDC discovery on the run.app URL
#   5. configure_auth.py — client secret + persona passwords from
#      Secret Manager (the realm imported credential-free)
#   6. domain-map auth.example.com and print the Cloudflare records
#      (DNS-only / grey cloud — the runbook's standing rule)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT}" || "${PROJECT}" == "(unset)" ]]; then
  echo "usage: scripts/cloud/deploy_auth.sh <project-id>" >&2
  exit 2
fi
REGION="${REGION:-us-west1}"
SQL_INSTANCE="${SQL_INSTANCE:-usali-demo}"
AR_REPO="${AR_REPO:-usali}"
AUTH_HOST="${AUTH_HOST:-auth.example.com}"
APP_HOST="${APP_HOST:-demo.example.com}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/usali-auth:$(git rev-parse --short HEAD)"

echo "== [1/6] cloud realm (credentials stripped)"
BUILD="$(mktemp -d)"
uv run python scripts/cloud/make_cloud_realm.py \
  --app-host "https://${APP_HOST}" --out "${BUILD}/realm-usali-cloud.json"
cp scripts/cloud/keycloak.Dockerfile "${BUILD}/Dockerfile"
# The login theme travels into the temp build context alongside the realm.
cp -R scripts/cloud/keycloak-theme/open-hospitality "${BUILD}/open-hospitality"

echo "== [2/6] image build (linux/amd64 — Cloud Run's platform)"
docker build --platform linux/amd64 -t "${IMAGE}" "${BUILD}"

echo "== [3/6] push + deploy"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker push "${IMAGE}"
sed -e "s|@IMAGE@|${IMAGE}|g" \
    -e "s|@PROJECT@|${PROJECT}|g" \
    -e "s|@REGION@|${REGION}|g" \
    -e "s|@SQL_INSTANCE@|${SQL_INSTANCE}|g" \
    -e "s|@AUTH_HOST@|${AUTH_HOST}|g" \
  scripts/cloud/auth-service.yaml.tmpl |
  gcloud run services replace - --project "${PROJECT}" --region "${REGION}"

# `services replace` ships the revision but grants nobody the door:
# the auth service is a PUBLIC OIDC issuer (browsers redirect to it),
# so allUsers gets run.invoker — authentication happens in Keycloak,
# not at Cloud Run's edge.
gcloud run services add-iam-policy-binding usali-auth \
  --member="allUsers" --role="roles/run.invoker" \
  --project "${PROJECT}" --region "${REGION}" >/dev/null

URL="$(gcloud run services describe usali-auth --project "${PROJECT}" \
  --region "${REGION}" --format='value(status.url)')"

echo "== [4/6] waiting for OIDC discovery at ${URL}"
for _ in $(seq 60); do
  if curl -fsS -o /dev/null "${URL}/realms/usali/.well-known/openid-configuration"; then
    break
  fi
  sleep 5
done
curl -fsS "${URL}/realms/usali/.well-known/openid-configuration" >/dev/null ||
  { echo "ERROR: discovery never came up at ${URL}" >&2; exit 1; }

echo "== [5/6] credential pass (client secret + persona passwords)"
uv run python scripts/cloud/configure_auth.py \
  --project "${PROJECT}" --auth-url "${URL}"

echo "== [6/6] domain mapping (${AUTH_HOST})"
if ! gcloud beta run domain-mappings describe --domain "${AUTH_HOST}" \
  --project "${PROJECT}" --region "${REGION}" >/dev/null 2>&1; then
  gcloud beta run domain-mappings create --service usali-auth \
    --domain "${AUTH_HOST}" --project "${PROJECT}" --region "${REGION}"
fi
echo
echo "Cloudflare records for ${AUTH_HOST} (DNS-ONLY / grey cloud — the"
echo "managed cert provisions through the unproxied record):"
gcloud beta run domain-mappings describe --domain "${AUTH_HOST}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format='table(status.resourceRecords[].name, status.resourceRecords[].type, status.resourceRecords[].rrdata)'
echo
echo "Deployed: ${URL} (issuer fixed to https://${AUTH_HOST})"
