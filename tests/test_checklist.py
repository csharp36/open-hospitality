from usali.models import Base, OrgChecklistOverride


def test_override_is_org_scoped_with_composite_pk():
    table = Base.metadata.tables["org_checklist_override"]
    assert {c.name for c in table.primary_key.columns} == {"org_id", "item_key"}
    assert table.c.org_id.nullable is False
    assert table.c.note.nullable is True
    # The CHECK is the schema mirror of ITEMS (design §5).
    checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert "ck_org_checklist_override_item_key" in checks


def test_override_model_is_orgscoped():
    from usali.models import OrgScoped
    assert issubclass(OrgChecklistOverride, OrgScoped)
