# Roster seeding

`usali seed-roster <csv> --property-id HISJ` creates employees, departments, and
Keycloak users for the operators.

```bash
cp seed/roster.example.csv seed/roster.csv   # then edit
usali seed-roster seed/roster.csv --property-id HISJ
```

`seed/roster.csv` and every other `*.csv`/`*.json` in this directory are
gitignored. Only `roster.example.csv` is tracked.

## Columns

| Column | Required | Notes |
|---|---|---|
| `full_name` | yes | |
| `pay_type` | yes | `hourly` or `salary` |
| `email` | for operators | Required when `role` is an operator role — provisioning a Keycloak user needs it |
| `role` | no | `org_admin`, `accountant`, `property_gm`, `department_manager`, `payroll_admin`, `employee`, or blank. `employee` and blank both mean "no login". |
| `department` | no | Created on demand |
| `employee_ref` | recommended | Stable external id (e.g. the incumbent system's employee number). Becomes the idempotency key. **Required whenever two rows share a name** — the loader refuses a file with repeated names and no ref, because it cannot tell a duplicate row from a second person. |

A row with no `role` gets **no Keycloak user** — `keycloak_subject` stays NULL
and the passwordless kiosk handles that person's identity. That is the correct
shape for hourly staff.

Seeding is idempotent by `full_name` within a property, so a re-run after a
partial failure tops up rather than duplicating.

## What a roster may not contain

The loader accepts an **allowlist of five identity/placement columns and nothing
else**. Anything not on that list — `SSN`, `IBAN`, `Tax ID`, `NSS`, `CURP`,
`Fecha de Nacimiento`, `sort_code`, `pay_grade`, whatever your export happens to
call it — causes the whole file to be refused.

The allowlist is the actual control. There is also a denylist of known-sensitive
substrings, but its only job is to produce a *clearer error message* for the
common cases; it is not what keeps data out, and it does not need to be
exhaustive. **Do not widen `ALLOWED_COLUMNS` to accommodate an export** — that is
the one change that would break this.

Two reasons, both concrete:

1. **There is nowhere to put them.** A Keycloak realm user is
   `username / email / firstName / lastName / credentials / realmRoles /
   attributes`. Nothing in a roster import consumes an SSN.
2. **It would defeat Pillar C.** Sensitive PII is HPKE-sealed in the browser and
   is never server-readable plaintext — that is the entire point of
   `pii_crypto.py` and the `Opener` seam. A flat file of SSNs on a developer
   laptop routes around all of it. Gitignored is not the same as protected: the
   file still lands in Time Machine, in backups, and in whatever indexes the
   disk.

Pay rate is excluded for a third reason: it is the single number that every
suppression rule in the system exists to protect (see the aggregate-suppression
tests in B3, C3, D1, and D3). It gets set through the app, encrypted, under
audit — not bulk-loaded from a file.

If you are migrating from an incumbent HR system, export the roster columns
only. Do not export the payroll tab and let the loader filter it; a plaintext
copy on disk is the harm, and by then it has already happened.
