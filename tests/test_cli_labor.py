"""CLI wiring for the B3 `promote-labor` backfill command.

Happy path against the real test database: seed an approved-but-unpromoted
timecard (with a pay rate + department), invoke the CLI, and assert the labor
facts now exist. The CLI connects via USALI_DB_URL (set by the db_url fixture)
and uses the configured payroll_period_anchor.
"""

from datetime import date

from sqlalchemy import select
from typer.testing import CliRunner

from tests.test_labor_promote import _approved_card, _seed, _shift
from usali.cli import app
from usali.models import UsaliLaborFact

runner = CliRunner()


def test_promote_labor_backfills_approved_timecards(db_session):
    # Config default anchor is the Monday 2026-01-05, so seed on that date.
    _dept, device_id, emp_id = _seed(db_session, pay_rate="20.00")
    _shift(db_session, device_id, emp_id, 5, 9, 17)  # one 8h day on 2026-01-05
    db_session.commit()
    _approved_card(db_session, emp_id)  # approved, but NOT yet promoted

    # Nothing promoted until the backfill runs.
    assert db_session.execute(select(UsaliLaborFact)).scalars().all() == []

    result = runner.invoke(app, ["promote-labor"])
    assert result.exit_code == 0, result.output
    assert "Promoted 1 labor facts across 1 approved timecards" in result.output

    facts = db_session.execute(select(UsaliLaborFact)).scalars().all()
    assert len(facts) == 1
    assert facts[0].business_date == date(2026, 1, 5)

    # Idempotent: a second backfill does not double-count.
    result = runner.invoke(app, ["promote-labor"])
    assert result.exit_code == 0, result.output
    assert len(db_session.execute(select(UsaliLaborFact)).scalars().all()) == 1
