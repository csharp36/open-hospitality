"""Chart template and per-org seeding (design D-OH27.1, .2)."""

from usali import gl_chart
from usali.models import GlAccount


REQUIRED_ROLES = {"guest_ledger_clearing", "accrued_payroll", "labor_wages_expense"}


def test_the_template_carries_every_engine_role():
    accounts = gl_chart.load_template()
    roles = {a.system_role for a in accounts if a.system_role}
    assert REQUIRED_ROLES <= roles


def test_the_template_is_a_superset_of_the_qbo_chart():
    """Every code the QBO push has ever emitted must exist in the new chart,
    or re-pointing the push (Task 10) would orphan existing QBO books."""
    import yaml
    legacy = {str(e["account_code"]) for e in
              yaml.safe_load(open("mapping/qbo_accounts.yaml"))}
    codes = {a.account_code for a in gl_chart.load_template()}
    assert legacy <= codes


def test_seeding_is_idempotent_and_org_scoped(db_session, founding_org):
    first = gl_chart.seed_chart(db_session, org_id=1)
    again = gl_chart.seed_chart(db_session, org_id=1)
    assert first == len(gl_chart.load_template())
    assert again == 0  # second run inserts nothing
    row = db_session.get(GlAccount, (1, "4000"))
    assert row is not None and row.account_type == "income"


def test_seeding_does_not_resurrect_an_edited_account(db_session, founding_org):
    """The OH-17 seed lesson: a re-run must never overwrite operator edits."""
    gl_chart.seed_chart(db_session, org_id=1)
    row = db_session.get(GlAccount, (1, "4000"))
    row.name = "Rooms Revenue (renamed)"
    db_session.flush()
    gl_chart.seed_chart(db_session, org_id=1)
    assert db_session.get(GlAccount, (1, "4000")).name == "Rooms Revenue (renamed)"


def test_seeding_classifies_the_settlement_and_tax_buckets(db_session, founding_org):
    """The unscheduled buckets the SOS renders from the chart: majors set,
    schedules NULL — the values verified by the template header's grep line."""
    gl_chart.seed_chart(db_session, org_id=1)
    expected = {
        "1000": "Settlements",
        "1100": "Settlements",
        "1200": "Settlements",
        "2100": "Taxes (Pass-Through)",
    }
    for code, major in expected.items():
        row = db_session.get(GlAccount, (1, code))
        assert row is not None
        assert row.usali_major_category == major
        assert row.usali_schedule_id is None
    # The clearing and accrual rows stay unclassified: 1210 is excluded from
    # the statement by system_role, 2200 is a balance-sheet accrual.
    for code in ("1210", "2200"):
        row = db_session.get(GlAccount, (1, code))
        assert row.usali_major_category is None
        assert row.usali_schedule_id is None
