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


def test_backfill_fills_null_usali_columns_and_keeps_operator_edits(
    db_session, founding_org
):
    gl_chart.seed_chart(db_session, org_id=1)
    row = db_session.get(GlAccount, (1, "1100"))
    row.name = "CC Clearing (renamed)"
    row.usali_major_category = None  # the pre-backfill chart this exists for
    row.usali_sub_category = "Operator's own bucket"  # hand-set: must survive
    db_session.flush()
    out = gl_chart.fill_usali(db_session, org_id=1)
    row = db_session.get(GlAccount, (1, "1100"))
    assert row.name == "CC Clearing (renamed)"
    assert row.usali_sub_category == "Operator's own bucket"
    assert row.usali_major_category == "Settlements"
    assert out.filled == 1


def test_backfill_never_overwrites_a_set_value_even_when_the_template_disagrees(
    db_session, founding_org
):
    gl_chart.seed_chart(db_session, org_id=1)
    row = db_session.get(GlAccount, (1, "4000"))
    row.usali_schedule_id = 99  # operator's hand-set value; the template says 1
    row.usali_major_category = None
    db_session.flush()
    gl_chart.fill_usali(db_session, org_id=1)
    row = db_session.get(GlAccount, (1, "4000"))
    assert row.usali_schedule_id == 99
    assert row.usali_major_category == "Operated Departments"


def test_backfill_twice_is_a_no_op_the_second_time(db_session, founding_org):
    gl_chart.seed_chart(db_session, org_id=1)
    db_session.get(GlAccount, (1, "1000")).usali_major_category = None
    db_session.flush()
    first = gl_chart.fill_usali(db_session, org_id=1)
    second = gl_chart.fill_usali(db_session, org_id=1)
    assert first.filled == 1
    assert second.filled == 0
    assert second.unchanged == len(gl_chart.load_template())


def test_backfill_leaves_the_orgs_own_accounts_entirely_alone(
    db_session, founding_org
):
    gl_chart.seed_chart(db_session, org_id=1)
    db_session.add(
        GlAccount(
            org_id=1,
            account_code="9999",
            name="Org's Own Expense",
            account_type="expense",
            is_active=True,
        )
    )
    db_session.flush()
    gl_chart.fill_usali(db_session, org_id=1)
    row = db_session.get(GlAccount, (1, "9999"))
    assert row.usali_schedule_id is None
    assert row.usali_major_category is None
    assert row.usali_sub_category is None
    assert row.usali_line_item is None


def test_backfill_does_not_insert_template_accounts_the_chart_lacks(
    db_session, founding_org
):
    gl_chart.seed_chart(db_session, org_id=1)
    db_session.delete(db_session.get(GlAccount, (1, "6000")))
    db_session.flush()
    out = gl_chart.fill_usali(db_session, org_id=1)
    assert db_session.get(GlAccount, (1, "6000")) is None
    # The missing row is outside both counts, not "left alone".
    assert out.filled == 0
    assert out.unchanged == len(gl_chart.load_template()) - 1


def test_usali_fields_tuple_matches_the_models_usali_columns():
    """A fifth usali_* column on GlAccount must land in _USALI_FIELDS or
    fill_usali would silently skip it; this is where that drift fails."""
    assert gl_chart._USALI_FIELDS == tuple(
        c.name for c in GlAccount.__table__.columns if c.name.startswith("usali_")
    )
