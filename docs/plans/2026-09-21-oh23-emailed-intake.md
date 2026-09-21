# OH-23 emailed night-audit intake — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a property's PMS emails its night-audit reports to a per-property address and they are validated, deduplicated and ingested through the redaction gate with no manual upload, with every message recorded as an event the property page shows.

**Architecture:** a Cloudflare Email Worker forwards raw mail (HMAC-signed) to `POST /api/intake/email`; the app resolves the address to an org and property on an unbound lookup, binds an org session, applies sender policy, validates each attachment against the property exactly as the upload endpoint does, dedupes by hash, and calls `process_document_bytes`. Design: [`2026-09-21-oh23-emailed-intake-design.md`](../design/2026-09-21-oh23-emailed-intake-design.md) — read it first; every decision below is cited by its D-OH23 number.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy / Alembic; pytest with `db_session`, `founding_org`, `two_tenant_world` (Docker); React + TanStack Query + vitest in `frontend/`; Cloudflare Workers (`wrangler`, plain JS, `node --test`).

**Conventions for every task:** TDD; American English; comments name the enforcement point or a test, never a cross-boundary guarantee; `uv run --frozen ruff check` and `uv run --frozen mypy --strict src` before each commit; the session's attribution trailer on every commit; never write an uploaded or emailed byte to disk (the gate: `tests/test_ingestion_boundary.py::_assert_no_file_carries` is reused).

**Worktree:** `feat/oh23-emailed-intake` from `main` (`6f8bd0e` or later). Sync with `uv sync --frozen --extra dev --extra face`.

---

### Task 1: models and migration (D-OH23.3, D-OH23.7, section 4)

**Files:**
- Modify: `src/usali/models.py` (two classes)
- Create: `migrations/versions/o1a0intake_email_intake_tables.py`
- Modify: `tests/test_models.py` (`test_tables_registered` set), `tests/test_l2_rls_wall.py` (the org-walled inventory literal), `tests/test_l4_org_grants.py` (head literal → `"o1a0intake"`), `tests/test_migration_on_populated_data.py` (`_L1_ORG_INDEPENDENT` gains `"property_intake_address"`)

- [ ] **Step 1: failing tests.** Add the two table names to `test_tables_registered`; add `email_intake_event` to the RLS inventory; change the head literal; add the address table to `_L1_ORG_INDEPENDENT`. Run those four tests: they fail (tables missing / head mismatch).

- [ ] **Step 2: models.** In `models.py`, next to `Invite`:

```python
class PropertyIntakeAddress(Base):
    """Per-property inbound address (D-OH23.3). NOT OrgScoped: the webhook
    looks it up by local part before any org is known, then binds a session
    to `org_id`. Operator routes filter org_id explicitly
    (tests/test_intake_email.py::test_an_address_of_another_org_is_invisible_and_unrotatable)."""
    __tablename__ = "property_intake_address"
    address_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_part: Mapped[str] = mapped_column(String(64), unique=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organization.org_id"))
    property_id: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sender_domains: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
```

(`property_id` carries a composite FK `(org_id, property_id) -> property(org_id, property_id)` in the migration, the pattern the money/PII spine uses; check how `NightAuditState` declares it and mirror.)

```python
class EmailIntakeEvent(OrgScoped, Base):
    """One row per message received for a property's address (D-OH23.7).
    Bodies are never stored; subject and errors pass mask_pans before the write
    (tests/test_intake_email.py::test_event_text_carries_no_card_numbers)."""
    __tablename__ = "email_intake_event"
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    address_id: Mapped[int] = mapped_column(Integer)
    property_id: Mapped[str] = mapped_column(String(50))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    envelope_from: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    auth_result: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(320), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32))
    attachments: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
```

`outcome` gets a CHECK constraint over the closed set in D-OH23.7 (spell it in the migration and in a module-level `INTAKE_OUTCOMES: frozenset[str]` in `src/usali/intake.py`, pinned equal by a test in Task 3).

- [ ] **Step 3: migration** `o1a0intake` with `down_revision = "b1e0signupalias"`: create both tables; for `email_intake_event` copy `l5a0orgsettings_org_settings.py` exactly for `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, and the `org_wall` policy with `USING` and `WITH CHECK` built from `usali.tenancy.RLS_ORG_VAR`; add no GRANT (default privileges cover it). The address table gets no policy (invite precedent; say so in the module docstring). Downgrade drops both.

- [ ] **Step 4:** run `uv run --frozen pytest tests/test_models.py tests/test_l2_rls_wall.py tests/test_l4_org_grants.py tests/test_migration_on_populated_data.py -q -p no:cacheprovider` → green. Then `tests/test_l5_per_org_stores.py tests/test_founding_org_seed_rls.py` (they enumerate tables too; fix any literal they carry). ruff, mypy.
- [ ] **Step 5: commit** `feat(intake): per-property inbound address and email intake event tables`.

### Task 2: settings, HMAC verification, the shared validator (D-OH23.2, D-OH23.5)

**Files:**
- Modify: `src/usali/config.py`
- Create: `src/usali/intake.py`
- Modify: `src/usali/night_audit_api.py` (upload endpoint and `_ingest_pack` call the shared validator)
- Create: `tests/test_intake.py`; modify `tests/test_config.py` (or wherever `_refuse_dev_secrets_in_prod` is tested) and `tests/test_night_audit.py` stays green

- [ ] **Step 1: settings.** In `Settings`: `email_intake_secret: str = "dev-intake-secret"` (added to the dev-default set `_refuse_dev_secrets_in_prod` refuses; extend its test), `email_intake_domain: str = "intake.example.test"`, `email_intake_max_bytes: int = 25 * 1024 * 1024`, `email_intake_window_seconds: int = 300`. Document each in the same comment style as the SMTP block.

- [ ] **Step 2: failing tests** in `tests/test_intake.py` for `intake.verify_signature(secret, timestamp, body, header, *, now, window) -> bool` (a vector: secret `"s"`, timestamp `"1700000000"`, body `b"hello"` → the hex the worker test in Task 6 must reproduce; compute it once with `hmac` and hard-code), stale timestamp (window exceeded) → False, wrong secret → False, malformed header → False; `intake.local_part(envelope_to)` → `"na-abc"` from `"na-abc@intake.example.test"`, lowercase, refuses anything with characters outside `[a-z0-9-]`; `intake.new_local_part()` matches `^na-[a-z2-7]{26}$`; `intake.sender_allowed(auth_result, envelope_from, sender_domains)` truth table: `dkim=pass` alone → True; `spf=pass` alone → True; neither → False; allowlist present and domain not in it → False even with dkim pass; `intake.attachments_of(raw_bytes) -> list[tuple[name, bytes]]` on an `EmailMessage` with one PDF part, one XLSX part, one text part → two entries, names scrubbed with `safe_component` (moved from `night_audit_api` into `intake` and re-imported there), an attachment with no filename → `attachment-1.pdf` / `.xlsx` by magic; a part whose bytes are neither format is dropped; `validate_for_property(session, data, property_id, pms_source) -> str | None` (it landed in `src/usali/night_audit_validation.py`, not `intake.py`; see the note on Task 3) returning `None` on OK or one of `"wrong_property" | "not_a_night_audit_report" | "unreadable"`, exercised on the Opera flash (HISJ ok; SSSJ → wrong_property), the mock pack (STDEMO ok), a `%PDF-` of garbage → unreadable, and the AutoClerk rate plan against an Opera property → not_a_night_audit_report.

- [ ] **Step 3: implement `intake.py`** with those functions, `INTAKE_OUTCOMES`, and `validate_for_property` factored from `upload_night_audit_report`'s single path and `_ingest_pack`'s per-section loop (detect with `section.title`; the business-date check stays in `night_audit_api`, which calls the validator first and then does its own date check). Refactor `night_audit_api` to call it; `tests/test_night_audit.py` must stay green untouched (that is the parity pin; if a message string changes, keep the old wording in the validator).

- [ ] **Step 4:** module tests, `tests/test_night_audit.py`, ruff, mypy. **Step 5: commit** `feat(intake): settings, signature check, sender policy, MIME attachments, shared validator`.

### Task 3: the webhook (D-OH23.1 app side, D-OH23.4, D-OH23.6, D-OH23.7, D-OH23.9)

**Files:**
- Create: `src/usali/intake_api.py`; modify `src/usali/server.py` (include the router; rate limiter on `app.state`)
- Read first: `src/usali/intake.py` (signature, local parts, sender policy, `attachments_of`, `safe_component`, `INTAKE_OUTCOMES`) and `src/usali/night_audit_validation.py` (the validator) — Task 2 split them.
- Create: `tests/test_intake_email.py`

- [ ] **Step 1: failing tests.** Build messages with `email.message.EmailMessage` (`msg["Subject"]`, `msg["Message-ID"]`, `msg.add_attachment(data, maintype="application", subtype="pdf", filename=...)`) and post with a helper `_post(client, raw, *, to, frm, auth=None, secret=...)` that signs like the worker; `auth=None` means `f"dkim=pass header.d={frm.rpartition('@')[2]}"`, because a bare `dkim=pass` carries no `header.d` and `sender_allowed` now requires the pass to be ALIGNED with the envelope sender (D-OH23.4, review decision 2026-09-21) — so the old default would have made every case a `sender_rejected`. `frm` must be a BARE address (no `<>`, and a dot-atom local part — `sender_allowed` refuses anything else now), or the helper strips the angle brackets before deriving `header.d`. Seed like `tests/test_night_audit.py` does (`_org_and_property`, mappings via `load_mappings`, `seed_properties`), create an address row directly. Cases as design section 5: 401 on bad signature and stale timestamp (nothing written anywhere: `_assert_no_file_carries` plus zero events); unknown local part → 200 `{"outcome": "unknown_address"}` and no event row; `auth="none"` → `sender_rejected` event, no batch; a bare `auth="dkim=pass"` (a pass for nobody in particular) → `sender_rejected` too; no attachment → `no_attachment`; Opera flash to HISJ's address → `ingested`, one `IngestBatch` transformed, one `ingestion_coverage` row, event `attachments[0]["batch_id"]` set; the same message again → `duplicate`, batch count unchanged; the mock pack to STDEMO's address → `ingested` with ONE entry whose `batch_id` is the first of the two batches the pack stages (D-OH23.7 fixes the entry shape at a single `batch_id`; the sha256 beside it is what finds the rest); the flash to SSSJ's address → `wrong_property`, zero batches; HotelKey: the three fixture XLSX plus the mock statistics PDF in one message to HKDEMO → four ingested; `b"%PDF-1.4 garbage"` → `failed`, one failed batch, one error record, HTTP 200; a revoked address → `revoked_address` event; `test_event_text_carries_no_card_numbers` (subject `"Report 4111 1111 1111 1111"` → stored subject masked); and the two-tenant case using `two_tenant_world` (an address on org 2's property; org 1's session sees no batch and no event). Every case ends with `_assert_no_file_carries(...)` over the app's dirs.

- [ ] **Step 2: implement** `POST /api/intake/email`: read body (cap), verify signature (401), rate limit (429), parse headers; lookup on `request.app.state.db_session_factory` (unbound) by local part; bind `OrgBoundSessionFactory(base, row.org_id)`; inside it: revoked → event; sender policy → event; attachments; per attachment dedupe (`select(IngestBatch).where(file_hash == sha, status == "transformed")`), `night_audit_validation.validate_for_property(session, data, row.property_id, prop.pms_source)` (the validator moved out of `intake.py` into `src/usali/night_audit_validation.py`; `intake.py` keeps only the DB-free message primitives), then `process_document_bytes(session, data, name, processed_dir=..., failed_dir=...)` with `ProcessingError` → `failed` (message via `mask_pans` — the gate already masks; keep it as data); outcome aggregation: all ingested → `ingested`; mix → `partial`; none attempted → the first refusal reason; commit the event LAST in its own transaction so a failed ingest (already rolled back and recorded by the gate) still leaves the event. Response `{outcome, attachments:[{name, sha256, bytes, outcome, batch_id?, error?}]}` — the same objects the event stores (D-OH23.7), `bytes` included.

- [ ] **Step 3:** run the module, `tests/test_ingestion_boundary.py`, ruff, mypy. **Step 4: commit** `feat(intake): POST /api/intake/email — signed, org-bound, validated, deduplicated`.

### Task 4: property endpoints (D-OH23.8 backend)

**Files:**
- Modify: `src/usali/property_config_api.py` (or a new `intake_address_api.py` under the same prefix; prefer the new module, included from `server.py`)
- Modify: `tests/test_intake_email.py` (endpoint cases) or create `tests/test_intake_address_api.py`

- [ ] Endpoints: `GET /api/properties/{pid}/intake-address` → `{address: "na-…@<domain>" | null, local_part, created_at, sender_domains}` (domain from settings); `POST …/intake-address` → creates if none active (409 if one exists); `POST …/intake-address/rotate` → revokes the active row (`revoked_at = now`) and creates a new one; `PUT …/intake-address` body `{sender_domains: [...]}` (validated: lowercase hostnames); `GET …/intake-events?limit=20` → newest first. Reads gate like `get_property_config`; writes like `set_fiscal_calendar` (`require_grants(ORG_ADMIN, PROPERTY_GM)` + `_require_onboardable_property`). Every query filters `org_id == tenancy.current_org_id(session)` explicitly on the address table (D-OH23.3 — a `Principal` carries org ALIASES, never an id; `current_org_id` is the predicate both walls read) and resolves the property through the org-bound `Property` lookup first. Tests: create/409, rotate (old revoked; mail to old → `revoked_address`), allowlist round trip, events list order and limit, a GM of another property refused, `test_an_address_of_another_org_is_invisible_and_unrotatable` (org 2's property via org 1's principal → 403: these routes are property-scoped, so the property gate answers first and confinement precedes existence — no existence oracle).
- [ ] Commit `feat(intake): property intake-address and intake-events endpoints`.

### Task 5: the property page (D-OH23.8 frontend)

**Files:**
- Modify: `frontend/src/api/client.ts` (+ `client.intake.test.ts`), `frontend/src/api/types.ts`, `frontend/src/pages/PropertyConfigPage.tsx`, `frontend/src/pages/PropertyConfigPage.test.tsx`

- [ ] Add `getIntakeAddress`, `createIntakeAddress`, `rotateIntakeAddress`, `setIntakeSenderDomains`, `getIntakeEvents` to the client with tests mirroring `client.propertyConfig.test.ts`. Add `IntakeSection` to `PropertyConfigPage`: no address → a "Create address" button; with address → the address in a read-only input with a visible `<label htmlFor>` ("Night-audit email address"), a Copy button (`navigator.clipboard`, guarded), "Rotate address" with a confirm step (a second click within the section, not `window.confirm`), the sender-domain field (comma-separated, saved on blur/Enter with a visible label), and a table of the last 20 events (received, from, outcome, attachments). Tests: renders null state; renders address and events; rotate requires confirmation and refetches; the domain field saves. Follow the page's existing section components for structure and the repo's a11y rule (visible labels, no aria-label-only names).
- [ ] `cd frontend && npx tsc --noEmit && npx oxlint && npm test`. Commit `feat(intake): property page shows, rotates and audits the night-audit email address`.

### Task 6: the Cloudflare worker and its deploy (D-OH23.1)

**Files:**
- Create: `cloudflare/email-intake/wrangler.toml`, `cloudflare/email-intake/src/worker.js`, `cloudflare/email-intake/src/sign.js`, `cloudflare/email-intake/test/sign.test.js`, `cloudflare/email-intake/package.json` (scripts `test: node --test`, devDependency `wrangler` pinned to the version `marketing/package.json` uses), `cloudflare/email-intake/README.md`
- Create: `.github/workflows/deploy-email-intake.yml`; modify `.github/workflows/ci.yml` (a job `email-intake` running `npm ci && npm test` in that directory)
- Create: `docs/runbooks/email-intake.md`

- [ ] `sign.js`: `export async function sign(secret, timestamp, bodyBytes)` → hex via `crypto.subtle` HMAC-SHA256 over `timestamp + "\n" + body`; the test asserts the same vector Task 2 hard-coded (secret `"s"`, timestamp `"1700000000"`, body `"hello"`).
- [ ] `worker.js`: `export default { async email(message, env, ctx) { … } }` per D-OH23.1: read `message.raw` into bytes; if over `env.MAX_BYTES` → forward to `env.FALLBACK_ADDRESS` and return; POST to `env.INTAKE_URL` with the five headers; on non-2xx or throw → `await message.forward(env.FALLBACK_ADDRESS)`. No parsing, no logging of bodies; `console.log` only the outcome JSON the app returns.
- [ ] `wrangler.toml`: `name = "oh-email-intake"`, `main = "src/worker.js"`, `compatibility_date`, `[vars] INTAKE_URL`, `MAX_BYTES`; secrets `INTAKE_SECRET` and `FALLBACK_ADDRESS` set by the workflow from GitHub secrets `EMAIL_INTAKE_SECRET`, `EMAIL_INTAKE_FALLBACK`.
- [ ] Workflow: `workflow_dispatch`; `npm ci`; `npx wrangler secret put` for the two secrets (piped from `${{ secrets.… }}`); `npx wrangler deploy`. Mirror `deploy-marketing.yml`'s comments about the token scope (this needs Workers Scripts: Edit and Email Routing Rules: Edit; the Pages token will not do — say so).
- [ ] Runbook: enable Email Routing on `intake.<domain>`, verify the fallback destination, add the catch-all rule → the worker, create the Workers-scoped token, put `EMAIL_INTAKE_SECRET` in GitHub and in GCP Secret Manager (`deploy_app.sh` gains `USALI_EMAIL_INTAKE_SECRET` from the secret and `USALI_EMAIL_INTAKE_DOMAIN=intake.mandati.ai` in `COMMON_ENV`), dispatch the worker deploy, then send a test message from a DKIM-signing domain to a property's address and read the event on the property page.
- [ ] `scripts/cloud/deploy_app.sh`: the two env entries above (secret via the same mount pattern the SMTP password uses).
- [ ] Commit `feat(intake): Cloudflare email worker, deploy workflow, runbook`.

### Task 7: roadmap, full gates, PR

- [ ] `.github/roadmap.yml` OH-23 → `shipped  # PR #<n>, 2026-09-…`; `docs/ROADMAP.md` Tier 0 row 4 Why column gains "Shipped: Cloudflare Email Worker → signed webhook; per-property token address; sender policy; event log on the property page (design 2026-09-21)"; §7 gains a dated deltas note. `docs/ARCHITECTURE.md`: one sentence on the intake path.
- [ ] `uv run --frozen ruff check && uv run --frozen mypy --strict src && uv run --frozen pytest -q -p no:cacheprovider`; `cd frontend && npx tsc --noEmit && npx oxlint && npm test`; `cd cloudflare/email-intake && npm test`; `docker build .`.
- [ ] PR body: the decisions, the tenancy shape (org-independent address table, org-bound everything else), the gate inheritance, the runbook steps that remain manual, and the test list.
