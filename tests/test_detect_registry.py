from usali.detect import load_registry
from usali.mapping.property_registry import seed_properties


def test_load_registry_returns_seeded_rows_in_legacy_shape(db_session):
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    rows = {(r["property_id"], r["pms_source"], r["match"]) for r in load_registry(db_session)}
    assert rows == {
        ("HISJ", "OPERA", "HOLIDAY INN & SUITES SAN JOSE"),
        ("SSSJ", "AUTOCLERK", "SURESTAY PLUS BY BW"),
    }


def test_load_registry_empty_when_unseeded(db_session):
    assert load_registry(db_session) == []
