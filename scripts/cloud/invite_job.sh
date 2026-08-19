#!/usr/bin/env bash
# Invite-origination job (Track B/B1, D-B4): mint ONE self-service signup
# invite and print its /signup?token= link. Invites stay CLI-only — no
# platform-admin HTTP surface (D-B4) — so this is the same `usali invite`
# command an operator runs locally, wrapped in a Cloud Run job so it can
# reach the demo's Cloud SQL. The link (and, in the console notifier, the
# "email") land in the job logs; the operator hands the link to the owner.
#
# Trigger (target email is override-per-run):
#   gcloud run jobs execute usali-invite \
#     --update-env-vars USALI_INVITE_EMAIL=owner@hotel.com --wait \
#     --region us-west1 --project <project>
# then read the printed link from the execution logs.
set -euo pipefail
# shellcheck disable=SC1091 # Runtime-relative path; env.sh is checked separately.
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

if [[ -z "${USALI_INVITE_EMAIL:-}" ]]; then
  echo "USALI_INVITE_EMAIL is required (the owner's email to invite)" >&2
  exit 2
fi

# No mocks and no seed: an invite is a single row. env.sh composed
# USALI_DB_URL from the password secret + the Cloud SQL socket; the CLI
# builds the link from USALI_PUBLIC_BASE_URL (the public app host).
exec usali invite "${USALI_INVITE_EMAIL}"
