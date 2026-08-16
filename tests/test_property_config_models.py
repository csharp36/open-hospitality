"""Property config models: room inventory, out-of-order rooms, fiscal calendar
(issue #8). Three new OrgScoped tables — CHECK constraints, uniques, and
composite (org_id, property_id) FKs pinned on the real schema, plus a
populated-downgrade pin proving the migration round-trips with rows present."""

import os
import re
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from tests.orgwall import ensure_app_role  # noqa: E402
from usali.models import (  # noqa: E402
    ADR_ROOM_BASES,
    CALENDAR_TYPES,
    OOO_REASON_CODES,
    FiscalCalendar,
    Organization,
    OutOfOrderRoom,
    Property,
    RoomInventory,
)


def _property(session, pid="HISJ"):
    # Property.org_id carries a real FK onto organization (l1a0orgid) — org 1
    # must exist before a property can reference it. `merge` so a second call
    # in the same test (a different property_id, same org) is a no-op re-fetch
    # rather than a duplicate-PK insert.
    session.merge(Organization(org_id=1, name="Org"))
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
    assert OOO_REASON_CODES == frozenset({
        "maintenance", "renovation", "damage", "deep_clean", "other",
        "do_not_rent", "owner_occupied",
    })
    assert CALENDAR_TYPES == frozenset({"calendar_month", "445"})


def test_check_constraints_match_the_vocab_frozensets(db_session):
    # The DB CHECK IN-lists and the Python frozensets are otherwise two
    # independent literals (the CHECK pinned via compare_metadata, the frozenset
    # via the vocab test above). Couple them directly here so that adding a code
    # to the frozenset — and the vocab test — while forgetting the CHECK (or the
    # reverse) fails HERE, honouring the models.py "kept in sync by test" claim.
    def _in_list(conname: str) -> set[str]:
        ddl = db_session.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": conname},
        ).scalar_one()
        return set(re.findall(r"'([^']*)'", ddl))

    assert _in_list("ck_ooo_reason_code") == set(OOO_REASON_CODES)
    assert _in_list("ck_fiscal_type") == set(CALENDAR_TYPES)


def test_out_of_order_accepts_dnr_reason_codes(db_session):
    _property(db_session)
    db_session.add_all([
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 2, 1), end_date=date(2026, 2, 28),
                       room_count=2, reason_code="do_not_rent"),
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 3, 1), end_date=date(2026, 3, 31),
                       room_count=1, reason_code="owner_occupied"),
    ])
    db_session.commit()
    assert db_session.execute(select(func.count()).select_from(OutOfOrderRoom)).scalar_one() == 2


# --- populated-downgrade pin -------------------------------------------------
#
# Mirrors the i2a0settle / j2a0crmdemand pins in tests/test_migration_on_populated_data.py
# (and the F1/L4 pins in test_l8_remediation.py / test_l4_org_grants.py): the
# shared `db_session`/`db_engine` fixtures are SESSION-scoped and shared by the
# whole suite, so downgrading them here would corrupt every other test's
# schema. Every existing populated-downgrade test in this repo instead spins
# its OWN throwaway container, migrates it independently, and disposes of it —
# this test follows that convention exactly rather than the generic sketch.


def _cfg(url: str) -> Config:
    # migrations/env.py reads USALI_DB_URL, not the config's sqlalchemy.url --
    # setting only the latter silently migrates whatever database that env var
    # already points at (the test_migration_on_populated_data convention).
    os.environ["USALI_DB_URL"] = url
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_migration_round_trips_with_rows_present():
    """Seed a row in each new table, then downgrade one step and back up.
    A no-op downgrade would leave the tables and fail the re-upgrade's create;
    dropping the tables while rows exist (the I6 lesson) is exercised rather
    than an empty-table downgrade, which every real downgrade will face."""
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            ensure_app_role(url)
            cfg = _cfg(url)
            command.upgrade(cfg, "head")
            engine = create_engine(url)
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO organization (org_id, name) VALUES (1, 'Org')"
                ))
                conn.execute(text(
                    "INSERT INTO property (property_id, org_id, name, pms_source) "
                    "VALUES ('HISJ', 1, 'HISJ', 'OPERA')"
                ))
                conn.execute(text(
                    "INSERT INTO room_inventory (property_id, effective_date, total_rooms) "
                    "VALUES ('HISJ', '2026-01-01', 140)"
                ))
                conn.execute(text(
                    "INSERT INTO out_of_order_room (property_id, start_date, end_date, "
                    "room_count, reason_code) VALUES "
                    "('HISJ', '2026-02-01', '2026-02-07', 3, 'renovation')"
                ))
                conn.execute(text(
                    "INSERT INTO fiscal_calendar (property_id, calendar_type, "
                    "fiscal_year_start_month, week_start_weekday) VALUES "
                    "('HISJ', '445', 1, 6)"
                ))

            command.downgrade(cfg, "l9a0deptfk")
            with engine.begin() as conn:
                tables = {r[0] for r in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ))}
            assert "room_inventory" not in tables
            assert "out_of_order_room" not in tables
            assert "fiscal_calendar" not in tables

            command.upgrade(cfg, "head")
            with engine.begin() as conn:
                counts = {
                    table: conn.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    ).scalar_one()
                    for table in ("room_inventory", "out_of_order_room", "fiscal_calendar")
                }
            engine.dispose()
            assert counts == {"room_inventory": 0, "out_of_order_room": 0, "fiscal_calendar": 0}, (
                "the tables are recreated empty -- dropped rows are the honest "
                "pre-migration shape, same as the I2/J2 table-drop downgrades"
            )
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous


# --- CHECK <-> frozenset coupling (#9 review) --------------------------------
#
# The literal DB CHECKs are a hand-transcribed mirror of the Python frozensets
# (models.py says so explicitly: "kept literal on purpose"). Nothing but a test
# holds the two copies in agreement, so a CHECK that silently narrows (drops a
# member) or a frozenset that grows without the migration keeping up would ship
# undetected. #8's review added exactly this pin for OOO/CALENDAR; #9 widened
# the DNR vocab and added the ADR-basis CHECK, so both must be coupled the same
# way. Reads the LIVE constraint via pg_get_constraintdef so it pins the schema
# the suite actually migrated to, not the migration source.


def _check_in_list(session, conname):
    ddl = session.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
        {"n": conname},
    ).scalar_one()
    return set(re.findall(r"'([^']*)'", ddl))


def test_dnr_reason_check_matches_frozenset(db_session):
    assert _check_in_list(db_session, "ck_ooo_reason_code") == set(OOO_REASON_CODES)


def test_adr_basis_check_matches_frozenset(db_session):
    assert _check_in_list(db_session, "ck_stat_config_adr_basis") == set(ADR_ROOM_BASES)


# --- the m2 downgrade RESTORES the narrowed 5-value DNR CHECK (#9 review) -----
#
# m2a0perffoundations widens ck_ooo_reason_code from the original 5 codes to 7
# (adding do_not_rent + owner_occupied). Its downgrade must put the 5-value
# CHECK back — a no-op downgrade that left 7 values would be a silent forward
# leak, and would let a `do_not_rent` row exist at a revision that predates the
# code ever knowing about it. Spins its OWN throwaway container (the shared
# db_session/db_engine are session-scoped; downgrading them corrupts the whole
# suite) exactly like test_migration_round_trips_with_rows_present above.


def test_m2_downgrade_restores_the_five_value_dnr_check():
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            ensure_app_role(url)
            cfg = _cfg(url)
            command.upgrade(cfg, "head")
            # Step ONE revision back: m2a0perffoundations -> m1a0propcfg. At
            # m1a0propcfg the out_of_order_room table EXISTS (it was created
            # there) but with the ORIGINAL 5-value CHECK that m2's downgrade
            # must have restored.
            command.downgrade(cfg, "m1a0propcfg")
            engine = create_engine(url)
            try:
                # FK anchors: composite (org_id, property_id) -> property.
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO organization (org_id, name) VALUES (1, 'Org')"
                    ))
                    conn.execute(text(
                        "INSERT INTO property (property_id, org_id, name, pms_source) "
                        "VALUES ('HISJ', 1, 'HISJ', 'OPERA')"
                    ))
                # A widened-vocabulary code must be REJECTED by the restored
                # 5-value CHECK. If m2's downgrade were a no-op (leaving 7
                # values) this INSERT would SUCCEED and the test would fail.
                with pytest.raises(IntegrityError) as exc:
                    with engine.begin() as conn:
                        conn.execute(text(
                            "INSERT INTO out_of_order_room "
                            "(property_id, org_id, start_date, end_date, room_count, "
                            "reason_code) VALUES "
                            "('HISJ', 1, '2026-02-01', '2026-02-07', 3, 'do_not_rent')"
                        ))
                assert "ck_ooo_reason_code" in str(exc.value)
                # A pre-widening code is still ACCEPTED — proving the constraint
                # is the restored 5-value one, not a dropped/absent constraint.
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO out_of_order_room "
                        "(property_id, org_id, start_date, end_date, room_count, "
                        "reason_code) VALUES "
                        "('HISJ', 1, '2026-03-01', '2026-03-07', 1, 'renovation')"
                    ))
            finally:
                engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous
