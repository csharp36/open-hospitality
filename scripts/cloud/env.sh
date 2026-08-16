#!/usr/bin/env bash
# Shared container-env composition (K4) — sourced by entrypoint.sh AND
# job.sh (one predicate, one function: the serving container and the
# seed job must never each invent the DB URL).
#
# Cloud Run mounts the Cloud SQL unix socket at /cloudsql/<instance>;
# the password arrives as USALI_DB_PASSWORD from Secret Manager. The
# URL is composed here — with the password URL-QUOTED: generated
# passwords are base64 and a raw '/' or '+' in the userinfo would
# corrupt the URL silently. An explicitly provided USALI_DB_URL (the
# local smoke, dev) always wins.
#
# USALI_DB_USER selects the identity (L2): the SERVING revision sets it
# to the RLS-bound app role (usali_app); the migrate-seed job leaves it
# unset and keeps the OWNER default — same URL shape either way, so the
# job writes exactly the world the service reads.
if [[ -z "${USALI_DB_URL:-}" && -n "${USALI_DB_PASSWORD:-}" \
      && -n "${CLOUDSQL_INSTANCE:-}" ]]; then
  USALI_DB_URL="$(python - <<'PY'
import os
import urllib.parse

user = os.environ.get("USALI_DB_USER", "usali")
pw = urllib.parse.quote(os.environ["USALI_DB_PASSWORD"], safe="")
inst = os.environ["CLOUDSQL_INSTANCE"]
print(f"postgresql+psycopg://{user}:{pw}@/usali?host=/cloudsql/{inst}",
      end="")
PY
)"
  export USALI_DB_URL
fi
