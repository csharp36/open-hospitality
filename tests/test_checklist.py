from usali.checklist import ChecklistItem, evaluate
from usali.models import Base, OrgChecklistOverride


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
    assert evaluate(db_session) == []
