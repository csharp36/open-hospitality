# Core Performance Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute and expose occupancy, ADR, RevPAR, TRevPAR, and labor-hours-/labor-cost-per-occupied-room for any date range and any named fiscal period — recomputed from primitives, with prior-period/prior-year comparisons and the operator trend bases (WoW, MTD, 30-day rolling avg+stdev, day-of-week), gated on a new per-day ingestion-coverage signal.

**Architecture:** Pure metric functions in a NEW module `src/usali/performance.py` (keeping the already-large `reporting.py` from bloating), reusing `reporting._rooms_by_day` / `_revenue_by_day` / `_labor_sections` / `_discloses`, `inventory.rooms_available`, and `fiscal.resolve_period`. Three schema additions: DNR reason codes (widened OOO CHECK), a per-property `property_stat_config.adr_room_basis`, and an OrgScoped `ingestion_coverage` table populated by the ingestion pipeline. HTTP surface: `GET /api/performance` in `portal_api.py`. Frontend: a KPI dashboard page.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 (typed `Mapped`), Alembic, Postgres 16 (RLS), pytest + testcontainers, `uv`; React + TypeScript + Vite + @tanstack/react-query + Vitest.

**Design doc:** `docs/design/2026-08-11-core-performance-statistics-design.md`.

**Depends on:** #8 (rooms-available, fiscal periods, property config). The `InventoryInconsistent` refusal from the #8 review remediation (PR #25) should land first; this plan's B-phase catches it.

---

## File Structure

**New source:**
- `src/usali/performance.py` — all pure metric/trend/comparison functions.
- `migrations/versions/m2a0perffoundations_performance_foundations.py` — widens the OOO reason CHECK; creates `property_stat_config` + `ingestion_coverage` with RLS.

**Modified source:**
- `src/usali/models.py` — extend `OOO_REASON_CODES`; add `ADR_ROOM_BASES`, `PropertyStatConfig`, `IngestionCoverage`.
- `src/usali/property_config_api.py` — read/write `adr_room_basis`.
- `src/usali/portal_api.py` — `GET /api/performance` + response models.
- `src/usali/ingestion.py` — record coverage rows as reports land.
- `docs/reference/performance-metrics.md` — add #9 formulas.
- `scripts/demo_seed.py` — ensure the demo world exposes the metrics.
- Frontend: `frontend/src/api/types.ts`, `client.ts`, `pages/PerformancePage.tsx`, `router.tsx`, `Layout.tsx`.

**New tests:** `tests/test_property_stat_config.py`, `tests/test_ingestion_coverage.py`, `tests/test_performance_service.py`, `tests/test_performance_trends.py`, `tests/test_performance_api.py`, `tests/test_performance_disclosure.py`, `frontend/src/pages/PerformancePage.test.tsx`.

**Modified tests (sync registries — EVERY new table updates all four):** `tests/test_l1_org_wall_migration.py` (org-id index list), `tests/test_l2_rls_wall.py` (RLS table set + count), `tests/test_l4_org_grants.py` (head pin → `m2a0perffoundations`), `tests/test_models.py` (table list), `tests/test_property_config_models.py` (reason-vocab + CHECK-coupling now includes DNR codes).

---

## Phase A — Foundations (config + coverage)

### Task A1: DNR reason codes

**Files:**
- Modify: `src/usali/models.py` (the `OOO_REASON_CODES` frozenset near line 562 and the `OutOfOrderRoom` `reason_code` CHECK literal near line 1273)
- Test: `tests/test_property_config_models.py` (existing vocab + CHECK-coupling tests)

- [ ] **Step 1: Write the failing test** — extend the vocab pin in `tests/test_property_config_models.py`:

```python
def test_reason_and_calendar_vocab_constants():
    assert OOO_REASON_CODES == frozenset({
        "maintenance", "renovation", "damage", "deep_clean", "other",
        "do_not_rent", "owner_occupied",
    })
    assert CALENDAR_TYPES == frozenset({"calendar_month", "445"})
```

Also add a DB-level acceptance test (the CHECK must accept the new codes):

```python
def test_out_of_order_accepts_dnr_reason_codes(db_session):
    _property(db_session)
    db_session.add_all([
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 2, 1), end_date=date(2026, 2, 28),
                       room_count=2, reason_code="do_not_rent"),
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 3, 1), end_date=date(2026, 3, 31),
                       room_count=1, reason_code="owner_occupied"),
    ])
    db_session.commit()  # no IntegrityError
    assert db_session.execute(select(func.count()).select_from(OutOfOrderRoom)).scalar_one() == 2
```

Add `from sqlalchemy import func` to the imports if absent.

- [ ] **Step 2: Run — expect FAIL** (`test_reason_and_calendar_vocab_constants` mismatch; the DB test fails only after A1-Step-4's migration exists, so run it after Step 4). Run: `uv run pytest tests/test_property_config_models.py::test_reason_and_calendar_vocab_constants -q` → FAIL.

- [ ] **Step 3: Extend the constant and the model CHECK mirror** in `src/usali/models.py`:

```python
OOO_REASON_CODES = frozenset({
    "maintenance", "renovation", "damage", "deep_clean", "other",
    "do_not_rent", "owner_occupied",
})
```

And the `OutOfOrderRoom.__table_args__` CHECK literal (mirror of the frozenset):

```python
        CheckConstraint(
            "reason_code IN ('maintenance', 'renovation', 'damage', 'deep_clean', "
            "'other', 'do_not_rent', 'owner_occupied')",
            name="ck_ooo_reason_code",
        ),
```

- [ ] **Step 4: Write the migration** `migrations/versions/m2a0perffoundations_performance_foundations.py` — this task's slice widens the CHECK (the same file also creates the Task A2/A3 tables). Header + reason-CHECK widen:

```python
"""Performance foundations: DNR reason codes, per-property stat config, and the
ingestion-coverage table (#9)."""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import RLS_ORG_VAR

revision = "m2a0perffoundations"
down_revision = "m1a0propcfg"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
_NEW_TABLES = ("property_stat_config", "ingestion_coverage")

_OOO_REASONS_OLD = "('maintenance', 'renovation', 'damage', 'deep_clean', 'other')"
_OOO_REASONS_NEW = ("('maintenance', 'renovation', 'damage', 'deep_clean', 'other', "
                    "'do_not_rent', 'owner_occupied')")


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def upgrade() -> None:
    # Widen the out-of-service reason vocabulary to carry DNR (do_not_rent) and
    # owner_occupied — held-out rooms reduce rooms_available exactly like OOO.
    op.drop_constraint("ck_ooo_reason_code", "out_of_order_room", type_="check")
    op.create_check_constraint(
        "ck_ooo_reason_code", "out_of_order_room", f"reason_code IN {_OOO_REASONS_NEW}"
    )
    # (Task A2 + A3 create_table calls are appended here.)


def downgrade() -> None:
    # (Task A2 + A3 drops are prepended here.)
    op.drop_constraint("ck_ooo_reason_code", "out_of_order_room", type_="check")
    op.create_check_constraint(
        "ck_ooo_reason_code", "out_of_order_room", f"reason_code IN {_OOO_REASONS_OLD}"
    )
```

- [ ] **Step 5: Run the vocab + DB tests** — `uv run pytest tests/test_property_config_models.py -q` → PASS (the CHECK-coupling test from the #8 review now confirms CHECK == the widened frozenset). If PR #25 has not merged yet and that coupling test is absent, the vocab + acceptance tests still pass.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(perf): add do_not_rent + owner_occupied out-of-service reasons (#9)"`

### Task A2: per-property `adr_room_basis` config

**Files:**
- Modify: `src/usali/models.py` (new `ADR_ROOM_BASES` constant + `PropertyStatConfig` model)
- Modify: `migrations/versions/m2a0perffoundations_*.py` (append `property_stat_config` create)
- Modify: `src/usali/property_config_api.py` (read in `get_config`, write via a PUT)
- Test: `tests/test_property_stat_config.py`, and extend `tests/test_property_config_api.py`

- [ ] **Step 1: Write the failing model test** `tests/test_property_stat_config.py`:

```python
from datetime import date  # noqa: F401 (kept parallel to sibling tests)

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from usali.models import ADR_ROOM_BASES, Organization, Property, PropertyStatConfig


def _property(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def test_stat_config_roundtrips_and_defaults_documented(db_session):
    _property(db_session)
    db_session.add(PropertyStatConfig(property_id="HISJ", adr_room_basis="exclude_comp_house"))
    db_session.commit()
    row = db_session.execute(select(PropertyStatConfig)).scalar_one()
    assert row.adr_room_basis == "exclude_comp_house" and row.org_id == 1


def test_stat_config_refuses_bad_basis(db_session):
    _property(db_session)
    db_session.add(PropertyStatConfig(property_id="HISJ", adr_room_basis="bogus"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_adr_room_bases_vocab():
    assert ADR_ROOM_BASES == frozenset({"as_reported", "exclude_comp_house"})
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: PropertyStatConfig`). Run: `uv run pytest tests/test_property_stat_config.py -q`.

- [ ] **Step 3: Add the constant + model** in `src/usali/models.py` (beside `CALENDAR_TYPES`, and a new OrgScoped model near `FiscalCalendar`):

```python
ADR_ROOM_BASES = frozenset({"as_reported", "exclude_comp_house"})
```

```python
class PropertyStatConfig(OrgScoped, Base):
    """Per-property performance-metric settings (issue #9). One row per property
    (the FiscalCalendar precedent). `adr_room_basis` decides whether comp and
    house-use rooms are netted out of ADR's rooms-sold denominator:
    `as_reported` uses ROOMS_OCCUPIED as the PMS reports it; `exclude_comp_house`
    subtracts segment COMPLIMENTARY/HOUSE_USE rooms (refusing loudly if the
    segment data is absent)."""

    __tablename__ = "property_stat_config"
    __table_args__ = (
        CheckConstraint(
            "adr_room_basis IN ('as_reported', 'exclude_comp_house')",
            name="ck_stat_config_adr_basis",
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_property_stat_config_property_org",
        ),
    )

    property_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    adr_room_basis: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 4: Append the table to the migration** `upgrade()` (after the CHECK widen), mirroring `m1a0propcfg`'s `fiscal_calendar` exactly (org_id server_default 1 + index, composite FK, created_at NOT NULL, then `_enable_rls`):

```python
    op.create_table(
        "property_stat_config",
        sa.Column("property_id", sa.String(length=50), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_property_stat_config_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("adr_room_basis", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("adr_room_basis IN ('as_reported', 'exclude_comp_house')",
                           name="ck_stat_config_adr_basis"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_property_stat_config_property_org"),
    )
```

And in `downgrade()` (before the CHECK revert): `op.drop_policy`/`DROP POLICY` then `op.drop_table("property_stat_config")`. Add both new tables to `_enable_rls` loop and to `_NEW_TABLES` downgrade drops:

```python
    for table in _NEW_TABLES:
        _enable_rls(table)
```
```python
    for table in _NEW_TABLES:
        op.execute(f"DROP POLICY {_POLICY} ON {table}")
    op.drop_table("ingestion_coverage")
    op.drop_table("property_stat_config")
```

- [ ] **Step 5: Run the model tests** — `uv run pytest tests/test_property_stat_config.py -q` → PASS.

- [ ] **Step 6: Wire the API** in `src/usali/property_config_api.py`. Add `adr_room_basis` to `ConfigResponse` (default-aware), a reader, and a `PUT /{property_id}/stat-config`. Read helper + response field:

```python
from usali.models import ADR_ROOM_BASES, PropertyStatConfig  # add to the models import


def _adr_room_basis(session: Session, property_id: str) -> str:
    row = session.get(PropertyStatConfig, property_id)
    return row.adr_room_basis if row is not None else "as_reported"
```

Add `adr_room_basis: str` to `ConfigResponse` and populate it in `get_config` via `_adr_room_basis(session, property_id)`. New write endpoint (ORM get-or-update, org-stamp-safe, audited — mirror `set_fiscal_calendar` exactly):

```python
class StatConfigBody(BaseModel):
    adr_room_basis: str


@router.put("/{property_id}/stat-config")
def set_stat_config(
    property_id: str, body: StatConfigBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> dict[str, str]:
    if body.adr_room_basis not in ADR_ROOM_BASES:
        raise HTTPException(status_code=422,
                            detail=f"adr_room_basis must be one of {sorted(ADR_ROOM_BASES)}")
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        row = session.get(PropertyStatConfig, property_id)
        if row is None:
            row = PropertyStatConfig(property_id=property_id, adr_room_basis=body.adr_room_basis)
            session.add(row)
        else:
            row.adr_room_basis = body.adr_room_basis
        _audit(session, principal, "stat_config_set", property_id)
        session.commit()
        return {"adr_room_basis": body.adr_room_basis}
```

- [ ] **Step 7: Add an API test** to `tests/test_property_config_api.py`:

```python
def test_put_stat_config_upserts_and_audits(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    assert c.put("/api/properties/HISJ/stat-config", json={"adr_room_basis": "bogus"},
                 headers=h).status_code == 422
    r = c.put("/api/properties/HISJ/stat-config",
              json={"adr_room_basis": "exclude_comp_house"}, headers=h)
    assert r.status_code == 200 and r.json()["adr_room_basis"] == "exclude_comp_house"
    # default surfaces on the config read
    assert c.get("/api/properties/HISJ/config", headers=h).json()["adr_room_basis"] == "exclude_comp_house"
    audits = db_session.execute(
        _select(AuditEvent).where(AuditEvent.action == "stat_config_set")
    ).scalars().all()
    assert len(audits) == 1
```

- [ ] **Step 8: Run** `uv run pytest tests/test_property_stat_config.py tests/test_property_config_api.py -q` → PASS. **Commit** — `feat(perf): per-property adr_room_basis stat config (#9)`.

### Task A3: `ingestion_coverage` table + pipeline hook

**Files:**
- Modify: `src/usali/models.py` (`IngestionCoverage` model)
- Modify: `migrations/versions/m2a0perffoundations_*.py` (append `ingestion_coverage` create — already in `_NEW_TABLES`)
- Modify: `src/usali/ingestion.py` (record coverage in `process_file` / the stage helpers)
- Test: `tests/test_ingestion_coverage.py`

- [ ] **Step 1: Write the failing model test** `tests/test_ingestion_coverage.py`:

```python
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from usali.models import IngestionCoverage, Organization, Property


def _property(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def test_coverage_is_unique_per_property_day_report(db_session):
    _property(db_session)
    db_session.add(IngestionCoverage(property_id="HISJ", business_date=date(2026, 7, 7),
                                     report_type="manager_flash"))
    db_session.commit()
    db_session.add(IngestionCoverage(property_id="HISJ", business_date=date(2026, 7, 7),
                                     report_type="manager_flash"))
    with pytest.raises(IntegrityError):
        db_session.commit()
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError`). `uv run pytest tests/test_ingestion_coverage.py -q`.

- [ ] **Step 3: Add the model** in `src/usali/models.py`:

```python
class IngestionCoverage(OrgScoped, Base):
    """Which source report types landed for a property on a business date
    (issue #9). One row per (property, business_date, report_type). A metric is
    'complete' for a day when the required report types are present; trend bases
    exclude data-incomplete days. Also the visibility surface for the #26 expense
    ingestion as it comes online (a new report_type appears here)."""

    __tablename__ = "ingestion_coverage"
    __table_args__ = (
        UniqueConstraint("property_id", "business_date", "report_type",
                         name="uq_ingestion_coverage_prop_date_report"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_ingestion_coverage_property_org"),
    )

    coverage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    business_date: Mapped[date] = mapped_column(Date)
    report_type: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 4: Append the table to the migration** `upgrade()` (mirror the pattern; BigInteger PK):

```python
    op.create_table(
        "ingestion_coverage",
        sa.Column("coverage_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_ingestion_coverage_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("report_type", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("property_id", "business_date", "report_type",
                            name="uq_ingestion_coverage_prop_date_report"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_ingestion_coverage_property_org"),
    )
```

- [ ] **Step 5: Run the model test** — `uv run pytest tests/test_ingestion_coverage.py -q` → PASS.

- [ ] **Step 6: Hook the pipeline.** In `src/usali/ingestion.py`, add a helper that records coverage idempotently (ORM get-or-create so the org-stamp `before_flush` hook applies), and call it from `process_file` once a file's `(property_id, business_date, report_type)` is known. `report_type` is the pipeline's existing per-file classification (see `process_file`'s dispatch on report type). Helper:

```python
def record_coverage(session: Session, property_id: str, business_date: date, report_type: str) -> None:
    """Idempotent: one coverage row per (property, business_date, report_type).
    ORM get-or-create so the org_id before_flush stamp applies (a Core insert
    would bypass it — the m1a0propcfg lesson)."""
    exists = session.execute(
        select(IngestionCoverage.coverage_id).where(
            IngestionCoverage.property_id == property_id,
            IngestionCoverage.business_date == business_date,
            IngestionCoverage.report_type == report_type,
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(IngestionCoverage(property_id=property_id, business_date=business_date,
                                      report_type=report_type))
```

Call `record_coverage(session, property_id, business_date, report_type)` inside `process_file` after staging succeeds, before the batch commit, for each `business_date` the file covers. (Statistics files may span DAY/MONTH/YEAR — record the DAY business_date.)

- [ ] **Step 7: Write the pipeline test** in `tests/test_ingestion_coverage.py` — run a real sample PDF through `process_file` and assert a coverage row appears:

```python
def test_process_file_records_coverage(db_session, tmp_path):
    import shutil
    from pathlib import Path
    from usali.ingestion import process_file
    from usali.mapping.loader import load_mappings
    from usali.mapping.property_registry import ensure_default_org, seed_properties
    from usali.mapping.schedules import seed_schedules

    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    ensure_default_org(db_session)
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()
    inbox = tmp_path / "inbox"; inbox.mkdir()
    name = "Manager Flash 07.07.2026 - Opera.pdf"
    shutil.copy(Path("docs/reference/samples") / name, inbox / name)
    process_file(db_session, inbox / name, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f")
    db_session.commit()
    rows = db_session.execute(select(IngestionCoverage)).scalars().all()
    assert any(r.business_date == date(2026, 7, 7) for r in rows)
```

(If the real property_id/report_type differ, assert on `report_type` presence rather than an exact string — read the `process_file` dispatch to use the correct `report_type` label.)

- [ ] **Step 8: Run** `uv run pytest tests/test_ingestion_coverage.py -q` → PASS. **Commit** — `feat(perf): ingestion_coverage table + pipeline hook (#9)`.

### Task A4: sync-registry updates (all four)

**Files:** `tests/test_l1_org_wall_migration.py`, `tests/test_l2_rls_wall.py`, `tests/test_l4_org_grants.py`, `tests/test_models.py`

- [ ] **Step 1: Update each registry** to include `property_stat_config` and `ingestion_coverage`, exactly as `m1a0propcfg`'s three tables were added:
  - `test_l2_rls_wall.py`: bump the RLS count literal by 2 and add both to the `expected` set; the `_PREDICATE` cross-pin test (if PR #25 merged) also covers `m2` — extend it to assert `_m2._PREDICATE == _l2._PREDICATE`.
  - `test_l1_org_wall_migration.py`: add both to the org-id index expectation.
  - `test_l4_org_grants.py`: change the head pin `m1a0propcfg` → `m2a0perffoundations`.
  - `test_models.py`: add both table names.
- [ ] **Step 2: Run all four** — `uv run pytest tests/test_l1_org_wall_migration.py tests/test_l2_rls_wall.py tests/test_l4_org_grants.py tests/test_models.py -q` → PASS.
- [ ] **Step 3: Commit** — `test(perf): register property_stat_config + ingestion_coverage in the tenancy registries (#9)`.

---

## Phase B — Metric primitives (`src/usali/performance.py`)

All B-tasks build one module. Each function is pure (session + args → frozen dataclass / Decimal). Reuse: `reporting._rooms_by_day`, `reporting._revenue_by_day`, `reporting._labor_sections`, `reporting._discloses`, `inventory.rooms_available`.

### Task B1: per-day primitive accessors

**Files:** Create `src/usali/performance.py`; Test: `tests/test_performance_service.py`

- [ ] **Step 1: Write the failing test:**

```python
from datetime import date
from decimal import Decimal

from usali.models import Organization, Property, UsaliStatisticFact
from usali.performance import _stat_by_day


def _prop(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def _stat(pid, d, code, value):
    return UsaliStatisticFact(property_id=pid, pms_source="OPERA", business_date=d,
                              metric_code=code, period="DAY", is_prior_year=False, value=value)


def test_stat_by_day_returns_daily_values(db_session):
    _prop(db_session)
    db_session.add_all([
        _stat("HISJ", date(2026, 1, 1), "ROOM_REVENUE", 10000),
        _stat("HISJ", date(2026, 1, 2), "ROOM_REVENUE", 12000),
    ])
    db_session.commit()
    got = _stat_by_day(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2), "ROOM_REVENUE")
    assert got == {date(2026, 1, 1): Decimal("10000"), date(2026, 1, 2): Decimal("12000")}
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError`). `uv run pytest tests/test_performance_service.py -q`.

- [ ] **Step 3: Implement** `src/usali/performance.py`:

```python
"""Core performance statistics (issue #9): occupancy, ADR, RevPAR, TRevPAR, and
labor productivity, recomputed from primitives over a date range or a fiscal
period, with prior-period/prior-year comparisons and operator trend bases.

Pure functions over the promoted fact tables. Room/revenue metrics carry no
per-employee money and are ungated; labor-COST metrics compose with the
reporting._discloses per-day guard (never a fresh SUM) so a caller-controlled
window cannot be a differencing oracle. Denominators come from
inventory.rooms_available (fail-loud). #26 adds the expense side (GOPPAR/CPOR).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import UsaliStatisticFact


def _stat_by_day(
    session: Session, property_id: str, start: date, end: date, metric_code: str
) -> dict[date, Decimal]:
    """A promoted DAY statistic per business date (last write wins on a dup, as
    statistics are as-of KPIs, never summed — the _rooms_by_day convention)."""
    rows = session.execute(
        select(UsaliStatisticFact.business_date, UsaliStatisticFact.value).where(
            UsaliStatisticFact.property_id == property_id,
            UsaliStatisticFact.business_date >= start,
            UsaliStatisticFact.business_date <= end,
            UsaliStatisticFact.metric_code == metric_code,
            UsaliStatisticFact.period == "DAY",
            UsaliStatisticFact.is_prior_year.is_(False),
        )
    ).all()
    return {d: Decimal(str(v)) for d, v in rows}


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
```

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): performance module + per-day stat accessor (#9)`.

### Task B2: comp/house-use segment netting (ADR basis)

**Files:** Modify `src/usali/performance.py`; Test: `tests/test_performance_service.py`

- [ ] **Step 1: Write the failing test** (segment COMPLIMENTARY/HOUSE_USE rooms net out; missing segment data under `exclude_comp_house` refuses):

```python
import pytest
from usali.models import UsaliSegmentFact
from usali.performance import AdrBasisUnavailable, adr_rooms_sold


def _seg(pid, d, seg, rooms):
    return UsaliSegmentFact(property_id=pid, pms_source="OPERA", business_date=d,
                            usali_segment=seg, period="DAY", rooms=rooms, room_revenue=0,
                            ingest_batch_id=1)


def test_adr_rooms_sold_as_reported_ignores_segments(db_session):
    _prop(db_session)
    db_session.add(_stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100))
    db_session.commit()
    sold = adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "as_reported")
    assert sold == Decimal("100")


def test_adr_rooms_sold_excludes_comp_and_house(db_session):
    _prop(db_session)
    db_session.add_all([
        _stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100),
        _seg("HISJ", date(2026, 1, 1), "COMPLIMENTARY", 3),
        _seg("HISJ", date(2026, 1, 1), "HOUSE_USE", 2),
        _seg("HISJ", date(2026, 1, 1), "TRANSIENT", 95),
    ])
    db_session.commit()
    sold = adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "exclude_comp_house")
    assert sold == Decimal("95")  # 100 - 3 - 2


def test_adr_rooms_sold_refuses_exclude_without_segments(db_session):
    _prop(db_session)
    db_session.add(_stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100))
    db_session.commit()  # no segment rows for the day
    with pytest.raises(AdrBasisUnavailable):
        adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "exclude_comp_house")
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError`).

- [ ] **Step 3: Implement** in `performance.py`:

```python
_COMP_HOUSE_SEGMENTS = ("COMPLIMENTARY", "HOUSE_USE")


class AdrBasisUnavailable(Exception):
    """`exclude_comp_house` was requested but a day in the window has no segment
    data to net comp/house-use from — refuse rather than silently not-excluding
    (adr-010)."""


def _comp_house_rooms_by_day(
    session: Session, property_id: str, start: date, end: date
) -> dict[date, Decimal]:
    rows = session.execute(
        select(UsaliSegmentFact.business_date, UsaliSegmentFact.rooms).where(
            UsaliSegmentFact.property_id == property_id,
            UsaliSegmentFact.business_date >= start,
            UsaliSegmentFact.business_date <= end,
            UsaliSegmentFact.period == "DAY",
            UsaliSegmentFact.usali_segment.in_(_COMP_HOUSE_SEGMENTS),
        )
    ).all()
    out: dict[date, Decimal] = {}
    for d, rooms in rows:
        out[d] = out.get(d, Decimal("0")) + Decimal(str(rooms))
    return out


def _segment_days(session: Session, property_id: str, start: date, end: date) -> set[date]:
    rows = session.execute(
        select(UsaliSegmentFact.business_date).where(
            UsaliSegmentFact.property_id == property_id,
            UsaliSegmentFact.business_date >= start,
            UsaliSegmentFact.business_date <= end,
            UsaliSegmentFact.period == "DAY",
        ).distinct()
    ).scalars().all()
    return set(rows)


def adr_rooms_sold(
    session: Session, property_id: str, start: date, end: date, basis: str
) -> Decimal:
    """Rooms sold on the ADR basis over the window. `as_reported` = Σ
    ROOMS_OCCUPIED; `exclude_comp_house` subtracts segment comp+house-use rooms,
    refusing (AdrBasisUnavailable) if any occupied day lacks segment data."""
    rooms = _stat_by_day(session, property_id, start, end, "ROOMS_OCCUPIED")
    total = sum(rooms.values(), Decimal("0"))
    if basis == "as_reported":
        return total
    seg_days = _segment_days(session, property_id, start, end)
    for d in rooms:
        if rooms[d] > 0 and d not in seg_days:
            raise AdrBasisUnavailable(
                f"{property_id} is set to exclude comp/house-use from ADR, but "
                f"{d.isoformat()} has occupied rooms and no market-segment data to "
                "net them from — ingest the segment statistics or switch the ADR basis"
            )
    comp_house = _comp_house_rooms_by_day(session, property_id, start, end)
    return total - sum(comp_house.values(), Decimal("0"))
```

Import `UsaliSegmentFact` at the top.

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): comp/house-use ADR rooms-sold basis (#9)`.

### Task B3: core metrics (occupancy, ADR, RevPAR, TRevPAR) + cross-check

**Files:** Modify `src/usali/performance.py`; Test: `tests/test_performance_service.py`

- [ ] **Step 1: Write the failing test** (including the AC's RevPAR-direct = ADR×occ agreement):

```python
from usali.performance import CoreMetrics, core_metrics
from usali.models import RoomInventory


def test_core_metrics_and_revpar_crosscheck(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat("HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat("HISJ", date(2026, 1, 1), "TOTAL_REVENUE", 18000),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    assert m.rooms_available == Decimal("100")
    assert m.rooms_sold == Decimal("80")
    assert m.occupancy == Decimal("0.8000")            # 80/100
    assert m.adr == Decimal("150.0000")                # 12000/80
    assert m.revpar == Decimal("120.0000")             # 12000/100
    assert m.trevpar == Decimal("180.0000")            # 18000/100
    # cross-check: ADR x occupancy == RevPAR within tolerance
    assert abs(m.adr * m.occupancy - m.revpar) <= Decimal("0.01")


def test_core_metrics_refuses_negative_denominator(db_session):
    # inventory.rooms_available raises InventoryInconsistent (from #8 remediation)
    # when OOO exceeds inventory; core_metrics must propagate, never divide.
    from usali.inventory import InventoryInconsistent
    from usali.models import OutOfOrderRoom
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=10))
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 1),
                                  end_date=date(2026, 1, 1), room_count=25, reason_code="do_not_rent"))
    db_session.add(_stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 5))
    db_session.commit()
    with pytest.raises(InventoryInconsistent):
        core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError`).

- [ ] **Step 3: Implement** in `performance.py` (4-dp quantize; zero-denominator → `None`, never divide-by-zero):

```python
from usali.inventory import rooms_available

_Q4 = Decimal("0.0001")


def _ratio(num: Decimal, den: Decimal) -> Decimal | None:
    if den == 0:
        return None
    return (num / den).quantize(_Q4)


@dataclass(frozen=True)
class CoreMetrics:
    start: date
    end: date
    rooms_available: Decimal
    rooms_sold: Decimal
    adr_rooms_sold: Decimal
    room_revenue: Decimal
    total_revenue: Decimal
    occupancy: Decimal | None
    adr: Decimal | None
    revpar: Decimal | None
    trevpar: Decimal | None
    adr_room_basis: str


def core_metrics(
    session: Session, property_id: str, start: date, end: date, *, basis: str
) -> CoreMetrics:
    """Occupancy, ADR, RevPAR, TRevPAR over [start, end]. Denominator is
    inventory.rooms_available (fail-loud). ADR divides room revenue by the
    basis-adjusted rooms-sold; occupancy uses ROOMS_OCCUPIED as-is."""
    avail = Decimal(str(rooms_available(session, property_id, start, end)))
    sold = sum(_stat_by_day(session, property_id, start, end, "ROOMS_OCCUPIED").values(), Decimal("0"))
    adr_sold = adr_rooms_sold(session, property_id, start, end, basis)
    room_rev = sum(_stat_by_day(session, property_id, start, end, "ROOM_REVENUE").values(), Decimal("0"))
    total_rev = sum(_stat_by_day(session, property_id, start, end, "TOTAL_REVENUE").values(), Decimal("0"))
    return CoreMetrics(
        start=start, end=end, rooms_available=avail, rooms_sold=sold, adr_rooms_sold=adr_sold,
        room_revenue=room_rev, total_revenue=total_rev,
        occupancy=_ratio(sold, avail), adr=_ratio(room_rev, adr_sold),
        revpar=_ratio(room_rev, avail), trevpar=_ratio(total_rev, avail),
        adr_room_basis=basis,
    )
```

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): core occupancy/ADR/RevPAR/TRevPAR (#9)`.

### Task B4: labor productivity (hours + cost per occupied room)

**Files:** Modify `src/usali/performance.py`; Test: `tests/test_performance_service.py` and `tests/test_performance_disclosure.py`

- [ ] **Step 1: Write the failing tests.** Hours-per-occupied-room (ungated) in `test_performance_service.py`; the disclosure gate in `test_performance_disclosure.py`. Hours test:

```python
from usali.performance import labor_productivity
from usali.models import UsaliLaborFact


def test_labor_hours_per_occupied_room(db_session):
    _prop(db_session)
    db_session.add(_stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80))
    db_session.add(UsaliLaborFact(property_id="HISJ", business_date=date(2026, 1, 1),
                                  department_id=None, hours=Decimal("160"), ot_hours=Decimal("0"),
                                  est_cost=Decimal("0"), timecard_id=None))
    db_session.commit()
    p = labor_productivity(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1))
    assert p.labor_hours == Decimal("160")
    assert p.hours_per_occupied_room == Decimal("2.0000")  # 160/80
```

(Match the real `UsaliLaborFact` required columns; if `timecard_id` is NOT NULL, seed a `Timecard` first per the model — read `models.py` around `UsaliLaborFact`.)

Disclosure test in `test_performance_disclosure.py` — labor COST is suppressed when a day is funded by < 2 priced employees, and two subtractable windows cannot isolate one person (mirror the existing SOS differencing tests). Reuse the existing labor-fixture helpers from `tests/` that seed priced timecards. Assert: with a single priced employee on a day, `cost_per_occupied_room` for that day is `None` (suppressed); with ≥ 2, it is a value.

- [ ] **Step 2: Run — expect FAIL** (`ImportError`).

- [ ] **Step 3: Implement** in `performance.py` — hours summed directly (ungated); cost via `_labor_sections` per day, suppressing a day whose cost `_labor_sections` reports as suppressed (never a fresh SUM):

```python
from usali.reporting import _labor_sections

@dataclass(frozen=True)
class LaborProductivity:
    labor_hours: Decimal
    rooms_sold: Decimal
    hours_per_occupied_room: Decimal | None
    labor_cost: Decimal | None            # None if any contributing day is suppressed
    cost_per_occupied_room: Decimal | None
    cost_suppressed: bool


def labor_productivity(
    session: Session, property_id: str, start: date, end: date
) -> LaborProductivity:
    sold = sum(_stat_by_day(session, property_id, start, end, "ROOMS_OCCUPIED").values(), Decimal("0"))
    hours_total = Decimal("0")
    cost_total = Decimal("0")
    suppressed = False
    for d in _days(start, end):
        _lines, cost, hours, _ot, _fte, day_suppressed, _unpriced = _labor_sections(
            session, property_id, d, d
        )
        hours_total += hours
        if day_suppressed:
            suppressed = True
        else:
            cost_total += cost
    labor_cost = None if suppressed else cost_total
    return LaborProductivity(
        labor_hours=hours_total, rooms_sold=sold,
        hours_per_occupied_room=_ratio(hours_total, sold),
        labor_cost=labor_cost,
        cost_per_occupied_room=None if labor_cost is None else _ratio(labor_cost, sold),
        cost_suppressed=suppressed,
    )
```

Confirm the `_labor_sections` return tuple order against `reporting.py:1806` and the meaning of the `suppressed` element (it is `True` when a per-day cost was withheld by `_discloses`). Adjust the destructuring if the reviewer finds the order differs.

- [ ] **Step 4: Run** both test files → PASS. **Commit** — `feat(perf): labor hours-/cost-per-occupied-room with per-day disclosure gate (#9)`.

### Task B5: reconciliation cross-checks

**Files:** Modify `src/usali/performance.py`; Test: `tests/test_performance_service.py`

- [ ] **Step 1: Write the failing test** — computed occupancy/ADR/RevPAR compared to the ingested `OCCUPANCY_PCT`/`ADR`/`REVPAR` statistics; divergence beyond tolerance is flagged, not raised:

```python
from usali.performance import reconciliation


def test_reconciliation_flags_divergence(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat("HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat("HISJ", date(2026, 1, 1), "ADR", 150),      # agrees
        _stat("HISJ", date(2026, 1, 1), "REVPAR", 999),   # diverges from computed 120
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["adr"].agrees is True
    assert recon["revpar"].agrees is False
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** a `ReconLine(computed, ingested, agrees)` dataclass and `reconciliation(...)` that averages the ingested DAY statistic over the window (weighted by rooms where appropriate — for ADR weight by rooms sold; for occupancy/RevPAR compare against the window aggregate the PMS would report) and compares within a tolerance (`Decimal("0.5")` on ADR/RevPAR currency, `Decimal("0.005")` on occupancy fraction). Keep it fail-soft (never raises). Full code mirrors the `_ratio` style; the reviewer verifies the ingested-stat aggregation choice.

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): PMS-KPI reconciliation cross-checks (#9)`.

### Task B6: metric-completeness predicate

**Files:** Modify `src/usali/performance.py`; Test: `tests/test_performance_service.py`

- [ ] **Step 1: Write the failing test** — a day is complete only when the required report types are in `ingestion_coverage` AND `rooms_available` resolves:

```python
from usali.performance import complete_days, REQUIRED_REPORT_TYPES
from usali.models import IngestionCoverage


def test_complete_days_requires_coverage_and_availability(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    # Day 1 fully covered; day 2 missing the stats report.
    for rt in REQUIRED_REPORT_TYPES:
        db_session.add(IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 1), report_type=rt))
    db_session.commit()
    got = complete_days(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2))
    assert got == {date(2026, 1, 1)}
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** in `performance.py`:

```python
from usali.inventory import InventoryNotConfigured, total_rooms_on
from usali.models import IngestionCoverage

# The report types a day needs to compute the #9 metrics. The statistics report
# supplies ROOMS_OCCUPIED / ROOM_REVENUE / TOTAL_REVENUE. Set to the actual
# report_type label(s) the pipeline records (see ingestion.process_file).
REQUIRED_REPORT_TYPES: frozenset[str] = frozenset({"manager_flash"})


def complete_days(session: Session, property_id: str, start: date, end: date) -> set[date]:
    """The business dates in [start, end] with every REQUIRED_REPORT_TYPE landed
    and an in-force room count (so rooms_available resolves). Trend bases and
    comparisons consume only these days."""
    have: dict[date, set[str]] = {}
    for d, rt in session.execute(
        select(IngestionCoverage.business_date, IngestionCoverage.report_type).where(
            IngestionCoverage.property_id == property_id,
            IngestionCoverage.business_date >= start,
            IngestionCoverage.business_date <= end,
        )
    ).all():
        have.setdefault(d, set()).add(rt)
    out: set[date] = set()
    for d in _days(start, end):
        if not REQUIRED_REPORT_TYPES <= have.get(d, set()):
            continue
        try:
            total_rooms_on(session, property_id, d)
        except InventoryNotConfigured:
            continue
        out.add(d)
    return out
```

(Set `REQUIRED_REPORT_TYPES` to the real label Task A3 records; if OPERA and AUTOCLERK use different report_type strings for the same content, model it as "any of a required-equivalence set".)

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): metric-completeness predicate over ingestion_coverage (#9)`.

---

## Phase C — Comparisons & trends

### Task C1: prior-period and prior-year comparisons

**Files:** Modify `src/usali/performance.py`; Test: `tests/test_performance_service.py`

- [ ] **Step 1: Write the failing test** — prior-period is the same-length prior window; prior-year is −1 year; < 1 year of history → prior-year is `None`, no divide-by-zero:

```python
from usali.performance import compare


def test_prior_period_and_prior_year(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2025, 1, 1), total_rooms=100))
    # current day 2026-01-08 occ 0.80; prior-period day 2026-01-07 occ 0.60
    db_session.add_all([
        _stat("HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 80),
        _stat("HISJ", date(2026, 1, 8), "ROOM_REVENUE", 12000),
        _stat("HISJ", date(2026, 1, 8), "TOTAL_REVENUE", 18000),
        _stat("HISJ", date(2026, 1, 7), "ROOMS_OCCUPIED", 60),
        _stat("HISJ", date(2026, 1, 7), "ROOM_REVENUE", 9000),
        _stat("HISJ", date(2026, 1, 7), "TOTAL_REVENUE", 15000),
    ])
    db_session.commit()
    cmp = compare(db_session, "HISJ", date(2026, 1, 8), date(2026, 1, 8), basis="as_reported")
    assert cmp.current.occupancy == Decimal("0.8000")
    assert cmp.prior_period.occupancy == Decimal("0.6000")
    assert cmp.prior_period_delta_pct["occupancy"] is not None       # +33.3%
    # no 2025 history for this window -> prior-year metrics None, delta None (no zero-div)
    assert cmp.prior_year is None or cmp.prior_year.occupancy is None
    assert cmp.prior_year_delta_pct["occupancy"] is None
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `compare(...)`: compute `core_metrics` for the current window, the immediately-prior same-length window (`start - (n)`, where `n = (end-start).days + 1`), and the −1-year window. Prior windows whose `core_metrics` cannot resolve a denominator (`InventoryNotConfigured`) yield `None` rather than raising. `delta_pct[metric] = (cur - prior)/prior * 100`, quantized, `None` when prior is `None`/0. Return a frozen `Comparison(current, prior_period, prior_year, prior_period_delta_pct, prior_year_delta_pct)`. Wrap the prior-year `core_metrics` in a try/except `InventoryNotConfigured` → `None`.

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): prior-period + prior-year comparisons (#9)`.

### Task C2: trend bases (WoW, MTD, 30-day rolling, day-of-week)

**Files:** Modify `src/usali/performance.py`; Test: `tests/test_performance_trends.py`

- [ ] **Step 1: Write the failing tests** — one per trend base, each asserting that **data-incomplete days are excluded** (seed a day with no `ingestion_coverage` and prove it doesn't move the average). Example (WoW):

```python
from usali.performance import trends


def _cover(session, pid, d, rt="manager_flash"):
    session.add(IngestionCoverage(property_id=pid, business_date=d, report_type=rt))


def test_wow_excludes_incomplete_days(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    # anchor = 2026-01-14. current 7 = Jan 8..14, prior 7 = Jan 1..7.
    for d in (date(2026, 1, i) for i in range(1, 15)):
        db_session.add(_stat("HISJ", d, "ROOMS_OCCUPIED", 70))
        db_session.add(_stat("HISJ", d, "ROOM_REVENUE", 10500))
        db_session.add(_stat("HISJ", d, "TOTAL_REVENUE", 14000))
        _cover(db_session, "HISJ", d)
    # an incomplete day (no coverage) with wild values must NOT move the average
    db_session.add(_stat("HISJ", date(2026, 1, 10), "ROOMS_OCCUPIED", 5))  # overwrites? no—dup day
    db_session.commit()
    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="as_reported")
    assert t.wow["occupancy"].current == Decimal("0.7000")
    assert t.wow["occupancy"].prior == Decimal("0.7000")
```

(Adjust the incomplete-day construction so it is genuinely a distinct, uncovered day — e.g. mark one of the 14 days uncovered and give it an outlier value, then assert the average matches the covered-only mean.)

Add tests for MTD (month-to-date through the anchor), 30-day rolling avg + stdev, and day-of-week (anchor weekday vs anchor−7).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `trends(session, property_id, anchor, *, basis)` returning a frozen `Trends` with `wow`, `mtd`, `rolling_30` (avg + stdev), `dow` maps keyed by metric name. Each builds a **per-day** metric series over the relevant window, filters to `complete_days`, and averages. Helper — per-day metric series:

```python
_METRIC_NAMES = ("occupancy", "adr", "revpar", "trevpar")


def _daily_series(
    session: Session, property_id: str, start: date, end: date, basis: str
) -> dict[date, dict[str, Decimal | None]]:
    """Per-day core metrics for the complete days in [start, end]."""
    keep = complete_days(session, property_id, start, end)
    series: dict[date, dict[str, Decimal | None]] = {}
    for d in sorted(keep):
        m = core_metrics(session, property_id, d, d, basis=basis)
        series[d] = {"occupancy": m.occupancy, "adr": m.adr,
                     "revpar": m.revpar, "trevpar": m.trevpar}
    return series
```

Then WoW = mean(series over anchor−6..anchor) vs mean(anchor−13..anchor−7); MTD = mean(first-of-month..anchor); rolling_30 = mean + population stdev over anchor−29..anchor (use `statistics.pstdev` on the non-None values, `None` if < 2 points); DoW = series[anchor] vs series[anchor−7]. Each metric handles `None` day-values by dropping them from the mean. Rolling stdev over Decimals: convert to float for `pstdev`, return `Decimal(str(...)).quantize(_Q4)`.

- [ ] **Step 4: Run** `uv run pytest tests/test_performance_trends.py -q` → PASS. **Commit** — `feat(perf): WoW/MTD/30-day-rolling/day-of-week trend bases (#9)`.

---

## Phase D — API + fiscal-period support

### Task D1: `GET /api/performance` (date range)

**Files:** Modify `src/usali/portal_api.py`; Test: `tests/test_performance_api.py`

- [ ] **Step 1: Write the failing API test** (mirror `tests/test_property_config_api.py`'s client/fixtures):

```python
def test_get_performance_range(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat("HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat("HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat("HISJ", date(2026, 1, 1), "TOTAL_REVENUE", 18000),
    ])
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&from=2026-01-01&to=2026-01-01",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["occupancy"] == "0.8000"
    assert body["adr_room_basis"] == "as_reported"


def test_performance_refuses_out_of_scope(db_engine, db_session, tmp_path):
    _org_and_property(db_session); _org_and_property(db_session, "SSSJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "property_gm", sub="gm-sss", property_id="SSSJ")
    tok = mint(roles=["property_gm"], sub="gm-sss", scopes=[{"property_id": "SSSJ", "department_id": None}])
    assert c.get("/api/performance?property=HISJ&from=2026-01-01&to=2026-01-02",
                 headers={"Authorization": f"Bearer {tok}"}).status_code == 403
```

(Import the `_stat` helper or inline `UsaliStatisticFact` rows; reuse `_client`/`_org_and_property`/`_admin_headers` from the property-config test module or duplicate them.)

- [ ] **Step 2: Run — expect FAIL** (404 / no route).

- [ ] **Step 3: Implement** the endpoint in `portal_api.py`. Add Pydantic response models (`CoreMetricsModel`, `ComparisonModel`, `TrendsModel`, `ReconModel`, `PerformanceResponse`) mapping the `performance.py` dataclasses (Decimals serialize as strings via the existing `_opt`/Decimal convention). Endpoint reads `basis` from `PropertyStatConfig` (default `as_reported`), gates via `require_property_access`, catches `InventoryNotConfigured`/`InventoryInconsistent`/`AdrBasisUnavailable` → 409, and assembles current + comparison + trends + reconciliation + completeness (`days_excluded`). Follow the `/api/labor/analytics` structure at `portal_api.py:762`:

```python
@router.get("/performance", dependencies=[Depends(require_property_access)])
def get_performance(
    session: SessionDep,
    property: Annotated[str, Query(alias="property")],
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query(alias="to")],
) -> PerformanceResponse:
    if to < from_:
        raise HTTPException(status_code=422, detail="'to' must not precede 'from'")
    basis = _adr_room_basis(session, property)   # helper shared with property_config_api
    try:
        cmp = compare(session, property, from_, to, basis=basis)
        recon = reconciliation(session, property, from_, to, cmp.current)
        trend = trends(session, property, to, basis=basis)
    except (InventoryNotConfigured, InventoryInconsistent, AdrBasisUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _performance_response(property, basis, cmp, recon, trend, ...)
```

- [ ] **Step 4: Run** `uv run pytest tests/test_performance_api.py -q` → PASS. **Commit** — `feat(perf): GET /api/performance for a date range (#9)`.

### Task D2: fiscal-period request form

**Files:** Modify `src/usali/portal_api.py`; Test: `tests/test_performance_api.py`

- [ ] **Step 1: Write the failing test** — `?property=&period=YYYY-Pnn` resolves the window via `fiscal.resolve_period` and returns the same shape; an unconfigured calendar → 409:

```python
def test_get_performance_by_fiscal_period(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add(FiscalCalendar(property_id="HISJ", calendar_type="calendar_month",
                                  fiscal_year_start_month=1, week_start_weekday=None))
    db_session.add(_stat("HISJ", date(2026, 1, 15), "ROOMS_OCCUPIED", 80))
    db_session.add(_stat("HISJ", date(2026, 1, 15), "ROOM_REVENUE", 12000))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&period=2026-P01", headers=_admin_headers(mint, db_session))
    assert r.status_code == 200 and r.json()["period"] == "2026-P01"
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** — make `from`/`to` optional and add `period: str | None`. When `period` is set, resolve `(start, end)` via `_fiscal_config` + `resolve_period` (catch `FiscalCalendarNotConfigured` → 409, `ValueError` → 422); echo `period` in the response. Exactly one of `{from&to}` or `period` required (else 422).

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): fiscal-period performance requests (#9)`.

### Task D3: metric drill-through

**Files:** Modify `src/usali/portal_api.py`; Test: `tests/test_performance_api.py`

The AC requires every metric to drill through to the underlying staged
transactions, consistent with the SOS. Revenue numerators (room revenue, total
revenue) reuse the existing financial-fact→stage machinery; rooms-sold drills to
the statistic stage.

- [ ] **Step 1: Write the failing test** — a drill-through of the room-revenue numerator returns staged transactions that sum to the metric's `room_revenue`:

```python
def test_performance_room_revenue_drills_through(db_engine, db_session, tmp_path, seed_six_pdfs):
    # seed_six_pdfs ingests the real sample set for HISJ (2026-07-07), producing
    # financial facts + their stage rows for the Rooms line.
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    r = c.get("/api/performance/room-revenue/transactions"
              "?property=HISJ&from=2026-07-07&to=2026-07-07", headers=h)
    assert r.status_code == 200
    txns = r.json()["transactions"]
    assert txns  # non-empty
    total = sum(Decimal(t["amount"]) for t in txns)
    # reconciles to the metric's room_revenue for the window
    m = core_metrics(db_session, "HISJ", date(2026, 7, 7), date(2026, 7, 7), basis="as_reported")
    assert abs(total - m.room_revenue) <= Decimal("0.01")
```

(Use the property_id the sample set actually seeds; read `tests/conftest.py::seed_six_pdfs` and the sample property. If the sample property differs from HISJ, use that id.)

- [ ] **Step 2: Run — expect FAIL** (no route).

- [ ] **Step 3: Implement** a thin endpoint that reuses `reporting.line_transactions` for the Rooms revenue line over the window (the same `(major, sub, line)` the SOS uses for room revenue), gated by `require_property_access`, returning the `StagedTxn` rows. Document in the response which drill-through target each metric uses (revenue → financial-fact stage; occupancy/ADR → statistic stage; labor → timecard).

- [ ] **Step 4: Run** → PASS. **Commit** — `feat(perf): metric drill-through to staged transactions (#9)`.

---

## Phase E — Frontend

### Task E1: API client + types

**Files:** Modify `frontend/src/api/types.ts`, `frontend/src/api/client.ts`; Test: `frontend/src/api/client.performance.test.ts`

- [ ] **Step 1: Write the failing client test** (mirror `client.propertyConfig.test.ts`): `getPerformance(property, {from, to})` and `getPerformance(property, {period})` call the right URL with auth headers and return the parsed body. Also `setStatConfig(property, basis)` → PUT `/stat-config`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** the TS types (`PerformanceResponse`, `CoreMetrics`, `Comparison`, `Trends`, `AdrRoomBasis`) and client functions using the existing `getJson`/`authHeaders` idioms.

- [ ] **Step 4: Run** `cd frontend && npx vitest run src/api/client.performance.test.ts` → PASS. **Commit** — `feat(perf): frontend api client for performance (#9)`.

### Task E2: KPI dashboard page

**Files:** Create `frontend/src/pages/PerformancePage.tsx`; Modify `frontend/src/router.tsx`, `frontend/src/Layout.tsx`; Test: `frontend/src/pages/PerformancePage.test.tsx`

- [ ] **Step 1: Write the failing page test** — renders the four KPI cards (occupancy/ADR/RevPAR/TRevPAR) from a mocked `getPerformance`, shows the labor-productivity stats, the prior-period/prior-year deltas, the stated `adr_room_basis`, and any reconciliation divergence badge. Mirror `PropertyConfigPage.test.tsx` (mock `../api/client`, `useGlobalProperty`).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `PerformancePage.tsx` (property from `useGlobalProperty`; a date-range picker + a fiscal-period selector; KPI cards with deltas; trend sparklines from `trends`; the stated treatment and reconciliation notes), register the route in `router.tsx`, add the nav link in `Layout.tsx`.

- [ ] **Step 4: Run** `cd frontend && npx vitest run src/pages/PerformancePage.test.tsx && npm run build` → PASS + clean build. **Commit** — `feat(perf): performance KPI dashboard page (#9)`.

---

## Phase F — Docs + demo

### Task F1: metrics documentation

**Files:** Modify `docs/reference/performance-metrics.md`

- [ ] **Step 1:** Append a "Performance metrics (#9)" section: the exact formula for each metric, the ADR comp/house-use basis, the RevPAR cross-check, the comparison and trend definitions, the completeness rule, and the disclosure note. State the GOPPAR/CPOR deferral to #26.
- [ ] **Step 2: Commit** — `docs(perf): document the #9 performance formulas`.

### Task F2: demo seed

**Files:** Modify `scripts/demo_seed.py`

- [ ] **Step 1:** Ensure the demo world (HISJ/SSSJ) already exposes these metrics: the six sample PDFs seed the statistics for 2026-07-07, and #8 seeded inventory + fiscal calendars. Add `PropertyStatConfig` rows (HISJ `exclude_comp_house`, SSSJ `as_reported`) and, if the demo needs a multi-day trend window, seed additional days of statistics + `ingestion_coverage` rows so WoW/rolling trends render. **HARD PROHIBITION: do not start any server or dev stack in this task** — edit the seed function only.
- [ ] **Step 2: Run** `uv run pytest tests/ -k demo -q` (or the seed's own test) → PASS. **Commit** — `feat(perf): seed stat config + coverage for the demo (#9)`.

---

## Final: full-suite gate + review

- [ ] Run the full gates: `uv run pytest -q`, `uv run ruff check src tests scripts`, `uv run mypy src`, and `cd frontend && npx vitest run && npm run build`. All green.
- [ ] Dispatch a final code-review subagent over the whole #9 diff.
- [ ] Then the three-lens adversarial review (analytics-correctness, disclosure/tenancy, migration/tests) per the project workflow, before the PR merges.
- [ ] `superpowers:finishing-a-development-branch`.

---

## Notes for the implementer

- **`_labor_sections` return order** (`reporting.py:1806`): `(lines, cost_total, hours_total, ot_total, fte, suppressed, unpriced)`. Verify before destructuring in B4; the `suppressed` flag drives cost suppression.
- **Decimals everywhere** for money/ratios; quantize ratios to 4 dp. Never divide by a zero denominator — return `None`.
- **Fail-loud** (adr-010): `rooms_available` and `adr_rooms_sold` raise; the endpoint maps them to 409. Do not swallow into a default.
- **Disclosure**: labor COST goes through `_labor_sections` per day, never a fresh SUM — this is the differencing-oracle guard. Labor HOURS are never suppressed.
- **Sync registries**: every new table (A2, A3) updates all four registry tests (Task A4) — the suite will fail loudly if you forget one.
- **Rebase** onto `main` once PR #25 (the #8 remediation) merges, so `InventoryInconsistent` exists for B3's test.
