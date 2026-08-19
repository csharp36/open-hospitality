"""create_first_property: inserts a bare property under the org-bound session,
generates a unique property_id, defaults timezone when omitted."""

from sqlalchemy import select

from usali.mapping.property_registry import create_first_property, ensure_default_org
from usali.models import Property
from usali.tenancy import FOUNDING_ORG_ID, bind_org_context


def test_creates_a_property_under_the_bound_org(db_session):
    ensure_default_org(db_session)
    bind_org_context(db_session, FOUNDING_ORG_ID)
    pid = create_first_property(
        db_session, FOUNDING_ORG_ID,
        name="Sunset Inn", pms_source="opera", wage_jurisdiction="US-CA",
    )
    db_session.commit()
    row = db_session.execute(
        select(Property).where(Property.property_id == pid)
    ).scalar_one()
    assert row.name == "Sunset Inn" and row.pms_source == "opera"
    assert row.org_id == FOUNDING_ORG_ID and row.wage_jurisdiction == "US-CA"
    assert pid.startswith("sunset-inn-")


def test_defaults_timezone_when_omitted(db_session):
    ensure_default_org(db_session)
    bind_org_context(db_session, FOUNDING_ORG_ID)
    pid = create_first_property(db_session, FOUNDING_ORG_ID,
                                name="No Tz Hotel", pms_source="autoclerk")
    db_session.commit()
    row = db_session.execute(
        select(Property).where(Property.property_id == pid)).scalar_one()
    assert row.timezone == "America/Los_Angeles"  # server default


def test_generated_ids_are_unique_across_calls(db_session):
    ensure_default_org(db_session)
    bind_org_context(db_session, FOUNDING_ORG_ID)
    a = create_first_property(db_session, FOUNDING_ORG_ID, name="Dup", pms_source="opera")
    b = create_first_property(db_session, FOUNDING_ORG_ID, name="Dup", pms_source="opera")
    db_session.commit()
    assert a != b
