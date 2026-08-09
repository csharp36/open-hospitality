#!/usr/bin/env bash
# K1 local smoke: build the image, run the migrate-seed job and the
# serving container against a THROWAWAY postgres (never the dev compose
# database), and walk the surface the way Cloud Run will see it:
#   - SPA serves on /
#   - the API refuses unauthenticated (401)
#   - one authenticated read (kiosk config, via the seeded device token)
#   - the job re-run no-ops on its sentinel
#   - the loopback mocks are NOT reachable at the container's address
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

NET=usali-k1-smoke
PG=usali-k1-pg
APP=usali-k1-app
JOB=usali-k1-job
IMG=usali-demo:k1-smoke
HOST_PORT="${K1_SMOKE_PORT:-18080}"

cleanup() {
  docker rm -f "$PG" "$APP" "$JOB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo "[smoke 1/6] docker build"
docker build -t "$IMG" .

echo "[smoke 2/6] throwaway postgres"
docker network create "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" \
  -e POSTGRES_USER=usali -e POSTGRES_PASSWORD=usali -e POSTGRES_DB=usali \
  postgres:16 >/dev/null
for _ in $(seq 60); do
  if docker exec "$PG" pg_isready -U usali >/dev/null 2>&1; then break; fi
  sleep 1
done

HPKE_KEY="$(uv run python - <<'PY'
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat)
key = ec.generate_private_key(ec.SECP256R1())
der = key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
print(base64.b64encode(der).decode())
PY
)"

ENVS=(
  -e "USALI_DB_URL=postgresql+psycopg://usali:usali@${PG}:5432/usali"
  -e "USALI_PII_HPKE_PRIVATE_KEY=${HPKE_KEY}"
  -e "USALI_PII_HPKE_KEY_ID=k1-smoke"
  -e "USALI_BIOMETRIC_MATCHING_ENABLED=true"
  -e "USALI_CRM_PROVIDER=delphi"
  # The smoke runs the LOCAL photo store (no GCS emulator): /tmp is the
  # one writable path, and job-written photos staying job-local is
  # accepted here — the cloud sets USALI_PHOTO_STORE_GCS_BUCKET (K4),
  # and the image deliberately keeps /app/punch-photos UNWRITABLE so a
  # bucket-less cloud deploy fails loudly at the first photo write
  # instead of quietly writing to ephemeral disk.
  -e "USALI_PHOTO_STORE_DIR=/tmp/punch-photos"
)

echo "[smoke 3/6] migrate + seed job"
docker run --name "$JOB" --network "$NET" "${ENVS[@]}" \
  --entrypoint /app/scripts/cloud/job.sh "$IMG"
TOKENS="$(mktemp -d)/kiosk-tokens"
docker cp "$JOB:/app/.demo-kiosk-token" "$TOKENS"
docker rm "$JOB" >/dev/null

echo "[smoke 4/6] job idempotence (re-run tops up, no-ops the world)"
docker run --rm --network "$NET" "${ENVS[@]}" \
  --entrypoint /app/scripts/cloud/job.sh "$IMG" \
  | grep -q "already seeded"

echo "[smoke 5/6] serve + walk"
docker run -d --name "$APP" --network "$NET" "${ENVS[@]}" \
  -p "${HOST_PORT}:8080" "$IMG" >/dev/null
BASE="http://127.0.0.1:${HOST_PORT}"
for _ in $(seq 60); do
  if curl -fsS -o /dev/null "$BASE/" 2>/dev/null; then break; fi
  sleep 1
done

curl -fsS "$BASE/" | grep -qi "<!doctype html" \
  || { echo "FAIL: SPA did not serve on /"; exit 1; }

code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/kiosk/config")"
[[ "$code" == "401" ]] \
  || { echo "FAIL: unauthenticated kiosk config was $code, wanted 401"; exit 1; }

TOKEN="$(awk '$1=="HISJ"{print $2}' "$TOKENS")"
curl -fsS -H "X-Kiosk-Token: ${TOKEN}" "$BASE/api/kiosk/config" \
  | grep -q "matching_enabled" \
  || { echo "FAIL: authenticated kiosk config read failed"; exit 1; }

# The disclosure pin: mocks bind the container's loopback — another
# container on the same network must NOT be able to reach them. Inline
# -c (never stdin: docker run without -i feeds python an EMPTY program
# that exits 0 — the first version of this check failed on exactly
# that) and fail-closed: only a probe that RAN and was REFUSED passes.
docker run --rm --network "$NET" --entrypoint python "$IMG" -c "
import socket, sys
try:
    socket.create_connection(('${APP}', 9400), timeout=2).close()
except OSError:
    sys.exit(0)  # unreachable — the good outcome
sys.exit(1)
" || { echo "FAIL: delphi mock check did not read as loopback-only"; exit 1; }

echo "[smoke 6/6] OK — SPA, 401, authenticated kiosk read, idempotent job, loopback-only mocks"
