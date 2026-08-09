"""What the E1 migration chain leaves behind.

The original version of this file re-executed the backfill INSERT against the
test session to prove it was lossless. That is no longer possible OR meaningful:
Task 9 dropped `employee.property_id` / `department_id` / `position_id`, so the
statement cannot run, and the backfill only ever executes DURING the chain, at a
point where those columns still exist.

The backfill's losslessness was verified when it was written (one primary per
employee, department and position preserved verbatim, terminated employees
closed the day AFTER their last worked day, sparse rows carried through). What
remains testable -- and worth pinning, because a missing constraint here is
worth $250 an occurrence -- is the SHAPE the finished chain produces.
"""

from sqlalchemy import inspect, text


def test_the_retiring_employee_columns_are_gone(db_session):
    """If these come back, so does the whole class of migration-ramp bug: every
    consumer needs a fallback again, and review found that mistake in six
    separate places."""
    columns = {c["name"] for c in inspect(db_session.bind).get_columns("employee")}
    assert "property_id" not in columns
    assert "department_id" not in columns
    assert "position_id" not in columns


def test_employee_assignment_exists_with_its_key(db_session):
    columns = {
        c["name"] for c in inspect(db_session.bind).get_columns("employee_assignment")
    }
    assert {
        "employee_id", "property_id", "department_id", "position_id",
        "is_primary", "status", "effective_from", "effective_to",
    } <= columns


def test_only_one_open_active_primary_is_possible(db_session):
    """The partial unique index behind the double-payment guarantee. Review
    reproduced $500 submitted where $250 was owed without it."""
    index = db_session.execute(text(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'employee_assignment' "
        "AND indexname = 'uq_one_active_primary_per_employee'"
    )).scalar_one_or_none()
    assert index is not None, "the one-primary index is missing"
    assert "UNIQUE" in index
    # Scoped to OPEN primaries so the ordinary transfer pattern -- close the old,
    # open the new -- stays legal. A first version without this forbade it.
    assert "effective_to IS NULL" in index


def test_actual_labor_facts_are_keyed_by_property_too(db_session):
    """One pay run writes facts for every property the employee worked at, so
    (pay_run_id, department_id) alone made the second row a duplicate-key
    violation -- the multi-property write could not execute at all."""
    constraints = {
        c["name"]: set(c["column_names"])
        for c in inspect(db_session.bind).get_unique_constraints(
            "usali_actual_labor_fact"
        )
    }
    assert any(
        cols == {"pay_run_id", "property_id", "department_id"}
        for cols in constraints.values()
    ), f"expected a (pay_run_id, property_id, department_id) key, got {constraints}"


def test_wage_jurisdiction_has_no_server_default(db_session):
    """An unset property must be an explicit decision, not silently Californian.
    overtime_rules refuses an UNRECOGNIZED jurisdiction, never an unset one."""
    default = db_session.execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'property' AND column_name = 'wage_jurisdiction'"
    )).scalar_one_or_none()
    assert default is None
