from sqlalchemy import select

from usali.mapping.loader import load_mappings
from usali.models import UsaliMappingDictionary


def test_autoclerk_mapping_covers_transaction_summary_codes(db_session):
    load_mappings(db_session, "mapping/autoclerk.yaml")
    db_session.commit()
    codes = set(
        db_session.execute(
            select(UsaliMappingDictionary.pms_trx_code).where(
                UsaliMappingDictionary.pms_source == "AUTOCLERK",
                UsaliMappingDictionary.usali_edition == 12,
            )
        ).scalars()
    )
    required = {
        "ROOM|ROOM_RENT", "ROOM|EARLY_CHECK_IN", "ROOM|LATE_CHECK_OUT",
        "ROOM|CANCELLATION_CHARGE", "ROOM|NO_SHOW_CHARGE", "CASH|CASH",
        "TAX|OCCUPANCY_TAX", "TAX|COUNTY_TAX", "TAX|TOURISM_FEE",
        "CREDIT_CARDS|AMERICAN_EXPRESS", "CREDIT_CARDS|DISCOVER",
        "CREDIT_CARDS|MASTERCARD", "CREDIT_CARDS|VISA",
        "ACCOUNTS|DIRECT_BILL", "ACCOUNTS|BWS", "ACCOUNTS|WRITE_OFF",
        "ACCOUNTS|SETTLED_IN_SHIFT4", "MISC|ELECTRIC_CHARGER", "MISC|SMOKING",
        "MISC|PET_FEE", "MISC|MISC_NON_TAXABLE", "LAUNDRY|SOAP",
        "CLC_DIRECT_BILL|CLC_DIRECT_BILL", "AIRBNB_DIRACT_BILL|AIRBNB_BILLING",
        "CANARY|EARLY_CHECK_IN", "CANARY|LATE_CHECK_OUT", "CANARY|PET_FEE",
        "CANARY|ROOM_UPGRADE_1", "CANARY|ROOM_UPGRADE_2", "PARKING|PARKING_FEES",
        "HIE_MARKET_SELL|WATER", "HIE_MARKET_SELL|SODA",
    }
    assert required <= codes
