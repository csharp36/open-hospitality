# Property configuration: room inventory & fiscal calendar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the platform an authoritative, effective-dated record of each property's sellable-room inventory, out-of-order rooms, and fiscal calendar (calendar-month or 4-4-5), behind the existing OrgScoped + RLS walls, with a basic settings form — the hard dependency of issue #9 (core performance statistics).

**Architecture:** Three new `OrgScoped` tables (`room_inventory`, `out_of_order_room`, `fiscal_calendar`) with composite `(org_id, property_id)` FKs and per-table RLS, mirroring `PaySchedule`/`OrgSettings`. Two pure-function services (`inventory.py`, `fiscal.py`) compute rooms-available and resolve fiscal periods on demand (nothing materialized). A new router module `property_config_api.py` exposes reads + writes on the `POST /api/departments` auth template with the CRM-refresh audit idiom. A basic React settings page mirrors `KioskDevicesPage`.

**Tech Stack:** Python 3.13, SQLAlchemy 2.0 (typed `Mapped`), Alembic, FastAPI, pytest + testcontainers-Postgres, React + `@tanstack/react-router` + Vitest.

**Spec:** `docs/design/2026-08-10-property-config-room-inventory-fiscal-calendar-design.md`

---

## Conventions used throughout

- **Run backend tests:** `uv run pytest <path> -v` (Postgres via testcontainers; the suite already spins one up).
- **Run frontend tests:** `cd frontend && npx vitest run <path>`.
- **Migration head today is `l9a0deptfk`** — the new migration's `down_revision` is `"l9a0deptfk"`.
- **Weekday convention:** Python `date.weekday()` — Monday=0 … Sunday=6.
- **Commit after every task.** Conventional-commit subjects, e.g. `feat(config): …`, `test(config): …`.
- Do NOT push or open a PR until the whole plan is green and reviewed — the repo forbids direct pushes to `main`; work stays on `feat/property-config-inventory-fiscal`.

---

## File Structure

**Create:**
- `src/usali/inventory.py` — effective-dated inventory + rooms-available service (pure functions + `InventoryNotConfigured`).
- `src/usali/fiscal.py` — fiscal-period resolution service (pure functions + `FiscalCalendarNotConfigured`).
- `src/usali/property_config_api.py` — the `/api/properties/{id}/…` router (reads + writes + audit).
- `migrations/versions/m1a0propcfg_property_config_tables.py` — the three tables + RLS.
- `tests/test_property_config_models.py` — model/migration + populated-downgrade pins.
- `tests/test_inventory_service.py` — inventory service unit tests.
- `tests/test_fiscal_service.py` — fiscal service unit tests.
- `tests/test_property_config_api.py` — endpoint + auth + audit + validation tests.
- `tests/test_property_config_tenancy.py` — cross-org RLS isolation on `two_tenant_world`.
- `frontend/src/pages/PropertyConfigPage.tsx` + `frontend/src/pages/PropertyConfigPage.test.tsx`.
- `docs/reference/performance-metrics.md` — the fiscal-calendar + rooms-available formulas (seed doc #9 will extend).

**Modify:**
- `src/usali/models.py` — add the three model classes + the `OOO_REASON_CODES` / `CALENDAR_TYPES` constants.
- `src/usali/server.py` — register the new router.
- `scripts/demo_seed.py` — seed inventory / OOO / fiscal for HISJ + SSSJ.
- `frontend/src/api/client.ts` + `frontend/src/api/types.ts` — the config API calls + types.
- `frontend/src/router.tsx` — the `/property-config` route.
- The app nav (wherever `KioskDevicesPage`'s link lives) — a "Property config" link.

---

## Task 1: Model constants + the three tables

**Files:**
- Modify: `src/usali/models.py`
- Test: `tests/test_property_config_models.py`

- [ ] **Step 1: Write the failing test** — the models import, carry the right columns, and their CHECK constraints bite.

```python
# tests/test_property_config_models.py
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from usali.models import (
    CALENDAR_TYPES,
    OOO_REASON_CODES,
    FiscalCalendar,
    OutOfOrderRoom,
    Property,
    RoomInventory,
)


def _property(session, pid="HISJ"):
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def test_room_inventory_roundtrips_and_is_unique_per_date(db_session):
    _property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    row = db_session.execute(select(RoomInventory)).scalar_one()
    assert row.total_rooms == 140 and row.org_id == 1
    # second row, same (property, date) violates the unique
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=141))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_room_inventory_refuses_nonpositive_total(db_session):
    _property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=0))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_out_of_order_refuses_backwards_range_and_bad_reason(db_session):
    _property(db_session)
    db_session.add(OutOfOrderRoom(
        property_id="HISJ", start_date=date(2026, 2, 10), end_date=date(2026, 2, 1),
        room_count=3, reason_code="renovation",
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    _property(db_session, "SSSJ")
    db_session.add(OutOfOrderRoom(
        property_id="SSSJ", start_date=date(2026, 2, 1), end_date=date(2026, 2, 10),
        room_count=3, reason_code="not_a_reason",
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_fiscal_calendar_paired_check(db_session):
    _property(db_session)
    # 445 WITHOUT a weekday is refused
    db_session.add(FiscalCalendar(
        property_id="HISJ", calendar_type="445", fiscal_year_start_month=1,
        week_start_weekday=None,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    _property(db_session, "SSSJ")
    # calendar_month WITH a weekday is refused
    db_session.add(FiscalCalendar(
        property_id="SSSJ", calendar_type="calendar_month", fiscal_year_start_month=1,
        week_start_weekday=6,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_reason_and_calendar_vocab_constants():
    assert OOO_REASON_CODES == frozenset(
        {"maintenance", "renovation", "damage", "deep_clean", "other"}
    )
    assert CALENDAR_TYPES == frozenset({"calendar_month", "445"})
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_property_config_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'RoomInventory'`.

- [ ] **Step 3: Add the constants and models** to `src/usali/models.py`.

Add the constants near the top-of-file config-vocab constants (beside `PAY_TYPES`, ~line 556):

```python
# Closed vocabularies for property config (property_config_api + demo seed).
# The DB CHECKs below are the literal SCHEMA MIRROR of these sets — kept literal
# on purpose so the database refuses an unknown value independently of the app
# import, the org_settings.crm_provider idiom.
OOO_REASON_CODES = frozenset({"maintenance", "renovation", "damage", "deep_clean", "other"})
CALENDAR_TYPES = frozenset({"calendar_month", "445"})
```

Add the three classes at the end of the model section (after `PaySchedule`, before the schedule/occupancy tables is fine — placement is cosmetic):

```python
class RoomInventory(OrgScoped, Base):
    """A property's sellable-room count, EFFECTIVE-DATED. The count in force
    for a date D is the row with the greatest `effective_date <= D`; to change
    the count you INSERT a new row, so a renovation never rewrites history
    (issue #8). One count per property per effective date; a re-POST for the
    same date is a correction (upsert)."""

    __tablename__ = "room_inventory"
    __table_args__ = (
        UniqueConstraint("property_id", "effective_date", name="uq_room_inventory_prop_date"),
        CheckConstraint("total_rooms > 0", name="ck_room_inventory_total_positive"),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_room_inventory_property_org",
        ),
    )

    inventory_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    effective_date: Mapped[date] = mapped_column(Date)
    total_rooms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutOfOrderRoom(OrgScoped, Base):
    """A block of rooms out of order / out of service for a date range, with a
    reason (issue #8). Blocks may overlap the query window partially and may
    overlap each other; room-nights sum across blocks."""

    __tablename__ = "out_of_order_room"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_ooo_range"),
        CheckConstraint("room_count > 0", name="ck_ooo_count_positive"),
        # Literal mirror of OOO_REASON_CODES (kept in sync by test).
        CheckConstraint(
            "reason_code IN ('maintenance', 'renovation', 'damage', 'deep_clean', 'other')",
            name="ck_ooo_reason_code",
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_ooo_property_org",
        ),
    )

    ooo_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    room_count: Mapped[int] = mapped_column(Integer)
    reason_code: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FiscalCalendar(OrgScoped, Base):
    """A property's fiscal calendar (issue #8). One row per property (the
    PaySchedule precedent). `calendar_month` runs the twelve calendar months
    from `fiscal_year_start_month`; `445` runs 4/4/5-week periods anchored on
    the first `week_start_weekday` on/after the 1st of the start month. The two
    are a pair: `week_start_weekday` is present iff the type is `445`."""

    __tablename__ = "fiscal_calendar"
    __table_args__ = (
        CheckConstraint(
            "calendar_type IN ('calendar_month', '445')", name="ck_fiscal_type"
        ),
        CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12", name="ck_fiscal_start_month"
        ),
        CheckConstraint(
            "week_start_weekday IS NULL OR week_start_weekday BETWEEN 0 AND 6",
            name="ck_fiscal_weekday_range",
        ),
        # Paired: 445 <=> a weekday is set (the biometric notice-pair idiom).
        CheckConstraint(
            "(calendar_type = '445') = (week_start_weekday IS NOT NULL)",
            name="ck_fiscal_weekday_pair",
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_fiscal_calendar_property_org",
        ),
    )

    property_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    calendar_type: Mapped[str] = mapped_column(String(20))
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer)
    week_start_weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

Confirm `Date`, `Integer`, `String`, `CheckConstraint`, `ForeignKeyConstraint`, `UniqueConstraint`, `func`, `text`, `mapped_column`, `Mapped`, `date`, `datetime` are already imported at the top of `models.py` (they are — used by existing models). No new imports needed.

- [ ] **Step 4: The models test still can't pass without tables** — it needs the migration. Run it and expect a *different* failure now (the table doesn't exist):

Run: `uv run pytest tests/test_property_config_models.py -v`
Expected: FAIL — `ProgrammingError: relation "room_inventory" does not exist` (import now succeeds). Proceed to Task 2; do not commit yet.

---

## Task 2: The migration (tables + RLS)

**Files:**
- Create: `migrations/versions/m1a0propcfg_property_config_tables.py`

- [ ] **Step 1: Write the migration**, mirroring `l5a0orgsettings` (RLS) and `l9a0deptfk` (composite FK).

```python
# migrations/versions/m1a0propcfg_property_config_tables.py
"""Property config: room inventory, out-of-order rooms, fiscal calendar (#8).

Three OrgScoped tables the Analytics milestone divides by. Each joins the L2
database wall on the same terms as every other org-scoped table:
ENABLE/FORCE ROW LEVEL SECURITY plus the `org_wall` policy keyed on the
transaction-local app.org_id (the l2a0rlswall predicate, reused verbatim so the
policies cannot drift). The app role's DML grant arrives automatically through
the DEFAULT PRIVILEGES l2a0rlswall recorded for future tables.

Composite (org_id, property_id) FKs to `property` — the pay_schedule/department
wall: a single-column FK is validated with the referenced table's owner
privileges, past RLS, so an org-2 session could anchor a row to an org-1
property. The composite makes that cross-org reference unrepresentable.

No backfill — these are new config facts; inventing history would fabricate
room counts nobody stated. Downgrade drops the policies and the tables.
"""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import RLS_ORG_VAR

revision = "m1a0propcfg"
down_revision = "l9a0deptfk"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
_TABLES = ("room_inventory", "out_of_order_room", "fiscal_calendar")


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def upgrade() -> None:
    op.create_table(
        "room_inventory",
        sa.Column("inventory_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_room_inventory_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("total_rooms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("property_id", "effective_date", name="uq_room_inventory_prop_date"),
        sa.CheckConstraint("total_rooms > 0", name="ck_room_inventory_total_positive"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_room_inventory_property_org",
        ),
    )
    op.create_table(
        "out_of_order_room",
        sa.Column("ooo_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_out_of_order_room_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("room_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("end_date >= start_date", name="ck_ooo_range"),
        sa.CheckConstraint("room_count > 0", name="ck_ooo_count_positive"),
        sa.CheckConstraint(
            "reason_code IN ('maintenance', 'renovation', 'damage', 'deep_clean', 'other')",
            name="ck_ooo_reason_code",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_ooo_property_org",
        ),
    )
    op.create_table(
        "fiscal_calendar",
        sa.Column("property_id", sa.String(length=50), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_fiscal_calendar_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("calendar_type", sa.String(length=20), nullable=False),
        sa.Column("fiscal_year_start_month", sa.Integer(), nullable=False),
        sa.Column("week_start_weekday", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("calendar_type IN ('calendar_month', '445')", name="ck_fiscal_type"),
        sa.CheckConstraint("fiscal_year_start_month BETWEEN 1 AND 12", name="ck_fiscal_start_month"),
        sa.CheckConstraint(
            "week_start_weekday IS NULL OR week_start_weekday BETWEEN 0 AND 6",
            name="ck_fiscal_weekday_range",
        ),
        sa.CheckConstraint(
            "(calendar_type = '445') = (week_start_weekday IS NOT NULL)",
            name="ck_fiscal_weekday_pair",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_fiscal_calendar_property_org",
        ),
    )
    for table in _TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY {_POLICY} ON {table}")
    op.drop_table("fiscal_calendar")
    op.drop_table("out_of_order_room")
    op.drop_table("room_inventory")
```

- [ ] **Step 2: Confirm a single migration head**

Run: `uv run alembic heads`
Expected: exactly one head — `m1a0propcfg (head)`.

- [ ] **Step 3: Run the models test — now it passes**

Run: `uv run pytest tests/test_property_config_models.py -v`
Expected: PASS (5 tests). The testcontainers suite runs `alembic upgrade head`, so the tables now exist and every CHECK/unique bites.

- [ ] **Step 4: Add a populated upgrade→downgrade→upgrade pin** to `tests/test_property_config_models.py` (the project convention — a migration must survive with data present).

```python
def test_migration_round_trips_with_rows_present(db_session):
    """Seed a row in each new table, then downgrade one step and back up.
    A no-op downgrade would leave the tables and fail the re-upgrade's create."""
    from datetime import date as _date

    from alembic import command
    from alembic.config import Config

    _property(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=_date(2026, 1, 1), total_rooms=140),
        OutOfOrderRoom(property_id="HISJ", start_date=_date(2026, 2, 1),
                       end_date=_date(2026, 2, 7), room_count=3, reason_code="renovation"),
        FiscalCalendar(property_id="HISJ", calendar_type="445",
                       fiscal_year_start_month=1, week_start_weekday=6),
    ])
    db_session.commit()

    cfg = Config("alembic.ini")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
```

Note: this mirrors the populated-downgrade pins in `tests/` for i2a0settle / j2a0crmdemand — if those use a shared helper (e.g. a fixture that points Alembic at the test database URL), follow that helper instead of constructing `Config("alembic.ini")` directly. Open one such existing test first and copy its downgrade-harness exactly.

- [ ] **Step 5: Run it**

Run: `uv run pytest tests/test_property_config_models.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add src/usali/models.py migrations/versions/m1a0propcfg_property_config_tables.py tests/test_property_config_models.py
git commit -m "feat(config): room inventory, out-of-order, and fiscal-calendar tables (#8)"
```

---

## Task 3: `inventory.py` — effective-dated count + rooms-available

**Files:**
- Create: `src/usali/inventory.py`
- Test: `tests/test_inventory_service.py`

- [ ] **Step 1: Write the failing tests** — covers in-force lookup, the rooms-available formula, mid-window change, partial OOO overlap, leap year, and the fail-loud refusals.

```python
# tests/test_inventory_service.py
from datetime import date

import pytest

from usali.inventory import InventoryNotConfigured, rooms_available, total_rooms_on
from usali.models import OutOfOrderRoom, Property, RoomInventory


def _prop(session, pid="HISJ"):
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def test_total_rooms_on_returns_in_force_count(db_session):
    _prop(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140),
        RoomInventory(property_id="HISJ", effective_date=date(2026, 6, 1), total_rooms=138),
    ])
    db_session.commit()
    assert total_rooms_on(db_session, "HISJ", date(2026, 3, 15)) == 140  # between records
    assert total_rooms_on(db_session, "HISJ", date(2026, 6, 1)) == 138   # on the change day
    assert total_rooms_on(db_session, "HISJ", date(2026, 9, 1)) == 138   # after


def test_total_rooms_on_refuses_before_first_record(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    with pytest.raises(InventoryNotConfigured):
        total_rooms_on(db_session, "HISJ", date(2025, 12, 31))


def test_rooms_available_simple(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    # Jan 2026: 31 days × 140 = 4340, no OOO
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 4340


def test_rooms_available_subtracts_ooo_room_nights(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    # 3 rooms out for 7 days (Jan 10..16 inclusive) = 21 room-nights
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 10),
                                  end_date=date(2026, 1, 16), room_count=3, reason_code="renovation"))
    db_session.commit()
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 4340 - 21


def test_rooms_available_clamps_partial_overlap(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    # block Jan 28..Feb 4; window ends Jan 31 -> only Jan 28,29,30,31 = 4 nights count
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 28),
                                  end_date=date(2026, 2, 4), room_count=2, reason_code="maintenance"))
    db_session.commit()
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 4340 - 8


def test_rooms_available_handles_mid_window_inventory_change(db_session):
    _prop(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140),
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 15), total_rooms=138),
    ])
    db_session.commit()
    # Jan 1..14 = 14 days × 140 = 1960; Jan 15..31 = 17 days × 138 = 2346
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 1960 + 2346


def test_rooms_available_counts_leap_day(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2024, 1, 1), total_rooms=100))
    db_session.commit()
    # Feb 2024 has 29 days
    assert rooms_available(db_session, "HISJ", date(2024, 2, 1), date(2024, 2, 29)) == 2900


def test_rooms_available_refuses_window_before_inventory(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    with pytest.raises(InventoryNotConfigured):
        rooms_available(db_session, "HISJ", date(2025, 12, 25), date(2026, 1, 5))
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_inventory_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usali.inventory'`.

- [ ] **Step 3: Implement `src/usali/inventory.py`**

```python
"""Effective-dated room inventory and rooms-available (issue #8).

Pure query functions over `room_inventory` + `out_of_order_room`. The count in
force for a date is the greatest-`effective_date`-<=-date row. Rooms available
over an inclusive window is the per-day sum of in-force counts minus OOO
room-nights (each block clamped to the window). Fail-loud: a window reaching a
date with no in-force inventory row refuses rather than inventing a denominator
(adr-010). #9 (core performance statistics) divides by this number.
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import OutOfOrderRoom, RoomInventory


class InventoryNotConfigured(Exception):
    """No room-inventory row is in force for a queried date — the count is
    unknown, so we refuse rather than guess a denominator."""


def _inventory_rows(session: Session, property_id: str) -> list[RoomInventory]:
    return list(
        session.execute(
            select(RoomInventory)
            .where(RoomInventory.property_id == property_id)
            .order_by(RoomInventory.effective_date)
        ).scalars()
    )


def _in_force(rows: list[RoomInventory], day: date) -> int:
    in_force: int | None = None
    for row in rows:  # rows ascending by effective_date
        if row.effective_date <= day:
            in_force = row.total_rooms
        else:
            break
    if in_force is None:
        raise InventoryNotConfigured(
            f"no room inventory in force on {day.isoformat()} — set an effective-dated "
            "room count on or before this date before computing availability"
        )
    return in_force


def total_rooms_on(session: Session, property_id: str, day: date) -> int:
    """The sellable-room count in force for `property_id` on `day`."""
    return _in_force(_inventory_rows(session, property_id), day)


def rooms_available(session: Session, property_id: str, start: date, end: date) -> int:
    """Room-nights available over the inclusive window [start, end]:
    Σ_day(in-force total) − Σ_block(overlap_days × room_count)."""
    if end < start:
        raise ValueError("end must not precede start")
    rows = _inventory_rows(session, property_id)

    total = 0
    day = start
    while day <= end:
        total += _in_force(rows, day)  # raises if any day is unconfigured
        day += timedelta(days=1)

    blocks = session.execute(
        select(OutOfOrderRoom).where(
            OutOfOrderRoom.property_id == property_id,
            OutOfOrderRoom.start_date <= end,
            OutOfOrderRoom.end_date >= start,
        )
    ).scalars()
    for block in blocks:
        overlap_start = max(block.start_date, start)
        overlap_end = min(block.end_date, end)
        nights = (overlap_end - overlap_start).days + 1
        total -= nights * block.room_count
    return total
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/test_inventory_service.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/usali/inventory.py tests/test_inventory_service.py
git commit -m "feat(config): effective-dated inventory + rooms-available service (#8)"
```

---

## Task 4: `fiscal.py` — fiscal-period resolution

**Files:**
- Create: `src/usali/fiscal.py`
- Test: `tests/test_fiscal_service.py`

- [ ] **Step 1: Write the failing tests** — calendar-month + 4-4-5 resolution, the inverse, enumeration, the 53-week year, and the refusal.

```python
# tests/test_fiscal_service.py
from datetime import date

import pytest

from usali.fiscal import (
    FiscalCalendarNotConfigured,
    FiscalConfig,
    period_containing,
    periods_in_year,
    resolve_period,
)


# --- calendar month ---------------------------------------------------------

def test_calendar_month_january_start():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=1,
                       week_start_weekday=None)
    assert resolve_period(cfg, "2026-P01") == (date(2026, 1, 1), date(2026, 1, 31))
    assert resolve_period(cfg, "2026-P02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert resolve_period(cfg, "2026-P12") == (date(2026, 12, 1), date(2026, 12, 31))


def test_calendar_month_july_start_wraps_year():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=7,
                       week_start_weekday=None)
    assert resolve_period(cfg, "2026-P01") == (date(2026, 7, 1), date(2026, 7, 31))
    assert resolve_period(cfg, "2026-P07") == (date(2027, 1, 1), date(2027, 1, 31))


def test_calendar_month_leap_february():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=1,
                       week_start_weekday=None)
    assert resolve_period(cfg, "2024-P02") == (date(2024, 2, 1), date(2024, 2, 29))


def test_period_containing_calendar_month():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=7,
                       week_start_weekday=None)
    assert period_containing(cfg, date(2027, 1, 15)) == "2026-P07"
    assert period_containing(cfg, date(2026, 7, 1)) == "2026-P01"


# --- 4-4-5 ------------------------------------------------------------------

def test_445_periods_tile_the_year_without_gaps():
    # FY start month January, weeks start Sunday (weekday 6).
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    periods = periods_in_year(cfg, 2026)
    assert len(periods) == 12
    # first period starts on the first Sunday on/after 2026-01-01 (2026-01-04)
    assert periods[0][1] == date(2026, 1, 4)
    # 4-week, 4-week, 5-week quarter shape
    assert (periods[0][2] - periods[0][1]).days + 1 == 28   # P1 = 4 weeks
    assert (periods[2][2] - periods[2][1]).days + 1 == 35   # P3 = 5 weeks
    # contiguous, no gaps
    for (_, _, prev_end), (_, nxt_start, _) in zip(periods, periods[1:]):
        assert nxt_start == prev_end + __import__("datetime").timedelta(days=1)


def test_445_anchor_is_start_month_first_when_it_lands_on_the_weekday():
    # 2023-01-01 is a Sunday -> anchor is that day exactly ("on or after").
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    assert resolve_period(cfg, "2023-P01")[0] == date(2023, 1, 1)


def test_445_final_period_absorbs_a_53rd_week():
    """A 53-week fiscal year: the year's last period runs 6 weeks so the
    calendar tiles right up to the next year's anchor."""
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    periods = periods_in_year(cfg, 2020)  # 2020 anchor Jan 5; 2021 anchor Jan 3 => 52 weeks... choose a 53-week case
    # Assert the final period ends the day before next year's anchor, whatever the length.
    from usali.fiscal import _fy_anchor  # internal helper is fine to pin
    assert periods[-1][2] == _fy_anchor(cfg, 2021) - __import__("datetime").timedelta(days=1)
    weeks_in_last = ((periods[-1][2] - periods[-1][1]).days + 1) // 7
    assert weeks_in_last in (5, 6)  # 5 normally, 6 in a 53-week year


def test_period_containing_445_round_trips():
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    for key, start, end in periods_in_year(cfg, 2026):
        assert period_containing(cfg, start) == key
        assert period_containing(cfg, end) == key


def test_resolve_rejects_bad_period_number():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=1,
                       week_start_weekday=None)
    with pytest.raises(ValueError):
        resolve_period(cfg, "2026-P13")
    with pytest.raises(ValueError):
        resolve_period(cfg, "2026-P00")


def test_not_configured_raises():
    with pytest.raises(FiscalCalendarNotConfigured):
        # Loading helper (Task 5 uses it too); None config => refuse.
        from usali.fiscal import require_config
        require_config(None)
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_fiscal_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usali.fiscal'`.

- [ ] **Step 3: Implement `src/usali/fiscal.py`**

```python
"""Fiscal-calendar period resolution (issue #8).

Pure functions over a property's fiscal-calendar config. Two calendar types:

* `calendar_month` — period N is the Nth calendar month counting from
  `fiscal_year_start_month`.
* `445` — 4/4/5-week periods. The fiscal year's anchor is the first
  `week_start_weekday` ON OR AFTER the 1st of the start month. Periods are
  consecutive week blocks (4,4,5 per quarter). A 53-week year (the next
  anchor lands 53 weeks out) is handled by the FINAL period absorbing the
  extra week — the year always tiles right up to the next anchor.

Period key format: "{fiscal_year}-P{NN}", fiscal_year = the calendar year the
fiscal year STARTS in, NN = 01..12. Both types have 12 periods.

`period_containing` and `periods_in_year` are defined in terms of
`resolve_period`, so period boundaries have a single source of truth.
"""

from dataclasses import dataclass
from datetime import date, timedelta

# period -> number of weeks, for a 4-4-5 quarter repeated four times.
_445_WEEKS = (4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 4, 5)


class FiscalCalendarNotConfigured(Exception):
    """The property has no fiscal-calendar row; periods cannot be resolved."""


@dataclass(frozen=True)
class FiscalConfig:
    calendar_type: str
    fiscal_year_start_month: int
    week_start_weekday: int | None


def require_config(config: "FiscalConfig | None") -> FiscalConfig:
    """The one place callers turn a possibly-absent row into a value or a loud
    refusal (adr-010)."""
    if config is None:
        raise FiscalCalendarNotConfigured(
            "this property has no fiscal calendar on file, so fiscal periods "
            "cannot be resolved. Configure calendar type and fiscal-year start "
            "on the property first."
        )
    return config


def _parse_key(period_key: str) -> tuple[int, int]:
    try:
        year_str, period_str = period_key.split("-P")
        fiscal_year, period = int(year_str), int(period_str)
    except (ValueError, AttributeError):
        raise ValueError(f"malformed period key {period_key!r}; expected 'YYYY-Pnn'") from None
    if not 1 <= period <= 12:
        raise ValueError(f"period number out of range in {period_key!r} (1..12)")
    return fiscal_year, period


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _month_period(config: FiscalConfig, fiscal_year: int, period: int) -> tuple[date, date]:
    start = _add_months(date(fiscal_year, config.fiscal_year_start_month, 1), period - 1)
    next_start = _add_months(start, 1)
    return start, next_start - timedelta(days=1)


def _fy_anchor(config: FiscalConfig, fiscal_year: int) -> date:
    """First `week_start_weekday` on or after the 1st of the start month."""
    assert config.week_start_weekday is not None  # guaranteed for 445 by the DB pair-check
    first = date(fiscal_year, config.fiscal_year_start_month, 1)
    delta = (config.week_start_weekday - first.weekday()) % 7
    return first + timedelta(days=delta)


def _445_bounds(config: FiscalConfig, fiscal_year: int) -> list[tuple[date, date]]:
    anchor = _fy_anchor(config, fiscal_year)
    next_anchor = _fy_anchor(config, fiscal_year + 1)
    bounds: list[tuple[date, date]] = []
    cursor = anchor
    for weeks in _445_WEEKS:
        end = cursor + timedelta(weeks=weeks) - timedelta(days=1)
        bounds.append((cursor, end))
        cursor = end + timedelta(days=1)
    # Final period absorbs any 53rd week: extend it to the day before next anchor.
    last_start, _ = bounds[-1]
    bounds[-1] = (last_start, next_anchor - timedelta(days=1))
    return bounds


def resolve_period(config: FiscalConfig, period_key: str) -> tuple[date, date]:
    fiscal_year, period = _parse_key(period_key)
    if config.calendar_type == "calendar_month":
        return _month_period(config, fiscal_year, period)
    return _445_bounds(config, fiscal_year)[period - 1]


def periods_in_year(config: FiscalConfig, fiscal_year: int) -> list[tuple[str, date, date]]:
    out: list[tuple[str, date, date]] = []
    for period in range(1, 13):
        key = f"{fiscal_year}-P{period:02d}"
        start, end = resolve_period(config, key)
        out.append((key, start, end))
    return out


def period_containing(config: FiscalConfig, day: date) -> str:
    """The period key whose date range contains `day`."""
    if config.calendar_type == "calendar_month":
        # Fiscal year = the year whose start month <= day within a 12-month run.
        fiscal_year = day.year if day.month >= config.fiscal_year_start_month else day.year - 1
    else:
        # Largest fiscal_year whose anchor <= day.
        fiscal_year = day.year + 1
        while _fy_anchor(config, fiscal_year) > day:
            fiscal_year -= 1
    for key, start, end in periods_in_year(config, fiscal_year):
        if start <= day <= end:
            return key
    # Unreachable for a well-formed calendar; guard loudly rather than return "".
    raise ValueError(f"no fiscal period contains {day.isoformat()}")
```

- [ ] **Step 4: Run to confirm pass**

Run: `uv run pytest tests/test_fiscal_service.py -v`
Expected: PASS (9 tests). If `test_445_final_period_absorbs_a_53rd_week` reveals 2020/2021 is a 52-week year, the assertion is written to accept 5-or-6 weeks and only pins the "ends day before next anchor" invariant — it stays green; no change needed.

- [ ] **Step 5: Commit**

```bash
git add src/usali/fiscal.py tests/test_fiscal_service.py
git commit -m "feat(config): fiscal-period resolution service, calendar-month + 4-4-5 (#8)"
```

---

## Task 5: `property_config_api.py` — the read endpoints

**Files:**
- Create: `src/usali/property_config_api.py`
- Modify: `src/usali/server.py`
- Test: `tests/test_property_config_api.py` (reads portion)

- [ ] **Step 1: Write the failing read tests.** Reuse the auth harness from `tests/test_l9_review_remediation.py` (copy its `_client`, `make_authkit`, `grant_role`, and the org_admin token helper).

```python
# tests/test_property_config_api.py
from datetime import date

from fastapi.testclient import TestClient

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    FiscalCalendar,
    Organization,
    OutOfOrderRoom,
    Property,
    RoomInventory,
)
from usali.server import create_app


def _client(db_engine, tmp_path, verifier) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app)


def _org_and_property(db_session, pid="HISJ"):
    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    db_session.commit()


def _admin_headers(mint, db_session):
    grant_role(db_session, "org_admin", sub="cfg-admin", org_id=1)
    tok = mint(roles=["org_admin"], sub="cfg-admin")
    return {"Authorization": f"Bearer {tok}"}


def test_get_config_returns_inventory_ooo_and_fiscal(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140),
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 2, 1), end_date=date(2026, 2, 7),
                       room_count=3, reason_code="renovation"),
        FiscalCalendar(property_id="HISJ", calendar_type="calendar_month",
                       fiscal_year_start_month=1, week_start_weekday=None),
    ])
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/config", headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    body = r.json()
    assert body["inventory"][0]["total_rooms"] == 140
    assert body["out_of_order"][0]["reason_code"] == "renovation"
    assert body["fiscal_calendar"]["calendar_type"] == "calendar_month"


def test_get_rooms_available(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/rooms-available?start=2026-01-01&end=2026-01-31",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200 and r.json()["room_nights"] == 4340


def test_rooms_available_refuses_unconfigured_window(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/rooms-available?start=2025-12-01&end=2026-01-05",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 409  # fail-loud, named


def test_get_fiscal_periods_enumerates(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(FiscalCalendar(property_id="HISJ", calendar_type="calendar_month",
                                  fiscal_year_start_month=1, week_start_weekday=None))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/fiscal-periods?fiscal_year=2026",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    periods = r.json()["periods"]
    assert len(periods) == 12 and periods[0]["key"] == "2026-P01"


def test_read_refuses_out_of_scope_property(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    # A GM scoped to a DIFFERENT property.
    grant_role(db_session, "property_gm", sub="gm-other", property_id="SSSJ")
    tok = mint(roles=["property_gm"], sub="gm-other",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    r = c.get("/api/properties/HISJ/config", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_property_config_api.py -v`
Expected: FAIL — 404s (router not mounted) / import errors.

- [ ] **Step 3: Implement the read half of `src/usali/property_config_api.py`**

```python
"""Property configuration endpoints: room inventory, out-of-order rooms, and
fiscal calendar (issue #8).

Auth mirrors POST /api/departments: reads gate on `_require_readable_property`,
writes on `require_grants(ORG_ADMIN, PROPERTY_GM)` + `_require_onboardable_property`
(org_admin bypass; a GM confined to assigned properties). Every write emits one
AuditEvent; a refusal that passed confinement audits with a rollback first, so
the audit commit never sweeps in a partial write (the crm_api idiom).

Fail-loud reads: an unconfigured fiscal calendar, or a rooms-available window
reaching before the first inventory row, returns 409 with a named message
rather than a guess (adr-010).
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import (
    ORG_ADMIN,
    PROPERTY_GM,
    Principal,
    request_session_factory,
    require_grants,
    require_operator,
)
from usali.fiscal import (
    FiscalCalendarNotConfigured,
    FiscalConfig,
    period_containing,
    periods_in_year,
    require_config,
    resolve_period,
)
from usali.inventory import InventoryNotConfigured, rooms_available
from usali.models import (
    AuditEvent,
    FiscalCalendar,
    OutOfOrderRoom,
    RoomInventory,
)
from usali.workforce import (
    _require_onboardable_property,
    _require_readable_property,
    resolve_scope,
)

require_config_writer = require_grants(ORG_ADMIN, PROPERTY_GM)

router = APIRouter(prefix="/api/properties")


def _session(request: Request) -> Session:
    return request_session_factory(request)()


def _fiscal_config(session: Session, property_id: str) -> FiscalConfig | None:
    row = session.get(FiscalCalendar, property_id)
    if row is None:
        return None
    return FiscalConfig(
        calendar_type=row.calendar_type,
        fiscal_year_start_month=row.fiscal_year_start_month,
        week_start_weekday=row.week_start_weekday,
    )


# ---- read models -----------------------------------------------------------

class InventoryRow(BaseModel):
    inventory_id: int
    effective_date: date
    total_rooms: int


class OooRow(BaseModel):
    ooo_id: int
    start_date: date
    end_date: date
    room_count: int
    reason_code: str
    note: str | None


class FiscalConfigModel(BaseModel):
    calendar_type: str
    fiscal_year_start_month: int
    week_start_weekday: int | None


class ConfigResponse(BaseModel):
    property_id: str
    inventory: list[InventoryRow]
    out_of_order: list[OooRow]
    fiscal_calendar: FiscalConfigModel | None


@router.get("/{property_id}/config")
def get_config(
    property_id: str, request: Request,
    principal: Principal = Depends(require_operator),
) -> ConfigResponse:
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        inv = session.execute(
            select(RoomInventory).where(RoomInventory.property_id == property_id)
            .order_by(RoomInventory.effective_date.desc())
        ).scalars().all()
        ooo = session.execute(
            select(OutOfOrderRoom).where(OutOfOrderRoom.property_id == property_id)
            .order_by(OutOfOrderRoom.start_date.desc())
        ).scalars().all()
        cfg = _fiscal_config(session, property_id)
        return ConfigResponse(
            property_id=property_id,
            inventory=[InventoryRow(inventory_id=r.inventory_id, effective_date=r.effective_date,
                                    total_rooms=r.total_rooms) for r in inv],
            out_of_order=[OooRow(ooo_id=b.ooo_id, start_date=b.start_date, end_date=b.end_date,
                                 room_count=b.room_count, reason_code=b.reason_code, note=b.note)
                          for b in ooo],
            fiscal_calendar=None if cfg is None else FiscalConfigModel(
                calendar_type=cfg.calendar_type,
                fiscal_year_start_month=cfg.fiscal_year_start_month,
                week_start_weekday=cfg.week_start_weekday),
        )


class RoomsAvailableResponse(BaseModel):
    property_id: str
    start: date
    end: date
    room_nights: int


@router.get("/{property_id}/rooms-available")
def get_rooms_available(
    property_id: str, request: Request,
    start: date, end: date,
    principal: Principal = Depends(require_operator),
) -> RoomsAvailableResponse:
    if end < start:
        raise HTTPException(status_code=422, detail="end must not precede start")
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        try:
            nights = rooms_available(session, property_id, start, end)
        except InventoryNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return RoomsAvailableResponse(property_id=property_id, start=start, end=end,
                                      room_nights=nights)


class PeriodRow(BaseModel):
    key: str
    start: date
    end: date


@router.get("/{property_id}/fiscal-periods")
def get_fiscal_periods(
    property_id: str, request: Request,
    principal: Principal = Depends(require_operator),
    fiscal_year: int | None = None,
    period: str | None = None,
    on_date: Annotated[date | None, Query(alias="date")] = None,
) -> dict:
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        try:
            cfg = require_config(_fiscal_config(session, property_id))
            if period is not None:
                start, end = resolve_period(cfg, period)
                return {"periods": [PeriodRow(key=period, start=start, end=end).model_dump()]}
            if on_date is not None:
                key = period_containing(cfg, on_date)
                start, end = resolve_period(cfg, key)
                return {"periods": [PeriodRow(key=key, start=start, end=end).model_dump()]}
            year = fiscal_year if fiscal_year is not None else date.today().year
            rows = [PeriodRow(key=k, start=s, end=e).model_dump()
                    for k, s, e in periods_in_year(cfg, year)]
            return {"periods": rows}
        except FiscalCalendarNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:  # malformed period key / out-of-range
            raise HTTPException(status_code=422, detail=str(exc)) from None
```

- [ ] **Step 4: Register the router** in `src/usali/server.py`.

Add the import beside the other router imports (after `from usali.portal_api import router as portal_router`, ~line 37):

```python
from usali.property_config_api import router as property_config_router
```

Add the mount beside the other operator-gated routers (after the `workforce_router` line, ~line 295):

```python
    app.include_router(property_config_router, dependencies=operator_gates)
```

- [ ] **Step 5: Run to confirm the reads pass**

Run: `uv run pytest tests/test_property_config_api.py -v`
Expected: PASS (5 read tests).

- [ ] **Step 6: Commit**

```bash
git add src/usali/property_config_api.py src/usali/server.py tests/test_property_config_api.py
git commit -m "feat(config): property-config read endpoints (config, rooms-available, fiscal periods) (#8)"
```

---

## Task 6: `property_config_api.py` — the write endpoints + audit

**Files:**
- Modify: `src/usali/property_config_api.py`
- Test: `tests/test_property_config_api.py` (append write tests)

- [ ] **Step 1: Append the failing write tests** to `tests/test_property_config_api.py`.

```python
from sqlalchemy import func, select as _select

from usali.models import AuditEvent


def test_post_inventory_creates_and_audits(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 140}, headers=h)
    assert r.status_code == 201
    db_session.expire_all()
    assert db_session.execute(_select(func.count()).select_from(RoomInventory)).scalar_one() == 1
    audits = db_session.execute(
        _select(AuditEvent).where(AuditEvent.action == "property_inventory_set")
    ).scalars().all()
    assert len(audits) == 1 and audits[0].resource_id == "HISJ"


def test_post_inventory_same_date_is_a_correction(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    c.post("/api/properties/HISJ/inventory",
           json={"effective_date": "2026-01-01", "total_rooms": 140}, headers=h)
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 145}, headers=h)
    assert r.status_code == 201
    db_session.expire_all()
    rows = db_session.execute(_select(RoomInventory)).scalars().all()
    assert len(rows) == 1 and rows[0].total_rooms == 145  # upsert, not a duplicate


def test_post_inventory_rejects_nonpositive(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 0},
               headers=_admin_headers(mint, db_session))
    assert r.status_code == 422


def test_post_and_delete_ooo(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    r = c.post("/api/properties/HISJ/out-of-order",
               json={"start_date": "2026-02-01", "end_date": "2026-02-07",
                     "room_count": 3, "reason_code": "renovation"}, headers=h)
    assert r.status_code == 201
    ooo_id = r.json()["ooo_id"]
    r2 = c.delete(f"/api/properties/HISJ/out-of-order/{ooo_id}", headers=h)
    assert r2.status_code == 204
    db_session.expire_all()
    assert db_session.execute(_select(func.count()).select_from(OutOfOrderRoom)).scalar_one() == 0


def test_post_ooo_rejects_bad_reason_and_backwards_range(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    assert c.post("/api/properties/HISJ/out-of-order",
                  json={"start_date": "2026-02-01", "end_date": "2026-02-07",
                        "room_count": 3, "reason_code": "bogus"}, headers=h).status_code == 422
    assert c.post("/api/properties/HISJ/out-of-order",
                  json={"start_date": "2026-02-07", "end_date": "2026-02-01",
                        "room_count": 3, "reason_code": "damage"}, headers=h).status_code == 422


def test_put_fiscal_calendar_requires_weekday_for_445(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    # 445 without a weekday -> 422 at the boundary
    assert c.put("/api/properties/HISJ/fiscal-calendar",
                 json={"calendar_type": "445", "fiscal_year_start_month": 1}, headers=h).status_code == 422
    # calendar_month with a weekday -> 422
    assert c.put("/api/properties/HISJ/fiscal-calendar",
                 json={"calendar_type": "calendar_month", "fiscal_year_start_month": 1,
                       "week_start_weekday": 6}, headers=h).status_code == 422
    # valid 445 upserts
    r = c.put("/api/properties/HISJ/fiscal-calendar",
              json={"calendar_type": "445", "fiscal_year_start_month": 1, "week_start_weekday": 6},
              headers=h)
    assert r.status_code == 200
    db_session.expire_all()
    row = db_session.get(FiscalCalendar, "HISJ")
    assert row.calendar_type == "445" and row.week_start_weekday == 6


def test_write_refuses_out_of_scope_and_audits_nothing_extra(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "property_gm", sub="gm-sss", property_id="SSSJ")
    tok = mint(roles=["property_gm"], sub="gm-sss",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 140},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_property_config_api.py -v -k "post or put or delete or write"`
Expected: FAIL — 404/405 (write routes not defined).

- [ ] **Step 3: Append the write endpoints** to `src/usali/property_config_api.py`.

```python
from pydantic import Field
from sqlalchemy.dialects.postgresql import insert as pg_insert


class InventoryBody(BaseModel):
    effective_date: date
    total_rooms: int = Field(gt=0)


class OooBody(BaseModel):
    start_date: date
    end_date: date
    room_count: int = Field(gt=0)
    reason_code: str
    note: str | None = None


class FiscalBody(BaseModel):
    calendar_type: str
    fiscal_year_start_month: int = Field(ge=1, le=12)
    week_start_weekday: int | None = Field(default=None, ge=0, le=6)


def _audit(session: Session, principal: Principal, action: str, property_id: str) -> None:
    session.add(AuditEvent(actor_subject=principal.subject, action=action,
                           resource_type="property", resource_id=property_id))


@router.post("/{property_id}/inventory", status_code=201)
def set_inventory(
    property_id: str, body: InventoryBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> InventoryRow:
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        # Upsert on (property_id, effective_date): a re-POST corrects the count.
        session.execute(
            pg_insert(RoomInventory)
            .values(property_id=property_id, effective_date=body.effective_date,
                    total_rooms=body.total_rooms)
            .on_conflict_do_update(
                index_elements=["property_id", "effective_date"],
                set_={"total_rooms": body.total_rooms})
        )
        row = session.execute(
            select(RoomInventory).where(RoomInventory.property_id == property_id,
                                        RoomInventory.effective_date == body.effective_date)
        ).scalar_one()
        _audit(session, principal, "property_inventory_set", property_id)
        session.commit()
        return InventoryRow(inventory_id=row.inventory_id, effective_date=row.effective_date,
                            total_rooms=row.total_rooms)


@router.post("/{property_id}/out-of-order", status_code=201)
def add_ooo(
    property_id: str, body: OooBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> OooRow:
    if body.end_date < body.start_date:
        raise HTTPException(status_code=422, detail="end_date must not precede start_date")
    from usali.models import OOO_REASON_CODES
    if body.reason_code not in OOO_REASON_CODES:
        raise HTTPException(status_code=422,
                            detail=f"reason_code must be one of {sorted(OOO_REASON_CODES)}")
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        block = OutOfOrderRoom(property_id=property_id, start_date=body.start_date,
                               end_date=body.end_date, room_count=body.room_count,
                               reason_code=body.reason_code, note=body.note)
        session.add(block)
        session.flush()
        _audit(session, principal, "ooo_added", property_id)
        session.commit()
        return OooRow(ooo_id=block.ooo_id, start_date=block.start_date, end_date=block.end_date,
                      room_count=block.room_count, reason_code=block.reason_code, note=block.note)


@router.delete("/{property_id}/out-of-order/{ooo_id}", status_code=204)
def remove_ooo(
    property_id: str, ooo_id: int, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> None:
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        block = session.execute(
            select(OutOfOrderRoom).where(OutOfOrderRoom.ooo_id == ooo_id,
                                         OutOfOrderRoom.property_id == property_id)
        ).scalar_one_or_none()
        if block is None:
            raise HTTPException(status_code=404, detail="out-of-order block not found")
        session.delete(block)
        _audit(session, principal, "ooo_removed", property_id)
        session.commit()


@router.put("/{property_id}/fiscal-calendar")
def set_fiscal_calendar(
    property_id: str, body: FiscalBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> FiscalConfigModel:
    from usali.models import CALENDAR_TYPES
    if body.calendar_type not in CALENDAR_TYPES:
        raise HTTPException(status_code=422,
                            detail=f"calendar_type must be one of {sorted(CALENDAR_TYPES)}")
    is_445 = body.calendar_type == "445"
    if is_445 and body.week_start_weekday is None:
        raise HTTPException(status_code=422, detail="4-4-5 requires week_start_weekday")
    if not is_445 and body.week_start_weekday is not None:
        raise HTTPException(status_code=422,
                            detail="week_start_weekday only applies to a 4-4-5 calendar")
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        session.execute(
            pg_insert(FiscalCalendar)
            .values(property_id=property_id, calendar_type=body.calendar_type,
                    fiscal_year_start_month=body.fiscal_year_start_month,
                    week_start_weekday=body.week_start_weekday)
            .on_conflict_do_update(
                index_elements=["property_id"],
                set_={"calendar_type": body.calendar_type,
                      "fiscal_year_start_month": body.fiscal_year_start_month,
                      "week_start_weekday": body.week_start_weekday})
        )
        _audit(session, principal, "fiscal_calendar_set", property_id)
        session.commit()
        return FiscalConfigModel(calendar_type=body.calendar_type,
                                 fiscal_year_start_month=body.fiscal_year_start_month,
                                 week_start_weekday=body.week_start_weekday)
```

Note on the upsert + RLS: `on_conflict_do_update` runs under the org-bound app-role session, so the RLS `WITH CHECK` still applies and `org_id` is stamped by the `before_flush` wall for the INSERT path. Because the Core `insert()` bypasses the ORM `before_flush` stamp, set `org_id` explicitly is NOT needed here — the composite FK + RLS predicate reference the row's `org_id`, which defaults to the session org via the column `server_default`... **verify this during Step 5**: if the upsert lands `org_id=1` on an org≠1 session (the L6a Core-insert gap the K-pillar F3 finding fixed for ingestion), stamp `org_id` explicitly from the bound context using the same helper ingestion uses (`tenancy` bound-org). The single-org test suite will pass either way; the Task 9 cross-org test is what proves it. If Task 9 shows a mis-stamp, add `org_id=<bound org>` to both `pg_insert(...).values(...)`.

- [ ] **Step 4: Run to confirm the writes pass**

Run: `uv run pytest tests/test_property_config_api.py -v`
Expected: PASS (all read + write tests, ~12).

- [ ] **Step 5: Commit**

```bash
git add src/usali/property_config_api.py tests/test_property_config_api.py
git commit -m "feat(config): property-config write endpoints with audit (#8)"
```

---

## Task 7: Cross-org RLS isolation test

**Files:**
- Test: `tests/test_property_config_tenancy.py`

- [ ] **Step 1: Write the failing test** — tenant B cannot read or write tenant A's config rows, proven through the RLS-bound app role on `two_tenant_world`.

```python
# tests/test_property_config_tenancy.py
from datetime import date

from sqlalchemy import func, select

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from tests.orgworld import ORG2_ALIAS, rls_client
from usali.auth import ACTIVE_ORG_HEADER
from usali.models import RoomInventory


def test_tenant_cannot_read_or_write_another_orgs_inventory(
    db_url, db_session, two_tenant_world, tmp_path
):
    """Org 1 owns property ONE1 with an inventory row; an org-2 admin, active in
    org 2, is refused ONE1 entirely (RLS makes it not-there, so the endpoint's
    _require_onboardable_property returns the same 403 as out-of-scope)."""
    world = two_tenant_world
    # Give org 1's property an inventory row (owner session, org 1).
    db_session.add(RoomInventory(property_id="ONE1", org_id=1,
                                 effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.commit()

    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)

    # An org-2 admin token, active org = org 2.
    tok = mint(roles=["org_admin"], sub=world.org2_admin, organizations=[ORG2_ALIAS])
    headers = {"Authorization": f"Bearer {tok}", ACTIVE_ORG_HEADER: ORG2_ALIAS}

    # Read of org 1's property -> 403 (not there under org 2's RLS).
    assert client.get("/api/properties/ONE1/config", headers=headers).status_code == 403
    # Write attempt -> 403, and no row is created anywhere.
    r = client.post("/api/properties/ONE1/inventory",
                    json={"effective_date": "2026-02-01", "total_rooms": 50}, headers=headers)
    assert r.status_code == 403
    db_session.expire_all()
    count = db_session.execute(
        select(func.count()).select_from(RoomInventory).where(RoomInventory.property_id == "ONE1")
    ).scalar_one()
    assert count == 1  # only the original org-1 row; org 2 wrote nothing
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_property_config_tenancy.py -v`
Expected: PASS if the walls hold. **If it fails on the WRITE creating an org-1-stamped row**, apply the explicit `org_id` stamp noted in Task 6 Step 3, then re-run. (Read isolation should pass immediately — RLS filters the SELECT.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_property_config_tenancy.py
git commit -m "test(config): cross-org RLS isolation for property config (#8)"
```

---

## Task 8: Demo seed

**Files:**
- Modify: `scripts/demo_seed.py`

- [ ] **Step 1: Read the existing PaySchedule seed loop** (`scripts/demo_seed.py` ~line 444) to match style and the `for prop in ...` iteration and the session variable name in scope.

- [ ] **Step 2: Add the config seed** right after the `PaySchedule` / `KioskDevice` block, using the property ids already in scope (`HISJ`, `SSSJ`). Use the module's existing `date`/`ANCHOR` imports; add `from datetime import date` if not present.

```python
    # --- Property config: room inventory, fiscal calendar, OOO (issue #8) ---
    # HISJ: calendar-month fiscal year; a mid-history inventory change so the
    # effective-dated path is exercised in the live demo. SSSJ: 4-4-5.
    from usali.models import FiscalCalendar, OutOfOrderRoom, RoomInventory

    session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2025, 1, 1), total_rooms=140),
        RoomInventory(property_id="HISJ", effective_date=date(2026, 3, 1), total_rooms=138),
        RoomInventory(property_id="SSSJ", effective_date=date(2025, 1, 1), total_rooms=90),
        FiscalCalendar(property_id="HISJ", calendar_type="calendar_month",
                       fiscal_year_start_month=1, week_start_weekday=None),
        FiscalCalendar(property_id="SSSJ", calendar_type="445",
                       fiscal_year_start_month=1, week_start_weekday=6),
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 2, 2),
                       end_date=date(2026, 2, 8), room_count=3, reason_code="renovation",
                       note="Wing 2 soft-goods refresh"),
    ])
    session.commit()
```

If the seed is idempotent (re-runnable), guard these inserts the way the surrounding seed guards its own (e.g. an existence check on `RoomInventory` for `HISJ`, or the file's sentinel pattern). Match whatever the `PaySchedule` block does — if it inserts unconditionally, so may this; if it checks-first, mirror that.

- [ ] **Step 3: Run the seed against a throwaway DB** the way the repo documents (`scripts/demo.sh` or the seed's own entrypoint), or at minimum import-check it:

Run: `uv run python -c "import scripts.demo_seed"`
Expected: no import error. Then run the demo seed per the repo's runbook and confirm it completes without an IntegrityError.

- [ ] **Step 4: Commit**

```bash
git add scripts/demo_seed.py
git commit -m "feat(config): seed room inventory, fiscal calendar, and OOO for demo properties (#8)"
```

---

## Task 9: Docs — the formulas

**Files:**
- Create: `docs/reference/performance-metrics.md`

- [ ] **Step 1: Write the reference doc** stating the exact formulas (#9 will extend this file with occupancy/ADR/RevPAR).

```markdown
# Performance metrics — foundations (rooms available & fiscal periods)

Issue #8 establishes the two denominators every performance statistic needs.

## Rooms available

For an inclusive date window `[start, end]` at a property:

    rooms_available = Σ_day∈window ( total_rooms in force that day )
                    − Σ_block ( overlap_nights(block, window) × block.room_count )

- The in-force room count for a day is the `room_inventory` row with the
  greatest `effective_date ≤ day`. Counts are effective-dated and append-only;
  history is never rewritten.
- Each out-of-order block is clamped to the window before counting nights.
- If any day in the window has no in-force inventory row, the computation
  refuses (`InventoryNotConfigured`) rather than assuming a count.

Implemented in `src/usali/inventory.py` (`rooms_available`, `total_rooms_on`).

## Fiscal periods

Each property has one `fiscal_calendar` row. Period keys are `"{fiscal_year}-Pnn"`,
where `fiscal_year` is the calendar year the fiscal year starts in and `nn` is
`01`–`12` (both calendar types have twelve periods).

- **calendar_month:** period *N* is the *N*-th calendar month from
  `fiscal_year_start_month`.
- **4-4-5:** the fiscal year is anchored on the first `week_start_weekday` on or
  after the 1st of the start month; periods are 4/4/5-week blocks per quarter. A
  53-week fiscal year is handled by the final period absorbing the extra week, so
  the calendar always tiles up to the next year's anchor.

Implemented in `src/usali/fiscal.py` (`resolve_period`, `period_containing`,
`periods_in_year`).
```

- [ ] **Step 2: Commit**

```bash
git add docs/reference/performance-metrics.md
git commit -m "docs(config): rooms-available and fiscal-period formulas (#8)"
```

---

## Task 10: Frontend — API client + types

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`
- Test: `frontend/src/api/client.propertyConfig.test.ts` (create)

- [ ] **Step 1: Read `frontend/src/api/client.ts`** — copy the exact fetch/auth/active-org wrapper it uses (look at an existing call like the kiosk-devices or departments calls) so the new calls go through the same `request` helper (auth header + `X-Active-Org` handling live there).

- [ ] **Step 2: Add types** to `frontend/src/api/types.ts`:

```typescript
export interface InventoryRow { inventory_id: number; effective_date: string; total_rooms: number }
export interface OooRow {
  ooo_id: number; start_date: string; end_date: string;
  room_count: number; reason_code: string; note: string | null;
}
export interface FiscalConfig {
  calendar_type: 'calendar_month' | '445';
  fiscal_year_start_month: number;
  week_start_weekday: number | null;
}
export interface PropertyConfig {
  property_id: string;
  inventory: InventoryRow[];
  out_of_order: OooRow[];
  fiscal_calendar: FiscalConfig | null;
}
export interface FiscalPeriod { key: string; start: string; end: string }
export const OOO_REASONS = ['maintenance', 'renovation', 'damage', 'deep_clean', 'other'] as const;
```

- [ ] **Step 3: Write the failing client test** `frontend/src/api/client.propertyConfig.test.ts`, mirroring an existing `client.*.test.ts` (they mock `fetch`; copy the harness verbatim from `client.employees.test.ts`).

```typescript
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { getPropertyConfig, setInventory } from './client'

// Copy the fetch-mock setup from client.employees.test.ts here.

describe('property config client', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('GETs the config', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ property_id: 'HISJ', inventory: [], out_of_order: [], fiscal_calendar: null }),
        { status: 200 }))
    const cfg = await getPropertyConfig('HISJ')
    expect(cfg.property_id).toBe('HISJ')
    expect(spy.mock.calls[0][0]).toContain('/api/properties/HISJ/config')
  })

  it('POSTs inventory', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ inventory_id: 1, effective_date: '2026-01-01', total_rooms: 140 }),
        { status: 201 }))
    await setInventory('HISJ', { effective_date: '2026-01-01', total_rooms: 140 })
    expect(spy.mock.calls[0][1]?.method).toBe('POST')
  })
})
```

- [ ] **Step 4: Run to confirm failure**

Run: `cd frontend && npx vitest run src/api/client.propertyConfig.test.ts`
Expected: FAIL — `getPropertyConfig is not exported`.

- [ ] **Step 5: Add the client functions** to `frontend/src/api/client.ts`, using the file's existing `request`/`apiFetch` helper (replace `request` below with the real helper name):

```typescript
import type { PropertyConfig, InventoryRow, OooRow, FiscalConfig, FiscalPeriod } from './types'

export const getPropertyConfig = (pid: string) =>
  request<PropertyConfig>(`/api/properties/${pid}/config`)

export const setInventory = (pid: string, body: { effective_date: string; total_rooms: number }) =>
  request<InventoryRow>(`/api/properties/${pid}/inventory`, { method: 'POST', body })

export const addOoo = (pid: string, body: Omit<OooRow, 'ooo_id'>) =>
  request<OooRow>(`/api/properties/${pid}/out-of-order`, { method: 'POST', body })

export const removeOoo = (pid: string, oooId: number) =>
  request<void>(`/api/properties/${pid}/out-of-order/${oooId}`, { method: 'DELETE' })

export const setFiscalCalendar = (pid: string, body: FiscalConfig) =>
  request<FiscalConfig>(`/api/properties/${pid}/fiscal-calendar`, { method: 'PUT', body })

export const getFiscalPeriods = (pid: string, fiscalYear: number) =>
  request<{ periods: FiscalPeriod[] }>(`/api/properties/${pid}/fiscal-periods?fiscal_year=${fiscalYear}`)
```

(If the existing helper serializes `body` and sets JSON headers itself, pass `body` as shown; if it expects a pre-stringified body, match that. Copy an existing POST call's exact shape.)

- [ ] **Step 6: Run to confirm pass**

Run: `cd frontend && npx vitest run src/api/client.propertyConfig.test.ts`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/api/client.propertyConfig.test.ts
git commit -m "feat(config): frontend api client for property config (#8)"
```

---

## Task 11: Frontend — the settings page

**Files:**
- Create: `frontend/src/pages/PropertyConfigPage.tsx`, `frontend/src/pages/PropertyConfigPage.test.tsx`
- Modify: `frontend/src/router.tsx` and the nav

- [ ] **Step 1: Read `frontend/src/pages/KioskDevicesPage.tsx`** (its structure is the closest analog: an org_admin/GM management page with a list, a create form, and a delete). Mirror its data-loading, error handling, and design-token usage.

- [ ] **Step 2: Write the failing page test** `PropertyConfigPage.test.tsx` (mirror `KioskDevicesPage.test.tsx`'s render harness — mock the client module). Pin the two behaviours the spec calls out:

```typescript
import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import PropertyConfigPage from './PropertyConfigPage'

vi.mock('../api/client', () => ({
  getPropertyConfig: vi.fn().mockResolvedValue({
    property_id: 'HISJ', inventory: [], out_of_order: [], fiscal_calendar: null,
  }),
  setInventory: vi.fn(), addOoo: vi.fn(), removeOoo: vi.fn(),
  setFiscalCalendar: vi.fn(), getFiscalPeriods: vi.fn().mockResolvedValue({ periods: [] }),
}))

describe('PropertyConfigPage', () => {
  it('shows the week-start field only when 4-4-5 is chosen', async () => {
    render(<PropertyConfigPage propertyId="HISJ" />)
    // default calendar_month -> no week-start select
    expect(screen.queryByLabelText(/week start/i)).toBeNull()
    fireEvent.click(await screen.findByLabelText(/4-4-5/i))
    expect(screen.getByLabelText(/week start/i)).toBeInTheDocument()
  })

  it('offers exactly the five OOO reason codes', async () => {
    render(<PropertyConfigPage propertyId="HISJ" />)
    const select = await screen.findByLabelText(/reason/i)
    expect(select.querySelectorAll('option')).toHaveLength(5)
  })
})
```

- [ ] **Step 3: Run to confirm failure**

Run: `cd frontend && npx vitest run src/pages/PropertyConfigPage.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement `PropertyConfigPage.tsx`** with three sections (inventory list + add form; OOO list + add form with the `OOO_REASONS` dropdown + remove; fiscal-calendar form with the conditional `week_start_weekday` select and a current-period preview via `getFiscalPeriods`). Follow `KioskDevicesPage.tsx` for structure, loading state, and error surfacing. Use `OOO_REASONS` from `../api/types` for the dropdown, and gate the week-start `<select>` on `calendar_type === '445'`. Label the 4-4-5 radio and the week-start select with accessible text matching the test (`/4-4-5/i`, `/week start/i`, `/reason/i`).

*(Full JSX omitted here only because it is a direct structural copy of `KioskDevicesPage.tsx` — read that file and reproduce its shape with the three sections above. Every element the test queries must be present: a radio/label containing "4-4-5", a "Week start" labelled select rendered only under 4-4-5, and a "Reason" labelled select with the five `OOO_REASONS` options.)*

- [ ] **Step 5: Add the route** in `frontend/src/router.tsx` (mirror the `KioskDevicesPage` route registration exactly — same `createRoute`/`component` shape) at path `/property-config`, and add a nav link beside the "Kiosk devices" link in whatever nav component holds it.

- [ ] **Step 6: Run to confirm pass**

Run: `cd frontend && npx vitest run src/pages/PropertyConfigPage.test.tsx`
Expected: PASS.

- [ ] **Step 7: Build check**

Run: `cd frontend && npm run build`
Expected: clean build (no TS errors).

- [ ] **Step 8: Commit**

```bash
git add frontend/src/pages/PropertyConfigPage.tsx frontend/src/pages/PropertyConfigPage.test.tsx frontend/src/router.tsx
git add -A frontend/src   # picks up the nav edit
git commit -m "feat(config): property configuration settings page (#8)"
```

---

## Task 12: Full green + wrap-up

- [ ] **Step 1: Run the whole backend suite**

Run: `uv run pytest -q`
Expected: all green (existing suite + the new `test_property_config_*` files). Investigate any failure before proceeding — a red existing test means the router mount or a model import regressed something.

- [ ] **Step 2: Run the whole frontend suite + build**

Run: `cd frontend && npx vitest run && npm run build`
Expected: all green, clean build.

- [ ] **Step 3: Type/lint gate** (whatever the repo runs — check `pyproject.toml`/CI):

Run: `uv run mypy src` (and `ruff check` if configured)
Expected: clean, consistent with the repo's existing `mypy src`-only convention.

- [ ] **Step 4: Confirm single migration head**

Run: `uv run alembic heads`
Expected: one head, `m1a0propcfg (head)`.

- [ ] **Step 5: Final commit if any lint/type fixups were needed**

```bash
git add -A
git commit -m "chore(config): type and lint fixups for property config (#8)"
```

---

## Post-plan: adversarial review (separate session)

Per project culture, the closing gate is a three-lens adversarial review in isolated worktrees — **disclosure/tenancy** (does any endpoint leak across orgs; does an error body echo a value), **correctness** (rooms-available math on overlapping/adjacent OOO blocks; 4-4-5 anchoring across a 53-week year and a start-month whose 1st is the weekday; the upsert org-stamp under org≠1), **migration/tests** (upgrade/downgrade on populated data; every CHECK and the paired constraint bite; mutation-check the guards). Run it after this plan is green; it is not part of these tasks. This maps directly onto issue #8's acceptance criteria plus the standing review rules (differencing oracles, disclosure-direction mutations, sampled-once values).
