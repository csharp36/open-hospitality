"""Mapping coverage and confidence report (P6 Task 3) against real seeded data.

Seeds the six sample PDFs via the shared `seed_six_pdfs` fixture, then asserts on
the coverage report: financial dictionary confidence/review breakdowns, staged vs
mapped trx codes, segment coverage, and the (lenient by design) statistics
leftovers. Consolidated test functions keep the expensive seeding runs few.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select

from usali.models import (
    IngestBatch,
    PmsDailyFinancialStage,
    UsaliFinancialFact,
    UsaliMappingDictionary,
)
from usali.render import render_coverage_json, render_coverage_text
from usali.reporting import coverage_report


def test_coverage_report_contents(db_session, seed_six_pdfs):
    report = coverage_report(db_session)

    assert [s.pms_source for s in report.sources] == ["AUTOCLERK", "OPERA"]
    opera = next(s for s in report.sources if s.pms_source == "OPERA")
    autoclerk = next(s for s in report.sources if s.pms_source == "AUTOCLERK")

    # --- OPERA financial dictionary: every staged trx code mapped, no exceptions.
    fin = opera.financial
    assert fin.staged_codes == 14
    assert fin.mapped_codes == 14
    assert fin.missing_codes == []
    assert fin.exception_count == 0

    # Breakdowns cover the whole dictionary, no entry left uncounted.
    assert sum(fin.by_confidence.values()) == fin.dictionary_entries
    assert sum(fin.by_review_status.values()) == fin.dictionary_entries

    # The needs-review worklist is nonempty and carries the parking mapping (5105)
    # with its line item and note intact.
    assert fin.by_review_status["needs-review"] == len(fin.needs_review) > 0
    parking = next(e for e in fin.needs_review if e.code == "5105")
    assert parking.line_item == "Parking"
    assert parking.notes == "Parking classification unverified"

    # --- AUTOCLERK financial: also fully mapped (strict pipeline already proved it).
    assert autoclerk.financial.missing_codes == []
    assert autoclerk.financial.exception_count == 0
    assert autoclerk.financial.staged_codes == autoclerk.financial.mapped_codes > 0

    # --- GL mapping (P8): every dictionary entry carries a QBO GL account, so the
    # journal-entry push has an account for every fact it will ever see.
    for source in (opera, autoclerk):
        assert source.financial.gl_unmapped_codes == []
        assert source.financial.gl_mapped == source.financial.dictionary_entries

    # End-to-end plumbing proof: loader -> dictionary -> transform -> fact. Every
    # promoted fact carries a GL account; the Opera Parking fact (Sch 4) lands on
    # the misc-income account specifically.
    facts = db_session.execute(select(UsaliFinancialFact)).scalars().all()
    assert facts
    assert all(f.gl_account_code is not None for f in facts)
    parking_fact = next(
        f for f in facts if f.pms_source == "OPERA" and f.usali_line_item == "Parking"
    )
    assert parking_fact.gl_account_code == "4200"

    # --- AUTOCLERK segments: all 21 staged rate-plan codes mapped, and nearly all
    # of them are LOW-confidence brand rate plans awaiting review.
    seg = autoclerk.segments
    assert seg.staged_codes == 21
    assert seg.mapped_codes == 21
    assert seg.unmapped_codes == []
    assert len(seg.needs_review) >= 19

    # --- Statistics are lenient by design: unmapped staged labels are reported as
    # informational leftovers, never errors.
    for source in (opera, autoclerk):
        stats = source.statistics
        assert stats.mapped_labels > 0
        assert len(stats.unmapped_labels) > 0
        assert stats.staged_labels == stats.mapped_labels + len(stats.unmapped_labels)


def test_coverage_renderers(db_session, seed_six_pdfs):
    report = coverage_report(db_session)

    text = render_coverage_text(report)
    assert "MAPPING COVERAGE REPORT" in text
    assert "SOURCE: OPERA" in text
    assert "SOURCE: AUTOCLERK" in text
    # The analyst-facing worklist: every needs-review mapping is visible with its note.
    assert "5105" in text
    assert "Parking classification unverified" in text
    assert "CLC" in text  # segment needs-review entries surface too
    # Staged-vs-mapped counts and the lenient statistics leftovers are stated.
    assert "14/14" in text
    assert "21/21" in text
    assert "lenient" in text
    # The GL mapping block (P8) states dictionary-entries-with-GL-account counts.
    assert "GL mapping" in text

    payload = json.loads(render_coverage_json(report))
    assert payload["report"] == "mapping_coverage"
    by_source = {s["pms_source"]: s for s in payload["sources"]}
    opera = by_source["OPERA"]
    assert opera["financial"]["staged_codes"] == 14
    assert opera["financial"]["mapped_codes"] == 14
    assert opera["financial"]["exception_count"] == 0
    assert {"code": "5105", "line_item": "Parking", "notes": "Parking classification unverified"} \
        in opera["financial"]["needs_review"]
    assert opera["financial"]["gl_mapped"] == opera["financial"]["dictionary_entries"]
    assert opera["financial"]["gl_unmapped_codes"] == []
    autoclerk = by_source["AUTOCLERK"]
    assert autoclerk["segments"]["staged_codes"] == 21
    assert autoclerk["segments"]["mapped_codes"] == 21
    assert len(autoclerk["segments"]["needs_review"]) >= 19
    assert len(autoclerk["statistics"]["unmapped_labels"]) > 0

    # JSON carries no floats anywhere — counts are ints, everything else strings.
    def _no_floats(node: object) -> None:
        assert not isinstance(node, float)
        if isinstance(node, dict):
            for v in node.values():
                _no_floats(v)
        elif isinstance(node, list):
            for v in node:
                _no_floats(v)

    _no_floats(payload)


def _dictionary_row(
    code: str, edition: int, line_item: str, gl: str | None = None
) -> UsaliMappingDictionary:
    return UsaliMappingDictionary(
        pms_source="OPERA", pms_trx_code=code, usali_edition=edition, usali_schedule_id=1,
        usali_major_category="Operated Departments", usali_sub_category="Rooms",
        usali_line_item=line_item, gl_account_code=gl, review_status="needs-review",
    )


def _staged_synthetic_row(db_session) -> None:
    batch = IngestBatch(
        pms_source="OPERA", report_type="trial_balance",
        source_file="synthetic.pdf", file_hash="0" * 64,
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(PmsDailyFinancialStage(
        property_id="TEST", pms_source="OPERA", report_type="trial_balance",
        business_date=date(2026, 7, 7), pms_trx_code="1000", raw_amount=Decimal("1.00"),
        source_file="synthetic.pdf", ingest_batch_id=batch.batch_id, row_hash="a" * 64,
    ))


def test_gl_unmapped_dictionary_rows_surface(db_session, founding_org):
    # A dictionary row without a GL account is a QBO-push blocker (Task 4 refuses
    # to build journal entries over GL-orphaned facts) — it must land on the
    # gl_unmapped_codes worklist, edition-scoped like the rest of financial coverage.
    _staged_synthetic_row(db_session)
    db_session.add(_dictionary_row("1000", 12, "Transient Rooms Revenue", gl="4000"))
    db_session.add(_dictionary_row("5999", 12, "Mystery Fee"))  # no GL account
    db_session.add(_dictionary_row("9911", 11, "Legacy Edition-11 Item"))  # other edition
    db_session.flush()

    fin = coverage_report(db_session).sources[0].financial
    assert fin.gl_mapped == 1
    assert fin.gl_unmapped_codes == ["5999"]  # the edition-11 row must not pollute

    text = render_coverage_text(coverage_report(db_session))
    assert "GL mapping" in text
    assert "5999" in text


# QBO account types the placeholder CoA may use. Deliberately excludes free-form
# strings: the mock QBO serves these as-is and real QBO rejects unknown types.
_QBO_ACCOUNT_TYPES = {
    "Income",
    "Bank",
    "Other Current Asset",
    "Accounts Receivable",
    "Other Current Liability",
    "Expense",
}


def test_gl_account_codes_exist_in_chart_of_accounts():
    # Every GL account referenced by the source dictionaries must exist in the
    # placeholder chart of accounts (mapping/qbo_accounts.yaml) the mock QBO serves,
    # and every entry must carry one — otherwise the QBO push has nowhere to post.
    accounts = yaml.safe_load(Path("mapping/qbo_accounts.yaml").read_text())
    account_codes = {a["account_code"] for a in accounts}
    assert len(account_codes) == len(accounts)  # no duplicate account codes
    assert all(a["name"] for a in accounts)
    assert all(a["account_type"] in _QBO_ACCOUNT_TYPES for a in accounts)
    # Task 4's JE builder posts direct-bill settlement debits to 1200 and hardcodes
    # 1210 (Guest Ledger Clearing) as the balancing line — both must exist.
    assert {"1200", "1210"} <= account_codes

    referenced: set[str] = set()
    for source_yaml in ("mapping/opera.yaml", "mapping/autoclerk.yaml"):
        for row in yaml.safe_load(Path(source_yaml).read_text()):
            gl = row.get("gl_account_code")
            assert gl, f"{source_yaml}: {row['code']} has no gl_account_code"
            assert gl in account_codes, (
                f"{source_yaml}: {row['code']} references GL account {gl} "
                f"missing from mapping/qbo_accounts.yaml"
            )
            referenced.add(gl)

    # Reverse direction: no dead accounts in the CoA. 1210 is exempt — it is the
    # Task 4 JE balancing line, written by the JE builder, never by a dictionary
    # entry; every other account must be referenced by at least one entry.
    assert account_codes - referenced == {"1210"}


def test_coverage_is_edition_scoped(db_session, founding_org):
    # The dictionary is keyed on (source, code, edition) and transform() maps against
    # ONE edition — coverage must be scoped the same way, or a second edition's rows
    # would double-count entries and pollute the worklist.
    _staged_synthetic_row(db_session)
    db_session.add(_dictionary_row("1000", 12, "Transient Rooms Revenue"))
    db_session.add(_dictionary_row("9911", 11, "Legacy Edition-11 Item"))
    db_session.flush()

    # Default edition (12): the edition-11 phantom row must not appear anywhere.
    fin = coverage_report(db_session).sources[0].financial
    assert fin.dictionary_entries == 1
    assert [e.code for e in fin.needs_review] == ["1000"]
    assert fin.mapped_codes == 1 and fin.missing_codes == []

    # Explicit edition 11: only the legacy row, and the staged code counts as missing.
    legacy = coverage_report(db_session, edition=11).sources[0].financial
    assert legacy.dictionary_entries == 1
    assert [e.code for e in legacy.needs_review] == ["9911"]
    assert legacy.mapped_codes == 0 and legacy.missing_codes == ["1000"]


def test_coverage_empty_database(db_session):
    # No staged data at all: an empty report, not an error — and the text renderer
    # says so instead of printing a bare banner.
    report = coverage_report(db_session)
    assert report.sources == []
    assert "nothing to cover" in render_coverage_text(report)
