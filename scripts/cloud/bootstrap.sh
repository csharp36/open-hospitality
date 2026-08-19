#!/usr/bin/env bash
# K2 GCP bootstrap (Pillar K, plan decisions 6 and 8): everything the
# cloud demo needs BELOW the running services — APIs, Artifact Registry,
# Cloud SQL (two databases), the photos bucket, secrets, service
# accounts + least-privilege bindings, and the $25 budget alert.
#
# IDEMPOTENT: every resource is describe-or-create; a second run is a
# no-op (the K2 gate). CREATE-ONLY: this script deletes nothing, ever —
# decommissioning lives in teardown.sh. Secret VALUES are generated
# in-process, piped straight into Secret Manager, and never printed.
#
# Run from a repo checkout (the HPKE keygen uses the project venv):
#   scripts/cloud/bootstrap.sh [project-id]
set -euo pipefail

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT}" || "${PROJECT}" == "(unset)" ]]; then
  echo "usage: scripts/cloud/bootstrap.sh <project-id>" >&2
  exit 2
fi

# Names are env-overridable: Cloud SQL reserves a deleted instance's
# name for about a week, so a teardown-then-rebootstrap inside that
# window needs SQL_INSTANCE pointed at a fresh name (see the runbook).
REGION="${REGION:-us-west1}"
SQL_INSTANCE="${SQL_INSTANCE:-usali-demo}"
AR_REPO="${AR_REPO:-usali}"
BUCKET="gs://${PROJECT}-usali-demo-photos"
APP_SA_NAME="usali-app"
DEPLOY_SA_NAME="usali-deploy"
APP_SA="${APP_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
DEPLOY_SA="${DEPLOY_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
BUDGET_NAME="usali-demo-budget-25"
SECRETS=(usali-db-password usali-app-db-password usali-provisioner-db-password
         keycloak-db-password keycloak-admin-password usali-hpke-private-key
         usali-field-encryption-key usali-admin-client-secret)

say() { printf '\n== %s\n' "$*"; }

say "APIs (${PROJECT})"
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com billingbudgets.googleapis.com \
  iam.googleapis.com \
  --project "${PROJECT}"

say "Artifact Registry (${AR_REPO}, ${REGION})"
gcloud artifacts repositories describe "${AR_REPO}" \
  --location="${REGION}" --project "${PROJECT}" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker --location="${REGION}" \
    --project "${PROJECT}" --description="usali cloud demo images"

say "Cloud SQL (${SQL_INSTANCE}: Postgres 16, db-f1-micro, 10GB HDD, zonal)"
# The demo tier (plan decision 8): shared-core, no SLA, ~\$9/mo — the
# whole point is the smallest thing that serves two quiet databases.
gcloud sql instances describe "${SQL_INSTANCE}" \
  --project "${PROJECT}" >/dev/null 2>&1 ||
  gcloud sql instances create "${SQL_INSTANCE}" \
    --project "${PROJECT}" \
    --database-version=POSTGRES_16 --edition=enterprise \
    --tier=db-f1-micro --region="${REGION}" \
    --storage-type=HDD --storage-size=10 \
    --availability-type=zonal \
    --no-assign-ip \
    --no-storage-auto-increase
    # --no-assign-ip (K7): the app reaches the DB through the Cloud SQL
    # Auth Proxy sidecar via the instance connection name, never a public
    # IP — so no public IP is the right surface (defense in depth; no
    # cost change). --no-storage-auto-increase (K7): a 10 GB HDD that
    # fills should fail LOUDLY on a fixed-size demo, not silently grow
    # storage that Cloud SQL can never shrink back (irreversible spend).

for db in usali keycloak; do
  gcloud sql databases describe "${db}" --instance="${SQL_INSTANCE}" \
    --project "${PROJECT}" >/dev/null 2>&1 ||
    gcloud sql databases create "${db}" --instance="${SQL_INSTANCE}" \
      --project "${PROJECT}"
done

say "Secrets (created once; values generated in-process, never printed)"
ensure_secret() { # name  <generator writing the value to stdout>
  local name="$1"
  shift
  # Create the container if absent — but the guard is VERSION existence,
  # not secret existence (K7 ops/cost lens): `secrets create` then
  # `versions add` are two steps, and an interruption between them (a
  # generator failure, a Ctrl-C) leaves a valueless secret that a
  # describe-guarded re-run would skip forever, surfacing far away as an
  # opaque "no versions" mount error at deploy. Adding a version when the
  # secret exists but is empty makes the recovery ("re-run bootstrap")
  # actually converge.
  if ! gcloud secrets describe "${name}" --project "${PROJECT}" \
    >/dev/null 2>&1; then
    gcloud secrets create "${name}" --project "${PROJECT}" \
      --replication-policy=user-managed --locations="${REGION}"
  fi
  if gcloud secrets versions list "${name}" --project "${PROJECT}" \
    --filter='state:enabled' --format='value(name)' 2>/dev/null \
    | grep -q .; then
    echo "   ${name}: exists"
  else
    "$@" | gcloud secrets versions add "${name}" \
      --project "${PROJECT}" --data-file=-
    echo "   ${name}: value added"
  fi
}

gen_password() { openssl rand -base64 24 | tr -d '\n'; }
gen_field_key() { openssl rand -base64 32 | tr -d '\n'; }
gen_hpke_key() {
  uv run python - <<'PY'
import base64
import sys

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat)

key = ec.generate_private_key(ec.SECP256R1())
der = key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
sys.stdout.write(base64.b64encode(der).decode())
PY
}

gen_personas() {
  uv run python - <<'PY'
import json
import secrets
import sys

personas = ("dev-gm", "dev-payroll", "dev-admin", "dev-accountant")
sys.stdout.write(json.dumps({p: secrets.token_urlsafe(12) for p in personas}))
PY
}

ensure_secret usali-db-password gen_password
# L2: the RLS-bound app role's password. The SERVING revision connects
# as usali_app (no BYPASSRLS, not the table owner — FORCE RLS applies);
# the migrate-seed job keeps the owner (usali).
ensure_secret usali-app-db-password gen_password
# D-B7: the least-privilege provisioner role's password. The SERVING
# revision opens a provisioner session (signup /complete) to write ONLY
# organization + role_assignment; a strong secret here replaces the
# config dev default (usali_provisioner).
ensure_secret usali-provisioner-db-password gen_password
ensure_secret keycloak-db-password gen_password
ensure_secret keycloak-admin-password gen_password
ensure_secret usali-hpke-private-key gen_hpke_key
ensure_secret usali-field-encryption-key gen_field_key
# The K3 pair. usali-admin-client-secret is runtime config (the app's
# Keycloak admin client) and rides the SECRETS accessor loop; the demo
# persona passwords are OPERATOR material — created here, deliberately
# never bound to the runtime SA (retrieve with
# `gcloud secrets versions access latest --secret=usali-demo-persona-passwords`).
ensure_secret usali-admin-client-secret gen_password
ensure_secret usali-demo-persona-passwords gen_personas

say "Cloud SQL users (passwords from Secret Manager)"
ensure_sql_user() { # user  secret-name
  if ! gcloud sql users list --instance="${SQL_INSTANCE}" \
    --project "${PROJECT}" --format='value(name)' | grep -qx "$1"; then
    gcloud sql users create "$1" --instance="${SQL_INSTANCE}" \
      --project "${PROJECT}" \
      --password="$(gcloud secrets versions access latest \
        --secret="$2" --project "${PROJECT}")"
    echo "   $1: created"
  else
    echo "   $1: exists"
  fi
}
ensure_sql_user usali usali-db-password
# L2: the app role. Cloud SQL users are never SUPERUSER and get no
# BYPASSRLS — but `sql users create` DOES make them cloudsqlsuperuser
# members with CREATEROLE/CREATEDB stamped on the role; the l2a0rlswall
# migration strips those two (dev/cloud parity), holds the RLS grants,
# and refuses to run until this role exists.
ensure_sql_user usali_app usali-app-db-password
# D-B7: the provisioner role. LOGIN only — the b1a0provrole migration
# strips CREATEROLE/CREATEDB and grants a role-specific permissive RLS
# policy on organization + role_assignment, and refuses to run until this
# role exists (CREATE ROLE is cluster-level, outside the migration chain).
# NOTE (existing environments): ensure_sql_user is describe-or-create and
# will NOT rotate a role that already exists. A demo where the role was
# created out-of-band with a placeholder password needs a one-time
# `gcloud sql users set-password usali_provisioner` to the secret value
# before the serving revision (which now mounts the secret) is deployed —
# see docs/deploy/demo-continuous-deploy.md.
ensure_sql_user usali_provisioner usali-provisioner-db-password
ensure_sql_user keycloak keycloak-db-password

say "Photos bucket (${BUCKET}: uniform access, public access PREVENTED)"
# The bucket only ever holds app-side ciphertext (K1) — and it is still
# locked down as if it held the photos themselves.
gcloud storage buckets describe "${BUCKET}" \
  --project "${PROJECT}" >/dev/null 2>&1 ||
  gcloud storage buckets create "${BUCKET}" \
    --project "${PROJECT}" --location="${REGION}" \
    --uniform-bucket-level-access --public-access-prevention

say "Service accounts"
ensure_sa() { # name  display
  if ! gcloud iam service-accounts describe \
    "$1@${PROJECT}.iam.gserviceaccount.com" \
    --project "${PROJECT}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$1" \
      --project "${PROJECT}" --display-name="$2"
    echo "   $1: created"
  else
    echo "   $1: exists"
  fi
}
ensure_sa "${APP_SA_NAME}" "usali demo runtime (app, auth, seed job)"
ensure_sa "${DEPLOY_SA_NAME}" "usali demo deploys"

say "IAM (least privilege; add-iam-policy-binding is a no-op when present)"
# A just-created service account can 400 as "does not exist" for a
# short propagation window (observed on the FIRST live run: the first
# binding landed, the second 400'd). Retry each binding through it.
bind() { # gcloud args...
  local attempt
  for attempt in $(seq 8); do
    if gcloud "$@" >/dev/null 2>&1; then
      return 0
    fi
    echo "   binding not ready (attempt ${attempt}/8), retrying in 5s"
    sleep 5
  done
  echo "ERROR: binding failed after retries: gcloud $*" >&2
  return 1
}
# Runtime SA: reach the database, read exactly its secrets, own exactly
# its bucket's objects — nothing else.
bind projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${APP_SA}" \
  --role="roles/cloudsql.client" --condition=None
for s in "${SECRETS[@]}"; do
  bind secrets add-iam-policy-binding "${s}" --project "${PROJECT}" \
    --member="serviceAccount:${APP_SA}" \
    --role="roles/secretmanager.secretAccessor"
done
bind storage buckets add-iam-policy-binding "${BUCKET}" \
  --member="serviceAccount:${APP_SA}" \
  --role="roles/storage.objectAdmin"
# Deploy SA: ship images and revisions, act as the runtime SA — and
# hold no data-plane roles at all.
bind projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/run.admin" --condition=None
bind projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/artifactregistry.writer" --condition=None
bind iam service-accounts add-iam-policy-binding "${APP_SA}" \
  --project "${PROJECT}" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/iam.serviceAccountUser"

say "Budget (\$25/mo, alerts at 50% / 90% / 100% — plan decision 8)"
BILLING="$(gcloud billing projects describe "${PROJECT}" \
  --format='value(billingAccountName)')"
if [[ -z "${BILLING}" ]]; then
  echo "ERROR: no billing account linked to ${PROJECT}" >&2
  exit 1
fi
if ! gcloud billing budgets list --billing-account="${BILLING}" \
  --format='value(displayName)' | grep -qx "${BUDGET_NAME}"; then
  gcloud billing budgets create --billing-account="${BILLING}" \
    --display-name="${BUDGET_NAME}" \
    --budget-amount=25USD \
    --filter-projects="projects/${PROJECT}" \
    --threshold-rule=percent=0.5 \
    --threshold-rule=percent=0.9 \
    --threshold-rule=percent=1.0
  echo "   ${BUDGET_NAME}: created"
else
  echo "   ${BUDGET_NAME}: exists"
fi

say "Bootstrap complete"
cat <<DONE
   project:   ${PROJECT}
   region:    ${REGION}
   sql:       ${SQL_INSTANCE} (databases: usali, keycloak)
   bucket:    ${BUCKET}
   registry:  ${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}
   runtime:   ${APP_SA}
   deploys:   ${DEPLOY_SA}
   secrets:   ${SECRETS[*]}
   (no secret VALUE was printed; access via Secret Manager IAM only)
DONE
