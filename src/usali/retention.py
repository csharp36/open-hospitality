"""What a filed ingest artifact keeps. Design D3-D6:
docs/design/2026-09-21-ingestion-boundary-redaction-design.md.

RETENTION is keyed exactly like ingestion._PIPELINES and pinned to it by
tests/test_retention.py::test_every_pipeline_has_a_retention_policy.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

from usali.adaptors.pdf import Word
from usali.adaptors.xlsx import XLSX_COL_STEP, XLSX_ROW_STEP
from usali.redaction import RedactionStats, redact_words


@dataclass(frozen=True)
class Policy:
    keep_columns: tuple[str, ...] | None = None  # XLSX allowlist; None = keep every word
    xlsx: bool = False  # title-block cells beginning "User:" are dropped for XLSX reports


KEEP_ALL = Policy()

RETENTION: dict[tuple[str, str], Policy] = {
    ("OPERA", "trial_balance"): KEEP_ALL,
    ("OPERA", "manager_flash"): KEEP_ALL,
    ("OPERA", "market_stats"): KEEP_ALL,
    ("AUTOCLERK", "transaction_summary"): KEEP_ALL,
    ("AUTOCLERK", "manager_report"): KEEP_ALL,
    ("AUTOCLERK", "rate_plan"): KEEP_ALL,
    ("SKYTOUCH", "hotel_journal"): KEEP_ALL,
    ("SKYTOUCH", "hotel_statistics"): KEEP_ALL,
    ("HOTELKEY", "hotel_statistics"): KEEP_ALL,
    ("HOTELKEY", "settlement"): Policy(xlsx=True, keep_columns=(
        "Account Category", "Date", "Time", "Transaction Number", "Folio Number",
        "Room Number", "Payment Type", "Payment Description", "Amount",
    )),
    ("HOTELKEY", "all_payments"): Policy(xlsx=True, keep_columns=("Payment Type", "Amount")),
    # Every column: the label column's account names are what the adapter
    # stages (design section 1, last paragraph); the AR slice owns that decision.
    ("HOTELKEY", "ar_aging"): Policy(xlsx=True, keep_columns=None),
}

_XLSX_HEADER_DROP_PREFIXES = ("User:",)


@dataclass
class RetainedSection:
    title: str
    pms_source: str
    report_type: str
    property_id: str
    business_date: date
    words: list[Word] = field(default_factory=list)


def _apply_columns(words: list[Word], keep: tuple[str, ...]) -> tuple[list[Word], int]:
    """Filter detail cells to an XLSX column allowlist (design D5.2).

    The header row is the first row (by ``round(top / XLSX_ROW_STEP)``) whose
    cell texts include every name in ``keep`` together; rows above it (title
    block, section captions) are free-form and pass through unfiltered, while
    the header row itself and every row below it keep only the cells whose
    column index (``round(x0 / XLSX_COL_STEP)``) belongs to a kept column —
    dropping the header labels, and every value beneath them, for any column
    not on the allowlist.
    """
    rows: dict[int, list[Word]] = defaultdict(list)
    for w in words:
        rows[round(w.top / XLSX_ROW_STEP)].append(w)

    header_idx: int | None = None
    for idx in sorted(rows):
        row_texts = {w.text for w in rows[idx]}
        if set(keep) <= row_texts:
            header_idx = idx
            break
    if header_idx is None:
        present = {w.text for w in words}
        missing = sorted(set(keep) - present)
        raise ValueError(f"no header row contains the kept columns: {', '.join(missing) or keep}")

    keep_cols = {round(w.x0 / XLSX_COL_STEP) for w in rows[header_idx] if w.text in keep}
    kept: list[Word] = []
    dropped = 0
    for w in words:
        row_idx = round(w.top / XLSX_ROW_STEP)
        if row_idx < header_idx:
            kept.append(w)
            continue
        if round(w.x0 / XLSX_COL_STEP) in keep_cols:
            kept.append(w)
        else:
            dropped += 1
    return kept, dropped


def _drop_header_cells(words: list[Word]) -> tuple[list[Word], int]:
    """Drop XLSX title-block cells naming the exporting operator (design D5.2,
    last sentence): any cell whose text starts with a prefix in
    ``_XLSX_HEADER_DROP_PREFIXES``."""
    kept = [w for w in words if not w.text.startswith(_XLSX_HEADER_DROP_PREFIXES)]
    return kept, len(words) - len(kept)


def retain_section(section: RetainedSection) -> tuple[RetainedSection, RedactionStats]:
    policy = RETENTION[(section.pms_source, section.report_type)]
    words = section.words
    cells_dropped = 0
    header_cells_dropped = 0
    if policy.keep_columns is not None:
        words, cells_dropped = _apply_columns(words, policy.keep_columns)
    if policy.xlsx:
        words, header_cells_dropped = _drop_header_cells(words)
    words, pan_stats = redact_words(words)
    stats = RedactionStats(
        pans_masked=pan_stats.pans_masked,
        cells_dropped=cells_dropped,
        header_cells_dropped=header_cells_dropped,
    )
    return replace(section, words=words), stats


def _received_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_artifact(
    *,
    source_file: str,
    data: bytes,
    kind: str,
    sections: list[RetainedSection],
    sections_dropped: list[str],
) -> dict[str, object]:
    total = RedactionStats()
    section_dicts: list[dict[str, object]] = []
    for section in sections:
        retained, stats = retain_section(section)
        total = RedactionStats(
            pans_masked=total.pans_masked + stats.pans_masked,
            cells_dropped=total.cells_dropped + stats.cells_dropped,
            header_cells_dropped=total.header_cells_dropped + stats.header_cells_dropped,
        )
        section_dicts.append(
            {
                "title": retained.title,
                "pms_source": retained.pms_source,
                "report_type": retained.report_type,
                "property_id": retained.property_id,
                "business_date": retained.business_date.isoformat(),
                "words": [{"text": w.text, "x0": w.x0, "top": w.top} for w in retained.words],
            }
        )
    return {
        "source_file": source_file,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "received_at": _received_at(),
        "kind": kind,
        "sections_dropped": list(sections_dropped),
        "redaction": {
            "pans_masked": total.pans_masked,
            "cells_dropped": total.cells_dropped,
            "header_cells_dropped": total.header_cells_dropped,
        },
        "sections": section_dicts,
    }


def write_artifact(processed_dir: Path, artifact: dict[str, object]) -> Path:
    stem = Path(str(artifact["source_file"])).stem or "upload"
    sha8 = str(artifact["sha256"])[:8]
    processed_dir.mkdir(parents=True, exist_ok=True)
    path = processed_dir / f"{stem}.{sha8}.redacted.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
    return path


def write_error_record(
    failed_dir: Path, *, source_file: str, data: bytes, error: str
) -> Path:
    stem = Path(source_file).stem or "upload"
    sha256 = hashlib.sha256(data).hexdigest()
    record: dict[str, object] = {
        "source_file": source_file,
        "sha256": sha256,
        "bytes": len(data),
        "received_at": _received_at(),
        "error": error[:500],
    }
    failed_dir.mkdir(parents=True, exist_ok=True)
    path = failed_dir / f"{stem}.{sha256[:8]}.error.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return path
