from sqlalchemy import func, select

from usali.mapping.property_registry import seed_properties
from usali.models import Organization, Property, PropertyDetectionAlias


def test_seed_properties_loads_rows(db_session):
    n = seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    assert n == 3
    props = {
        p.property_id: p
        for p in db_session.execute(select(Property)).scalars().all()
    }
    assert set(props) == {"HISJ", "SSSJ", "STDEMO"}
    assert props["HISJ"].pms_source == "OPERA"
    assert props["SSSJ"].pms_source == "AUTOCLERK"
    assert props["STDEMO"].pms_source == "SKYTOUCH"

    aliases = db_session.execute(select(PropertyDetectionAlias)).scalars().all()
    assert {(a.property_id, a.pms_source, a.match_phrase) for a in aliases} == {
        ("HISJ", "OPERA", "HOLIDAY INN & SUITES SAN JOSE"),
        ("SSSJ", "AUTOCLERK", "SURESTAY PLUS BY BW"),
        ("STDEMO", "SKYTOUCH", "REDSTONE TEST INN"),
    }
    # A single default organization is created and shared by all properties.
    org_count = db_session.execute(
        select(func.count()).select_from(Organization)
    ).scalar_one()
    assert org_count == 1
    assert props["HISJ"].org_id == props["SSSJ"].org_id
    assert props["STDEMO"].org_id == props["HISJ"].org_id


def test_seed_properties_is_idempotent(db_session):
    seed_properties(db_session, "mapping/properties.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    assert db_session.execute(
        select(func.count()).select_from(Property)
    ).scalar_one() == 3
    assert db_session.execute(
        select(func.count()).select_from(PropertyDetectionAlias)
    ).scalar_one() == 3
    assert db_session.execute(
        select(func.count()).select_from(Organization)
    ).scalar_one() == 1


def test_seed_properties_carries_crm_ref(db_session):
    """J3: `crm_ref` is the provider-side property identity, declared in
    the registry file exactly like wage_jurisdiction — human-authored,
    optional, and a property without one refuses at pull time by name."""
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()
    props = {
        p.property_id: p
        for p in db_session.execute(select(Property)).scalars().all()
    }
    assert props["HISJ"].crm_ref == "DELPHI-HISJ"
    assert props["SSSJ"].crm_ref is None


def test_a_reseed_without_crm_ref_does_not_blank_one(db_session, tmp_path):
    """The wage_jurisdiction rule applied to crm_ref: a re-seed only
    OVERWRITES when the registry states a value — silence in the file
    must not blank what an operator set."""
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    bare = tmp_path / "properties.yaml"
    bare.write_text(
        '- {match: "HOLIDAY INN & SUITES SAN JOSE", property_id: HISJ, '
        "pms_source: OPERA, wage_jurisdiction: US-CA}\n"
    )
    seed_properties(db_session, bare)
    db_session.commit()

    hisj = db_session.execute(
        select(Property).where(Property.property_id == "HISJ")
    ).scalar_one()
    assert hisj.crm_ref == "DELPHI-HISJ"


def test_j7_a_reseed_with_a_changed_crm_ref_overwrites(db_session, tmp_path):
    """The other direction, previously unpinned (the J7 review deleted
    crm_ref from the upsert set_ and nothing failed): when the registry
    DOES state a value, re-seeding corrects the row — an operator fixing
    a wrong ref in properties.yaml must not be silently ignored."""
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    changed = tmp_path / "properties.yaml"
    changed.write_text(
        '- {match: "HOLIDAY INN & SUITES SAN JOSE", property_id: HISJ, '
        "pms_source: OPERA, wage_jurisdiction: US-CA, "
        "crm_ref: DELPHI-HISJ-2}\n"
    )
    seed_properties(db_session, changed)
    db_session.commit()

    hisj = db_session.execute(
        select(Property).where(Property.property_id == "HISJ")
    ).scalar_one()
    assert hisj.crm_ref == "DELPHI-HISJ-2"
