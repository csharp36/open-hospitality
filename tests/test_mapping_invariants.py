"""The bridge under the parity gate (cutover note §4): per-account journal
parity guarantees the statement's totals only while a fact's
(usali_schedule_id, usali_major_category) pair is a function of its GL
account. This file pins that invariant on the dictionaries themselves —
no DB, no posting — so it breaks the day a dictionary edit breaks it,
before an operator's totals do."""

from pathlib import Path
from typing import Any

import yaml

from usali.mapping.loader import MappingEntry

MAPPING_DIR = Path(__file__).resolve().parents[1] / "mapping"

# The row shape that classifies a mapping/*.yaml file as a PMS dictionary
# (mapping/ also holds statistics, ledgers, properties, and chart files
# with other shapes). Classification is deliberately looser than
# validation: MappingEntry is what enforces the full schema once a file
# is classified.
_DICTIONARY_KEYS = frozenset(
    {"source", "code", "edition", "major", "sub", "line_item"}
)

_KNOWN_DICTIONARIES = {"opera.yaml", "skytouch.yaml", "autoclerk.yaml"}


def _discover_dictionaries() -> dict[str, list[MappingEntry]]:
    """Glob mapping/*.yaml and parse every dictionary-shaped file through
    MappingEntry. Classification is by row shape, not by filename, so a
    future dictionary joins the invariant without anyone editing this
    test; test_every_known_dictionary_is_discovered is what notices a
    rename or removal. One dictionary-shaped row classifies the whole
    file, and every row of a classified file must then validate — a
    half-valid dictionary raises here rather than quietly dropping out
    of the invariant."""
    found: dict[str, list[MappingEntry]] = {}
    for path in sorted(MAPPING_DIR.glob("*.yaml")):
        raw: Any = yaml.safe_load(path.read_text())
        if not isinstance(raw, list):
            continue
        rows: list[Any] = raw
        if not any(
            isinstance(row, dict) and _DICTIONARY_KEYS.issubset(row)
            for row in rows
        ):
            continue
        found[path.name] = [MappingEntry(**row) for row in rows]
    return found


def test_every_known_dictionary_is_discovered() -> None:
    """A renamed or deleted dictionary must fail loudly here, not
    silently shrink what the invariant below covers."""
    assert _KNOWN_DICTIONARIES <= set(_discover_dictionaries())


def test_usali_classification_is_a_function_of_the_gl_account() -> None:
    """No gl_account_code may carry two distinct (schedule, major) pairs,
    within one dictionary or across them — the exact condition under
    which per-account parity implies per-bucket equality of the SOS
    totals (cutover note §4).

    That grain EXACTLY — do not "strengthen" this to (sub, line_item).
    The line grain fans out per trx code by design: account 1100 alone
    carries eleven line tuples across the dictionaries (note §4), so a
    sub/line version of this test can never pass and must not exist.

    Entries with gl_account_code null post nothing (note §2.1: a
    NULL-mapped fact enters neither side of parity) and sit outside the
    invariant."""
    classifications: dict[str, dict[tuple[int | None, str], list[str]]] = {}
    for name, entries in _discover_dictionaries().items():
        for e in entries:
            if e.gl_account_code is None:
                continue
            pair = (e.schedule_id, e.major)
            witnesses = classifications.setdefault(
                e.gl_account_code, {}
            ).setdefault(pair, [])
            witnesses.append(f"{name}:{e.source}/{e.code}")

    conflicts = {
        account: {pair: rows[:5] for pair, rows in pairs.items()}
        for account, pairs in classifications.items()
        if len(pairs) > 1
    }
    assert conflicts == {}, (
        "a GL account carries more than one (schedule, major) "
        "classification, so per-account parity no longer guarantees the "
        f"statement's totals: {conflicts}"
    )
