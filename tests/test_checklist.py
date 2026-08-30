from datetime import date

from tests.grants import grant_role
from tests.orgworld import set_demand_feed
from tests.test_integrations import _connect
from usali.checklist import ITEMS, ChecklistItem, ItemStatus, evaluate, summarize
from usali.models import (
    Base,
    FiscalCalendar,
    IngestBatch,
    OrgChecklistOverride,
    Property,
    RoomInventory,
)


def test_override_is_org_scoped_with_composite_pk():
    table = Base.metadata.tables["org_checklist_override"]
    assert {c.name for c in table.primary_key.columns} == {"org_id", "item_key"}
    assert table.c.org_id.nullable is False
    assert table.c.note.nullable is True
    assert table.c.created_by.nullable is False
    assert table.c.created_at.nullable is False
    # The CHECK is the schema mirror of ITEMS (design §5).
    checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert "ck_org_checklist_override_item_key" in checks


def test_override_model_is_orgscoped():
    from usali.models import OrgScoped
    assert issubclass(OrgChecklistOverride, OrgScoped)


def _item(key, *, done, required=False):
    return ChecklistItem(
        key=key, title=f"T {key}", description=f"D {key}",
        required=required, where="/setup", probe=lambda _session: done,
    )


def test_open_when_probe_says_not_done(db_session, founding_org):
    [row] = evaluate(db_session, items=(_item("payroll", done=False),))
    assert row.status == "open"


def test_done_when_probe_says_done(db_session, founding_org):
    [row] = evaluate(db_session, items=(_item("payroll", done=True),))
    assert row.status == "done"


def test_dismissed_when_an_override_exists_and_probe_is_open(db_session, founding_org):
    db_session.add(OrgChecklistOverride(org_id=1, item_key="payroll", created_by="s"))
    db_session.commit()
    [row] = evaluate(db_session, items=(_item("payroll", done=False),))
    assert row.status == "dismissed"


def test_done_outranks_a_dismissal(db_session, founding_org):
    """D-B4.4: the operator dismissed payroll, then actually connected it."""
    db_session.add(OrgChecklistOverride(org_id=1, item_key="payroll", created_by="s"))
    db_session.commit()
    [row] = evaluate(db_session, items=(_item("payroll", done=True),))
    assert row.status == "done"


def test_a_raising_probe_degrades_only_that_item(db_session, founding_org):
    """Design §8: loud but contained — and never `done`."""
    def _boom(_session):
        raise RuntimeError("probe exploded")

    bad = ChecklistItem(key="payroll", title="T", description="D", required=False,
                        where="/setup", probe=_boom)
    rows = evaluate(db_session, items=(bad, _item("team", done=True)))
    by_key = {r.key: r for r in rows}
    assert by_key["payroll"].status == "error"
    assert by_key["payroll"].status != "done"
    assert by_key["team"].status == "done"


def test_a_db_error_in_one_probe_does_not_cascade(db_session, founding_org):
    """A DBAPI failure poisons the session; without a rollback every later
    probe that touches the database would degrade too, and §8's containment
    would be a fiction. `_item()`'s probe ignores the session entirely, so it
    can't exercise a poisoned transaction — the second probe here must issue
    a real query for this test to mean anything."""
    from sqlalchemy import text

    def _bad_query(session):
        session.execute(text("SELECT * FROM no_such_table"))
        return True

    def _touches_db(session):
        session.execute(text("SELECT 1"))
        return True

    bad = ChecklistItem(key="payroll", title="T", description="D", required=False,
                        where="/setup", probe=_bad_query)
    team = ChecklistItem(key="team", title="T", description="D", required=False,
                        where="/setup", probe=_touches_db)
    rows = evaluate(db_session, items=(bad, team))
    by_key = {r.key: r for r in rows}
    assert by_key["payroll"].status == "error"
    assert by_key["team"].status == "done"   # NOT "error" — this is the point


def test_item_status_mirrors_checklist_item_fields():
    """Adding a field to one and not the other must fail loudly, not silently
    drop it from the API payload."""
    from dataclasses import fields
    from usali.checklist import ChecklistItem, ItemStatus
    assert ({f.name for f in fields(ItemStatus)} - {"status", "detail"}
            == {f.name for f in fields(ChecklistItem)} - {"probe"})


def test_evaluate_uses_the_module_registry_by_default(db_session, founding_org):
    assert {r.key for r in evaluate(db_session)} == {item.key for item in ITEMS}


def _status_of(db_session, key):
    return {r.key: r for r in evaluate(db_session)}[key].status


def test_registry_keys_match_the_schema_mirror(db_session):
    """models.py's CHECK literal and ITEMS must not drift (design §5)."""
    from usali.models import Base
    table = Base.metadata.tables["org_checklist_override"]
    [check] = [
        c for c in table.constraints
        if getattr(c, "name", None) == "ck_org_checklist_override_item_key"
    ]
    in_check = {
        part.strip().strip("'")
        for part in str(check.sqltext).split("(")[-1].rstrip(")").split(",")
    }
    assert in_check == {item.key for item in ITEMS}


def test_first_report_is_open_then_done(db_session, founding_org):
    assert _status_of(db_session, "first_report") == "open"
    db_session.add(IngestBatch(org_id=1, pms_source="OPERA", report_type="trial_balance",
                               source_file="f.pdf", file_hash="h"))
    db_session.commit()
    assert _status_of(db_session, "first_report") == "done"


def test_room_inventory_needs_at_least_one_property(db_session, founding_org):
    """An org with no properties must NOT satisfy the probe vacuously."""
    assert _status_of(db_session, "room_inventory") == "open"


def test_room_inventory_done_only_when_every_property_has_a_row(db_session, founding_org):
    db_session.add_all([
        Property(property_id="HISJ", org_id=1, name="H", pms_source="OPERA"),
        Property(property_id="SSSJ", org_id=1, name="S", pms_source="OPERA"),
    ])
    db_session.add(RoomInventory(org_id=1, property_id="HISJ",
                                 effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    assert _status_of(db_session, "room_inventory") == "open"  # SSSJ still missing
    db_session.add(RoomInventory(org_id=1, property_id="SSSJ",
                                 effective_date=date(2026, 1, 1), total_rooms=90))
    db_session.commit()
    assert _status_of(db_session, "room_inventory") == "done"
    # Effective-dating means a property legitimately has MANY inventory rows
    # (a renovation inserts a new one rather than updating). distinct() is
    # what makes the subset check tolerate that; without it this would fail.
    db_session.add(RoomInventory(org_id=1, property_id="HISJ",
                                 effective_date=date(2026, 6, 1), total_rooms=132))
    db_session.commit()
    assert _status_of(db_session, "room_inventory") == "done"


def test_fiscal_calendar_done_when_every_property_has_a_row(db_session, founding_org):
    db_session.add(Property(property_id="HISJ", org_id=1, name="H", pms_source="OPERA"))
    db_session.commit()
    assert _status_of(db_session, "fiscal_calendar") == "open"
    db_session.add(FiscalCalendar(org_id=1, property_id="HISJ",
                                  calendar_type="calendar_month",
                                  fiscal_year_start_month=1, week_start_weekday=None))
    db_session.commit()
    assert _status_of(db_session, "fiscal_calendar") == "done"


def test_demand_feed_reads_the_org_credential_row(db_session, founding_org):
    """OH-17 (D-OH17.1): "not connected" is the ABSENCE of a demand_feed
    credential row, not an empty `crm_provider` sentinel on an always-present
    one. The probe still derives its answer from what is actually configured
    (D-B4.1) — only the shape of "configured" changed."""
    set_demand_feed(db_session, "")
    db_session.commit()
    assert _status_of(db_session, "demand_feed") == "open"
    set_demand_feed(db_session, "delphi")
    db_session.commit()
    assert _status_of(db_session, "demand_feed") == "done"


def test_team_needs_a_second_subject(db_session, founding_org):
    grant_role(db_session, "org_admin", sub="founder", org_id=1)
    assert _status_of(db_session, "team") == "open"
    grant_role(db_session, "accountant", sub="second-human", org_id=1)
    assert _status_of(db_session, "team") == "done"


def test_where_and_unavailable_reason_are_paired():
    """D-B4.8: an item either routes somewhere real or says why it does not.
    Exactly one of the two, never both and never neither — an item with a
    reason AND a link would render a link the reason contradicts, and one
    with neither is the dead end this decision exists to remove."""
    for item in ITEMS:
        assert (item.where is None) == (item.unavailable_reason is not None), item.key


def test_every_item_has_a_connect_surface():
    """D-OH17.12, the mirror image of the tripwire this replaces. OH-17 gave
    all three integration items a real `where`, so a null one now means a
    regression rather than an honest gap."""
    assert [i.key for i in ITEMS if i.where is None] == []


def test_the_integration_items_route_to_integrations():
    by_key = {i.key: i for i in ITEMS}
    for key in ("payroll", "accounting", "demand_feed"):
        assert by_key[key].where == "/integrations"
        assert by_key[key].unavailable_reason is None


def test_payroll_and_accounting_read_the_credential_row(db_session, unconnected_org):
    """D-OH17.8: the probe is a presence check on what is actually configured
    for THIS tenant — still derived (D-B4.1), never a stored status.

    `unconnected_org`, not bare `founding_org`: `ensure_default_org` seeds
    org 1's payroll and accounting rows unconditionally (D-OH17.15), so under
    `founding_org` alone both would already read "done" and `_connect` below
    would collide with the seed on the (org_id, integration) primary key
    instead of testing anything."""
    assert _status_of(db_session, "payroll") == "open"
    assert _status_of(db_session, "accounting") == "open"
    _connect(db_session, "payroll", "gusto", api_token="t", company_id="c")
    _connect(db_session, "accounting", "qbo", realm_id="r", refresh_token="tok")
    assert _status_of(db_session, "payroll") == "done"
    assert _status_of(db_session, "accounting") == "done"


def _row(key, status, *, required=False):
    return ItemStatus(key=key, title=f"T {key}", description=f"D {key}",
                      required=required, where="/setup", status=status)


def test_summarize_all_open():
    rows = [_row("a", "open"), _row("b", "open")]
    summary = summarize(rows)
    assert summary.open_count == 2
    assert summary.error_count == 0
    assert summary.all_clear is False


def test_summarize_all_done():
    rows = [_row("a", "done"), _row("b", "done")]
    summary = summarize(rows)
    assert summary.open_count == 0
    assert summary.error_count == 0
    assert summary.all_clear is True


def test_summarize_mixed_statuses():
    rows = [_row("a", "done"), _row("b", "open"), _row("c", "dismissed")]
    summary = summarize(rows)
    assert summary.open_count == 1
    assert summary.error_count == 0
    assert summary.all_clear is False


def test_summarize_an_error_blocks_all_clear_even_with_no_open_items():
    """ADR-010 / design §8: an item we could not check is not a finished
    item. A tenant whose only non-done item errored must NOT read as
    all_clear, even though open_count is zero."""
    rows = [_row("a", "done"), _row("b", "error")]
    summary = summarize(rows)
    assert summary.open_count == 0
    assert summary.error_count == 1
    assert summary.all_clear is False
