from decimal import Decimal

from sqlalchemy import func, select

from usali.models import (
    IngestBatch,
    UsaliFinancialFact,
    UsaliSegmentFact,
    UsaliStatisticFact,
)


def test_all_six_reports_through_the_drop_folder(db_session, tmp_path, seed_six_pdfs):
    # Prior phases' outputs unchanged (seed_six_pdfs ran the whole pipeline).
    assert db_session.scalar(select(func.count()).select_from(UsaliFinancialFact)) == 46
    assert db_session.scalar(select(func.count()).select_from(UsaliStatisticFact)) == 103

    seg = db_session.execute(select(UsaliSegmentFact)).scalars().all()
    by_key = {(f.pms_source, f.usali_segment, f.period): (f.rooms, f.room_revenue) for f in seg}

    # OPERA DAY: five segments reconciling to the report total (62 rooms / 10,395.00),
    # with comp + house-use occupancy included and revenue-free.
    opera_day = {k: v for k, v in by_key.items() if k[0] == "OPERA" and k[2] == "DAY"}
    assert sum((v[0] for v in opera_day.values()), Decimal("0")) == Decimal("62")
    assert sum((v[1] for v in opera_day.values()), Decimal("0")) == Decimal("10395.00")
    assert by_key[("OPERA", "GROUP", "DAY")] == (Decimal("0"), Decimal("0.00"))
    assert by_key[("OPERA", "COMPLIMENTARY", "DAY")][1] == Decimal("0.00")

    # AUTOCLERK DAY: reconciles to 50 nights / 6,129.03; CLC contract visible.
    ac_day = {k: v for k, v in by_key.items() if k[0] == "AUTOCLERK" and k[2] == "DAY"}
    assert sum((v[0] for v in ac_day.values()), Decimal("0")) == Decimal("50")
    assert sum((v[1] for v in ac_day.values()), Decimal("0")) == Decimal("6129.03")
    assert by_key[("AUTOCLERK", "CONTRACT", "DAY")] == (Decimal("1"), Decimal("75.00"))

    statuses = set(db_session.execute(select(IngestBatch.status)).scalars())
    assert statuses == {"transformed"}
    assert len(list((tmp_path / "processed").glob("*.pdf"))) == 6
