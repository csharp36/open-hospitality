# Pillar C — Payroll Integration Design

**Date:** 2026-07-15
**Status:** Approved for planning
**Depends on:** Pillar A (auth, workforce model, scoped RBAC, PII/encryption/audit) and Pillar B (B1 kiosk capture, B2 timecards/approval, B3 estimated labor cost + Schedule 14/15) — all merged.

## Context: closing the loop with real money

Pillar B put an **estimated** labor cost in the P&L (B3: approved hours × pay rate with California overtime → Schedule 14/15, labeled an estimate). Pillar C replaces the estimate with a payroll provider's **actual gross-to-net**, and does it through a **provider-agnostic adaptor layer** so the pilot can plug into ADP *or* Gusto — and switch — without software changes. Payroll is bought, not built: the provider computes gross-to-net, taxes, and disbursement; open-hospitality owns the data and orchestrates the run.

This is also the phase where **real ePHI/PII** (SSN, bank account, tax elections) enters the system, so the encryption architecture is the spine of the design, not an afterthought.

## Goal

open-hospitality stores each employee's sensitive payroll PII **sealed client-side so the server never holds it in plaintext at rest**, assembles an approved pay period, pushes it through a swappable provider port to a (mocked) ADP/Gusto, pulls back the actual gross-to-net, and shows **estimate vs actual vs variance** in the P&L. Provider selection is a config switch. No in-house gross-to-net, no scheduling, no biometrics.

## Decisions locked with the user (2026-07-15)

1. **open-hospitality owns the PII (thick vault).** We store SSN/bank/tax ourselves rather than delegating to the provider, because owning the data makes a provider swap a *re-push*, not a re-onboarding — the strongest position for the "plug into either without software changes" goal. We accept the resulting ePHI surface and secure it (below).
2. **Actuals sit parallel to the estimate, with variance.** Provider actuals are stored as a separate fact beside B3's estimate; the SOS shows both and the variance (actual − estimate). The estimate is not discarded — the estimate-vs-actual gap is itself the labor-control signal.
3. **Two runnable mock servers + two real adapters.** Production-shaped `GustoAdapter` and `AdpAdapter` (real httpx) run against local mock servers now; go-live changes only endpoints and credentials. This mirrors the existing `qbo-mock` pattern.
4. **Sensitive PII is sealed client-side with HPKE against an HSM-held key.** The browser/native client seals PII with HPKE (RFC 9180) to a recipient public key whose private key lives in an HSM in prod (software keystore in non-prod). The server parks only the sealed blob; plaintext exists only transiently, inside the HSM/app boundary, at provider-send time.
5. **Blind overwrite — no server-side plaintext read-back.** The server never returns a sealed value to any client. A Payroll Admin can replace a value (client seals a new one) but not view it; the UI shows "on file / not on file." Plaintext never flows back to a browser, never rests, never logs.
6. **DHKEM-P256 HPKE suite behind an `Opener` seam; concrete HSM/KMS product chosen at deploy.** The seam (`public_key`/`open`/`reseal`), the dev software opener, and a suite-tagged `DHKEM(P-256, HKDF-SHA256) + HKDF-SHA256 + AES-256-GCM` are fixed now; the prod custody product (AWS KMS `DeriveSharedSecret`, CloudHSM/PKCS#11, or other) is a deployment choice the seam abstracts.

## Architecture

```
open-hospitality (system of record for PII + hours)                  payroll provider
──────────────────────────────────────────────                  ────────────────
                          seal (HPKE → HSM pubkey)
  browser / native app  ─────────────────────────►  employee_payroll_profile
  (Payroll Admin enters SSN/bank; sealed before it                (sealed blobs at rest — server-opaque)
   ever leaves the client)
                                                     pay_schedule   (biweekly, matches timecards)
                                                     approved hours (from B2/B3)
                                                          │
                                                          │  assemble pay run
                                                          ▼
                                                   Opener.open() ── HSM decapsulation (KEM) ──► plaintext (transient)
                                                          │
                                          ┌───────────────┴───────────────┐
                             reseal to provider key                 decrypt-and-send
                             (if API supports field-level enc)      over TLS (Gusto/ADP)
                                          │                               │
                                          ▼                               ▼
                                   PayrollProvider port ──► GustoAdapter ──http──► gusto-mock
                                     (canonical model)  └──► AdpAdapter   ──http──► adp-mock
                                                                 (real httpx; base_url = mock now, real at go-live)
                                          ◄──── actual gross-to-net (per employee + employer burden) ────┘
                                                          │
                                             usali_actual_labor_fact  (DEPARTMENT AGGREGATES only)
                                                          │
                                     SOS: Schedule 14 estimate (B3) + actual (C) + VARIANCE
```

Because the vault is ours, `USALI_PAYROLL_PROVIDER=gusto|adp` is the only thing that changes to switch providers — no data migration, no re-onboarding.

## Crypto architecture (the spine)

### Two regimes, split by whether the server computes on the field

The system already encrypts two fields symmetrically (`compensation_note`, `pay_rate` via A2.2's `EncryptedString`, a server-held key). Pillar C keeps that regime for data the server must *read*, and adds a second, stronger regime for data the server only *parks and forwards*.

| Regime | Fields | Scheme | Server can read? | Rationale |
|---|---|---|---|---|
| **Store-and-forward** | SSN, bank routing/account, W-4 / tax elections | **HPKE**, sealed client-side to the HSM key | **No** — opaque at rest | Server never computes on these; it only forwards them to the provider. |
| **Compute-on** | `pay_rate`, hours | symmetric `EncryptedString` (existing) | Yes | B3's estimate does arithmetic on `pay_rate`; it cannot be sealed to a key the server cannot open. |

`pay_rate` is the one field that is both compute-on (ours, for the estimate) and forwarded (to the provider, for gross-to-net). It stays symmetric and is sent to the provider like any other field. Only the pure store-and-forward fields get HPKE — which is also the cleanest minimization story.

### HPKE suite and the sealed envelope

Suite: `DHKEM(P-256, HKDF-SHA256) + HKDF-SHA256 + AES-256-GCM` (KEM 0x0010, KDF 0x0001, AEAD 0x0002). Chosen because P-256 ECDH is supported by AWS KMS `DeriveSharedSecret` **and** CloudHSM/PKCS#11, so the same suite works across likely prod custody targets.

Each stored value is a self-describing envelope so the format survives key rotation and future suite additions:

```
{ suite_id, key_id, enc (encapsulated ephemeral pubkey), ciphertext }
```

- **Versioned recipient keys.** A rotation issues a new `key_id`; old private keys are retained in the HSM for decryption. No corpus re-seal on rotation. New seals use the current key.
- **AAD binds context.** The AEAD's associated data binds the envelope to the field and employee (e.g. `employee_id || field_name`), so a ciphertext cannot be silently moved between fields or records.

### The `Opener` seam

Injected into `create_app` exactly like `TokenVerifier`, `KeycloakAdmin`, and `PhotoStore`:

- `public_key(key_id: str | None) -> RecipientKey` — the current recipient public key, served to the client to seal against. **Served over the authenticated app channel and pinned**: a substituted public key silently defeats the scheme, so authenticity is a hard requirement, not a nicety.
- `open(envelope) -> bytes` — HSM does the KEM decapsulation; returns plaintext transiently. `SoftwareOpener` in dev/test (in-process P-256 private key), `HsmOpener` in prod (the concrete KMS/HSM product is a deploy choice behind this method).
- `reseal(envelope, recipient_pubkey) -> envelope` — open then re-seal to a provider's key, for the re-wrap path.

The whole vault is thus offline-testable: `SoftwareOpener` + a client-side seal helper in tests, no HSM.

### Blind overwrite

The vault only ever *receives* sealed blobs (write) and *emits* plaintext transiently at provider-send (inside the HSM/app boundary). There is no read path that returns a sealed value to a client. The admin UI shows "on file / not on file" per field and supports replace-by-reseal; correcting a typo means re-entering and re-sealing. Plaintext PII never rests server-side, never returns to a browser, never enters a log.

### Downstream: re-wrap vs plaintext-over-TLS

At provider-send, the adapter's `capabilities.supports_field_encryption` selects:
- **reseal** — `Opener.reseal(...)` to the provider's public key; the provider-encrypted blob travels in the API. Plaintext stays inside the HSM/app boundary.
- **decrypt-and-send over TLS** — `Opener.open(...)` then send in the normal JSON body over TLS. This is the realistic path for Gusto and ADP, which accept SSN/bank as fields over TLS and do not offer field-level encryption. Plaintext is transient in app memory and in the TLS payload.

## Data model (Alembic migrations)

- **`employee_payroll_profile`** — 1:1 with `employee`; the ePHI is isolated in its own table so it can be separately gated, audited, and rotated, and the hot `employee` row stays lean. Columns: `ssn_sealed`, `bank_account_sealed`, `bank_routing_sealed`, `tax_elections_sealed` (each a sealed-envelope blob with its own `key_id`/`suite_id`), non-sensitive `account_type`, and set/cleared flags derivable from NULL. No plaintext columns.
- **`pay_schedule`** — per property (or org): frequency (biweekly, aligned to the timecard/period anchor), check-date offset.
- **`pay_run`** — one run per property + period: `status` (`draft → submitted → processed → failed`), `provider`, `provider_run_id`, `submitted_at`, `processed_at`.
- **`usali_actual_labor_fact`** — actuals rolled to **department aggregates** (gross, employer taxes/burden, hours), lineage to `pay_run`; natural-key unique so a re-pull is idempotent (mirrors B3's `usali_labor_fact` pattern — no PMS `stage_id`). Individual per-employee gross-to-net is **not** in this table.

## RBAC (reuses A2.2/B — no new authorization concepts)

- **Sealed-PII write (seal/replace):** `payroll_admin` only; every write audited. There is no read endpoint.
- **`pay_schedule` config:** `payroll_admin` (or `org_admin`).
- **Run payroll (assemble/submit/fetch):** `payroll_admin`.
- **Actual labor facts:** department aggregates, **not PII** — visible to finance/GM like any other fact (same premise as B3). B3's single-employee-department cost suppression applies to actuals too.
- **Per-employee pay-run detail (gross-to-net):** `payroll_admin` only, audited (like B3's compensation gate).

## Reporting

The SOS gains an **actual** Schedule 14 beside B3's **estimate**, plus a **variance** (actual − estimate) per department and total. Schedule 15 similarly carries actual hours where the provider returns them. Actuals are stamped with their pay-run date; B3's estimate labeling and single-employee suppression are reused for the actual and variance columns.

## Security & PII surface

- **Two key-management concerns, both prerequisites for C1:** (a) the **HPKE/HSM** key management for the sealed vault; (b) the existing **symmetric `field_encryption_key` production fail-fast** (chip `task_8ff678f0`) — `pay_rate` stays symmetric, and real payroll makes running under the committed dev-default key unacceptable. Both are addressed in C1.
- **Public-key authenticity/pinning** for the sealed-PII flow (§ Opener seam).
- **PII minimization in facts** — only department aggregates in `usali_actual_labor_fact`; individual gross-to-net gated to `payroll_admin`.
- **Secrets** — provider credentials via the established secrets pattern (Jasypt local / Secrets Manager prod); never in the repo.
- **Transport** — TLS to providers.
- **BAA/DPA with the chosen provider (ADP/Gusto) is a go-live prerequisite** — the provider processes PII on our behalf. It does not block mock development.
- **Mock servers accept only synthetic data** and must never be aimed at a real provider endpoint in CI.

## Testing & gates

- **Offline test loop, as established:** `SoftwareOpener` + a client-side seal helper stand in for the HSM and browser; `gusto-mock`/`adp-mock` (or in-memory provider fakes) stand in for the providers. No HSM, no live provider, no network in the test loop.
- Assert: a sealed value round-trips (client seal → `open` → plaintext) and is opaque at rest; **there is no endpoint that returns a sealed value** (blind-overwrite proven by the absence + a negative test); the AAD binding rejects a ciphertext moved to another field/record; envelope key-version metadata survives a simulated rotation; a pay-run submit assembles the right period and lands aggregated actual facts; the provider switch is config-only (both adapters pass the same port contract test); re-pull is idempotent; sealed PII is unreadable by non-payroll roles and never logged; the SOS shows estimate, actual, and variance after a run.
- Existing gates unchanged and green throughout: pytest + Testcontainers Postgres, strict mypy (`packages=["usali"]`), ruff; frontend build (`tsc -b`)/oxlint/vitest/Playwright. The financial, workforce, and B1–B3 suites must stay green.

## Implementation phases (one spec, planned as three)

- **C1 — Sealed PII vault.** The HPKE `Opener` seam (`SoftwareOpener`, `public_key`/`open`/`reseal`, the self-describing envelope, versioned keys, AAD binding) **and** the symmetric `field_encryption_key` prod fail-fast; then `employee_payroll_profile` + `pay_schedule` + Payroll-Admin blind-overwrite write API + audit + the client-side sealing in the onboarding UI. *Ships: the secured foundation. No provider yet.*
- **C2 — Provider port + adapters + round-trip.** The `PayrollProvider` port and canonical model; `GustoAdapter` + `AdpAdapter` (real httpx); `gusto-mock` + `adp-mock` servers; `pay_run`; assemble-approved-period → (reseal or decrypt-over-TLS via the Opener) → mock provider → fetch actuals → store aggregated `usali_actual_labor_fact`; provider selected by config. *Ships: the swappable end-to-end payroll round-trip.*
- **C3 — Variance reporting + reconciliation.** Estimate-vs-actual + variance in the SOS; threshold alerts when the variance is large; the Payroll-Admin per-employee pay-run detail. *Ships: the payoff — actuals and the estimate-vs-actual signal in the P&L.*

## Out of scope (explicit)

- **In-house gross-to-net** — tax tables, withholding math, garnishments, direct-deposit execution. The provider does this; we orchestrate and store the results.
- **Multi-state wage rules** — the provider handles state tax on the actuals; open-hospitality's own *estimate* (B3) remains California-only and must not be presented as jurisdiction-agnostic.
- **Employee self-service onboarding** — in C1 a Payroll Admin enters and seals PII in their browser on the employee's behalf. An employee-seals-their-own-PII path (a lightweight link) is a later enhancement, not C1.
- **Scheduling** — still a later, separate pillar.
- **Biometric templates / face recognition** — permanently rejected (Pillar B).
- **Reading sealed PII back to a screen** — deliberately impossible by design (blind overwrite).

## Definition of done

A Payroll Admin enters an employee's SSN/bank/W-4; the browser seals each field with HPKE to the HSM's public key and the server stores only the sealed blobs (no plaintext at rest, no read-back). A biweekly pay run assembles the approved period, the Opener decrypts (or reseals) the PII inside the HSM/app boundary, and the active provider adapter — Gusto or ADP, chosen by config, running against its mock — returns actual gross-to-net. Actuals land as department aggregates in `usali_actual_labor_fact`, and the property's Summary Operating Statement shows **Schedule 14 estimate, actual, and variance** (and Schedule 15 hours). Switching the provider is a config change with no data migration. The symmetric `field_encryption_key` fails fast in prod, and the HPKE private key never leaves the HSM. All gates green.
