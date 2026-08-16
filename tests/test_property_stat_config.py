import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from usali.models import ADR_ROOM_BASES, Organization, Property, PropertyStatConfig


def _property(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def test_stat_config_roundtrips_and_defaults_documented(db_session):
    _property(db_session)
    db_session.add(PropertyStatConfig(property_id="HISJ", adr_room_basis="exclude_comp_house"))
    db_session.commit()
    row = db_session.execute(select(PropertyStatConfig)).scalar_one()
    assert row.adr_room_basis == "exclude_comp_house" and row.org_id == 1


def test_stat_config_refuses_bad_basis(db_session):
    _property(db_session)
    db_session.add(PropertyStatConfig(property_id="HISJ", adr_room_basis="bogus"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_adr_room_bases_vocab():
    assert ADR_ROOM_BASES == frozenset({"as_reported", "exclude_comp_house"})
