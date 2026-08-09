"""Renderers for the P6 reports (Summary Operating Statement, mapping coverage).

Pure presentation layer: report dataclasses in, strings out. No database
access — reports come from `usali.reporting`. SOS formats:

- text: sectioned fixed-width layout for terminals
- json: nested structure with Decimals as exact strings (never floats)
- csv:  flat quoted rows `(section, sub, line_item, period, value)`

The coverage report renders as text (per-source sections with the needs-review
worklist) and json (nested dict, ints and strings only — never floats).

Flat fact-table exports (`usali.reporting.export_rows`) render via `rows_to_csv`
(quoted CSV, header from the first row's column order) and `rows_to_json` (a JSON
array of string-valued objects).

The CPA monthly pack renders as text (`render_cpa_pack_text`), json
(`render_cpa_pack_json`; Decimals as exact strings), and three per-report CSVs
(`sales_report_csv` / `tax_report_csv` / `ar_report_csv`) — CPAs import each
report into its own spreadsheet, so csv is per-report, never one mixed stream.
"""

import csv
import io
import json
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from usali.reporting import (
    CoverageReport,
    CpaPack,
    NeedsReviewEntry,
    SosLine,
    SosReport,
    SourceCoverage,
)

_RULE_WIDTH = 76
_LABEL_WIDTH = 58
_AMOUNT_WIDTH = 18
_METRIC_WIDTH = 20
_STAT_COL_WIDTH = 12
_EDITION_NOTE = "Revenue-side statement; expenses live in accounting."


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _amount_row(label: str, amount: Decimal, indent: int = 0) -> str:
    """Label left, amount right — always at least one space between them."""
    pad = " " * indent
    return f"{pad}{label:<{_LABEL_WIDTH - indent}} {_money(amount):>{_AMOUNT_WIDTH}}"


def _percent(part: Decimal, whole: Decimal) -> str:
    if whole == 0:
        return "n/a"
    pct = (part / whole * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{pct}%"


def _period_heading(sos: SosReport) -> str:
    if sos.business_date is not None:
        return f"Business date: {sos.business_date.isoformat()}"
    return f"Period: {sos.date_from} .. {sos.date_to}"


def _line_section(
    out: list[str], heading: str, lines: list[SosLine], total_label: str, total: Decimal
) -> None:
    out.append("")
    out.append(heading)
    for line in lines:
        out.append(_amount_row(line.line_item, line.total, indent=4))
    out.append(_amount_row(total_label, total, indent=2))


def render_sos_text(sos: SosReport) -> str:
    """Sectioned fixed-width text layout of the full SOS."""
    rule = "=" * _RULE_WIDTH
    out: list[str] = [
        rule,
        "SUMMARY OPERATING STATEMENT",
        f"Property: {sos.property_id}    PMS source: {sos.pms_source}",
        _period_heading(sos),
        _EDITION_NOTE,
        rule,
        "",
        "OPERATED DEPARTMENTS",
    ]
    for dept in sos.operated_departments:
        out.append(f"  {dept.sub_category}")
        for line in dept.lines:
            out.append(_amount_row(line.line_item, line.total, indent=4))
        out.append(_amount_row(f"Total {dept.sub_category}", dept.total, indent=2))

    _line_section(
        out,
        "MISCELLANEOUS INCOME",
        sos.misc_income,
        "Total Miscellaneous Income",
        sos.misc_income_total,
    )
    out.append("")
    out.append(_amount_row("TOTAL OPERATING REVENUE", sos.total_operating_revenue))

    if sos.rooms_segments:
        out.append("")
        out.append("ROOMS SEGMENT SPLIT")
        out.append(f"  {'Segment':<30} {'Rooms':>12} {'Revenue':>16} {'% Revenue':>12}")
        total_revenue = sum((s.room_revenue for s in sos.rooms_segments), Decimal("0"))
        for seg in sos.rooms_segments:
            pct = _percent(seg.room_revenue, total_revenue)
            out.append(
                f"  {seg.segment:<30} {seg.rooms:>12} {_money(seg.room_revenue):>16} {pct:>12}"
            )

    if sos.taxes:
        _line_section(
            out,
            "TAXES COLLECTED (PASS-THROUGH)",
            sos.taxes,
            "Total Taxes Collected",
            sos.taxes_total,
        )
    if sos.settlements:
        _line_section(
            out, "SETTLEMENTS", sos.settlements, "Total Settlements", sos.settlements_total
        )
    if sos.other:
        _line_section(out, "OTHER (UNSCHEDULED)", sos.other, "Total Other", sos.other_total)

    if sos.statistics:
        has_prior = any(
            row.day_prior is not None or row.mtd_prior is not None or row.ytd_prior is not None
            for row in sos.statistics
        )
        columns = ["DAY", "MTD", "YTD"]
        if has_prior:
            columns += ["DAY PY", "MTD PY", "YTD PY"]
        out.append("")
        out.append("STATISTICS")
        out.append(
            "  "
            + "Metric".ljust(_METRIC_WIDTH)
            + " "
            + " ".join(c.rjust(_STAT_COL_WIDTH) for c in columns)
        )
        for row in sos.statistics:
            values = [row.day, row.mtd, row.ytd]
            if has_prior:
                values += [row.day_prior, row.mtd_prior, row.ytd_prior]
            cells = " ".join(("" if v is None else str(v)).rjust(_STAT_COL_WIDTH) for v in values)
            out.append(f"  {row.metric_code:<{_METRIC_WIDTH}} {cells}")

    return "\n".join(out) + "\n"


def _line_dict(line: SosLine) -> dict[str, str]:
    return {
        "major": line.major,
        "sub_category": line.sub_category,
        "line_item": line.line_item,
        "total": str(line.total),
    }


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def render_sos_json(sos: SosReport) -> str:
    """Nested JSON with Decimals as exact strings and dates as ISO strings."""
    payload: dict[str, object] = {
        "report": "summary_operating_statement",
        "property_id": sos.property_id,
        "pms_source": sos.pms_source,
        "business_date": None if sos.business_date is None else sos.business_date.isoformat(),
        "date_from": None if sos.date_from is None else sos.date_from.isoformat(),
        "date_to": None if sos.date_to is None else sos.date_to.isoformat(),
        "operated_departments": [
            {
                "sub_category": dept.sub_category,
                "total": str(dept.total),
                "lines": [_line_dict(line) for line in dept.lines],
            }
            for dept in sos.operated_departments
        ],
        "misc_income": [_line_dict(line) for line in sos.misc_income],
        "misc_income_total": str(sos.misc_income_total),
        "total_operating_revenue": str(sos.total_operating_revenue),
        "taxes": [_line_dict(line) for line in sos.taxes],
        "taxes_total": str(sos.taxes_total),
        "settlements": [_line_dict(line) for line in sos.settlements],
        "settlements_total": str(sos.settlements_total),
        "other": [_line_dict(line) for line in sos.other],
        "other_total": str(sos.other_total),
        "rooms_segments": [
            {
                "segment": seg.segment,
                "rooms": str(seg.rooms),
                "room_revenue": str(seg.room_revenue),
            }
            for seg in sos.rooms_segments
        ],
        "statistics": [
            {
                "metric_code": row.metric_code,
                "day": _opt(row.day),
                "mtd": _opt(row.mtd),
                "ytd": _opt(row.ytd),
                "day_prior": _opt(row.day_prior),
                "mtd_prior": _opt(row.mtd_prior),
                "ytd_prior": _opt(row.ytd_prior),
            }
            for row in sos.statistics
        ],
    }
    return json.dumps(payload, indent=2)


def _line_rows(section: str, lines: list[SosLine]) -> list[list[str]]:
    return [
        [section, line.sub_category, line.line_item, "TOTAL", str(line.total)] for line in lines
    ]


def render_sos_csv(sos: SosReport) -> str:
    """Flat quoted CSV: one `(section, sub, line_item, period, value)` row per cell."""
    rows: list[list[str]] = [
        ["section", "sub", "line_item", "period", "value"],
        ["meta", "property_id", "", "", sos.property_id],
        ["meta", "pms_source", "", "", sos.pms_source],
    ]
    if sos.business_date is not None:
        rows.append(["meta", "business_date", "", "", sos.business_date.isoformat()])
    else:
        rows.append(
            [
                "meta",
                "date_from",
                "",
                "",
                "" if sos.date_from is None else sos.date_from.isoformat(),
            ]
        )
        rows.append(
            ["meta", "date_to", "", "", "" if sos.date_to is None else sos.date_to.isoformat()]
        )

    for dept in sos.operated_departments:
        rows += _line_rows("operated_departments", dept.lines)
        rows.append(["operated_departments_total", dept.sub_category, "", "TOTAL", str(dept.total)])
    rows += _line_rows("misc_income", sos.misc_income)
    rows.append(["misc_income_total", "", "", "TOTAL", str(sos.misc_income_total)])
    rows.append(["total_operating_revenue", "", "", "TOTAL", str(sos.total_operating_revenue)])
    rows += _line_rows("taxes", sos.taxes)
    rows.append(["taxes_total", "", "", "TOTAL", str(sos.taxes_total)])
    rows += _line_rows("settlements", sos.settlements)
    rows.append(["settlements_total", "", "", "TOTAL", str(sos.settlements_total)])
    rows += _line_rows("other", sos.other)
    rows.append(["other_total", "", "", "TOTAL", str(sos.other_total)])

    for seg in sos.rooms_segments:
        rows.append(["rooms_segments", seg.segment, "rooms", "TOTAL", str(seg.rooms)])
        rows.append(["rooms_segments", seg.segment, "room_revenue", "TOTAL", str(seg.room_revenue)])

    for row in sos.statistics:
        for period, value in (
            ("DAY", row.day),
            ("MTD", row.mtd),
            ("YTD", row.ytd),
            ("DAY_PRIOR", row.day_prior),
            ("MTD_PRIOR", row.mtd_prior),
            ("YTD_PRIOR", row.ytd_prior),
        ):
            if value is not None:
                rows.append(["statistics", row.metric_code, "", period, str(value)])

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerows(rows)
    return buf.getvalue()


# --- Flat fact-table exports for ERP/BI ------------------------------------------


def rows_to_csv(rows: list[dict[str, str]], fieldnames: Sequence[str] | None = None) -> str:
    """Flat export rows as quoted CSV.

    The header comes from `fieldnames` when given (pass `reporting.export_columns`
    so an export that selects zero rows still emits a header-only file), otherwise
    from the first row's keys. With zero rows and no `fieldnames` there is nothing
    to derive a header from, so the result is the empty string.
    """
    if fieldnames is None:
        if not rows:
            return ""
        fieldnames = list(rows[0])
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=list(fieldnames), quoting=csv.QUOTE_ALL, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def rows_to_json(rows: list[dict[str, str]]) -> str:
    """Flat export rows as a JSON array of string-valued objects (`[]` when empty)."""
    return json.dumps(rows, indent=2)


# --- Mapping coverage and confidence report -------------------------------------

_CODE_WIDTH = 28
_SEGMENT_WIDTH = 24


def _counts_inline(counts: dict[str, int]) -> str:
    return "  ".join(f"{name} {count}" for name, count in counts.items()) or "(none)"


def _worklist(out: list[str], heading: str, entries: list[NeedsReviewEntry]) -> None:
    if not entries:
        return
    out.append(f"    {heading}:")
    for e in entries:
        item = e.line_item
        if len(item) > _SEGMENT_WIDTH:
            item = item[: _SEGMENT_WIDTH - 1] + "…"  # make the cut visible
        note = "" if e.notes is None else f"  {e.notes}"
        out.append(f"      {e.code:<{_CODE_WIDTH}} {item.ljust(_SEGMENT_WIDTH)}{note}".rstrip())


def _source_section(out: list[str], sc: SourceCoverage) -> None:
    out.append("")
    out.append(f"SOURCE: {sc.pms_source}")
    out.append("-" * _RULE_WIDTH)

    fin = sc.financial
    out.append(f"  Financial dictionary: {fin.dictionary_entries} entries")
    out.append(f"    Confidence:    {_counts_inline(fin.by_confidence)}")
    out.append(f"    Review status: {_counts_inline(fin.by_review_status)}")
    out.append(f"    Staged trx codes mapped: {fin.mapped_codes}/{fin.staged_codes}")
    if fin.missing_codes:
        out.append(f"    MISSING from dictionary: {', '.join(fin.missing_codes)}")
    out.append(f"    Mapping exceptions banked: {fin.exception_count}")
    out.append(
        f"    GL mapping: {fin.gl_mapped}/{fin.dictionary_entries} entries carry a GL account"
    )
    if fin.gl_unmapped_codes:
        out.append(
            f"    GL UNMAPPED (QBO push refuses these): {', '.join(fin.gl_unmapped_codes)}"
        )
    _worklist(out, "Needs-review worklist", fin.needs_review)

    seg = sc.segments
    out.append("")
    out.append(f"  Segments dictionary: {seg.dictionary_entries} entries")
    out.append(f"    Staged segment codes mapped: {seg.mapped_codes}/{seg.staged_codes}")
    if seg.unmapped_codes:
        out.append(f"    UNMAPPED (promotion fails on these): {', '.join(seg.unmapped_codes)}")
    _worklist(out, "Needs-review worklist", seg.needs_review)

    stats = sc.statistics
    out.append("")
    out.append(f"  Statistics dictionary: {stats.dictionary_entries} entries")
    out.append(
        f"    Staged labels mapped: {stats.mapped_labels}/{stats.staged_labels} "
        f"({len(stats.unmapped_labels)} stage-only; matching is lenient by design)"
    )
    for label in stats.unmapped_labels:
        out.append(f"      {label}")


def render_coverage_text(report: CoverageReport) -> str:
    """Per-source sections: dictionary breakdowns, staged-vs-mapped, review worklists."""
    rule = "=" * _RULE_WIDTH
    out: list[str] = [rule, "MAPPING COVERAGE REPORT", rule]
    if not report.sources:
        out.append("")
        out.append("No staged data — nothing to cover.")
    for sc in report.sources:
        _source_section(out, sc)
    return "\n".join(out) + "\n"


def _needs_review_dicts(entries: list[NeedsReviewEntry]) -> list[dict[str, str | None]]:
    return [{"code": e.code, "line_item": e.line_item, "notes": e.notes} for e in entries]


def render_coverage_json(report: CoverageReport) -> str:
    """Nested JSON: counts as ints, names as strings — never floats."""
    payload: dict[str, object] = {
        "report": "mapping_coverage",
        "sources": [
            {
                "pms_source": sc.pms_source,
                "financial": {
                    "dictionary_entries": sc.financial.dictionary_entries,
                    "by_confidence": sc.financial.by_confidence,
                    "by_review_status": sc.financial.by_review_status,
                    "needs_review": _needs_review_dicts(sc.financial.needs_review),
                    "staged_codes": sc.financial.staged_codes,
                    "mapped_codes": sc.financial.mapped_codes,
                    "missing_codes": sc.financial.missing_codes,
                    "exception_count": sc.financial.exception_count,
                    "gl_mapped": sc.financial.gl_mapped,
                    "gl_unmapped_codes": sc.financial.gl_unmapped_codes,
                },
                "segments": {
                    "dictionary_entries": sc.segments.dictionary_entries,
                    "needs_review": _needs_review_dicts(sc.segments.needs_review),
                    "staged_codes": sc.segments.staged_codes,
                    "mapped_codes": sc.segments.mapped_codes,
                    "unmapped_codes": sc.segments.unmapped_codes,
                },
                "statistics": {
                    "dictionary_entries": sc.statistics.dictionary_entries,
                    "staged_labels": sc.statistics.staged_labels,
                    "mapped_labels": sc.statistics.mapped_labels,
                    "unmapped_labels": sc.statistics.unmapped_labels,
                },
            }
            for sc in report.sources
        ],
    }
    return json.dumps(payload, indent=2)


# --- CPA monthly pack (sales, tax, A/R) -------------------------------------------

_DAYS_WIDTH = 6
_BALANCE_WIDTH = 16


def render_cpa_pack_text(pack: CpaPack) -> str:
    """Sectioned fixed-width text layout of the full CPA pack."""
    rule = "=" * _RULE_WIDTH
    out: list[str] = [
        rule,
        "CPA MONTHLY PACK",
        f"Property: {pack.property_id}    PMS source: {pack.pms_source}",
        f"Month: {pack.month}",
        rule,
        "",
        "SALES REPORT",
    ]
    if pack.sales.lines:
        # Label field mirrors _amount_row(indent=2) so the MTD column's right edge
        # lines up with every other amount in the pack; Days trails to the right.
        label_width = _LABEL_WIDTH - 2
        out.append(
            f"  {'Line item':<{label_width}} {'MTD':>{_AMOUNT_WIDTH}} {'Days':>{_DAYS_WIDTH}}"
        )
        for line in pack.sales.lines:
            label = f"{line.sub_category} / {line.line_item}"
            if len(label) > label_width:
                label = label[: label_width - 1] + "…"  # make the cut visible
            out.append(
                f"  {label:<{label_width}} {_money(line.mtd_amount):>{_AMOUNT_WIDTH}}"
                f" {line.day_count:>{_DAYS_WIDTH}}"
            )
    else:
        out.append("  (no revenue facts this month)")
    out.append(_amount_row("TOTAL OPERATING REVENUE", pack.sales.total_operating_revenue))

    out.append("")
    out.append("TAX REPORT")
    for tax in pack.taxes.lines:
        gl = tax.gl_account_code or "-"
        out.append(_amount_row(f"{tax.line_item}  (GL {gl})", tax.mtd_amount, indent=4))
    if not pack.taxes.lines:
        out.append("  (no tax facts this month)")
    out.append(_amount_row("Total Taxes Collected", pack.taxes.taxes_total, indent=2))
    out.append(
        _amount_row("Room Revenue (occupancy-tax base)", pack.taxes.room_revenue_base, indent=2)
    )

    out.append("")
    out.append("A/R LEDGER BALANCES")
    if pack.ar.balances:
        out.append(
            f"  {'Ledger':<{_SEGMENT_WIDTH}} {'Opening':>{_BALANCE_WIDTH}}"
            f" {'Closing':>{_BALANCE_WIDTH}} {'Movement':>{_BALANCE_WIDTH}}"
        )
        for bal in pack.ar.balances:
            out.append(
                f"  {bal.ledger_code:<{_SEGMENT_WIDTH}}"
                f" {_money(bal.opening_balance):>{_BALANCE_WIDTH}}"
                f" {_money(bal.closing_balance):>{_BALANCE_WIDTH}}"
                f" {_money(bal.movement):>{_BALANCE_WIDTH}}"
            )
    else:
        out.append("  (no ledger balance facts this month)")

    return "\n".join(out) + "\n"


def render_cpa_pack_json(pack: CpaPack) -> str:
    """Nested JSON with Decimals as exact strings (never floats)."""
    payload: dict[str, object] = {
        "report": "cpa_pack",
        "property_id": pack.property_id,
        "pms_source": pack.pms_source,
        "month": pack.month,
        "sales_report": {
            "lines": [
                {
                    "major": line.major,
                    "sub_category": line.sub_category,
                    "line_item": line.line_item,
                    "mtd_amount": str(line.mtd_amount),
                    "day_count": line.day_count,
                }
                for line in pack.sales.lines
            ],
            "total_operating_revenue": str(pack.sales.total_operating_revenue),
        },
        "tax_report": {
            "lines": [
                {
                    "line_item": tax.line_item,
                    "gl_account_code": tax.gl_account_code,
                    "mtd_amount": str(tax.mtd_amount),
                }
                for tax in pack.taxes.lines
            ],
            "taxes_total": str(pack.taxes.taxes_total),
            "room_revenue_base": str(pack.taxes.room_revenue_base),
        },
        "ar_report": {
            "balances": [
                {
                    "ledger_code": bal.ledger_code,
                    "ledger_name": bal.ledger_name,
                    "opening_balance": str(bal.opening_balance),
                    "closing_balance": str(bal.closing_balance),
                    "movement": str(bal.movement),
                }
                for bal in pack.ar.balances
            ],
        },
    }
    return json.dumps(payload, indent=2)


def _csv_text(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerows(rows)
    return buf.getvalue()


def sales_report_csv(pack: CpaPack) -> str:
    """Quoted CSV: one row per sales line, then the TOTAL_OPERATING_REVENUE row."""
    rows: list[list[str]] = [["major", "sub_category", "line_item", "mtd_amount", "day_count"]]
    rows += [
        [line.major, line.sub_category, line.line_item, str(line.mtd_amount),
         str(line.day_count)]
        for line in pack.sales.lines
    ]
    rows.append(
        ["TOTAL_OPERATING_REVENUE", "", "", str(pack.sales.total_operating_revenue), ""]
    )
    return _csv_text(rows)


def tax_report_csv(pack: CpaPack) -> str:
    """Quoted CSV: tax lines, then TAXES_TOTAL and ROOM_REVENUE_BASE rows."""
    rows: list[list[str]] = [["line_item", "gl_account_code", "mtd_amount"]]
    rows += [
        [tax.line_item, tax.gl_account_code or "", str(tax.mtd_amount)]
        for tax in pack.taxes.lines
    ]
    rows.append(["TAXES_TOTAL", "", str(pack.taxes.taxes_total)])
    rows.append(["ROOM_REVENUE_BASE", "", str(pack.taxes.room_revenue_base)])
    return _csv_text(rows)


def ar_report_csv(pack: CpaPack) -> str:
    """Quoted CSV of ledger balances (header-only when the property has none)."""
    rows: list[list[str]] = [
        ["ledger_code", "ledger_name", "opening_balance", "closing_balance", "movement"]
    ]
    rows += [
        [bal.ledger_code, bal.ledger_name, str(bal.opening_balance),
         str(bal.closing_balance), str(bal.movement)]
        for bal in pack.ar.balances
    ]
    return _csv_text(rows)
