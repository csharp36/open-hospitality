"""record_request: normalizes the PMS name, de-dupes per (org_alias, normalized),
allows the same PMS across workspaces, reports is_new."""

from sqlalchemy import select

from usali import pms_interest
from usali.models import PmsInterestRequest
from usali.mapping.property_registry import ensure_default_org


def test_normalize_collapses_spacing_case_and_punctuation():
    n = pms_interest._normalize
    assert n("HotelKey") == n("hotel key") == n("Hotel-Key!") == "hotelkey"


def test_records_and_dedupes_within_a_workspace(db_session):
    ensure_default_org(db_session)
    row1, new1 = pms_interest.record_request(
        db_session, org_alias="sky-group", email="a@example.test", raw_pms="HotelKey")
    db_session.commit()
    assert new1 is True and row1.normalized_pms == "hotelkey"
    _, new2 = pms_interest.record_request(
        db_session, org_alias="sky-group", email="a@example.test", raw_pms="hotel key")
    db_session.commit()
    assert new2 is False  # same (org_alias, normalized) -> de-duped
    rows = db_session.execute(
        select(PmsInterestRequest).where(
            PmsInterestRequest.normalized_pms == "hotelkey")).scalars().all()
    assert len(rows) == 1


def test_same_pms_different_workspace_is_a_distinct_request(db_session):
    ensure_default_org(db_session)
    _, a = pms_interest.record_request(
        db_session, org_alias="alias-a", email="a@example.test", raw_pms="SkyTouch")
    _, b = pms_interest.record_request(
        db_session, org_alias="alias-b", email="b@example.test", raw_pms="skytouch")
    db_session.commit()
    assert a is True and b is True  # ('alias-a','skytouch') and ('alias-b','skytouch') differ
