#!/usr/bin/env bash
# Serving-container entrypoint (K1, plan decisions 1-2): the loopback
# mocks + the API under one supervisor. The mocks bind 127.0.0.1 (their
# CLI default) — present so the demand-refresh and payroll demos work,
# never reachable from outside the container. Die-together: the first
# process to exit takes the container down and Cloud Run restarts it;
# a half-alive container (API up, mocks gone) would demo refusals
# instead of the product.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

usali qbo-mock --port 9200 &
usali gusto-mock --port 9300 &
usali adp-mock --port 9301 &
usali delphi-mock --port 9400 &
usali tripleseat-mock --port 9401 &
# Cloud Run injects PORT; 0.0.0.0 because the container edge is Cloud
# Run's ingress, not the host loopback. --proxy-headers: the platform
# terminates TLS and overwrites X-Forwarded-* — without the trust,
# server-generated absolute URLs would speak http:// (K5).
usali serve --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers &

wait -n
exit 1
