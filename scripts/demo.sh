#!/usr/bin/env bash
# GM demo: one command from the encrypted-volume roster export to a running
# stack seeded with the real staff (identities only — every rate, SSN, and
# bank account is synthetic; see scripts/demo_seed.py).
#
#   scripts/demo.sh [--fresh] /Volumes/Employees/inn-flow-roster-2026-07-18.json
#
# Steps:
#   1. stop any running dev stack (the API must restart with the demo HPKE key)
#   2. --fresh: wipe the database volume for a from-scratch rebuild
#   3. generate-once + export the shared dev HPKE key, so the sealed synthetic
#      PII the seeder plants is openable by the API across restarts
#   4. recreate the Keycloak container (re-imports realm-usali.json, which
#      carries the dev-gm / dev-payroll / dev-admin demo logins)
#   5. scripts/dev.sh start  (postgres, keycloak, migrate, API, mocks, vite)
#   6. seed: roster import (compensation fields filtered) + the demo world
#
# The roster JSON never leaves the encrypted volume; it is read once, filtered
# to identity/org fields, and only those land in the local demo database.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRESH=0
ROSTER=""

for arg in "$@"; do
  case "$arg" in
    --fresh) FRESH=1 ;;
    -h|--help)
      sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) ROSTER="$arg" ;;
  esac
done

if [[ -z "$ROSTER" ]]; then
  echo "usage: scripts/demo.sh [--fresh] /path/to/inn-flow-roster.json" >&2
  exit 2
fi
if [[ ! -f "$ROSTER" ]]; then
  echo "ERROR: roster not found: $ROSTER — is the encrypted volume mounted?" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p .dev

echo "[demo 1/5] stopping any running dev stack"
scripts/dev.sh stop >/dev/null 2>&1 || true

if (( FRESH )); then
  echo "[demo 2/5] wiping database volume (--fresh)"
  docker compose down -v
else
  echo "[demo 2/5] keeping existing database (pass --fresh to wipe)"
fi

# Shared dev HPKE key: generated once, exported to the seeder AND (via the
# environment dev.sh's children inherit) to the API process. Without a shared
# persistent key the server generates an ephemeral one and every sealed value
# the seeder plants would fail to open.
KEY_FILE="$ROOT/.dev/demo-hpke-key.b64"
if [[ ! -f "$KEY_FILE" ]]; then
  echo "[demo 3/5] generating dev HPKE keypair (.dev/demo-hpke-key.b64)"
  uv run python - >"$KEY_FILE" <<'PY'
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat)
key = ec.generate_private_key(ec.SECP256R1())
der = key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
print(base64.b64encode(der).decode())
PY
else
  echo "[demo 3/5] reusing dev HPKE key (.dev/demo-hpke-key.b64)"
fi
USALI_PII_HPKE_PRIVATE_KEY="$(tr -d '\n' <"$KEY_FILE")"
export USALI_PII_HPKE_PRIVATE_KEY
export USALI_PII_HPKE_KEY_ID="demo-1"

# Face matching (Pillar F): ON for the demo — synthetic people, dev
# enablement is free (plan decision 7). The models are fetched sha256-pinned
# (~280MB, once); if the fetch fails the stack still runs and the kiosk
# falls back to search-only via /kiosk/config.
export USALI_BIOMETRIC_MATCHING_ENABLED=true

# CRM demand feed (Pillar J): ON for the demo, pointed at the local Delphi
# mock dev.sh starts on :9400. The seed pulls the fixed demand world once
# (a fat Thursday group block), and the API serves it to the Schedule page.
export USALI_CRM_PROVIDER=delphi
echo "[demo 3b/5] face matching deps + models (fetch once, verify always)"
uv sync --extra dev --extra face --quiet
uv run python scripts/fetch_face_models.py || \
  echo "WARNING: face models unavailable — kiosk will run search-only"

# Keycloak keeps its realm in the container filesystem, so realm-user changes
# (the demo logins) only land on a fresh container. Recreate deterministically.
echo "[demo 4/5] recreating Keycloak (realm re-import with demo logins)"
docker compose up -d --force-recreate keycloak

scripts/dev.sh start

echo "[demo 5/5] seeding"
uv run python scripts/demo_seed.py --roster "$ROSTER"

cat <<EOF

============================================================
 Demo ready: http://localhost:5173
------------------------------------------------------------
 Logins (password: devpass)
   dev-gm          property GM view (HISJ + SSSJ)
   dev-payroll     payroll admin (sick leave, sealed PII, pay runs)
   dev-admin       org admin (onboarding)
   dev-accountant  accountant (reports, QBO)
 Kiosk device tokens: .demo-kiosk-token
 Stop everything:     scripts/dev.sh stop
============================================================
EOF
