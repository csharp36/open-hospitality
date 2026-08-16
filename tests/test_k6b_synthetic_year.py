"""K6b: the synthetic financial year (Pillar K, decision 1 repaired).

The K6-era cloud seed ingested `docs/reference/samples/*.pdf` — REAL
production figures — into the public instance, breaking the pillar's
fictitious-by-construction invariant. The repair: every cloud figure is
now INVENTED by `usali.synthetic_year`, a deterministic generator that
speaks the adaptors' record shapes so everything still flows through
the real stage → transform/promote pipeline and every report surface
(SOS, occupancy, market mix, A/R) has a full year to show.

The pins here are the invariant's teeth: figures are mapped-only
(differenced against the curated YAMLs — an unmapped synthetic code
would pollute coverage), internally coherent (ADR × occupied = room
revenue; segments reconcile exactly to their TOTAL row, which the
strict segment promote enforces), deterministic (a re-run seed must
no-op), and NOT the real numbers (the sample-day figures must never
reappear).
"""

import importlib.util
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.segment_promote import promote_segments
from usali.stage import stage_records
from usali.stats_promote import promote_statistics
from usali.stats_stage import stage_statistics
from usali.synthetic_year import (
    PROPERTY_SOURCE,
    YEAR_END,
    synthetic_dates,
    synthetic_day,
)
from usali.transform import transform

REPO = Path(__file__).parent.parent


def _mapped_codes(source: str) -> set[str]:
    path = REPO / "mapping" / ("opera.yaml" if source == "OPERA" else "autoclerk.yaml")
    return {row["code"] for row in yaml.safe_load(path.read_text()) if row["source"] == source}


def _curated_stat_labels(source: str) -> set[str]:
    rows = yaml.safe_load((REPO / "mapping" / "statistics.yaml").read_text())
    return {row["label"] for row in rows if row["source"] == source}


def _mapped_segment_codes(source: str) -> set[str]:
    rows = yaml.safe_load((REPO / "mapping" / "segments.yaml").read_text())
    return {row["code"] for row in rows if row["source"] == source}


def _curated_ledger_labels() -> set[str]:
    rows = yaml.safe_load((REPO / "mapping" / "ledgers.yaml").read_text())
    return {row["label"] for row in rows}


# Sample days spread across the year (both season extremes, both week
# halves) — cheap enough to run every property through each.
SPOT_DATES = [date(2025, 8, 4), date(2025, 11, 15), date(2026, 1, 20),
              date(2026, 4, 11), date(2026, 7, 7), date(2026, 7, 31)]


def test_the_year_is_365_days_ending_at_the_anchor():
    days = synthetic_dates()
    assert len(days) == 365
    assert days[-1] == YEAR_END
    assert days[0] == date(2025, 8, 1)
    assert days == sorted(days)


def test_deterministic_by_construction():
    for pid in PROPERTY_SOURCE:
        assert synthetic_day(pid, date(2026, 3, 14)) == synthetic_day(pid, date(2026, 3, 14))


def test_every_financial_code_is_mapped():
    for pid, source in PROPERTY_SOURCE.items():
        mapped = _mapped_codes(source)
        for d in SPOT_DATES:
            day = synthetic_day(pid, d)
            assert day.financial, (pid, d)
            for rec in day.financial:
                assert rec.pms_trx_code in mapped, (pid, d, rec.pms_trx_code)
                assert rec.pms_source == source
                assert rec.property_id == pid
                assert rec.business_date == d


def test_every_stat_label_is_curated_and_daily():
    for pid, source in PROPERTY_SOURCE.items():
        curated = _curated_stat_labels(source)
        expected_period = "DAY" if source == "OPERA" else "Today"
        for d in SPOT_DATES:
            stats = synthetic_day(pid, d).statistics
            assert stats
            for rec in stats:
                assert rec.metric_label in curated, (pid, rec.metric_label)
                assert rec.period_label == expected_period
                assert rec.is_prior_year is False


def test_segments_reconcile_exactly_to_their_total_row():
    for pid, source in PROPERTY_SOURCE.items():
        mapped = _mapped_segment_codes(source)
        for d in SPOT_DATES:
            segs = synthetic_day(pid, d).segments
            assert segs
            for measure in ("ROOMS", "ROOM_REVENUE"):
                rows = [s for s in segs if s.measure == measure]
                totals = [s for s in rows if s.segment_code == "TOTAL"]
                parts = [s for s in rows if s.segment_code != "TOTAL"]
                assert len(totals) == 1
                assert sum(p.value for p in parts) == totals[0].value
                for p in parts:
                    assert p.segment_code in mapped, (pid, p.segment_code)
                    assert p.period_label == "DAY"


def test_internal_coherence_opera():
    for d in SPOT_DATES:
        day = synthetic_day("HISJ", d)
        fin = {r.pms_trx_code: r.raw_amount for r in day.financial}
        stats = {r.metric_label: r.value for r in day.statistics}
        room_rev = fin["1000"]
        occupied = stats["Rooms Occupied"]
        available = stats["Available Rooms minus OOO Rooms"]
        assert room_rev > 0
        assert stats["Room Revenue"] == room_rev
        assert occupied <= available
        # The stats speak the same world as the trial balance.
        assert stats["ADR"] == (room_rev / occupied).quantize(Decimal("0.01"))
        assert fin["7100"] == (room_rev * Decimal("0.10")).quantize(Decimal("0.01"))
        # Payments negative, revenue positive, and the TB nets to zero
        # (synthetic settlements exactly offset synthetic charges).
        for code in ("9002", "9003", "9004", "9005", "9007"):
            assert fin[code] < 0
        assert sum(fin.values()) == 0
        # Segments carry the SAME room revenue the TB carries.
        seg_total = next(s.value for s in day.segments
                         if s.segment_code == "TOTAL" and s.measure == "ROOM_REVENUE")
        assert seg_total == room_rev
        seg_rooms = next(s.value for s in day.segments
                         if s.segment_code == "TOTAL" and s.measure == "ROOMS")
        assert seg_rooms == occupied


def test_internal_coherence_autoclerk():
    for d in SPOT_DATES:
        day = synthetic_day("SSSJ", d)
        fin = {r.pms_trx_code: r.raw_amount for r in day.financial}
        stats = {r.metric_label: r.value for r in day.statistics}
        room_rent = fin["ROOM|ROOM_RENT"]
        assert room_rent > 0
        occupied = stats["Occupancy - Occupied"]
        assert stats["Occupancy - ADR"] == (room_rent / occupied).quantize(Decimal("0.01"))
        for code in ("CREDIT_CARDS|VISA", "CREDIT_CARDS|MASTERCARD",
                     "CREDIT_CARDS|AMERICAN_EXPRESS", "ACCOUNTS|DIRECT_BILL"):
            assert fin[code] < 0


def test_ledgers_are_opera_only_and_hotel_balance_adds_up():
    assert synthetic_day("SSSJ", date(2026, 2, 2)).ledgers == []
    curated = _curated_ledger_labels()
    for d in SPOT_DATES:
        ledgers = {r.ledger_label: r.amount for r in synthetic_day("HISJ", d).ledgers}
        assert set(ledgers) == curated
        assert ledgers["Hotel Balance"] == (
            ledgers["Guest Ledger / Balance Today"]
            + ledgers["AR Ledger / Balance Today"]
            + ledgers["Deposit Ledger / Balance Today"]
            + ledgers["Package Ledger / Balance Today"]
        )


def test_seasonality_is_present():
    def mean_occupied(pid: str, dates: list[date]) -> Decimal:
        vals = []
        label = "Rooms Occupied" if pid == "HISJ" else "Occupancy - Occupied"
        for d in dates:
            stats = {r.metric_label: r.value for r in synthetic_day(pid, d).statistics}
            vals.append(stats[label])
        return sum(vals) / len(vals)

    # Margins, not bare inequalities — noise alone must not pass this
    # (a flattened weekday/month table survived the bare > in review).
    july = [date(2026, 7, x) for x in range(1, 29)]
    january = [date(2026, 1, x) for x in range(1, 29)]
    assert mean_occupied("HISJ", july) - mean_occupied("HISJ", january) > 15
    # The economy property leans leisure: Saturdays beat Tuesdays.
    saturdays = [d for d in synthetic_dates() if d.weekday() == 5]
    tuesdays = [d for d in synthetic_dates() if d.weekday() == 1]
    assert mean_occupied("SSSJ", saturdays) - mean_occupied("SSSJ", tuesdays) > 3


def test_the_real_sample_figures_never_reappear():
    """The exact figures the real 2026-07-07 exports carried must not be
    reproducible from the generator — synthetic means synthetic."""
    hisj = {r.pms_trx_code: r.raw_amount for r in synthetic_day("HISJ", date(2026, 7, 7)).financial}
    assert hisj["1000"] != Decimal("10395.00")
    sssj = {r.pms_trx_code: r.raw_amount for r in synthetic_day("SSSJ", date(2026, 7, 7)).financial}
    assert sssj["ROOM|ROOM_RENT"] != Decimal("6129.03")


def test_full_pipeline_one_synthetic_day_per_property(db_session, founding_org):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    load_mappings(db_session, "mapping/autoclerk.yaml")
    db_session.commit()

    d = date(2026, 6, 15)
    for pid, source in PROPERTY_SOURCE.items():
        day = synthetic_day(pid, d)
        stage_records(db_session, day.financial,
                      source_file=f"synthetic:{pid}", file_hash=f"synth-{pid}")
        db_session.commit()
        result = transform(db_session, source=source, business_date=d, edition=12)
        db_session.commit()
        assert result.unmapped == 0, (pid, result)
        assert result.reconciled is True
        assert result.mapped > 0

        stage_statistics(db_session, day.statistics,
                         source_file=f"synthetic:{pid}", file_hash=f"synth-{pid}-stats")
        db_session.commit()
        stats = promote_statistics(db_session, "mapping/statistics.yaml",
                                   source=source, business_date=d)
        assert stats.promoted > 0

        from usali.segment_stage import stage_segments
        stage_segments(db_session, day.segments,
                       source_file=f"synthetic:{pid}", file_hash=f"synth-{pid}-seg")
        db_session.commit()
        segs = promote_segments(db_session, "mapping/segments.yaml",
                                source=source, business_date=d)
        assert segs.promoted_segments > 0


def test_the_cloud_seed_swaps_documents_for_the_synthetic_year():
    """job.sh passes --synthetic-year (call-site pin), and demo_seed's
    flag branch REPLACES the real-sample ingestion — the two paths are
    exclusive, so real figures cannot ride along with synthetic ones."""
    job = (REPO / "scripts" / "cloud" / "job.sh").read_text()
    assert "--synthetic-year" in job
    seed = (REPO / "scripts" / "demo_seed.py").read_text()
    assert "--synthetic-year" in seed
    branch = re.search(r"if args\.synthetic_year:\s*\n(.*?)\n\s*else:\s*\n(.*?)\n\n",
                       seed, re.DOTALL)
    assert branch is not None, "demo_seed lost the synthetic-vs-documents branch"
    assert "_seed_synthetic_year" in branch.group(1)
    assert "_seed_documents" in branch.group(2)
    assert "_seed_documents" not in branch.group(1)


def _load_demo_seed():
    spec = importlib.util.spec_from_file_location(
        "demo_seed_k6b", REPO / "scripts" / "demo_seed.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seeding_the_year_twice_is_a_no_op(db_session, founding_org, monkeypatch):
    """The cloud job re-runs on every deploy; the second pass must skip
    every already-transformed day rather than double-staging it."""
    import usali.synthetic_year as sy
    from sqlalchemy import func, select

    from usali.models import IngestBatch

    monkeypatch.setattr(sy, "synthetic_dates",
                        lambda: [date(2026, 6, 1), date(2026, 6, 2)])
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    load_mappings(db_session, "mapping/autoclerk.yaml")
    # The synthetic year now records ingestion_coverage, which carries a
    # composite (org_id, property_id) FK to property — so the properties must
    # exist first, exactly as _seed_world guarantees in the real cloud job
    # before _seed_synthetic_year runs.
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    demo_seed = _load_demo_seed()
    demo_seed._seed_synthetic_year(db_session)
    batches = db_session.scalar(select(func.count()).select_from(IngestBatch))
    assert batches > 0
    assert db_session.scalar(
        select(func.count()).select_from(IngestBatch)
        .where(IngestBatch.status != "transformed")) == 0

    demo_seed._seed_synthetic_year(db_session)
    assert db_session.scalar(
        select(func.count()).select_from(IngestBatch)) == batches
