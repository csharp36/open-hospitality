"""Run the migration chain against a POPULATED database.

The rest of the suite migrates a fresh EMPTY container, so every backfill
statement in the chain -- the load-bearing `INSERT ... SELECT FROM employee` in
`e1a0`, the defensive demotion in `e1e0`, and the hire-date correction in `e1i0`
-- processes ZERO ROWS in every test run. They were pinned by nothing, and three
separate adversarial reviews each had to verify them by hand against a throwaway
Postgres. One of them found a real bug that way: assignments backdated to the
payroll anchor for employees hired months later, which put people in pay runs
for periods predating their employment AND admitted them at kiosks on backdated
business dates.

This spins its OWN container so it can start at the pre-E1 revision, insert data
of the shape a real pilot database holds, and migrate forward -- which is the
only way any of those statements execute at all.

Deliberately slow (a second container plus a full chain, ~30s) and deliberately
separate from the session-scoped fixture, whose database is already at head.
"""

import os
from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from tests.orgwall import ensure_app_role, ensure_provisioner_role  # noqa: E402

# The revision immediately before E1 -- where a real pilot database would have
# been sitting when this work started.
_PRE_E1 = "178480aede62"
_ANCHOR = date(2026, 1, 5)


def _cfg(url: str) -> Config:
    # `migrations/env.py` reads USALI_DB_URL, not the config's sqlalchemy.url --
    # setting only the latter silently migrates whatever database that env var
    # already points at. conftest sets it for the same reason.
    #
    # The fixture RESTORES it afterwards. It is process-global and the session
    # fixture set it first: leaving this module's throwaway URL behind pointed
    # ten unrelated tests at a container that no longer exists.
    os.environ["USALI_DB_URL"] = url
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def populated_url():
    """A database migrated to pre-E1, populated, then migrated to head."""
    previous = os.environ.get("USALI_DB_URL")
    try:
        yield from _populate()
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous


def _populate():
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        url = pg.get_connection_url()
        # The RLS-bound app role, before any upgrade reaches l2a0rlswall
        # (which refuses without it — CREATE ROLE is cluster-level).
        ensure_app_role(url)
        ensure_provisioner_role(url)   # b1a0provrole refuses without it too
        command.upgrade(_cfg(url), _PRE_E1)

        engine = create_engine(url)
        with engine.begin() as conn:
            # One statement per execute: psycopg refuses a multi-statement
            # string with bound-parameter support enabled. `wage_jurisdiction`
            # does not exist yet at this revision -- e1b0 adds it.
            conn.execute(text(
                "INSERT INTO organization (org_id, name) VALUES (1, 'El Sendero LLC')"
            ))
            conn.execute(text(
                "INSERT INTO property (property_id, org_id, name, pms_source) "
                "VALUES ('SJCES', 1, 'HIE', 'OPERA')"
            ))
            conn.execute(text(
                "INSERT INTO department (department_id, property_id, name) "
                "VALUES (1, 'SJCES', 'Front Office')"
            ))
            # The shapes that matter, each a different interaction with the
            # anchor. `hire_date` is the one the backfill ignored.
            #
            # `pay_rate` is EncryptedString, so what is stored is ciphertext.
            # The E2 backfill copies it VERBATIM -- ciphertext to ciphertext,
            # never decrypting -- so an opaque placeholder is not a shortcut
            # here, it is exactly what the column holds. Employee 4 has none,
            # which exercises the backfill's `WHERE pay_rate IS NOT NULL`.
            for emp_id, name, hire, term, rate in (
                (1, "Long timer",  date(2025, 3, 1),  None,  "enc:20.00"),
                (2, "Post anchor", date(2026, 7, 15), None,  "enc:21.00"),
                (3, "On anchor",   _ANCHOR,           None,  "enc:22.00"),
                (4, "No hire date", None,             None,  None),  # unrated
                (5, "Left early",  date(2025, 6, 1),  date(2025, 12, 1), "enc:23.00"),
                (6, "Hired then left", date(2026, 6, 1), date(2026, 6, 20), "enc:24.00"),
            ):
                conn.execute(
                    text("""
                        INSERT INTO employee (employee_id, full_name, property_id,
                                              department_id, pay_type, hire_date,
                                              termination_date, pay_rate)
                        VALUES (:i, :n, 'SJCES', 1, 'hourly', :h, :t, :r)
                    """),
                    {"i": emp_id, "n": name, "h": hire, "t": term, "r": rate},
                )
            # E5: sealed payroll profiles of the three shapes the deposit
            # backfill must handle. Envelopes are opaque Text to the schema,
            # so placeholders are exactly what the columns hold -- the E5
            # backfill copies ciphertext VERBATIM, never parsing, and the
            # bytes surviving untouched is precisely what the tests assert.
            for emp_id, acct, routing, atype in (
                (1, "env:acct1", "env:rout1", "savings"),   # complete pair
                (2, "env:acct2", None,        None),        # half-sealed
                (3, None,        None,        None),        # ssn-only profile
                (4, None,        None,        "savings"),   # type-only claim
            ):
                conn.execute(
                    text("""
                        INSERT INTO employee_payroll_profile
                               (employee_id, ssn_sealed, bank_account_sealed,
                                bank_routing_sealed, account_type)
                        VALUES (:i, 'env:ssn', :a, :r, :t)
                    """),
                    {"i": emp_id, "a": acct, "r": routing, "t": atype},
                )
            # H2: a REAL pre-H pay run riding the chain — h2a0wagepath adds
            # `sick_hours` with a 0 backfill (pre-G3 runs paid no sick; 0 is
            # the truth, not a placeholder) and `payload_fingerprint` with NO
            # backfill (NULL reads stale — one re-send self-heals). Money
            # columns are EncryptedString: opaque placeholders are exactly
            # what the columns hold. department_id stays NULL — the pre-C3
            # legacy shape rides too.
            conn.execute(text(
                "INSERT INTO pay_run (pay_run_id, property_id, period_start, "
                "period_end, check_date, status, provider, created_by) "
                "VALUES (1, 'SJCES', '2026-06-08', '2026-06-21', "
                "'2026-06-26', 'processed', 'gusto', 'adm')"
            ))
            conn.execute(text(
                "INSERT INTO pay_run_line (pay_run_id, employee_id, hours, "
                "gross, employee_taxes, employer_taxes, net) "
                "VALUES (1, 1, 80.00, 'enc:g', 'enc:et', 'enc:rt', 'enc:n')"
            ))
            conn.execute(text(
                "INSERT INTO provider_employee_ref (employee_id, provider, "
                "provider_employee_id) VALUES (1, 'gusto', 'g-emp-1')"
            ))
            # F1: a REAL pre-F punch riding the chain — f1a0face adds the
            # match columns with NO backfill, so history must come out
            # NULL/NULL and every new CHECK must admit it (before F8 the
            # module migrated an always-empty punch table).
            conn.execute(text(
                "INSERT INTO kiosk_device (device_id, property_id, name, "
                "token_hash, enrolled_by) "
                "VALUES (1, 'SJCES', 'iPad', :h, 'adm')"
            ), {"h": "a" * 64})
            conn.execute(text(
                "INSERT INTO punch (employee_id, kiosk_device_id, "
                "punch_type, punched_at, business_date) "
                "VALUES (1, 1, 'clock_in', "
                "'2026-07-07T16:00:00+00:00', '2026-07-07')"
            ))
        engine.dispose()

        command.upgrade(_cfg(url), "head")
        yield url


def _assignment(url: str, employee_id: int) -> dict:
    engine = create_engine(url)
    with engine.begin() as conn:
        row = conn.execute(
            text("""
                SELECT property_id, effective_from, effective_to, is_primary, status
                  FROM employee_assignment WHERE employee_id = :i
            """),
            {"i": employee_id},
        ).mappings().one()
    engine.dispose()
    return dict(row)


def test_the_chain_applies_to_a_populated_database(populated_url):
    """The baseline claim: it runs at all with real rows present. Every other
    test in the suite migrates an empty database, where the backfill is a no-op
    and could be arbitrarily broken without anything noticing."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM employee_assignment")
        ).scalar_one()
    engine.dispose()
    assert count == 6, "every employee must come out of the backfill with a placement"


def test_an_employee_hired_after_the_anchor_does_not_start_before_they_were_hired(
    populated_url
):
    """THE bug `e1i0hiredate` exists for.

    `e1a0` set `effective_from` to the anchor unconditionally. It guarded the
    OTHER end -- `effective_to` uses `GREATEST(termination_date + 1, ANCHOR)` --
    and never applied the symmetric guard to the start. So someone hired
    2026-07-15 got an assignment starting 2026-01-05: they appeared in
    `employee_ids_with_primary_at` for pay periods predating their employment,
    and `employee_serves_property` admitted them at a kiosk on backdated
    business dates.
    """
    a = _assignment(populated_url, 2)
    assert a["effective_from"] == date(2026, 7, 15), (
        "employment starts when the person was hired, not when the migration ran"
    )


def test_a_pre_anchor_hire_still_starts_at_the_anchor(populated_url):
    """The correction must only move starts FORWARD. Backdating someone's
    assignment to their 2025 hire date would reach into periods this system
    never costed and has no timecards for -- the anchor is deliberately the
    earliest representable employment date."""
    assert _assignment(populated_url, 1)["effective_from"] == _ANCHOR


def test_a_hire_exactly_on_the_anchor_is_unchanged(populated_url):
    """The boundary. `GREATEST(hire_date, ANCHOR)` and the `hire_date > ANCHOR`
    guard must agree here, and neither may shift it by a day."""
    assert _assignment(populated_url, 3)["effective_from"] == _ANCHOR


def test_an_unknown_hire_date_falls_back_to_the_anchor(populated_url):
    """NULL `hire_date` is the common case for rows that predate this system.
    It must not become NULL `effective_from` -- the column is NOT NULL and the
    date predicates have no answer for a missing start."""
    a = _assignment(populated_url, 4)
    assert a["effective_from"] == _ANCHOR


def test_a_pre_anchor_termination_yields_an_interval_in_force_on_no_date(
    populated_url
):
    """Someone who left before the anchor cannot be given a live assignment, and
    cannot be given an INVERTED one either -- the date predicates would answer
    incoherently. Zero-width is the honest representation."""
    a = _assignment(populated_url, 5)
    assert a["effective_from"] == a["effective_to"] == _ANCHOR


def test_a_hire_and_departure_both_after_the_anchor_keep_a_valid_interval(
    populated_url
):
    """Both guards active at once, which is where an interval inverts if the
    start is moved forward without regard for the end. Hired 06-01, left 06-20:
    the assignment must cover exactly that window, start <= end."""
    a = _assignment(populated_url, 6)
    assert a["effective_from"] == date(2026, 6, 1)
    assert a["effective_to"] == date(2026, 6, 21), "exclusive end: last day + 1"
    assert a["effective_from"] < a["effective_to"]


def test_no_assignment_has_an_inverted_interval(populated_url):
    """The invariant across every shape at once. An inverted interval is not
    caught by any constraint -- `effective_to` is merely nullable -- and it makes
    `in_effect_on` answer False on every date, silently removing someone from
    payroll and from every scope query."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        bad = conn.execute(text("""
            SELECT count(*) FROM employee_assignment
             WHERE effective_to IS NOT NULL AND effective_to < effective_from
        """)).scalar_one()
    engine.dispose()
    assert bad == 0


def test_the_backfill_gives_every_employee_exactly_one_primary(populated_url):
    """Two primaries is the double-payment state. The partial unique index only
    forbids two OPEN active ones, so the backfill producing a duplicate for an
    already-terminated employee would slip past it."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT employee_id, count(*) AS n
              FROM employee_assignment WHERE is_primary
             GROUP BY employee_id HAVING count(*) <> 1
        """)).fetchall()
    engine.dispose()
    assert rows == [], f"employees with a primary count other than 1: {rows}"


# --- E2: the rate backfill, on the same populated database -------------------


def _rates(url: str, employee_id: int) -> list[dict]:
    engine = create_engine(url)
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT r.rate_type, r.effective_from, r.effective_to
                  FROM assignment_rate r
                  JOIN employee_assignment a
                    ON a.assignment_id = r.assignment_id
                 WHERE a.employee_id = :i
                 ORDER BY r.rate_type, r.effective_from
            """),
            {"i": employee_id},
        ).mappings().all()
    engine.dispose()
    return [dict(r) for r in rows]


def test_every_rated_employee_gets_exactly_one_regular_rate(populated_url):
    """One row per assignment, `regular` only. `ot`/`dot`/`holiday` have no
    source to backfill FROM -- the engine derives OT and DT from the
    jurisdiction multipliers and nothing has ever recorded a holiday rate -- so
    leaving them absent keeps today's numbers unchanged."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        by_type = dict(conn.execute(text(
            "SELECT rate_type, count(*) FROM assignment_rate GROUP BY rate_type"
        )).all())
    engine.dispose()
    assert set(by_type) == {"regular"}, f"unexpected rate types backfilled: {by_type}"
    # Five of six employees carry a rate; the unrated one gets no row rather
    # than a zero one. Zero is a PRICED value and would enter the suppression
    # population as real cost.
    assert by_type["regular"] == 5


def test_a_rate_never_starts_before_the_placement_it_prices(populated_url):
    """`GREATEST(a.effective_from, ANCHOR)`. The employee hired 2026-07-15 has an
    assignment starting then (e1i0), so their RATE must start then too -- not at
    the anchor. Pricing a placement before it existed is the same defect e1i0
    fixed one table over."""
    rates = _rates(populated_url, 2)
    assert rates, "a rated employee must get a rate row"
    assert rates[0]["effective_from"] == date(2026, 7, 15)


def test_a_rate_never_outlives_the_placement_it_prices(populated_url):
    """`LEAST(..., effective_to)`. Employee 6 was hired 06-01 and left 06-20, so
    their assignment is [06-01, 06-21). A rate starting after 06-21 would be an
    interval no date predicate can reason about."""
    rates = _rates(populated_url, 6)
    assert rates
    r = rates[0]
    assert r["effective_from"] == date(2026, 6, 1)
    assert r["effective_to"] == date(2026, 6, 21)
    assert r["effective_from"] < r["effective_to"]


def test_no_rate_has_an_inverted_interval(populated_url):
    """Across every shape at once. Nothing constrains this -- `effective_to` is
    merely nullable -- and an inverted rate resolves on no date, so the employee
    silently becomes uncostable rather than mispriced."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        bad = conn.execute(text("""
            SELECT count(*) FROM assignment_rate
             WHERE effective_to IS NOT NULL AND effective_to < effective_from
        """)).scalar_one()
    engine.dispose()
    assert bad == 0


def test_two_open_rates_of_one_type_are_rejected(populated_url):
    """The partial unique index. Two simultaneously-open `regular` rates is a
    genuine ambiguity about what someone is paid; the ordinary raise (close one,
    open the next) leaves a CLOSED row beside an OPEN one and must stay legal --
    which is why the index is scoped to `effective_to IS NULL`."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        assignment_id = conn.execute(text(
            "SELECT assignment_id FROM assignment_rate WHERE effective_to IS NULL LIMIT 1"
        )).scalar_one()
    with _pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO assignment_rate
                           (assignment_id, rate_type, amount, effective_from)
                    VALUES (:a, 'regular', 'x', DATE '2026-09-01')
                """),
                {"a": assignment_id},
            )
    # ...but closing one and opening the next is the ordinary raise, and legal.
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO assignment_rate
                       (assignment_id, rate_type, amount, effective_from, effective_to)
                VALUES (:a, 'regular', 'x', DATE '2025-01-01', DATE '2025-06-01')
            """),
            {"a": assignment_id},
        )
        conn.execute(text(
            "DELETE FROM assignment_rate WHERE effective_to = DATE '2025-06-01'"
        ))
    engine.dispose()


# --- E3: the classification backfill, on the same populated database ---------


def test_the_status_backfill_agrees_with_the_termination_dates(populated_url):
    """`e3a0class` derives the enum from the date it is REPLACING as the status
    signal: employees 5 and 6 left (termination_date set) and must come out
    `terminated`; the other four must come out `active`. Nothing on a pilot
    database can backfill `leave` or `inactive` -- the incumbent's data cannot
    express them, which is half the reason the enum exists."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        statuses = dict(conn.execute(text(
            "SELECT employee_id, employment_status FROM employee ORDER BY employee_id"
        )).all())
    engine.dispose()
    assert statuses == {
        1: "active", 2: "active", 3: "active", 4: "active",
        5: "terminated", 6: "terminated",
    }


def test_every_existing_employee_is_backfilled_payroll_complete(populated_url):
    """TRUE for every pre-E3 row: these people are being paid today, and a flag
    that blocks the pilot's next pay run the day it deploys is a false alarm.
    (New hires get the MODEL default, False -- that direction is pinned by the
    preflight tests, not here, because no post-migration insert happens in this
    module.)"""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        incomplete = conn.execute(text(
            "SELECT count(*) FROM employee WHERE NOT payroll_data_complete"
        )).scalar_one()
    engine.dispose()
    assert incomplete == 0


def test_the_new_columns_are_actually_not_null(populated_url):
    """THE surviving mutant of the E3 review: deleting the migration's two
    `nullable=False` steps passed all 1013 tests, because every test schema
    comes from `alembic upgrade head` and nothing ever asserted the shape.
    The model would promise NOT NULL while the database accepted NULLs from
    any raw-SQL fixup or ETL — the recorded 'same revision, two schemas'
    class, on columns that gate the pay run and the kiosk. Behavioural pin:
    a NULL in either column must be REFUSED by the real schema."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)
    for column_sql in (
        "employment_status, payroll_data_complete, termination_date) "
        "VALUES (102, 'No status', 'hourly', NULL, TRUE, NULL)",
        "employment_status, payroll_data_complete, termination_date) "
        "VALUES (103, 'No flag', 'hourly', 'active', NULL, NULL)",
    ):
        with _pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO employee (employee_id, full_name, pay_type, "
                    + column_sql
                ))
    engine.dispose()


def test_downgrade_refuses_atomically_when_an_excluded_value_exists(populated_url):
    """The downgrade's advertised behaviour, previously pinned by nothing:
    narrowing pay_type back to varchar(10) must REFUSE (not truncate) while a
    19-char `exclude_from_payroll` exists, and the refusal must be atomic —
    transactional DDL rolls back the column drops, leaving the database fully
    at e3a0class rather than half-dropped. Self-contained: the refusal rolls
    everything back and the finally-block removes the extra row, so tests
    after it (H2 appended several) see head state untouched."""
    import pytest as _pytest
    from sqlalchemy.exc import DBAPIError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        head_before = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        conn.execute(text(
            "INSERT INTO employee (employee_id, full_name, pay_type, "
            "employment_status, payroll_data_complete) "
            "VALUES (110, 'Olive Owner', 'exclude_from_payroll', 'active', TRUE)"
        ))
    try:
        with _pytest.raises((DBAPIError, Exception)) as exc_info:
            command.downgrade(_cfg(populated_url), "e2b0droprate")
        assert "too long" in str(exc_info.value), (
            "the refusal must be the length check, not some other failure"
        )
        with engine.begin() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            cols = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'employee'"
                ))
            }
            deposit_table = conn.execute(text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'deposit_account'"
            )).scalar_one()
        assert version == head_before, "a failed downgrade must not move the revision"
        assert {"employment_status", "payroll_data_complete", "full_part_time",
                "i9_submitted_on", "w4_submitted_on"} <= cols, (
            "transactional DDL must roll the drops back — no half-schema"
        )
        # The refusal happens SEVERAL migrations into the chain (e5a0's table
        # drop runs first and succeeds) — atomicity must cover the whole run,
        # or a failed downgrade leaves the revision claiming a table that is
        # gone. Verified: the deposit table survives the rolled-back chain.
        assert deposit_table == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM employee WHERE employee_id = 110"))
        engine.dispose()


def test_status_and_termination_date_cannot_disagree(populated_url):
    """The CHECK constraint, exercised in both directions on the real schema.
    The enum answers the status question and the date answers the date
    question; a row where they disagree would make the kiosk (which reads the
    enum) and the schedule builder (which compares the date) disagree about
    whether one person is employed."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)
    with _pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO employee (employee_id, full_name, pay_type,
                                      employment_status, payroll_data_complete,
                                      termination_date)
                VALUES (100, 'Dated but active', 'hourly',
                        'active', TRUE, DATE '2026-06-01')
            """))
    with _pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO employee (employee_id, full_name, pay_type,
                                      employment_status, payroll_data_complete,
                                      termination_date)
                VALUES (101, 'Terminated but dateless', 'hourly',
                        'terminated', TRUE, NULL)
            """))
    engine.dispose()


# --- E5: the deposit-account backfill, on the same populated database --------


def _deposit_rows(url: str, employee_id: int) -> list[dict]:
    engine = create_engine(url)
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT ordinal, allocation_type, allocation_value, account_type,
                       sealed_account, sealed_routing, legacy_sealed
                  FROM deposit_account WHERE employee_id = :i ORDER BY ordinal
            """),
            {"i": employee_id},
        ).mappings().all()
    engine.dispose()
    return [dict(r) for r in rows]


def test_the_deposit_backfill_copies_ciphertext_verbatim(populated_url):
    """The backfill moves envelopes it CANNOT verify -- it never opens, so a
    corrupted copy is undetectable until a real pay run tries to send money.
    Byte-identity is the whole assertion. One row, remainder (a single
    account receiving everything is what the old schema meant), legacy flag
    set so the open path derives the OLD aad."""
    [row] = _deposit_rows(populated_url, 1)
    assert row["ordinal"] == 1
    assert row["allocation_type"] == "remainder"
    assert row["allocation_value"] is None
    assert row["legacy_sealed"] is True
    assert row["sealed_account"] == "env:acct1"
    assert row["sealed_routing"] == "env:rout1"
    assert row["account_type"] == "savings"


def test_a_half_sealed_profile_backfills_incomplete_not_absent(populated_url):
    """A profile holding only the account envelope still becomes a row -- the
    '' placeholder in the missing slot fails SealedEnvelope.parse loudly and
    reads as not-on-file, so preflight names the employee exactly as the
    missing COLUMN made it do before E5. Dropping the row instead would turn
    a half-entered employee into a no-deposit-data employee silently."""
    [row] = _deposit_rows(populated_url, 2)
    assert row["sealed_account"] == "env:acct2"
    assert row["sealed_routing"] == ""
    assert row["account_type"] is None, (
        "unknown stays unknown — the backfill must not invent an ACH account "
        "class the employee never stated (preflight names it instead)"
    )


def test_a_bankless_profile_backfills_no_deposit_row(populated_url):
    """An ssn-only profile has no deposit destination to represent. A row of
    pure placeholders would claim one exists."""
    assert _deposit_rows(populated_url, 3) == []


def test_the_profile_bank_columns_are_gone_at_head(populated_url):
    """e5b0dropbank. Leaving them would leave TWO places a deposit
    destination can live, and the retiring one reads more naturally at a
    call site (the e2b0 lesson). The envelopes live on in deposit_account --
    byte-identity pinned above."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        cols = {
            r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'employee_payroll_profile'"
            ))
        }
    engine.dispose()
    assert "bank_account_sealed" not in cols
    assert "bank_routing_sealed" not in cols
    assert "account_type" not in cols
    assert {"ssn_sealed", "tax_elections_sealed"} <= cols


def test_deposit_schema_refuses_illegal_chains(populated_url):
    """The three E5 constraints plus the NOT NULLs, exercised on the REAL
    schema (the E3 review's surviving mutant was exactly an unpinned
    nullable=False). Every INSERT here must be refused."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)
    refusals = (
        # second remainder for employee 1 (partial unique index)
        "(1, 2, 'remainder', NULL, 'checking', 'e', 'e', FALSE)",
        # remainder carrying a value (CHECK)
        "(4, 1, 'remainder', 50.00, 'checking', 'e', 'e', FALSE)",
        # percent carrying NO value (CHECK, other direction)
        "(4, 1, 'percent', NULL, 'checking', 'e', 'e', FALSE)",
        # duplicate (employee, ordinal) for employee 1 (unique constraint)
        "(1, 1, 'amount', 25.00, 'checking', 'e', 'e', FALSE)",
        # NULL ordinal (NOT NULL)
        "(4, NULL, 'remainder', NULL, 'checking', 'e', 'e', FALSE)",
        # NULL sealed_account (NOT NULL)
        "(4, 1, 'remainder', NULL, 'checking', NULL, 'e', FALSE)",
    )
    for values in refusals:
        with _pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO deposit_account (employee_id, ordinal, "
                    "allocation_type, allocation_value, account_type, "
                    "sealed_account, sealed_routing, legacy_sealed) VALUES "
                    + values
                ))
    engine.dispose()


def test_a_type_only_profile_backfills_a_placeholder_row(populated_url):
    """The fourth shape (the E5 review's untested branch): account_type set,
    nothing sealed. The row preserves the one fact the profile held — the
    stated type — with '' in both never-sealed slots, so preflight names the
    employee instead of the destination-claim silently vanishing. GET status
    honestly reports both slots not-on-file."""
    [row] = _deposit_rows(populated_url, 4)
    assert row["account_type"] == "savings"
    assert row["sealed_account"] == "" and row["sealed_routing"] == ""
    assert row["legacy_sealed"] is True


def test_e5b0_downgrade_restores_only_the_restorable(populated_url):
    """The review's surviving mutant M4: the e5b0 downgrade restore SQL was
    deletable with a green suite. Pinned in both directions here by RUNNING
    the downgrade: a single still-legacy chain comes back byte-identical
    (with NULLIF undoing the '' placeholders), a multi-row employee's
    columns stay EMPTY, and unknown account_type survives as NULL rather
    than a fabricated choice. Self-contained: it downgrades (through the
    later revisions, H2's included) and re-upgrades to head, restoring
    state, and removes its own extra row — tests after it (H2 appended
    several) see head untouched."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        # Make employee 1 a multi-row chain — the not-restorable shape. The
        # ordinal-1 row loses its remainder flag FIRST (the partial unique
        # index refuses two remainders, even transiently within order).
        conn.execute(text(
            "UPDATE deposit_account SET allocation_type = 'amount', "
            "allocation_value = 50.00 WHERE employee_id = 1 AND ordinal = 1"
        ))
        conn.execute(text(
            "INSERT INTO deposit_account (employee_id, ordinal, "
            "allocation_type, allocation_value, account_type, sealed_account, "
            "sealed_routing, legacy_sealed) VALUES "
            "(1, 2, 'remainder', NULL, 'checking', 'env:new', 'env:new', FALSE)"
        ))
    try:
        command.downgrade(_cfg(populated_url), "e5a0deposit")
        with engine.begin() as conn:
            rows = {
                r[0]: (r[1], r[2], r[3])
                for r in conn.execute(text(
                    "SELECT employee_id, bank_account_sealed, "
                    "bank_routing_sealed, account_type "
                    "FROM employee_payroll_profile ORDER BY employee_id"
                ))
            }
        # Single still-legacy half-sealed chain: byte-identical restore,
        # '' placeholder back to NULL, unknown type STAYS NULL.
        assert rows[2] == ("env:acct2", None, None)
        # Type-only claim restores its one fact.
        assert rows[4] == (None, None, "savings")
        # Multi-row employee: honestly EMPTY, not a guessed collapse.
        assert rows[1] == (None, None, None)
    finally:
        command.upgrade(_cfg(populated_url), "head")
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM deposit_account "
                "WHERE employee_id = 1 AND ordinal = 2"
            ))
            conn.execute(text(
                "UPDATE deposit_account SET allocation_type = 'remainder', "
                "allocation_value = NULL WHERE employee_id = 1 AND ordinal = 1"
            ))
        engine.dispose()


# --- E4: the sick-leave ledger — empty by decision, sign-checked -------------


def test_the_sick_ledger_exists_empty_and_sign_checked(populated_url):
    """e4a0sick creates the ledger EMPTY — no backfill, because the
    incumbent's balances may be running the pre-2024 24/48 ruleset and
    statutory hours must not be invented (opening balances are audited human
    adjustments instead). The sign CHECK is the semantics: a positive usage
    row would silently GRANT hours; every wrong-signed shape is refused by
    the real schema, and the NOT NULLs hold (the E3 surviving-mutant
    lesson)."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM sick_leave_ledger")
        ).scalar_one()
    assert count == 0, "the ledger backfills NOTHING — that is the decision"

    refusals = (
        "(1, 'accrual', -1.00, DATE '2026-07-01')",   # accrual must be > 0
        "(1, 'usage_void', -1.00, DATE '2026-07-01')",  # void must be > 0
        "(1, 'accrual', 0.00, DATE '2026-07-01')",
        "(1, 'usage', 8.00, DATE '2026-07-01')",      # usage must be < 0
        "(1, 'adjustment', 0.00, DATE '2026-07-01')", # adjustment nonzero
        "(1, 'accrual', 1.00, NULL)",                 # effective_on NOT NULL
        "(1, NULL, 1.00, DATE '2026-07-01')",         # entry_type NOT NULL
    )
    for values in refusals:
        with _pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO sick_leave_ledger "
                    "(employee_id, entry_type, hours, effective_on) VALUES "
                    + values
                ))
    # ...and the legal shapes insert (then clean up).
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO sick_leave_ledger (employee_id, entry_type, hours, "
            "effective_on) VALUES (1, 'accrual', 0.27, DATE '2026-07-01'), "
            "(1, 'usage', -0.27, DATE '2026-07-02'), (1, 'usage_void', 0.27, DATE '2026-07-02'), "
            "(1, 'adjustment', 20.32, DATE '2026-07-03')"
        ))
        conn.execute(text("DELETE FROM sick_leave_ledger WHERE employee_id = 1"))
    engine.dispose()


def test_the_sick_ledger_carries_recorded_caps_and_dedupes_usage(populated_url):
    """Post-review schema: cap_hours exists (the fold clamps at RECORDED
    caps, never today's), and the partial unique index refuses an identical
    usage retry on the real schema."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'sick_leave_ledger'"
        ))}
        assert "cap_hours" in cols
        conn.execute(text(
            "INSERT INTO sick_leave_ledger (employee_id, entry_type, hours, "
            "effective_on) VALUES (1, 'usage', -4.00, DATE '2026-07-05')"
        ))
    with _pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO sick_leave_ledger (employee_id, entry_type, "
                "hours, effective_on) VALUES "
                "(1, 'usage', -4.00, DATE '2026-07-05')"
            ))
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sick_leave_ledger WHERE employee_id = 1"))
    engine.dispose()


def test_a_pre_f_punch_comes_out_null_null(populated_url):
    """The f1a0face match columns admit history: the punch inserted at the
    pre-E1 revision arrives at head with NULL match_state and NULL
    match_score — 'recorded before matching existed' — under all four new
    CHECKs (SQL's NULL-passing IN semantics is the load-bearing detail)."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT match_state, match_score FROM punch WHERE employee_id = 1"
        )).one()
    engine.dispose()
    assert row == (None, None)


# --- H2: what the run paid, and what the payload shipped ---------------------


def test_a_pre_h_line_backfills_zero_sick_hours(populated_url):
    """The backfill truth (decision 5): pre-G3 runs paid no sick, so 0 is a
    FACT about this line, not a placeholder — the content-based §246(n)
    guard (H6) will read it as 'this run paid zero sick hours' and compare
    the ledger against it. A wrong backfill here is a wrong blocker verdict
    on every pre-H period forever. The combined `hours` snapshot must ride
    untouched beside it."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT hours, sick_hours FROM pay_run_line WHERE employee_id = 1"
        )).one()
    engine.dispose()
    assert row[0] == 80
    assert row[1] == 0


def test_a_pre_h_ref_comes_out_null_fingerprint(populated_url):
    """NO data backfill for the fingerprint (decision 8): computing one here
    would require hashing ciphertext this migration has no business reading
    column-by-column, and a WRONG stored fingerprint reads 'not stale' — a
    silently skipped re-send of edited deposit data. NULL reads STALE: one
    re-send self-heals every pre-H ref. Fail toward the extra send, never
    the skipped one."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        fp = conn.execute(text(
            "SELECT payload_fingerprint FROM provider_employee_ref "
            "WHERE employee_id = 1 AND provider = 'gusto'"
        )).scalar_one()
    engine.dispose()
    assert fp is None


def test_sick_hours_is_not_null_sign_checked_and_defaults_to_zero(populated_url):
    """The shape, on the REAL schema (the E3 surviving-mutant lesson).
    Explicit NULL and a negative are both refused — a negative would let a
    void REDUCE what a run claims it paid, and the H6 comparison direction
    flips on sign. An insert that OMITS the column lands 0: H2 ships before
    H5, so `execute_pay_run` does not write the column yet — the server
    default is what keeps the wage path working through that window, and 0
    stays the honest value for a run that recorded no sick."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)
    for values in (
        # explicit NULL (NOT NULL)
        "(1, 2, 80.00, NULL, 'enc:g', 'enc:et', 'enc:rt', 'enc:n')",
        # negative sick (CHECK)
        "(1, 2, 80.00, -4.00, 'enc:g', 'enc:et', 'enc:rt', 'enc:n')",
    ):
        with _pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO pay_run_line (pay_run_id, employee_id, "
                    "hours, sick_hours, gross, employee_taxes, "
                    "employer_taxes, net) VALUES " + values
                ))
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO pay_run_line (pay_run_id, employee_id, hours, "
            "gross, employee_taxes, employer_taxes, net) "
            "VALUES (1, 3, 24.00, 'enc:g', 'enc:et', 'enc:rt', 'enc:n')"
        ))
        landed = conn.execute(text(
            "SELECT sick_hours FROM pay_run_line "
            "WHERE pay_run_id = 1 AND employee_id = 3"
        )).scalar_one()
        conn.execute(text(
            "DELETE FROM pay_run_line WHERE pay_run_id = 1 AND employee_id = 3"
        ))
    engine.dispose()
    assert landed == 0


def test_h2_downgrade_drops_both_columns_and_reupgrade_rebackfills(populated_url):
    """What makes this downgrade lossless: both columns are re-derivable —
    0 is the recorded pre-H truth and NULL self-heals through one re-send —
    so dropping them loses nothing the pre-H schema can express. Downgrades
    ONE step, verifies both columns are gone (a half-dropped pair would
    leave the model and the schema disagreeing at f1a0face), then restores
    head and re-verifies the backfill."""
    engine = create_engine(populated_url)
    command.downgrade(_cfg(populated_url), "f1a0face")
    with engine.begin() as conn:
        line_cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pay_run_line'"
        ))}
        ref_cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'provider_employee_ref'"
        ))}
    assert "sick_hours" not in line_cols
    assert "payload_fingerprint" not in ref_cols
    command.upgrade(_cfg(populated_url), "head")
    with engine.begin() as conn:
        sick = conn.execute(text(
            "SELECT sick_hours FROM pay_run_line WHERE employee_id = 1"
        )).scalar_one()
        fp = conn.execute(text(
            "SELECT payload_fingerprint FROM provider_employee_ref "
            "WHERE employee_id = 1"
        )).scalar_one()
    engine.dispose()
    assert sick == 0
    assert fp is None


# --- I2: the settlement table — the missing terminal resolution --------------


def test_the_settlement_table_exists_empty_and_positive_checked(populated_url):
    """i2a0settle creates `wage_settlement` EMPTY — no backfill, because no
    settlements exist historically (the mechanism is new with this pillar);
    inventing rows would fabricate money history. The sign CHECK is the
    semantics on the REAL schema (the E3 surviving-mutant lesson): a
    negative settlement would UN-PAY someone, and a ZERO one records an act
    that had no effect (the F6 acknowledgment rule — the endpoint refuses
    zero deltas, and the schema refuses them independently). The FKs refuse
    a settlement against a run or employee that never existed, and the note
    is BOUNDED free text (the TimecardAdjustment.reason posture). Several
    rows per (run, employee) are LEGAL — re-drift settles again — so no
    unique constraint may sneak in."""
    import pytest as _pytest
    from sqlalchemy.exc import DataError, IntegrityError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM wage_settlement")
        ).scalar_one()
    assert count == 0, "the table backfills NOTHING — that is the decision"

    refusals = (
        "(1, 1, 0.00, 'adm', 'manual check #88')",    # zero: act w/o effect
        "(1, 1, -4.00, 'adm', 'manual check #88')",   # negative: un-pays
        "(1, 1, NULL, 'adm', 'manual check #88')",    # hours NOT NULL
        "(1, 1, 4.00, NULL, 'manual check #88')",     # actor NOT NULL
        "(1, 1, 4.00, 'adm', NULL)",                  # note NOT NULL
        "(999999, 1, 4.00, 'adm', 'manual check #88')",  # no such run
        "(1, 999999, 4.00, 'adm', 'manual check #88')",  # no such employee
    )
    for values in refusals:
        with _pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO wage_settlement (pay_run_id, employee_id, "
                    "hours, actor_subject, note) VALUES " + values
                ))
    # The note is bounded, not unbounded text.
    with _pytest.raises(DataError):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO wage_settlement (pay_run_id, employee_id, "
                "hours, actor_subject, note) VALUES (1, 1, 4.00, 'adm', :n)"
            ), {"n": "x" * 301})
    # ...and the legal shape inserts TWICE for one (run, employee) —
    # re-drift settles again; created_at lands from the server default.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO wage_settlement (pay_run_id, employee_id, hours, "
            "actor_subject, note) VALUES "
            "(1, 1, 4.00, 'adm', 'manual check #88'), "
            "(1, 1, 0.25, 'adm', 'residual after late punch')"
        ))
        stamped = conn.execute(text(
            "SELECT count(*) FROM wage_settlement "
            "WHERE created_at IS NOT NULL"
        )).scalar_one()
        conn.execute(text("DELETE FROM wage_settlement"))
    engine.dispose()
    assert stamped == 2


def test_i2_downgrade_drops_the_table_and_reupgrade_recreates_it_empty(
    populated_url,
):
    """Downgrade drops the table — WHILE settlements exist in it (the I6
    migration lens: the previous test cleaned up after itself, so the
    downgrade used to be exercised only against an empty table). Dropping
    recorded settlements is the honest pre-I shape, and the failure
    direction is LOUD either way: post-I code on the downgraded schema
    fails every preflight on the missing table (nothing can submit), and
    rolled-back code re-raises exactly the blockers those settlements
    had cleared. Re-upgrade recreates it empty."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO wage_settlement "
            "(pay_run_id, employee_id, hours, actor_subject, note) VALUES "
            "(1, 1, 4.00, 'adm', 'rows present THROUGH the downgrade')"
        ))
    command.downgrade(_cfg(populated_url), "h2a0wagepath")
    with engine.begin() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ))}
    assert "wage_settlement" not in tables
    command.upgrade(_cfg(populated_url), "head")
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM wage_settlement")
        ).scalar_one()
    engine.dispose()
    assert count == 0


# --- J2: the CRM demand tables, created empty, append-only -------------------


def test_the_crm_tables_exist_empty_and_are_constraint_checked(populated_url):
    """`crm_pull_batch` + `crm_demand_snapshot` (plan decision 3): created
    EMPTY (no pulls exist historically), append-only by construction. The
    unique is (batch_id, stay_date) — a batch that says two different
    things about one day is corrupt — and deliberately NOTHING wider: a
    re-pull is a NEW batch with rows for the same stay dates, and pace
    depends on that history existing. Figures are nullable (None = the
    provider does not speak that dimension) but never negative; labels
    are bounded; the horizon must not be inverted."""
    import pytest as _pytest
    from sqlalchemy.exc import DataError, IntegrityError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        for table in ("crm_pull_batch", "crm_demand_snapshot"):
            assert conn.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608
            ).scalar_one() == 0

    def refuse(sql, exc=IntegrityError):
        with _pytest.raises(exc):
            with engine.begin() as conn:
                conn.execute(text(sql))

    # Batch refusals: FK, inverted horizon, NULL provider.
    refuse(
        "INSERT INTO crm_pull_batch (property_id, provider, horizon_start, "
        "horizon_end) VALUES ('NOPE', 'delphi', '2026-08-03', '2026-08-09')"
    )
    refuse(
        "INSERT INTO crm_pull_batch (property_id, provider, horizon_start, "
        "horizon_end) VALUES ('SJCES', 'delphi', '2026-08-09', '2026-08-03')"
    )
    refuse(
        "INSERT INTO crm_pull_batch (property_id, provider, horizon_start, "
        "horizon_end) VALUES ('SJCES', NULL, '2026-08-03', '2026-08-09')"
    )

    with engine.begin() as conn:
        batch_id = conn.execute(text(
            "INSERT INTO crm_pull_batch (property_id, provider, "
            "horizon_start, horizon_end) VALUES "
            "('SJCES', 'delphi', '2026-08-03', '2026-08-09') "
            "RETURNING batch_id"
        )).scalar_one()
        stamped = conn.execute(text(
            "SELECT pulled_at IS NOT NULL FROM crm_pull_batch "
            "WHERE batch_id = :b"
        ), {"b": batch_id}).scalar_one()
    assert stamped

    # Snapshot rows: absent dimensions (NULLs) are legal; negatives are
    # not, per figure; labels are bounded; one row per (batch, stay_date).
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
            "rooms_on_books, group_rooms, event_covers, labels) VALUES "
            f"({batch_id}, '2026-08-06', 132, 40, NULL, 'Acme Corp Annual')"
        ))
        conn.execute(text(
            "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
            "rooms_on_books, group_rooms, event_covers, labels) VALUES "
            f"({batch_id}, '2026-08-07', NULL, NULL, NULL, '')"
        ))
    for column_values in (
        "132, -1, NULL", "-1, NULL, NULL", "NULL, NULL, -1",
    ):
        refuse(
            "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
            "rooms_on_books, group_rooms, event_covers, labels) VALUES "
            f"({batch_id}, '2026-08-08', {column_values}, '')"
        )
    refuse(  # unknown batch
        "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
        "rooms_on_books, group_rooms, event_covers, labels) VALUES "
        "(999999, '2026-08-08', 1, NULL, NULL, '')"
    )
    refuse(  # a batch may not disagree with itself about one day
        "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
        "rooms_on_books, group_rooms, event_covers, labels) VALUES "
        f"({batch_id}, '2026-08-06', 100, NULL, NULL, '')"
    )
    refuse(  # labels are BOUNDED operator text
        "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
        "rooms_on_books, group_rooms, event_covers, labels) VALUES "
        f"({batch_id}, '2026-08-08', 1, NULL, NULL, '{'x' * 301}')",
        exc=DataError,
    )

    # THE APPEND-ONLY PIN: a re-pull is a new batch carrying the SAME stay
    # dates — legal, because pace is a comparison across batches and any
    # unique wide enough to forbid this would destroy the data that makes
    # pace computable.
    with engine.begin() as conn:
        second = conn.execute(text(
            "INSERT INTO crm_pull_batch (property_id, provider, "
            "horizon_start, horizon_end) VALUES "
            "('SJCES', 'delphi', '2026-08-03', '2026-08-09') "
            "RETURNING batch_id"
        )).scalar_one()
        conn.execute(text(
            "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
            "rooms_on_books, group_rooms, event_covers, labels) VALUES "
            f"({second}, '2026-08-06', 140, 45, NULL, 'Acme Corp Annual')"
        ))
        count = conn.execute(text(
            "SELECT count(*) FROM crm_demand_snapshot "
            "WHERE stay_date = '2026-08-06'"
        )).scalar_one()
    assert count == 2

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM crm_demand_snapshot"))
        conn.execute(text("DELETE FROM crm_pull_batch"))
    engine.dispose()


def test_j2_downgrade_drops_both_tables_and_reupgrade_recreates_empty(
    populated_url,
):
    """Downgrade drops both tables WHILE rows exist (the I6 lesson: carry
    rows through the drop, never exercise it empty). Dropping pulled
    demand history is the honest pre-J shape — demand is decision-support
    data, no guard reads it, so nothing goes silent. Re-upgrade recreates
    both empty."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        batch_id = conn.execute(text(
            "INSERT INTO crm_pull_batch (property_id, provider, "
            "horizon_start, horizon_end) VALUES "
            "('SJCES', 'tripleseat', '2026-08-03', '2026-08-09') "
            "RETURNING batch_id"
        )).scalar_one()
        conn.execute(text(
            "INSERT INTO crm_demand_snapshot (batch_id, stay_date, "
            "rooms_on_books, group_rooms, event_covers, labels) VALUES "
            f"({batch_id}, '2026-08-06', NULL, NULL, 120, 'Nguyen Wedding')"
        ))
    command.downgrade(_cfg(populated_url), "i2a0settle")
    with engine.begin() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ))}
    assert "crm_pull_batch" not in tables
    assert "crm_demand_snapshot" not in tables
    command.upgrade(_cfg(populated_url), "head")
    with engine.begin() as conn:
        for table in ("crm_pull_batch", "crm_demand_snapshot"):
            assert conn.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608
            ).scalar_one() == 0
    engine.dispose()


# --- J3: crm_ref on property (the mapping change) ----------------------------


def test_j3_crm_ref_lands_nullable_and_bounded(populated_url):
    """`property.crm_ref` (plan decision 6): the provider-side identity,
    declared in the registry like wage_jurisdiction. NULLABLE on purpose —
    NULL is the honest state for "no CRM speaks for this property", and
    the pull path refuses it BY NAME rather than guessing. Bounded: a ref
    is an identifier, not a payload."""
    import pytest as _pytest
    from sqlalchemy.exc import DataError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        nullable, existing = conn.execute(text(
            "SELECT is_nullable, "
            "(SELECT count(*) FROM property WHERE crm_ref IS NOT NULL) "
            "FROM information_schema.columns "
            "WHERE table_name = 'property' AND column_name = 'crm_ref'"
        )).one()
    assert nullable == "YES"
    assert existing == 0  # pre-J3 rows come out NULL, never a default

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE property SET crm_ref = 'DELPHI-HISJ' "
            "WHERE property_id = 'SJCES'"
        ))
    with _pytest.raises(DataError):
        with engine.begin() as conn:
            conn.execute(text(
                f"UPDATE property SET crm_ref = '{'x' * 101}' "
                "WHERE property_id = 'SJCES'"
            ))
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE property SET crm_ref = NULL WHERE property_id = 'SJCES'"
        ))
    engine.dispose()


def test_j3_downgrade_drops_the_column_with_data_present(populated_url):
    """Downgrade WITH a ref set (the I6 lesson: carry data through the
    drop). Losing a crm_ref on rollback silences nothing — the pull path
    that reads it is gone with the code — and re-upgrade restores the
    column NULL, to be re-seeded from the registry file."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE property SET crm_ref = 'DELPHI-HISJ' "
            "WHERE property_id = 'SJCES'"
        ))
    command.downgrade(_cfg(populated_url), "j2a0crmdemand")
    with engine.begin() as conn:
        gone = conn.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'property' AND column_name = 'crm_ref'"
        )).scalar_one()
    assert gone == 0
    command.upgrade(_cfg(populated_url), "head")
    with engine.begin() as conn:
        refs = conn.execute(text(
            "SELECT count(*) FROM property WHERE crm_ref IS NOT NULL"
        )).scalar_one()
    assert refs == 0
    engine.dispose()


# --- L1: the org wall groundwork — org_id on every tenant-owned table --------

# The exclusion judgment, pinned (recorded in the l1a0orgid docstring):
# platform-curated reference data with no org dimension. Everything else
# in the public schema except alembic_version must carry org_id NOT NULL.
# `invite` joins this set for a different reason (D-B3, Task 3 of the B1
# plan): an invite precedes any tenant, so it is plain Base, not OrgScoped —
# it carries no org_id at all, by design, not because it is reference data.
# `otp_challenge` joins for the same reason (D-B6, Task 4): OTP gates signup,
# before any tenant exists.
# `pms_interest_request` joins for the same reason (Part-2): platform-level PMS
# demand read across orgs — it stores the requesting workspace as an `org_alias`
# STRING, deliberately carrying no org_id at all.
_L1_ORG_INDEPENDENT = {
    "usali_schedule", "usali_mapping_dictionary", "invite", "otp_challenge",
    "pms_interest_request",
}

# Tables the pre-E1 population actually filled — the backfill must land
# org 1 on real rows here, not just add an empty column.
_L1_POPULATED = {
    "organization", "property", "department", "employee",
    "employee_assignment", "assignment_rate", "deposit_account",
    "employee_payroll_profile", "pay_run", "pay_run_line",
    "provider_employee_ref", "kiosk_device", "punch",
}


def test_l1_every_tenant_table_carries_org_id_and_the_backfill_landed_org_1(
    populated_url,
):
    """The denormalization claim itself, on the REAL schema: every table in
    the public schema is org-scoped unless it is one of the two enumerated
    reference tables (or alembic_version) — when in doubt, tenant-owned —
    and every row that rode the chain came out org 1, because all existing
    data belongs to the single A2.1 org by construction. NOT NULL is
    asserted per-table (the E3 surviving-mutant lesson: a deleted
    nullable=False step is invisible to an empty-schema suite)."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        tables = {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ))}
        org_cols = {
            r[0]: r[1] for r in conn.execute(text(
                "SELECT table_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'org_id'"
            ))
        }
        scoped = tables - _L1_ORG_INDEPENDENT - {"alembic_version"}
        assert scoped <= set(org_cols), (
            f"tenant-owned tables missing org_id: {sorted(scoped - set(org_cols))}"
        )
        assert all(org_cols[t] == "NO" for t in scoped), (
            f"org_id must be NOT NULL everywhere: "
            f"{sorted(t for t in scoped if org_cols[t] != 'NO')}"
        )
        # The judgment's other half: the reference tables must NOT have
        # grown the column — a mapping dictionary keyed per-org without a
        # key restructure would be a lie about what the unique enforces.
        assert not (_L1_ORG_INDEPENDENT & set(org_cols))

        populated_seen = set()
        for table in sorted(scoped):
            total, off_org = conn.execute(text(
                f"SELECT count(*), count(*) FILTER (WHERE org_id <> 1) "
                f"FROM {table}"  # noqa: S608
            )).one()
            assert off_org == 0, f"{table}: {off_org} rows backfilled off org 1"
            if total:
                populated_seen.add(table)
    engine.dispose()
    assert _L1_POPULATED <= populated_seen, (
        "the populated world must exercise the backfill on real rows: "
        f"missing {sorted(_L1_POPULATED - populated_seen)}"
    )


def test_l1_an_insert_omitting_org_id_lands_the_founding_org(populated_url):
    """The no-behavioral-change guarantee: until L2/L3 bind a real org
    context to sessions, every existing write path omits org_id — the
    server default routes those rows to org 1, the founding tenant by
    construction. (Unlike wage_jurisdiction, this default cannot encode a
    wrong decision while exactly one org exists; L2's RLS turns it into a
    refusal the moment it could.)"""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        landed = conn.execute(text(
            "INSERT INTO audit_event (actor_subject, action, resource_type, "
            "resource_id) VALUES ('adm', 'l1-pin', 'employee', '1') "
            "RETURNING org_id"
        )).scalar_one()
        conn.execute(text("DELETE FROM audit_event WHERE action = 'l1-pin'"))
    engine.dispose()
    assert landed == 1


def test_l1_a_cross_org_reference_refuses_on_the_composite_fk(populated_url):
    """THE FK-bypass world (D1's known subtlety): Postgres runs FK checks
    with owner privileges, so RLS alone cannot stop a buggy write from
    referencing another org's parent row. Where the reference carries
    money or PII, the composite (org_id, x_id) FK makes the SCHEMA refuse.
    Every insert below defaults to org 1 and names an org-2 parent; every
    one must be refused — and the control shows the same shape succeeds
    when the orgs agree."""
    from sqlalchemy.exc import IntegrityError

    engine = create_engine(populated_url)

    refusals = (
        # pay-run lines -> employee: org 1's run would PAY org 2's person.
        "INSERT INTO pay_run_line (pay_run_id, employee_id, hours, gross, "
        "employee_taxes, employer_taxes, net) "
        "VALUES (1, 900, 8.00, 'enc:g', 'enc:et', 'enc:rt', 'enc:n')",
        # ...and -> pay_run: org 2 injecting a line INTO org 1's run.
        "INSERT INTO pay_run_line (org_id, pay_run_id, employee_id, hours, "
        "gross, employee_taxes, employer_taxes, net) "
        "VALUES (2, 1, 900, 8.00, 'enc:g', 'enc:et', 'enc:rt', 'enc:n')",
        # wage_settlement -> employee: settling money onto the wrong org.
        "INSERT INTO wage_settlement (pay_run_id, employee_id, hours, "
        "actor_subject, note) VALUES (1, 900, 4.00, 'adm', 'x')",
        # role_assignment -> property: an RBAC grant over another org's hotel.
        "INSERT INTO role_assignment (keycloak_subject, role, property_id) "
        "VALUES ('sub-rival', 'gm', 'RIVAL')",
        # the timecard chain: org 1 card for org 2's employee...
        "INSERT INTO timecard (employee_id, period_start, period_end, status) "
        "VALUES (900, '2026-06-08', '2026-06-21', 'open')",
        # ...and an org-1 punch rolled into org 2's card.
        "INSERT INTO punch (employee_id, kiosk_device_id, punch_type, "
        "punched_at, business_date, timecard_id) "
        "VALUES (1, 1, 'clock_in', '2026-07-08T16:00:00+00:00', "
        "'2026-07-08', 900)",
        # the PII satellites: a deposit chain attached across the wall.
        "INSERT INTO deposit_account (employee_id, ordinal, allocation_type, "
        "sealed_account, sealed_routing) VALUES (900, 1, 'remainder', 'e', 'e')",
        # the employment record that decides whose pay run issues a check.
        "INSERT INTO employee_assignment (employee_id, property_id, status, "
        "is_primary, effective_from) VALUES (900, 'SJCES', 'active', TRUE, "
        "'2026-06-01')",
    )
    # Setup lives INSIDE the try: a half-built rival world must still be
    # torn down (the cleanup DELETEs are all WHERE org_id = 2, safe on any
    # partial state), or it leaks into the module-scoped database.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO organization (org_id, name) VALUES (2, 'Rival LLC')"
            ))
            conn.execute(text(
                "INSERT INTO property (property_id, org_id, name, pms_source) "
                "VALUES ('RIVAL', 2, 'Rival Hotel', 'OPERA')"
            ))
            conn.execute(text(
                "INSERT INTO employee (employee_id, org_id, full_name, "
                "pay_type, employment_status, payroll_data_complete) "
                "VALUES (900, 2, 'Rival Worker', 'hourly', 'active', TRUE)"
            ))
            # The org-2 timecard is itself the first control: a composite
            # reference where the orgs AGREE inserts fine.
            conn.execute(text(
                "INSERT INTO timecard (timecard_id, org_id, employee_id, "
                "period_start, period_end, status) "
                "VALUES (900, 2, 900, '2026-06-08', '2026-06-21', 'open')"
            ))
        for sql in refusals:
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(text(sql))
        # The control, same shape as the last refusal with the orgs agreeing.
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO employee_assignment (org_id, employee_id, "
                "property_id, status, is_primary, effective_from) "
                "VALUES (2, 900, 'RIVAL', 'active', TRUE, '2026-06-01')"
            ))
    finally:
        with engine.begin() as conn:
            for cleanup in (
                "DELETE FROM employee_assignment WHERE org_id = 2",
                "DELETE FROM timecard WHERE org_id = 2",
                "DELETE FROM employee WHERE org_id = 2",
                "DELETE FROM property WHERE org_id = 2",
                "DELETE FROM organization WHERE org_id = 2",
            ):
                conn.execute(text(cleanup))
        engine.dispose()


def test_l1_kc_org_alias_lands_null_bounded_and_unique(populated_url):
    """`organization.kc_org_alias` (plan decision 3): the join key to the
    Keycloak organization alias, resolved DB-side in L3 — nothing numeric
    from a token is ever an org_id. Existing orgs come out NULL (no alias
    has been declared; inventing one would bind the org to a Keycloak
    object that does not exist). Bounded — an alias is an identifier —
    and UNIQUE: two orgs answering to one alias would make alias->org
    resolution ambiguous, which is the exact guessing L3 refuses."""
    from sqlalchemy.exc import DataError, IntegrityError

    engine = create_engine(populated_url)
    with engine.begin() as conn:
        declared = conn.execute(text(
            "SELECT count(*) FROM organization WHERE kc_org_alias IS NOT NULL"
        )).scalar_one()
    assert declared == 0, "pre-L1 orgs come out NULL, never a fabricated alias"

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO organization (org_id, name) VALUES (2, 'Rival LLC')"
            ))
            # Two aliasless orgs are legal: NULLs are distinct in the unique.
            conn.execute(text(
                "UPDATE organization SET kc_org_alias = 'pilot' WHERE org_id = 1"
            ))
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE organization SET kc_org_alias = 'pilot' "
                    "WHERE org_id = 2"
                ))
        with pytest.raises(DataError):
            with engine.begin() as conn:
                conn.execute(text(
                    f"UPDATE organization SET kc_org_alias = '{'x' * 101}' "
                    "WHERE org_id = 2"
                ))
    finally:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE organization SET kc_org_alias = NULL WHERE org_id = 1"
            ))
            conn.execute(text("DELETE FROM organization WHERE org_id = 2"))
        engine.dispose()


def test_l1_downgrade_carries_rows_through_and_reupgrade_rebackfills(
    populated_url,
):
    """Downgrade WITH the full populated world present (the I6 lesson) —
    dropping org_id must carry every row through, restore the original
    single-column FKs under their ORIGINAL names (older downgrades drop
    them BY NAME; a renamed restore would strand any deeper rollback),
    and drop kc_org_alias. Re-upgrade re-lands org 1 on everything."""
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        before = {
            table: conn.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608
            ).scalar_one()
            for table in ("employee", "punch", "pay_run_line",
                          "deposit_account", "assignment_rate")
        }
    command.downgrade(_cfg(populated_url), "j3a0crmref")
    with engine.begin() as conn:
        org_tables = {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'org_id'"
        ))}
        alias_gone = conn.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'organization' "
            "AND column_name = 'kc_org_alias'"
        )).scalar_one()
        after = {
            table: conn.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608
            ).scalar_one()
            for table in before
        }
        restored = {r[0] for r in conn.execute(text(
            "SELECT conname FROM pg_constraint WHERE contype = 'f' "
            "AND conname IN ('timecard_employee_id_fkey', "
            "'fk_punch_timecard', 'fk_pay_run_line_department', "
            "'fk_timecard_adjustment_property', "
            "'wage_settlement_employee_id_fkey')"
        ))}
    assert org_tables == {"organization", "property"}, (
        "the pre-L1 world: only property carries org_id"
    )
    assert alias_gone == 0
    assert after == before, "rows must ride the downgrade untouched"
    assert restored == {
        "timecard_employee_id_fkey", "fk_punch_timecard",
        "fk_pay_run_line_department", "fk_timecard_adjustment_property",
        "wage_settlement_employee_id_fkey",
    }
    command.upgrade(_cfg(populated_url), "head")
    with engine.begin() as conn:
        off_org = conn.execute(text(
            "SELECT count(*) FROM employee WHERE org_id <> 1"
        )).scalar_one()
        rows = conn.execute(text("SELECT count(*) FROM employee")).scalar_one()
    engine.dispose()
    assert off_org == 0 and rows == before["employee"]


def test_l1_upgrade_refuses_a_multi_org_database(populated_url):
    """The backfill's own precondition, refused loudly: l1a0orgid predates
    multi-tenancy, so on a database that somehow holds TWO orgs it cannot
    know which rows belong to whom — and guessing would move money. The
    refusal must be atomic (transactional DDL): the revision stays at
    j3a0crmref, and removing the extra org lets the real upgrade proceed.
    Self-contained: restores head state."""

    command.downgrade(_cfg(populated_url), "j3a0crmref")
    engine = create_engine(populated_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organization (org_id, name) VALUES (2, 'Rival LLC')"
        ))
    try:
        with pytest.raises(Exception, match="single-org"):
            command.upgrade(_cfg(populated_url), "head")
        with engine.begin() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == "j3a0crmref", (
            "a refused upgrade must not move the revision"
        )
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM organization WHERE org_id = 2"))
        command.upgrade(_cfg(populated_url), "head")
        engine.dispose()
