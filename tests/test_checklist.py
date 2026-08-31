from datetime import date

from tests.credentials import plant_credential, unreadable_ciphertext
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


def test_demand_feed_is_the_one_item_without_a_surface():
    """D-OH17.12 as amended by D-OH17.16 — the mirror image of the tripwire
    this replaces, narrowed to the one honest gap that remains.

    An EXACT set, not a membership check, so it fails in both directions.
    Any OTHER item losing its `where` is a regression. `demand_feed` GAINING
    one is the signal that `crm_ref` became settable and the paired
    `unavailable_reason` must go with it — the same "the failing test is the
    signal" shape the B4 tripwire had."""
    assert [i.key for i in ITEMS if i.where is None] == ["demand_feed"]


def test_every_item_route_is_pinned():
    """The Python half of the dead-link pair; `frontend/src/router.test.ts`
    is meant to hold the other half, asserting that each of these paths
    resolves to a real SPA route. Neither side can see the other's language,
    so the set is pinned in both and a new `where` fails here first.

    If that file is absent or has stopped pinning this same list, the pair is
    broken and this test is on its own — which is the state that let
    "/integrations" sit in this list while no such route existed, shipping two
    dead links on the setup checklist.
    """
    assert sorted({i.where for i in ITEMS if i.where is not None}) == [
        "/employees",
        "/integrations",
        "/property-config",
        "/upload",
    ]


def test_the_connectable_integration_items_route_to_integrations():
    by_key = {i.key: i for i in ITEMS}
    for key in ("payroll", "accounting"):
        assert by_key[key].where == "/integrations"
        assert by_key[key].unavailable_reason is None
    # Not demand_feed: credentials do not finish that connection (D-OH17.16),
    # so it carries the reason instead of the route. Pinned here as well as
    # above because these two are what a reader greps for when asking "where
    # do the integration items go?" and a silent absence would answer wrong.
    assert by_key["demand_feed"].where is None
    assert "property reference" in by_key["demand_feed"].unavailable_reason


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


def test_an_unreadable_credential_reads_as_could_not_check(db_session, unconnected_org):
    """ADR-005's rotation hazard meeting D-B4.1's derived status.

    `integrations.CredentialUnreadable` (OH-17 Task 12) is raised when a
    stored credential cannot be decrypted — a rotated `field_encryption_key`,
    with no envelope or key version to fall back on. It reaches the checklist
    through `has_credential`, and lands in `evaluate`'s per-item guard
    (checklist.py:85), so the item derives to `error` with the exception NAME
    as its detail.

    That is the honest answer, and both alternatives are lies. `done` would
    put a green badge over a credential nothing can read. `open` — which is
    what a `resolve_*` returning None would have produced — silently re-opens
    a finished item and invites the operator to reconnect an integration that
    is fine, while the rotation is never named anywhere. `error` says the one
    true thing: this could not be checked, and here is what went wrong.

    The second half is CONTAINMENT (design §8). `team` is probed AFTER
    demand_feed and issues a real query, so it is the item that would break
    if one unreadable credential poisoned the walk — one broken integration
    must not take the whole checklist down with it."""
    plant_credential(db_session, "demand_feed", "delphi",
                     subscription_key=unreadable_ciphertext("delphi-key"))
    _connect(db_session, "payroll", "gusto", api_token="t", company_id="c")
    # Two distinct subjects, so `team` derives to a definite `done` rather
    # than a bare "not error" — the containment assertion below is stronger
    # when the later probe reaches a real answer.
    grant_role(db_session, "org_admin", sub="first-subject")
    grant_role(db_session, "property_gm", sub="second-subject")

    rows = evaluate(db_session)
    by_key = {r.key: r for r in rows}
    assert by_key["demand_feed"].status == "error"
    assert by_key["demand_feed"].detail == "CredentialUnreadable"
    # Stated as its own assertion because these are the two failures that
    # matter, and a future change could reach either without touching the
    # line above.
    assert by_key["demand_feed"].status not in ("done", "open")

    summary = summarize(rows)
    assert summary.all_clear is False
    assert summary.error_count == 1

    # Containment: everything else still evaluates on its own terms — the
    # readable credential, the absent one, and the probe that runs after the
    # failure and touches the database.
    assert by_key["payroll"].status == "done"
    assert by_key["accounting"].status == "open"
    assert by_key["team"].status == "done"


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
