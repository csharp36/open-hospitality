from sqlalchemy import select

from usali.mapping.loader import load_mappings
from usali.models import UsaliMappingDictionary


def test_load_opera_mappings_covers_trial_balance_codes(db_session):
    load_mappings(db_session, "mapping/opera.yaml")
    db_session.commit()
    codes = set(
        db_session.execute(
            select(UsaliMappingDictionary.pms_trx_code).where(
                UsaliMappingDictionary.pms_source == "OPERA",
                UsaliMappingDictionary.usali_edition == 12,
            )
        ).scalars()
    )
    required = {"1000", "5105", "5106", "5107", "5007", "7100", "7101", "7102", "7104",
                "9002", "9003", "9004", "9005", "9007"}
    assert required <= codes


def test_loader_is_idempotent(db_session):
    load_mappings(db_session, "mapping/opera.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    db_session.commit()
    rows = db_session.execute(select(UsaliMappingDictionary)).scalars().all()
    keys = {(r.pms_source, r.pms_trx_code, r.usali_edition) for r in rows}
    assert len(rows) == len(keys)
