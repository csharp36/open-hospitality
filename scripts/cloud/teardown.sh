#!/usr/bin/env bash
# K2 teardown (plan decision 8): remove every billable resource the
# bootstrap (and the K3/K4 deploys) created, leaving only the empty
# project. DESTRUCTIVE and gated on typing the project id back.
#
# Cloud SQL caveat: Google reserves a deleted instance's NAME for about
# a week. Re-bootstrapping inside that window must override
# SQL_INSTANCE with a fresh name.
set -euo pipefail

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT}" || "${PROJECT}" == "(unset)" ]]; then
  echo "usage: scripts/cloud/teardown.sh <project-id>" >&2
  exit 2
fi
REGION="${REGION:-us-west1}"
SQL_INSTANCE="${SQL_INSTANCE:-usali-demo}"
AR_REPO="${AR_REPO:-usali}"
BUCKET="gs://${PROJECT}-usali-demo-photos"
BUDGET_NAME="usali-demo-budget-25"

echo "This DELETES the usali demo stack in ${PROJECT}:"
echo "  Cloud Run services/jobs, SQL instance ${SQL_INSTANCE} (ALL demo data),"
echo "  ${BUCKET}, the demo secrets, both service accounts,"
echo "  Artifact Registry ${AR_REPO}, and the budget."
printf 'Type the project id to continue: '
read -r confirm
[[ "${confirm}" == "${PROJECT}" ]] || { echo "aborted"; exit 1; }

gone() { printf '   %s\n' "$*"; }

echo "== Domain mappings (K3/K5 — a deleted service ORPHANS its mapping)"
for domain in demo.example.com auth.example.com; do
  if gcloud beta run domain-mappings describe --domain="${domain}" \
    --region="${REGION}" --project "${PROJECT}" >/dev/null 2>&1; then
    gcloud beta run domain-mappings delete --domain="${domain}" \
      --region="${REGION}" --project "${PROJECT}" --quiet
    gone "mapping ${domain} (delete the Cloudflare CNAME by hand)"
  fi
done

echo "== Cloud Run services + jobs (K3/K4 — absent is fine)"
for svc in usali-app usali-auth; do
  if gcloud run services describe "${svc}" --region="${REGION}" \
    --project "${PROJECT}" >/dev/null 2>&1; then
    gcloud run services delete "${svc}" --region="${REGION}" \
      --project "${PROJECT}" --quiet
    gone "service ${svc}"
  fi
done
for job in usali-migrate-seed; do
  if gcloud run jobs describe "${job}" --region="${REGION}" \
    --project "${PROJECT}" >/dev/null 2>&1; then
    gcloud run jobs delete "${job}" --region="${REGION}" \
      --project "${PROJECT}" --quiet
    gone "job ${job}"
  fi
done

echo "== Cloud SQL"
if gcloud sql instances describe "${SQL_INSTANCE}" \
  --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud sql instances delete "${SQL_INSTANCE}" \
    --project "${PROJECT}" --quiet
  gone "instance ${SQL_INSTANCE} (name reserved by Google ~1 week)"
fi
# K7 ops lens: a bootstrap that used SQL_INSTANCE=usali-demo-2 (the
# documented name-reservation workaround) leaves that instance running
# when teardown is later invoked without the override — the dominant
# cost line, silently orphaned under the completion banner below. Name
# every other usali-* SQL instance loudly.
OTHER_SQL="$(gcloud sql instances list --project "${PROJECT}" \
  --filter="name~^usali AND NOT name=${SQL_INSTANCE}" \
  --format='value(name)' 2>/dev/null || true)"
if [[ -n "${OTHER_SQL}" ]]; then
  echo "   WARNING: other usali SQL instance(s) still BILLING — not this"
  echo "   teardown's target (\$SQL_INSTANCE=${SQL_INSTANCE}). Re-run with"
  echo "   SQL_INSTANCE set to each, or delete by hand:"
  printf '     %s\n' ${OTHER_SQL}
fi

echo "== Bucket"
if gcloud storage buckets describe "${BUCKET}" \
  --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud storage rm -r "${BUCKET}" --project "${PROJECT}"
  gone "${BUCKET}"
fi

echo "== Secrets"
for s in usali-db-password usali-app-db-password keycloak-db-password \
  keycloak-admin-password usali-hpke-private-key \
  usali-field-encryption-key usali-admin-client-secret \
  usali-demo-persona-passwords; do
  if gcloud secrets describe "${s}" --project "${PROJECT}" \
    >/dev/null 2>&1; then
    gcloud secrets delete "${s}" --project "${PROJECT}" --quiet
    gone "secret ${s}"
  fi
done

echo "== Service accounts"
for sa in usali-app usali-deploy; do
  email="${sa}@${PROJECT}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "${email}" \
    --project "${PROJECT}" >/dev/null 2>&1; then
    gcloud iam service-accounts delete "${email}" \
      --project "${PROJECT}" --quiet
    gone "${email}"
  fi
done

echo "== Artifact Registry"
if gcloud artifacts repositories describe "${AR_REPO}" \
  --location="${REGION}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud artifacts repositories delete "${AR_REPO}" \
    --location="${REGION}" --project "${PROJECT}" --quiet
  gone "repository ${AR_REPO}"
fi

echo "== Budget"
BILLING="$(gcloud billing projects describe "${PROJECT}" \
  --format='value(billingAccountName)' 2>/dev/null || true)"
if [[ -n "${BILLING}" ]]; then
  BUDGET_ID="$(gcloud billing budgets list --billing-account="${BILLING}" \
    --filter="displayName=${BUDGET_NAME}" --format='value(name)' | head -1)"
  if [[ -n "${BUDGET_ID}" ]]; then
    gcloud billing budgets delete "${BUDGET_ID}" --quiet
    gone "budget ${BUDGET_NAME}"
  fi
fi

echo "Teardown complete — nothing billable remains."
