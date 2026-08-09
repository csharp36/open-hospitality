# Open Hospitality — Architecture

Local, no-cloud pipeline that ingests hotel PMS report PDFs, maps transactions to USALI
schedules, and loads a unified financial database. See `docs/` for design and reference.

## Quickstart

### Prerequisites

- **Docker** running (Postgres + Keycloak)
- **[uv](https://docs.astral.sh/uv/)** — `scripts/dev.sh` runs Python through `uv run`
- **Node 20+** — for the Vite dev server

One-time setup:

```bash
uv sync                          # Python deps (creates .venv)
cd frontend && npm install; cd ..  # frontend deps
```

### Run everything: `scripts/dev.sh`

Brings up the whole stack in dependency order — containers, then the DB migration, then the API,
the provider mocks, and the frontend — waiting for each to be genuinely ready (not just launched):

```bash
scripts/dev.sh start      # bring everything up
scripts/dev.sh status     # per-service state, port, and pid
scripts/dev.sh logs api   # tail one service's log (follows)
scripts/dev.sh restart    # stop then start
scripts/dev.sh stop       # stop everything (DB volume preserved)
```

`start` flags: `--no-mocks` (skip the three provider mocks), `--no-frontend` (API only, no Node).

| Service | Port | Notes |
|---|---|---|
| Portal (Vite) | **http://localhost:5173** | the app — start here |
| API (`usali serve`) | 8100 | Vite proxies `/api` + `/ingest` to it |
| Postgres | 5433 | docker compose; volume survives `stop` |
| Keycloak | 9080 | docker compose; realm `usali` auto-imported |
| qbo-mock / gusto-mock / adp-mock | 9200 / 9300 / 9301 | mock QuickBooks + payroll providers |

Background services write pidfiles and logs under `.dev/` (gitignored). `start` is idempotent —
already-running services are left alone — and it refuses to start a service whose port is held by a
process it doesn't own (`status` shows that as `foreign`).

**Log in** with the seeded dev user: `dev-accountant` / `devpass` (see [Local auth](#local-auth-keycloak)).

### First run: seed some data

`dev.sh` migrates the schema but deliberately does **not** seed — a fresh database means an empty
portal. Load the reference data and one sample day (with the stack up, or at least Postgres):

```bash
uv run usali seed-schedules
uv run usali seed-properties
uv run usali seed-mappings mapping/opera.yaml
uv run usali ingest "docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf" \
    --source OPERA --report trial_balance --property-id HISJ
uv run usali transform --source OPERA --business-date 2026-07-07 --edition 12
```

Expected final output: `mapped=14 unmapped=0 reconciled=True`. Then open the portal and pick
property **HISJ**, business date **2026-07-07**.

To wipe the database and start over: `scripts/dev.sh stop && docker compose down -v` (the script
never deletes the volume itself).

### Manual / step-by-step

The same thing without the script — useful when you want one piece at a time:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d          # Postgres on host port 5433
.venv/bin/alembic upgrade head
.venv/bin/usali seed-schedules
.venv/bin/usali seed-properties
.venv/bin/usali seed-mappings mapping/opera.yaml
.venv/bin/usali ingest "docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf" \
    --source OPERA --report trial_balance --property-id HISJ
.venv/bin/usali transform --source OPERA --business-date 2026-07-07 --edition 12
```

Expected final output: `mapped=14 unmapped=0 reconciled=True`.

### Autoclerk (second source)

```bash
.venv/bin/usali seed-mappings mapping/autoclerk.yaml
.venv/bin/usali ingest "docs/reference/samples/Autoclerk - Transaction Summary 07.07.2026.pdf" \
    --source AUTOCLERK --report transaction_summary --property-id SSSJ
.venv/bin/usali transform --source AUTOCLERK --business-date 2026-07-07 --edition 12
```

`ingest` derives the business date from the report itself — no date flag needed.

Expected final output: `mapped=32 unmapped=0 reconciled=True`.

### Drop-and-forget ingestion

```bash
# one-shot: auto-detects source/report/property from the PDF itself
.venv/bin/usali process path/to/any-report.pdf

# sentinel directory: everything dropped into inbox/ is processed automatically
.venv/bin/usali watch

# or a local upload endpoint (localhost, no auth):
.venv/bin/usali serve &
curl -F "file=@docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf" \
    http://127.0.0.1:8100/ingest
```

Successes are filed to `processed/`, failures to `failed/` with the error recorded on
`ingest_batch`. Re-processing the same file (or date) is a safe no-op.

Property detection resolves a report's property from the `property_detection_alias` DB
table, seeded from `mapping/properties.yaml` via `usali seed-properties`; the YAML is a
seed source, not read at ingest time. To register a new property: add a row to
`mapping/properties.yaml` and re-run `usali seed-properties` (or insert `property` +
`property_detection_alias` rows directly).

### Portal

`usali serve` also exposes the read-only portal API under `/api/*` and, when a
frontend build exists at `frontend/dist/`, serves the SPA at `/`. For development
(requires Node 20+) — `scripts/dev.sh start` does all of this for you; by hand it is:

```bash
.venv/bin/usali serve &                  # API on 127.0.0.1:8100
cd frontend && npm install && npm run dev  # Vite dev server on 5173, proxies /api + /ingest
```

For a production-style run, `cd frontend && npm run build`, then `usali serve`
serves the built SPA directly — no Node needed at runtime.

The portal has light and dark themes — the toggle at the right of the nav bar
persists your choice in the browser (falls back to the OS preference).

End-to-end tests (`cd frontend && npm run e2e`) drive the full stack with
Playwright: `scripts/e2e_backend.py` boots a throwaway Testcontainers Postgres
seeded with the six sample PDFs (plus a mock QuickBooks Online on port 9200 and
a mock Gusto payroll provider on port 9300), and the tests exercise upload,
statement drill-through, coverage, the CPA monthly pack, a real QBO push with
idempotent re-push, and the pay-run round-trip (C2 submit/fetch plus C3's
explicit-click employee detail) against the Vite dev server. Requires a running Docker daemon and a one-time
`npx playwright install chromium`.

Statistics reports (Opera Manager Flash, Autoclerk Manager Report) are auto-detected the
same way as the financial reports. Their canonical KPIs (ADR, RevPAR, occupancy, arrivals,
…) land in `usali_statistic_fact` keyed by DAY/MTD/YTD period (plus prior-year for Opera),
comparable side by side across both PMSs.

Segmentation reports (Opera Market Code Statistics, Autoclerk Revenue by Rate Plan) are
auto-detected too, landing a **strictly reconciled** Rooms segment split
(Transient/Group/Contract/Complimentary/House-use) in `usali_segment_fact`.

## Local auth (Keycloak)

The platform is now behind Keycloak OIDC — every `/api/*` and `/ingest` call
requires an operator bearer token, so the API is closed by default. Start
Keycloak (and Postgres) with:

```bash
docker compose up -d keycloak postgres   # Keycloak on 127.0.0.1:9080, realm `usali`
```

The `usali` realm is auto-imported from `keycloak/realm-usali.json` on startup.
A dev login is seeded: user `dev-accountant` / password `devpass`, which holds
the `accountant` operator role. The portal redirects to Keycloak on load; after
signing in you land back in the SPA via `/callback`.

**Roles.** The operator roles are `org_admin`, `accountant`, `property_gm`,
`department_manager`, and `payroll_admin` — all five reach the financial
portal. `employee` is self-service only (A2) and does not. See "Roles &
scope" below for how property/department scoping and the PII gate work.

**Running the API.** `usali serve` now requires a reachable OIDC issuer. The
defaults target the compose Keycloak above; point at another issuer via
`USALI_OIDC_ISSUER`, `USALI_OIDC_JWKS_URL`, and `USALI_OIDC_AUDIENCE`.

**Frontend config for deployment.** The SPA reads its OIDC settings at build
time — set `VITE_OIDC_AUTHORITY` and `VITE_OIDC_CLIENT_ID` to swap the issuer
and client for a deployed environment.

**Known A1 limitation — no token refresh yet.** `automaticSilentRenew` and
`monitorSession` are off, and the OIDC scope omits `offline_access`, so Keycloak
issues no refresh token. An expired session simply bounces the user back to
Keycloak to re-authenticate. Enabling silent renew later requires adding
`offline_access` (or `prompt=none` session monitoring) — deferred to a later
phase.

The test suite and Playwright e2e need no running Keycloak: they authenticate
against an offline mock JWT issuer (`tests/authkit.py`) that mints tokens a local
`TokenVerifier` accepts.

**`USALI_FIELD_ENCRYPTION_KEY`.** PII fields (e.g. employee compensation) are
stored AES-256-GCM encrypted. The key is a base64-encoded 32-byte value; a
dev/test default ships for local use, but production must set
`USALI_FIELD_ENCRYPTION_KEY` (from AWS Secrets Manager). There is no
key-versioning yet, so rotating the key makes existing ciphertext unreadable —
a Pillar C concern.

**Roles & scope.** `org_admin`, `accountant`, and `payroll_admin` see all
properties; `property_gm` is scoped to its property and `department_manager`
to its department — financial endpoints 403 on out-of-scope properties, and
`/api/employees` filters by scope. Only `payroll_admin` may read PII
(`GET /api/employees/{id}/compensation`), and every such read is recorded in
`audit_event`. Scope resolution is hybrid: the JWT `scopes` claim when
present, else the `role_assignment` table — the table is authoritative today;
mirroring assignments into Keycloak `scopes` claims is a later optimization.

**Operator onboarding (A2.3).** `POST /api/employees` onboards an employee:
for an operator role it provisions a Keycloak user via the `usali-admin`
admin client and writes the authoritative `role_assignment` row; an operator
role requires an `email` (422 if missing, since it provisions a login). An
hourly employee with no role is recorded with `keycloak_subject = NULL` — no
login is provisioned. `POST /api/employees/{id}/terminate` disables the
Keycloak user and marks the employee record terminated. Both endpoints are
gated to `org_admin`/`property_gm`; a `property_gm` may only onboard or
terminate within its own property (403 otherwise). `GET /api/me` returns the
caller's roles, which the SPA uses to show the admin **Employees** page and
nav link to `org_admin`/`property_gm` only (enforcement stays server-side —
the nav link is a convenience, not a security boundary).

Hourly-employee self-service is **Pillar B**, deferred from this phase. The
photo-punch kiosk (below) ships in this phase as **B1**.

## Time clock (kiosk)

**Enroll.** `POST /api/kiosk-devices` registers an iPad for a property —
gated to `org_admin`/`property_gm` (a GM may only enroll for its own
property). The response carries the device token **in plaintext exactly
once**; paste it into the iPad at `/kiosk`. The server only ever stores its
SHA-256 hash, so a lost token can't be recovered — enroll a new device
instead. `POST /api/kiosk-devices/{id}/revoke` disables a device
immediately, and `GET /api/kiosk-devices` lists a property's devices
(revoked or not).

**Trust model.** A punch is **device-authenticated, not
user-authenticated** — the employee taps their name on the kiosk with no PIN
or password. This is deliberate: it mirrors the pilot's real flow. Buddy
punching is deterred by the live photo captured at punch time plus manager
review at timecard approval (B2), not by a secret. **No face recognition is
performed** — photos are human-reviewed evidence only, never biometric
templates, and a kiosk may only punch its own property's staff
(`GET /api/kiosk/employees`, `POST /api/kiosk/punch`, both authenticated via
the `X-Kiosk-Token` header rather than an operator session).

**Photos.** Punch photos are encrypted at rest with AES-256-GCM
(`USALI_PHOTO_STORE_DIR`; S3 + SSE-KMS is a deployment-time drop-in for
prod, not built yet) and purged `USALI_PUNCH_PHOTO_RETENTION_DAYS` (default
90) days after the timecard is approved — see "Timecards & approval" below
for the read gate and the purge job.

**Double taps.** An identical punch (same employee, same type) repeated
within 60 seconds is refused with a 409 — a double-tap on a touchscreen is
not a second event. A device's `last_seen_at` is only rewritten once every
5 minutes, so a busy kiosk does not write a row per poll.

**Business date.** Punches are attributed to a business date in the
**property's** local timezone (`property.timezone`, IANA), with a cutoff of
`USALI_PUNCH_BUSINESS_DAY_CUTOFF_HOUR` (default 4): a punch before 4 AM
property-local time is attributed to the prior business date, so an
overnight shift lands on the same business day as the revenue it supported —
matching the PMS night audit.

The kiosk page (`/kiosk`) lives **outside** the OIDC guard — it authenticates
via the pasted device token, not an operator login. Tapping an employee's
name captures a live photo and records Clock In / Start Lunch / End Lunch /
Clock Out.

**The `usali-admin` client.** Provisioning talks to Keycloak's admin REST API
through `usali-admin` — a confidential, service-account-enabled client
(`keycloak/realm-usali.json`). In production its service account needs the
`realm-management` client roles `manage-users` and `view-users`. The dev
secret is `dev-admin-secret`; override via `USALI_KC_ADMIN_BASE_URL`,
`USALI_KC_ADMIN_REALM`, `USALI_KC_ADMIN_CLIENT_ID`, and
`USALI_KC_ADMIN_CLIENT_SECRET`. The test suite never calls this client — it
injects an in-memory fake (`InMemoryKeycloakAdmin`), so onboarding is
offline-testable.

## Timecards & approval

B2 turns raw punches into **biweekly timecards** a GM can review and approve.
Periods are deterministic, derived from `USALI_PAYROLL_PERIOD_ANCHOR` (default
`2026-01-05`, a Monday): the period containing a business date is
`anchor + 14 × ((date − anchor) // 14)`, fourteen days long. Move the anchor and
every period moves with it — there is no period table to keep in sync.

**Hours are a pure function over a merged timeline** — the immutable `punch`
rows *plus* additive `timecard_adjustment` rows, sorted together. A correction
**adds an event; it never edits a punch**, so the punch table stays the
evidentiary record and every correction carries an actor and a reason. Lunch is
unpaid. An unclosed span (a missed clock-out) pays **zero** rather than guessing
an end time — it surfaces as a warning for the manager instead of as free hours.

**Warnings are flagged, never priced** (gross-to-net is Pillar C):
`missing_clock_in`, `missing_clock_out`, `missing_lunch_start`,
`missing_lunch_end`, plus the California meal-break checks — `no_meal_break`
(over 5 hours worked, no lunch at all), `late_meal_break` (lunch started after
the 5th hour of work), and `short_meal_break` (a lunch under 30 minutes).

**The API.** Every route below is `org_admin` / `property_gm` only, and a GM is
confined to its own property — scope comes from the `role_assignment` table, not
from a co-held global VIEW role, so an accountant-plus-GM cannot approve hours at
a property they do not run. Out-of-scope cards are *invisible* in the list, not
merely un-approvable.

```
GET  /api/timecards[?status=open]        # cards the caller may approve
GET  /api/timecards/{id}                 # per-day hours + warnings
POST /api/timecards/{id}/adjustments     # 201 — an additive, audited correction
POST /api/timecards/{id}/approve         # approve and LOCK
GET  /api/punches/{id}/photo             # the evidence photo (audited)
```

An adjustment body is `{punch_type, adjusted_at, business_date, reason}`. It is
refused with a 422 if the `punch_type` is not one of the four kiosk types, if
`business_date` falls outside the card's own period (that would book hours into a
period they did not happen in), if `adjusted_at` is timezone-**naive** (a guessed
zone silently moves someone's paid hours), or if `reason` is empty or longer than
300 characters. Adjustments are recorded in `audit_event`.

**Approval locks the card.** Re-approving is a 409, and so is any adjustment made
afterwards — a locked card is locked. Approval is audited and starts the photo
retention clock. Note the states are `open → approved`: the design's `submitted`
step is deliberately not built, because employees have no login and so no actor
could ever submit.

**The punch photo is the only photo-read path in the system** — B1 shipped with
zero photo egress by design. `GET /api/punches/{id}/photo` is approver-only,
confined to the punch's property, sent `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`, and **audited on every read** (`view_punch_photo`)
— the audit row is committed *before* any byte reaches the wire. It is audited on
the **success path only**: a 403 or 404 released nothing, and logging denials would
let an operator flood the PHI-access trail by hammering a URL. The content type
follows the actual bytes, not the store's optimism: a JPEG goes out as
`image/jpeg`, and anything else goes out inert as `application/octet-stream` +
`Content-Disposition: attachment` — never as an image a browser will render. A
purged (or never-stored) photo is a plain 404. **No face matching is performed
anywhere** — this is a human looking at a picture.

**Retention purge.** Once a card has been approved longer than
`USALI_PUNCH_PHOTO_RETENTION_DAYS` (default 90), its photos are deleted and the
pointers cleared:

```bash
.venv/bin/usali purge-punch-photos     # → "Purged N punch photos"
```

Only `approved` cards past the cutoff and not already purged are touched, and the
card is stamped (`photos_purged_at`) so a second run is a no-op. If the storage
backend fails on one key, that key is logged and skipped and the card is left
**unstamped** so the next run retries it — the batch is never left half-purged
with a card marked done.

The portal gains a **Timecards** page (nav link shown to `org_admin` /
`property_gm`; enforcement is server-side either way) listing cards with hours and
status, with a detail panel of per-day hours, warnings, and an Approve button.

**Known gaps.** `TimecardModel` carries no per-day punch ids, so the review page
cannot render photo thumbnails yet even though the endpoint exists — extending the
detail model is the natural follow-up. A shift that crosses the 04:00 business-date
cutoff (e.g. clock in 23:00, clock out 07:00 next morning) is split across two
business dates by design, so each half is flagged as a missing punch and the shift
shows zero paid minutes until a manager adds a correcting adjustment. Overnight
shifts that end before 04:00 (true night-audit) pair correctly and pay. Cross-cutoff
span pairing is deferred. **B3** adds overtime, pay rates, and labor cost —
B2 ships *approved hours*, nothing priced.

## Labor cost → USALI facts

B3 closes the loop: it turns B2's **approved** hours into estimated labor cost and
surfaces it on the Summary Operating Statement. Nothing here is priced until a
timecard is approved and locked, so the cost can never drift under the number.

**Encrypted pay rate.** Each employee carries an hourly `pay_rate`, stored
AES-256-GCM encrypted at rest exactly like `compensation_note`. Only
`payroll_admin` may read or write it:

```
GET /api/employees/{id}/pay-rate     # decrypt + return the rate (audited: read_pay_rate)
PUT /api/employees/{id}/pay-rate     # set the rate, 0 < rate ≤ 10000, max 2 decimals (audited: write_pay_rate)
```

Every read and write is recorded in `audit_event`. A non-payroll role is refused
with a 403, and a nonpositive, absurd, or sub-cent rate with a 422 — the envelope
is the real guard against a fat-fingered rate.

**California overtime (California-only).** A pure engine (`src/usali/overtime.py`,
no database) classifies each business date's approved hours into regular (1×),
overtime (1.5×), and double-time (2×) under California rules, computed per
workweek: daily hours over **8** are OT and over **12** are DT; regular hours over
**40** in a workweek are OT (the **no-pyramiding** rule — hours already counted as
daily OT/DT are not recounted toward the 40); the **7th consecutive day** worked in
a workweek pays its first 8 hours at OT and the rest at DT; **exempt** employees
(`position.flsa_exempt`) are excluded from overtime entirely. The workweek is
anchored to the same Monday as the payroll period, so a 14-day timecard is exactly
two workweeks. These are California rules and must not be read as
jurisdiction-agnostic. Meal-break premiums are **flagged by B2 but not priced** —
pricing, gross-to-net, and tax withholding are all Pillar C.

**Promote on approval.** Approving a timecard reads the employee's encrypted rate
**server-side**, runs the OT engine, and writes **department-level aggregates** to
`usali_labor_fact` (one row per worked business date). Individual pay rates never
leave the server and are never stored or logged — only summed cost — which is the
whole point of the Payroll-Admin segregation. An employee with no rate yet still
promotes *hours* with `est_cost = 0` (the hours side stays complete; cost fills in
once a rate is set). The same holds for **exempt** staff regardless of any rate on
file: the estimate prices *hourly* labor, so a salaried employee's hours promote
with `est_cost = 0` — salaries are Pillar C's gross-to-net, not hours × rate.
Promote is **idempotent** — it deletes a timecard's prior
labor facts and re-inserts, so re-running never double-counts. A CLI backfill
promotes every approved card (for cards approved before B3 shipped, or to
re-promote after a rate correction):

```bash
.venv/bin/usali promote-labor     # → "Promoted N labor facts across M approved timecards"
```

**On the statement.** The SOS gains two labor sections, unioned in and kept
deliberately **outside** the operating-revenue reconciliation (labor is expense,
not revenue, so it must not perturb that balance):

- **Schedule 14 — Payroll Related Expenses:** per-department estimated cost. It is
  an **ESTIMATE** — meal premiums are unpriced and Pillar C's real gross-to-net
  supersedes it — and is labeled as such wherever it surfaces.
- **Schedule 15 — Payroll / FTE Reporting:** total hours, overtime hours, and an
  estimated FTE (a 40-hour-workweek equivalent prorated to the report window).

Both appear on `GET /api/sos` and render on the portal's statement page under an
"estimate" badge. All money is carried as `Decimal` to the column scale, never
float.

## Payroll PII vault (Pillar C1)

C1 stands up the **sealed PII vault**: an employee's SSN, bank account, bank
routing, and tax elections are encrypted **in the browser** to a key the server
cannot open, so the most sensitive fields are never plaintext at rest — and never
readable by the server at all. This is a different regime from B3's encrypted
`pay_rate`, and the split is deliberate:

- **Store-and-forward PII** (SSN, bank, tax) is **sealed client-side with HPKE**
  and only forwarded to a payroll provider (C2). The server holds an opaque
  envelope, so it needs no ability to read these — and doesn't have one.
- **Compute-on data** (`pay_rate`, `compensation_note`) stays on the existing
  server-side symmetric `EncryptedString` (AES-256-GCM). The server *computes* on
  `pay_rate` — it runs the overtime engine and sums labor cost (B3) — so it cannot
  be sealed to a key the server can't open.

**The `Opener` seam.** The HPKE recipient **private** key lives behind an `Opener`
injected into `create_app` like every other seam. `SoftwareOpener` holds the key
in-process and is **dev/test only**; `HsmOpener` — a deploy-time drop-in against
the same Protocol that keeps the private key in an HSM/KMS — is **deliberately not
built in C1** (exactly as `S3PhotoStore` was left for B1). The suite is
`DHKEM(P-256, HKDF-SHA256) + HKDF-SHA256 + AES-256-GCM`, using `pyhpke` on the
server and `@hpke/core` in the browser (same author, built to interoperate).

**Fail-fast in production.** `USALI_ENV=prod` refuses to run under either dev
default. It refuses the in-process `SoftwareOpener` (prod requires an
HSM-backed `Opener`, which C1 does not ship), **and** it refuses the committed
dev-default `field_encryption_key` — the `Settings` validator raises at
construction, so nothing can start under the repo's symmetric key. In dev/test,
both defaults are fine.

**Blind overwrite — there is no read path.** The vault is write/replace only:

```
GET /api/payroll/pii-public-key                    # the pinned recipient public key
PUT /api/payroll/employees/{id}/profile            # store sealed envelopes verbatim
GET /api/payroll/employees/{id}/profile            # "on file / not on file" flags ONLY
```

`GET .../profile` returns booleans (`ssn_on_file`, `bank_account_on_file`, …) plus
the non-sensitive `account_type` — **never** a sealed value or plaintext. Correcting
a typo is a full re-entry + re-seal, not an edit; there is no endpoint that hands
back a stored envelope. The write validates envelope **structure** only (it never
opens a ciphertext, which would put plaintext server-side outside C2's send path);
a malformed envelope is a 422 that stores nothing (all-or-nothing across fields).
The public-key route is reachable by any operator (so the client can fetch and pin
the key); every profile and pay-schedule route is **Payroll-Admin-gated and
audited** (`write_payroll_pii`, `write_pay_schedule`).

**Self-describing versioned envelope.** A sealed field is stored as
`{version, suite, key_id, enc, ct}` (`enc` is the HPKE encapsulated key, a
65-byte SEC1 uncompressed P-256 point). The envelope is self-describing so the
wire format survives key rotation and future suites. The client seals with
`aad = "<employee_id>:<field>"`, binding each ciphertext to its exact field and
employee — a sealed blob cannot be moved between fields or people. The AAD is
**not** stored; the server reconstructs it deterministically at open time (C2).
The public key is served on the authenticated channel so the client can pin it —
a substituted key would silently defeat the sealing. A committed cross-library
fixture (`tests/fixtures/hpke_interop.json`) pins the browser↔server contract:
`@hpke/core` seals it and `pyhpke` must open it (and vice-versa), so a future
upgrade on either side that changes the suite, `info`, AAD, or point encoding
fails a test instead of silently failing to decrypt a real SSN.

**Pay schedule.** A property's pay cadence lives in `pay_schedule`
(`frequency`, `anchor`, `check_date_offset_days`), Payroll-Admin-gated:

```
PUT /api/payroll/properties/{id}/pay-schedule
GET /api/payroll/properties/{id}/pay-schedule
```

**Scope note.** C1 ships the secured foundation — the crypto seam, the vault, the
write API, and the client-side sealing UI. **C2** (below) adds the provider port,
adapters, and the pay-run round-trip (where the sealed envelopes are finally
opened and forwarded); **C3** adds variance reporting. The `HsmOpener` arrives
with the production deployment.

## Payroll runs (Pillar C2)

C2 proves the adapter-layer thesis: an approved pay period flows through a
provider-agnostic `PayrollProvider` port to a Gusto-shaped **or** ADP-shaped
payroll provider — selected by configuration alone — and the provider's actual
gross-to-net lands back as department-aggregated USALI facts.

**One port, two adapters, two mocks.** The whole app speaks one canonical model
(`src/usali/payroll_provider.py`; deliberately narrow — sync an employee, submit
a pay run, fetch its result; money is `Decimal` dollars everywhere). Two real
httpx adapters map it onto two **deliberately different** wire shapes, each with
a local mock server:

```bash
.venv/bin/usali gusto-mock   # 127.0.0.1:9300 — snake_case, dollars as decimal strings, static bearer
.venv/bin/usali adp-mock     # 127.0.0.1:9301 — camelCase envelopes, integer cents, OAuth client-credentials
```

The mocks apply different tax rates (15%/10% employee/employer vs 18%/11%) so a
symmetric adapter bug cannot accidentally pass both, and one contract test suite
runs parametrized over both adapters with zero per-provider test bodies — that
suite is the swappability proof. `USALI_PAYROLL_PROVIDER=gusto|adp` is the
**only** switch; defaults target the local mocks, and
`USALI_GUSTO_BASE_URL`/`_API_TOKEN`/`_COMPANY_ID` or
`USALI_ADP_BASE_URL`/`_CLIENT_ID`/`_CLIENT_SECRET` point an adapter at a real
endpoint — the same config-only discipline as the QBO push. Any other provider
value refuses to build an adapter at all: the first pay-run request raises
rather than silently falling back to a default.

**Preflight names every blocker — before any network call.** Executing a run
first assembles the period from B2's approved hours through B3's California
overtime engine, then refuses with a 422 listing **every** blocker by employee
and missing field: a missing pay rate, missing sealed PII (SSN / bank account /
bank routing), an unapproved timecard, or no pay schedule for the property
(`check_date` is `period_end + check_date_offset_days`, so C1's `pay_schedule`
is load-bearing). Nothing is created and no provider is contacted until the
data is clean — a Payroll Admin fixes data problems, not failed submits.

**The vault opens only at send.** Employee sync is the first production use of
C1's `Opener.open`: each sealed field is decrypted with its reconstructed
`"<employee_id>:<field>"` AAD and handed straight to the provider over the
adapter's HTTPS connection (decrypt-and-send; the local mocks are loopback
HTTP). Plaintext exists only inside that call — never stored, never logged,
never echoed into an error message (`ProviderError` carries status + the
provider's response detail only, and the contract suite pins that). Resealing
to a provider key instead of decrypting is reserved for providers whose
`capabilities()` report field-level encryption — neither mock (true to the real
Gusto/ADP public APIs) supports it, so C2 exercises the decrypt-and-send path.

**Actuals land period-grain.** Providers return per-period totals, so
`usali_actual_labor_fact` carries `period_start`/`period_end` plus department
**aggregates** (hours, gross, employer burden) — the provider's truth, no
fabricated daily spread. Per-employee gross-to-net lines are stored in
`pay_run_line` encrypted at rest (the compute-on `EncryptedString` regime, like
`pay_rate`), readable only by C3's Payroll-Admin detail endpoint — C2's API
responses never carry per-employee money. One run per property + period:
re-POSTing the same period is a 409 unless the existing run is `failed`, in
which case the failed run (and its lines) is replaced — there is no separate
retry endpoint. Fetching results is an explicit step with polling semantics
(the mocks process synchronously, but the port models the async reality; a
still-processing run reports its current status with zero lines), and a
re-fetch replaces rather than double-counts.

**The API.** All routes are `payroll_admin`-gated and audited
(`submit_pay_run`, `pay_run_failed`, `fetch_pay_run_results`):

```
POST /api/payroll/runs                      # {property, in_period}: preflight → sync → submit (201)
GET  /api/payroll/runs?property=HISJ        # list runs (period, status, provider, check date)
GET  /api/payroll/runs/{id}                 # department aggregates only — NO per-employee money
POST /api/payroll/runs/{id}/fetch-results   # pull results → encrypted lines + aggregates
```

Preflight blockers are a 422 whose detail names each one; a duplicate period is
a 409; a provider-failed submit is a 502 (the failed run row is persisted so a
re-POST replaces it). The portal gains a **Pay runs** page (`/payroll`, nav
link shown to `payroll_admin`; enforcement server-side as always): create a run
for a property + period — a 422 renders the blocker list verbatim, which is the
feature — then watch it through submitted → Fetch results → processed with the
department aggregates. The Playwright e2e drives the full stack against the
mock Gusto on 9300: browser → API → vault open → provider → actual facts.

**Honesty note — what go-live still requires.** The adapters encode Gusto's and
ADP's documented public API *styles*; they are exercised against local mocks,
not certified against the vendors. Going live means verifying each adapter
against the provider's real sandbox, obtaining real credentials, and executing
a BAA/DPA with the provider — an endpoints-and-credentials exercise, not a code
restructure, which is the point of the port. **C3** (below) adds
estimate-vs-actual variance on the Summary Operating Statement (B3's Schedule 14
estimate against these actuals).

## Variance reporting & pay-run detail (Pillar C3)

C3 closes Pillar C: the Summary Operating Statement now shows **estimate vs
actual vs variance** for labor, and a Payroll Admin can open the audited
per-employee pay-run detail. The loop is complete — B2's approved hours are
priced into an estimate (B3), a provider prices the real gross-to-net (C2), and
the statement holds the two against each other (C3).

**The variance block.** When at least one **processed** pay run's period
intersects the SOS window, the statement gains **"Schedule 14 — Payroll: actual
vs estimate"**: per-department Est / Actual / Variance / Employer burden /
Hours, plus a totals row. Like B3's labor sections it lives deliberately
**outside** the operating-revenue reconciliation. Design calls, all deliberate:

- **Variance compares estimate to actual GROSS.** B3's estimate models gross
  wages only (hours × rate with OT multipliers), so employer burden — which has
  no estimate counterpart — is shown as its own actual-only column and is
  **excluded from the variance math**.
- **Full-period semantics, labeled.** One block sums over ALL processed pay
  periods intersecting the window (disjoint by construction), and the covered
  periods are listed on the block (`Pay periods: 2026-07-06..2026-07-19, …`).
  A pay period can extend past the SOS window, so the block covers the runs'
  *full* periods and **will not visually tie** to the window-clipped estimate
  section above it — the period labels are the honest explanation. Per-period
  drill-down is the pay-run detail page.
- **An alert is a statement flag, not a notification pipeline.** A line (and
  the totals row) carries an `alert` badge when the estimate is positive and
  |variance| ≥ `USALI_LABOR_VARIANCE_ALERT_PCT` percent of it (default **10**),
  or when there is actual cost with **no estimate baseline** at all (no
  baseline is itself the alarm). No emails or webhooks — that is later
  infrastructure.
- **Suppression extends to the actual side, including burden.** Distinct
  employees are counted from the **union** of both sides (estimate: labor fact
  → timecard → employee; actual: pay-run lines). A department with fewer than
  two people has est/actual/**burden**/variance all hidden (hours still shown)
  — employer burden is ≈ a fixed percentage of gross, so publishing it would
  re-derive the solo rate. Totals exclude suppressed departments on **both**
  sides (complementary — otherwise total-minus-lines recovers the hidden
  values), and suppressed lines never carry an alert (an alert's direction is
  itself a signal).

The block rides `GET /api/sos` as one nullable `labor_variance` object — `null`
whenever no processed run touches the window — and the statement page renders
nothing in that case.

**The audited per-employee detail.** `GET /api/payroll/runs/{id}/lines` is the
**only** per-employee money read in the system: name, hours, gross, employee
taxes, employer taxes, net — the decrypted `pay_run_line` values (money as
decimal strings). It is `payroll_admin`-only and **audited on every read**
(`read_pay_run_lines`, one row per request — the compensation-gate convention).
Denials (403/404) are *not* audited, matching the punch-photo gate: the trail
records actual egress, not attempts. On a submitted-not-yet-fetched run the
money fields return their `"0"` placeholders as-is — the run `status` tells
that story. In the portal, the Pay runs detail card shows the lines only after
an explicit **"Show employee detail"** click (and re-hides them when you switch
runs), so every audit row corresponds to a human who asked to see pay — never
a navigation side effect.

**Pillar C is complete.** Estimate (B3) → provider actuals (C2) → variance on
the statement (C3). Go-live still requires what the C2 honesty note says:
verifying each adapter against the provider's real sandbox, real credentials,
and a BAA/DPA with the provider.

## Scheduling (Pillar D)

D1 opens Pillar D: a GM assembles next week's schedule from **shift templates**
and sees, live as they assign, projected regular/OT hours per employee and
projected cost per department — with the warnings surfaced **before** the week
starts instead of on a timecard after the money is owed. The portal gains a
**Schedule** page (`/schedule`, nav link shown to `org_admin`/`property_gm`;
enforcement server-side as always), property-confined exactly like timecard
approval: scope comes from the `role_assignment` table, so an
accountant-plus-GM cannot schedule a property they do not run.

**Weeks live on the payroll Monday grid.** A schedule week's `week_start` must
be a Monday on the same grid as the payroll workweek (derived from
`USALI_PAYROLL_PERIOD_ANCHOR`) — an off-grid start is a 422, because the whole
projection promise rests on the schedule week *being* the workweek: weekly-40
and 7th-consecutive-day projection are then exact within the week. The server
is authoritative; the week picker accepts any date and renders the 422 detail
verbatim rather than duplicating the anchor client-side.

**The projection is the existing California OT engine, fed scheduled hours.**
A pure engine (`src/usali/schedule_projection.py`, no database) converts each
shift into projected worked hours — the shift span minus an assumed **30-minute
unpaid meal on shifts over 6 hours** (config
`USALI_SCHEDULE_MEAL_THRESHOLD_HOURS` / `_MEAL_DEDUCTION_MINUTES`; exactly 6h
does not deduct), mirroring how such a day actually punches; the assumption is
stated in the panel footer — and feeds B3's `compute_overtime` per employee.
**Warnings flag, never block** (a 9-hour scheduled day may be deliberate; the
GM decides):

- `scheduled_overtime` — a day's projected OT/DT hours, attributed to the day;
- `clopening` — rest between consecutive shifts under 10h (config
  `USALI_SCHEDULE_MIN_REST_HOURS`), attributed to the second shift's date.
  The rule is **night-spanning**: it warns only when the pair spans a night
  (the shifts sit on different business dates, or the rest itself crosses a
  calendar midnight) — a same-day split shift (breakfast + dinner service with
  a short afternoon gap) is normal hotel practice and does not warn;
- `seventh_day` — an employee scheduled on all 7 dates of the week (the OT
  engine's own trigger, so the warning and the priced OT agree).

The only hard 422s are **true data errors**: a `business_date` outside the
week, an end-before-start shift that doesn't declare `crosses_midnight`, an
overlapping shift for the same employee (including midnight-crossing overlaps),
an employee terminated on that date, and a cross-property
employee/department/template (a 422 that never becomes an existence oracle over
another property's ids). Duplicate week or template name is a 409.

**The money discipline, carried to schedules.** Per-employee figures are
**HOURS ONLY** — a per-employee projected cost would hand any scheduler
`rate = cost ÷ hours` for the encrypted, Payroll-Admin-gated pay rate, so no
rate and no per-employee money value appears anywhere in the response (pinned
by a response-text test) or on the page. Cost appears solely as **department
aggregates**, priced server-side from the decrypted rates (regular 1×, OT
1.5×, DT 2× from each employee's projected split), with B3's suppression:
a department with **fewer than two distinct assigned employees** has
`est_cost: null` (hours still shown), and `total_est_cost` sums only the
non-suppressed departments — complementary, so total-minus-lines cannot
re-derive a hidden value. An employee whose day spans departments has that
day's cost attributed **proportionally** by each department's share of the
day's hours — never silently dumped on one department. **Exempt** staff
project hours but no OT warnings and no cost (a salary is not a wage), and
rate-less employees price nothing; both fold into an `unpriced_hours` counter
the panel footnotes. **Open shifts** (`employee_id` null) count toward
department scheduled hours but toward no one's OT and no cost.

```
POST   /api/schedule/templates                    # create a template (409 on duplicate name)
GET    /api/schedule/templates?property=HISJ      # list a property's templates
DELETE /api/schedule/templates/{id}               # 204; 409 while any shift references it
POST   /api/schedule/weeks                        # {property, week_start}: draft, version 0
GET    /api/schedule/weeks?property=&week_start=  # the week + its shifts (404 = none yet)
POST   /api/schedule/weeks/{id}/shifts            # add a shift (employee_id null = OPEN)
PUT    /api/schedule/shifts/{id}                  # edit/reassign — full revalidation
DELETE /api/schedule/shifts/{id}                  # 204
GET    /api/schedule/weeks/{id}/projection        # hours, warnings, dept cost aggregates
POST   /api/schedule/weeks/{id}/publish           # bump version, stamp, audit
```

Templates are **provenance, not a straitjacket**: picking one pre-fills a
shift's times/department (all editable), a shift records its `template_id`,
editing a template never rewrites existing shifts, and a referenced template
refuses deletion with a 409. The projection panel is **live** — every shift
mutation invalidates the projection query, so the numbers move as the GM
assigns.

**Publish is republish, not hard-lock.** Publishing flips `draft → published`,
bumps `version` (first publish is v1), stamps who/when, and writes a
`publish_schedule` audit row. Editing a published week stays allowed — it is
simply stale until published again, which bumps the version and audits again;
the wall grid (and the kiosk my-week view, below) always follow the latest
published version.
**Print is CSS only**: "Print week" is `window.print()` plus Tailwind `print:`
variants that hide everything but the week grid and its version badge — and
since no per-employee money exists anywhere on the page, the print view cannot
leak it either.

### D2: demand targets, forecast hints, and the kiosk my-week window

D2 makes the schedule answerable to demand and finally visible to the people
on it: **labor standards** convert a GM-entered **occupancy forecast** into
target hours per department per day, shown against scheduled hours in the
builder — and an employee taps their name on the kiosk and sees their week
from the latest **published** schedule.

```
PUT    /api/schedule/standards                            # upsert BY department
GET    /api/schedule/standards?property=HISJ              # list; DELETE /standards/{id} → 204
PUT    /api/schedule/forecast                             # {property, days:[{business_date, occupied_rooms}]}
GET    /api/schedule/forecast?property=&week_start=       # 7 days: entered value + history hints
GET    /api/schedule/weeks/{id}/targets                   # target vs scheduled HOURS per dept per day
PUT    /api/schedule/employees/{id}/availability-note     # scheduler-gated, audited
GET    /api/kiosk/my-week?employee_id=&week_start=        # X-Kiosk-Token — published shifts only
```

**Labor standards: two bases, one standard per department.** A standard says
how a department's target hours derive from demand: `fixed_hours_per_day`
(a constant daily target — e.g. front desk needs 16h whatever the house count)
or `minutes_per_occupied_room` (multiplies the GM's forecast — 30 min/room ×
40 rooms = 20h). `PUT` upserts **by department** (the model's unique
constraint); re-PUT replaces basis and value. An unknown basis is a 422
straight from the schema, value must be > 0, and an unknown department and a
cross-property department share one 422 — no existence oracle over another
property's ids.

**The forecast is the GM's number; history hints inform, never dictate.** The
GM enters expected occupied rooms per day (`PUT` upserts per property-day and
writes **one** `write_occupancy_forecast` audit row per request — the
plan-changing write is "the GM saved a forecast", not each of its days;
negative rooms is a 422). The week `GET` decorates each day with two hints
computed from our **own promoted facts** — `UsaliStatisticFact` with
`metric_code ROOMS_OCCUPIED`, `DAY` period, current-year:
`hint_same_day_last_week` (the value at `business_date − 7d`) and
`hint_trailing_avg` (the mean of the latest ≤ 7 per-date values strictly
before `week_start`, rounded half-up to a whole room). Both dedupe per date —
a re-promoted date resolves to the **latest** fact. Where facts are absent
the hints are **null, never 0**, and a property with zero facts gets all-null
hints, not a 500. In the builder the hints render muted beside each input
("last wk: 38 · avg: 41") and **never auto-fill** — the GM's number is the
forecast.

**Targets are HOURS-only — the rate-leak rationale, stated.** The targets
endpoint returns, per department per day, `{target_hours | null,
scheduled_hours}` plus week totals — and **no money anywhere** (pinned by a
response-text test). This is deliberate, not an omission: a target *cost*
would be an average department rate × hours, and for a small priced
population that average **is** an individual's rate — exactly the derivation
hole the C3 and D1 adversarial reviews each caught once. Scheduled cost with
the priced-population suppression already exists in the projection panel;
D2 adds **no new money surface**. If a cost target is ever wanted, it must
define its own safe rate source (a GM-entered budget rate, say) — never a
derivation from real rates.

**Null is not zero.** A `minutes_per_occupied_room` day with **no forecast**
has `target_hours: null` — absence of a forecast is not zero demand, and a
silent 0 target would paint any schedule over-target. The week's
`target_total` sums the non-null days, and a `days_without_forecast` counter
tells the truth about the gap (the builder footnotes it: "per-room targets
need one"). A department with shifts but no standard shows null targets all
week; a department with a standard but no shifts appears too — the GM must
see an unstaffed target, not just a target-less staffing. `scheduled_hours`
come from the **same meal-adjusted `shift_hours`** the projection uses, so
target-vs-scheduled compares like with like — and **open shifts are
included**: they are planned coverage. The builder renders each cell as
`scheduled/target` with a warn tone when scheduled exceeds target and a
muted "—" where no target exists.

**Kiosk my-week: published-only, own shifts, device-confined.** The my-week
endpoint rides B1's device token (`X-Kiosk-Token`); both params are required —
the kiosk UI computes the grid Monday client-side, which keeps the endpoint
dumb. It returns the tapped employee's **own shifts only** from the
**latest published** schedule (department names, "HH:MM" times, a
`crosses_midnight` flag the UI renders as "(+1d)"): a draft week is invisible
by design and comes back `published: false`, which the kiosk shows as
"No published schedule yet". An unknown employee id, an id from **another
property**, and a terminated employee all share one indistinguishable 403
(the punch endpoint's oracle collapse — the device never learns which rule
refused it); a revoked or missing token is refused exactly as for punching.
Every successful read — including a `published: false` answer — writes a
`kiosk_my_week` audit row with the device as the actor (`kiosk:<device_id>`;
there is no user on the kiosk), so schedule reads from the lobby are
traceable; denials are unaudited, per the egress-only rule. The response
carries no money and no other employee's name — both pinned by
response-text tests. On the kiosk, "My week"
sits beside the punch buttons after the name tap, with a back button to
punching.

**Availability notes: operational, never money, never medical.** Each
employee gains an optional `availability_note` (≤ 300 chars; longer is a
422) — a scheduler-maintained free-text aid ("can't work Tuesdays"), not
PII. The `PUT` is scheduler-gated, confined on the **employee's** property
(the id the caller picked asserts nothing), and audited
(`write_availability_note`) like every plan-changing write. The asymmetry is
deliberate: writes are scheduler-gated and audited, while reads ride the
general operator roster without extra gating or audit — the note is designed
to be operator-visible. The note travels
on the workforce roster the builder already loads and renders muted beside
the employee selects in the add-shift/reassign flows, with an inline edit
labeled "scheduling note (visible to managers)". It never appears on the
kiosk.

### D3: adherence, current-week merge, and cross-week clopening

D3 closes the loop: after (and during) a published week the GM sees how
reality tracked the plan, the current week's projection stops pretending the
past is still a plan, and clopening finally sees across week boundaries.

```
GET  /api/schedule/weeks/{id}/adherence?as_of=    # scheduled vs punched + exceptions
GET  /api/schedule/weeks/{id}/projection?as_of=   # now merges punches-to-date
```

**Adherence: scheduled vs punched, from the merged timeline.** The adherence
endpoint (`require_scheduler`, property-confined like every schedule read)
returns scheduled vs punched hours per department per day for the **elapsed**
part of the week — days strictly before `as_of` (optional; default = server
today): a future day has nothing to adhere to, and today is mid-shift. A
fully-future week returns empty days and exceptions, not an error. Punched
hours come from **B2's merged timeline** (`compute_timecard`: immutable
punches + additive audited adjustments), never raw punches — so **a
manager-corrected day is clean by design**: a punch missing its clock-out
that was fixed with a timecard adjustment shows the corrected hours and no
exception. Corrections are honored, not re-litigated.

**Three exception rules**, per employee per elapsed day (hours aggregated
across that day's shifts):

- `no_show` — an assigned shift that day, zero worked minutes;
- `unscheduled_punch` — worked minutes with no shift that day;
- `deviation` — both sides > 0 and |punched − scheduled| ≥
  `USALI_SCHEDULE_ADHERENCE_DEVIATION_MINUTES` (default 60 — a 30-minute-short
  day is normal life, not an exception).

**Open shifts** count toward department scheduled hours but can never
no-show — nobody was assigned. Department attribution: scheduled hours go to
the **shift's** department; punched hours follow the day's scheduled
department split for scheduled employees (a day spanning departments splits
its punched hours **proportionally** to the scheduled split — single-
department days, the norm, are exact); **unscheduled punches attribute to
the employee's home department** (`employee.department_id`, "Unassigned"
when null). **Hours only, everywhere** — the money rule holds: adherence
adds no money surface, and a response-text test pins it. In the portal the
panel appears on **published weeks only** (a draft has nothing to adhere
to): a `punched/scheduled` dept × day table (amber when punched falls short
of scheduled — the per-employee threshold verdict lives in the exceptions
list, which is grouped by code: "Tue 2026-07-07 — no show: Hank H (scheduled
7.50)"), plus a Refresh button, because punches arrive server-side
independently of anything the page mutates.

**Current-week merge: the projection meets reality.** The projection endpoint
gains `as_of` (optional; default = server today). When it falls inside the
week, days strictly before `as_of` feed each employee's **punched** hours —
the same merged timeline — into the OT math, and the response carries
`merged_through` (the last merged day), which the panel renders as "Includes
actual hours through {date}". **The zero-punch rule:** a merged day replaces
scheduled hours **only where the employee has worked minutes that day** — an
elapsed day with no punches keeps its scheduled hours, because mid-week
"no punches" means *no data yet* (kiosk down, card not assembled), not
*worked zero*; zeroing every elapsed day would crater the projection for
anyone whose punches lag. Adherence owns no-show truth; the merge is about
OT realism. Projected OT now reflects what actually happened — a punched
9.5-hour Monday flows into the daily-OT and weekly-40 math, a
worked-unscheduled day counts toward the weekly OT (its cost lands nowhere
rather than being dumped on a guessed department), and cost aggregates price
the merged day rows with B3's suppression rules unchanged (re-pinned by a
regression test). Clopening and `seventh_day` **warnings stay plan-derived**
— rest planning is about the plan — though the OT pricing honestly prices a
really-worked 7th day, since merged hours feed the same overtime engine.

**Cross-week clopening.** The projection always loads the adjacent weeks'
schedules (`week_start ± 7d`, **any status** — draft or published, the
latest saved state is the honest input) as **context shifts**: they join each
employee's rest-pairing sequence but contribute nothing to hours, OT,
department hours, or cost, and a context shift alone never creates an
employee row. A boundary warning is emitted only when the **second** shift of
a sub-floor night-spanning pair is in-week — a Sunday-night close followed by
Monday's open warns on the Monday shift in *this* week's projection; the
mirror-image warning belongs to the other week's projection. No double
counting, no blind spot.

**Pillar D is complete** — and with it the platform roadmap, Pillars A–D.
What remains is pilot go-live, not features: the `HsmOpener` deploy-time
drop-in for C1's sealed-envelope seam (a real HSM/KMS behind the same
`Opener` Protocol), verifying each payroll adapter against the provider's
real sandbox with real credentials and a BAA/DPA (the C2 honesty note), and
confirming the hotel's real night-audit time for the business-date cutoff
(`USALI_PUNCH_BUSINESS_DAY_CUTOFF_HOUR`).

## CRM demand feed (Pillar J)

The scheduler learns about demand before it arrives: a **read-only,
inbound, forward-looking** feed from a hotel sales CRM — group blocks,
booking pace, event covers — surfaced beside the GM's forecast and as
per-day chips in the schedule builder. It is not a CRM: no writes, no
contacts, no money.

```
POST   /api/crm/refresh          # {property} — one audited pull, today..+90d
GET    /api/crm/demand?property=&start=&end=   # latest snapshot per stay-date
```

**One provider, config-selected.** `USALI_CRM_PROVIDER=delphi|tripleseat`
(empty = off — the pull refuses with a loud 503 naming the switch; the read
surface degrades to "no demand data"). Two adapters against deliberately
different wire shapes prove the port: Amadeus-Delphi-style (paged
PascalCase, rooms-on-the-books + room blocks) and Tripleseat-style
(snake_case events with covers). Local mocks run on :9400/:9401 (`usali
delphi-mock` / `usali tripleseat-mock`, in dev.sh beside the payroll
mocks); the real integrations are a base-URL + credential change.

**The importer is an allowlist; everything else is dropped unread.**
Adapters read exactly the mapped fields; every other wire field — contacts
above all, and every revenue/rate field — is dropped **without its value
ever being bound to a variable**, counted by field name in the pull report.
A sensitive-patterns guard refuses any allowlist that names a
contact/revenue field, so the control cannot be widened quietly. Block and
event **labels** (a wedding's name is somebody's name) are bounded operator
working data: they render on the scheduler page only — never in logs,
never on the kiosk (the my-week payload's key set is pinned by test).

**Snapshots are append-only; pace is the point.** Each pull writes a new
batch of per-stay-date snapshots (property lives only on the batch). A
re-pull never updates in place — booking pace ("140 on the books today vs
90 last pull") is a comparison of snapshots, so the history is the data.
Readers take the newest **covering** voice per stay-date: a batch speaks
for every date inside its declared horizon, including by silence — a
newer pull that covered a date and stated nothing means the demand is
gone (the block cancelled), not that the old figure is still current.
`demand_pace` pairs each current voice with the previous covering one.

**Demand informs the forecast; it never becomes it.** A dimension the
provider does not speak is **absent, never 0** (the D2 null rule on the
wire and in the UI), figures never move a target or fill an input, and
each pull is an explicit audited act (`crm_refresh` pointing at the batch;
refusals audit too). A property pulls only if the registry declares its
`crm_ref` in `mapping/properties.yaml` — silence refuses by name. Cadence
is a deployment concern: cron hits the endpoint; the demo pulls once at
seed (a fat Thursday group block is the talking point).

## Reports & exports

Once data is loaded, three read-only commands report against the fact tables:

```bash
# Summary Operating Statement (revenue side) for one property and date...
.venv/bin/usali report --property HISJ --date 2026-07-07

# ...or a date range, as json or csv, optionally to a file
.venv/bin/usali report --property HISJ --from 2026-07-01 --to 2026-07-31 \
    --format json --out sos-july.json

# Mapping coverage & confidence: dictionary breakdowns, staged-vs-mapped codes,
# and the needs-review worklist, per PMS source
.venv/bin/usali coverage
.venv/bin/usali coverage --format json --out coverage.json

# Flat fact-table exports for ERP/BI hand-off (financial, statistics, or segments)
.venv/bin/usali export --table financial --from 2026-07-01 --to 2026-07-31 --out facts.csv
.venv/bin/usali export --table statistics --from 2026-07-07 --to 2026-07-07 \
    --property SSSJ --format json
```

`report` supports `--format text|json|csv`; `coverage` supports `text|json`; `export`
supports `csv|json` and always emits exact stringified values (Decimals as plain
strings, never floats).

## QBO push & CPA pack

P8 posts normalized USALI data into QuickBooks Online as one balanced journal
entry per business date (idempotent — re-pushing an unchanged day is a no-op),
and renders the CPA monthly pack (sales, taxes, A/R). The portal gains matching
`/reports` and `/qbo` pages.

```bash
# Run the mock QuickBooks Online server (in-memory; the default push target;
# runs in the foreground — use a second terminal for the commands below)
.venv/bin/usali qbo-mock                       # 127.0.0.1:9200

# Preview the journal entries without posting anything...
.venv/bin/usali qbo-push --property HISJ --month 2026-07 --dry-run

# ...then push for real: one JE per date, outcomes recorded in the push ledger
.venv/bin/usali qbo-push --property HISJ --date 2026-07-07
.venv/bin/usali qbo-push --property HISJ --month 2026-07

# Push ledger: what was pushed, failed, or went stale (facts changed after push)
.venv/bin/usali qbo-status --property HISJ --month 2026-07

# CPA monthly pack: sales, tax, and A/R reports (text/json to stdout or a file;
# csv writes sales_report.csv, tax_report.csv, ar_report.csv into --out DIR)
.venv/bin/usali cpa-pack --property HISJ --month 2026-07
.venv/bin/usali cpa-pack --property HISJ --month 2026-07 --format csv --out packs/july
```

Configuration (env vars, all prefixed `USALI_`) — defaults target the local
mock, so pointing at real Intuit is a config-only change:

| Env var | Default (mock) | Real QBO |
|---|---|---|
| `USALI_QBO_BASE_URL` | `http://127.0.0.1:9200` | Intuit API base URL |
| `USALI_QBO_CLIENT_ID` | `mock` | your app's OAuth2 client id |
| `USALI_QBO_CLIENT_SECRET` | `mock` | your app's OAuth2 client secret |
| `USALI_QBO_REALM_ID` | `mock` | company realm id |
| `USALI_QBO_REFRESH_TOKEN` | `mock` | refresh token from Intuit's consent flow |

**Token rotation:** the mock rotates refresh tokens like real Intuit and keeps
state in memory — if a later, separate `qbo-push` invocation fails with
`invalid_grant`, restart the mock (already-pushed dates never need a token).
Real deployments must persist the rotated token — a known post-pilot task.

**Placeholder chart of accounts:** the GL codes in `mapping/opera.yaml` /
`mapping/autoclerk.yaml` (`gl_account_code`) and the account list the mock
serves (`mapping/qbo_accounts.yaml`) are a curated **placeholder** CoA. Before
pushing to a real QBO company, the CPA replaces `mapping/qbo_accounts.yaml`
with the company's real chart, updates the `gl_account_code` values in the
mapping YAMLs to match, and reloads them with `usali seed-mappings` — the same
review discipline as the transaction-code dictionary.

## Multi-tenancy (Pillar L)

The single-org trust boundary becomes a **security** boundary between
untrusted strangers — the load-bearing prerequisite of self-service
onboarding. `org_id` lands on every tenant-owned table; the same
fail-closed, loud-over-silent posture as every pillar, now between tenants.
Design: `docs/design/2026-08-01-d1-tenant-isolation-design.md` (isolation)
and `d2-keycloak-tenancy-design.md` (one realm, org-scoped authority).

**Two independent walls, either alone sufficient to stop a leak.** The
*application wall* is a session hook that adds an `org_id = current` criterion
to every ORM SELECT that touches an org-scoped table — a handler that forgets
to filter still gets filtered, and a session with NO org context refuses
loudly (never a silently unscoped result). The *database wall* is per-table
RLS with `FORCE ROW LEVEL SECURITY`, keyed on a transaction-local session
variable: the serving revision connects as a NON-owner role (`usali_app`, no
`BYPASSRLS`), and an unset variable yields **zero rows**, not all rows. One
predicate feeds both walls, so they can disagree only by one being dropped —
and each drop is pinned by a test the other wall cannot save.

**Org resolution: membership claim → validated active org → the session
variable.** The realm emits a KC 26 organization-membership claim; `auth.py`
parses it into the principal's memberships. The SPA sends `X-Active-Org` (an
alias); the server validates active ∈ memberships, resolves the alias to an
`org_id` through the `organization` table (the DB is the source of truth —
**nothing numeric from the token is ever an org_id**), and only the validated
org binds the transaction. Refusals name nothing: a non-member or unknown
alias → 403 (no existence oracle); a multi-org token without the header → 400
(ambiguity refuses rather than guesses); a single-org token defaults.

**Role authority is org-scoped DB grants, not token roles.** Realm roles are
realm-global — an org_admin of tenant A carries the role in a token used while
active in tenant B — so they gate only the coarse operator/employee door.
Effective authority for a request is the `role_assignment` grants VISIBLE
UNDER THE ACTIVE ORG'S RLS (`property_id` NULL = an org-wide grant). The
marquee: **org_admin of A, active in B, is refused on every org-gated
surface** — the two factors intersect, and a stale grant row can never
out-rank the realm's coarse claim.

**The stores outside Postgres go per-org too.** GCS/photo object keys gain an
`org/<id>/` prefix AND the AES-256-GCM data key becomes per-org (HKDF-derived
from the master key salted by `org_id`), so a prefix-routing bug yields
UNDECRYPTABLE ciphertext, not another hotel's punch photos. Org 1's key is
DEFINED as the current master key, so existing objects stay readable with no
re-encrypt. Per-org integration config lives in `org_settings` (the
`crm_provider` a tenant pulls from; empty = off); `USALI_CRM_PROVIDER` seeds
org 1's row only, and runtime reads the active org's row, never env.

**Provisioning is a primitive, not a signup surface.** `provision_tenant()`
chains KC organization → first admin user → membership → DB `organization` row
+ org-wide `org_admin` grant, **idempotently** (find-or-create at every step),
on an owner (RLS-bypassing) session. No HTTP endpoint, no self-service flow —
those are onboarding steps designed on top of this. The SPA gains only the
minimal active-org mechanics: it stores the active org (sessionStorage, so two
browser tabs can hold two different active orgs), injects `X-Active-Org`, and
shows an org picker **only** when the claim lists more than one org — a
single-org user sees nothing new.

Every org is **fictitious by construction**, per-org: the live cloud demo
stays a single invented tenant (org 1), and the two-tenant coexistence proof
is a test (`tests/test_l7_two_org_walk.py`), never the default seed.

## Test

```bash
.venv/bin/python -m pytest        # requires a running Docker daemon (Testcontainers)
```

## Layout

- `src/usali/` — pipeline: PDF adaptors, normalization, stage repo, mapping loader, transform, CLI
- `mapping/` — curated USALI schedule + Opera→USALI mapping YAML
- `migrations/` — Alembic schema migrations
- `docs/` — design docs, USALI reference, source-system notes, and sample PDFs
- `tests/` — unit + Testcontainers integration tests
