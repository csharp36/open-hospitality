"""create_first_property: inserts a property and its detection alias under the
org-bound session, stores pms_source uppercase, generates a unique
property_id, defaults timezone when omitted."""

from sqlalchemy import select

from usali.mapping.property_registry import create_first_property, ensure_default_org
from usali.models import Property, PropertyDetectionAlias
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
    # Stored UPPERCASE from the lowercase API spelling: the form the adapters
    # stamp on facts and the detection registry is compared against
    # (create_first_property's docstring).
    assert row.name == "Sunset Inn" and row.pms_source == "OPERA"
    assert row.org_id == FOUNDING_ORG_ID and row.wage_jurisdiction == "US-CA"
    assert pid.startswith("sunset-inn-")
    # The detection alias: the typed name, under the same uppercase source,
    # in the same org -- what load_registry hands detect().
    alias = db_session.execute(
        select(PropertyDetectionAlias).where(PropertyDetectionAlias.property_id == pid)
    ).scalar_one()
    assert (alias.org_id, alias.pms_source, alias.match_phrase) == (
        FOUNDING_ORG_ID, "OPERA", "Sunset Inn")


def test_refuses_a_blank_name(db_session):
    """Belt to the API validator's braces: the name is the detection alias's
    match phrase, and an empty phrase matches every header."""
    import pytest

    ensure_default_org(db_session)
    bind_org_context(db_session, FOUNDING_ORG_ID)
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="blank"):
            create_first_property(db_session, FOUNDING_ORG_ID, name=blank, pms_source="opera")
    assert db_session.execute(select(Property)).first() is None


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
