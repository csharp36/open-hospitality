#!/usr/bin/env bash
# Migrate-seed job entrypoint (K1, plan decision 7): mocks -> alembic ->
# demo seed, in that order — the repo's migrate-before-deploy standard.
# The seed pulls payroll and demand through the REAL adapters over HTTP
# to the loopback mocks (the G6 posture, preserved in the cloud), and it
# is sentinel-guarded: re-running the job tops up and no-ops the world.
# The ONLY roster is the fictitious cloud one; there is no flag to point
# this at anything else.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

usali gusto-mock --port 9300 &
usali delphi-mock --port 9400 &
usali tripleseat-mock --port 9401 &

# No curl in the slim image; wait for the mock ports with stdlib python.
python - <<'PY'
import socket
import time

for port in (9300, 9400, 9401):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        raise SystemExit(f"mock on 127.0.0.1:{port} never came up")
PY

alembic upgrade head
# --synthetic-year (K6b): the cloud world's figures are INVENTED — the
# sample PDFs carry real production numbers and must never be seeded here.
python scripts/demo_seed.py --roster scripts/cloud/demo_roster.json --synthetic-year
