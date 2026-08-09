#!/usr/bin/env bash
# K4 cloud smoke — walks the DEPLOYED stack on its run.app URLs (no DNS
# dependency): SPA on /, API refuses unauthenticated, a REAL operator
# token via the browser flow (mint_demo_token.py), kiosk enrollment
# through the legitimate admin endpoint, the kiosk config read with the
# minted device token, a demand refresh against the in-container
# loopback mock, and job idempotence (a re-execute exits 0 because the
# seed no-ops on its sentinel — a non-idempotent seed would die on its
# own unique constraints).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT}" || "${PROJECT}" == "(unset)" ]]; then
  echo "usage: scripts/cloud/smoke_cloud.sh <project-id>" >&2
  exit 2
fi
REGION="${REGION:-us-west1}"

APP_URL="$(gcloud run services describe usali-app --project "${PROJECT}" \
  --region "${REGION}" --format='value(status.url)')"
AUTH_URL="$(gcloud run services describe usali-auth --project "${PROJECT}" \
  --region "${REGION}" --format='value(status.url)')"
echo "app:  ${APP_URL}"
echo "auth: ${AUTH_URL}"

echo "== [1/6] SPA serves on /"
curl -fsS "${APP_URL}/" | grep -qi "<!doctype html" \
  || { echo "FAIL: SPA did not serve"; exit 1; }

echo "== [2/6] the API refuses unauthenticated"
code="$(curl -s -o /dev/null -w '%{http_code}' "${APP_URL}/api/kiosk/config")"
[[ "${code}" == "401" ]] \
  || { echo "FAIL: unauthenticated kiosk config was ${code}"; exit 1; }

echo "== [3/6] operator token via the REAL browser flow (dev-gm)"
TOKEN="$(uv run python scripts/cloud/mint_demo_token.py \
  --project "${PROJECT}" --auth-url "${AUTH_URL}")"
[[ -n "${TOKEN}" ]] || { echo "FAIL: no token minted"; exit 1; }

echo "== [4/6] kiosk enrollment through the admin endpoint"
DEVICE_TOKEN="$(curl -fsS -X POST "${APP_URL}/api/kiosk-devices" \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -d '{"property": "HISJ", "name": "smoke kiosk"}' |
  python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")"
curl -fsS -H "X-Kiosk-Token: ${DEVICE_TOKEN}" "${APP_URL}/api/kiosk/config" \
  | grep -q "matching_enabled" \
  || { echo "FAIL: kiosk config read with the enrolled device failed"; exit 1; }

echo "== [5/6] demand refresh against the loopback mock"
REFRESH="$(curl -fsS -X POST "${APP_URL}/api/crm/refresh" \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -d '{"property": "HISJ"}')"
echo "${REFRESH}" | python3 -c "
import json, sys
body = json.load(sys.stdin)
assert body['days_written'] >= 1, body['days_written']
assert body['provider'] == 'delphi'
print(f\"   refreshed: {body['days_written']} days, \"
      f\"dropped by name: {sorted(body['dropped_fields'])}\")
"

echo "== [6/6] job idempotence (re-execute no-ops on the sentinel)"
gcloud run jobs execute usali-migrate-seed --wait \
  --project "${PROJECT}" --region "${REGION}" >/dev/null

echo "OK — SPA, 401, browser-flow token, kiosk enroll+read, demand refresh, idempotent job"
