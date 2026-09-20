# SkyTouch statistics period fix (plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SkyTouch Hotel Statistics rows promote to `usali_statistic_fact` for
DAY and MTD (and prior-year YTD), not just the plain YTD column.

**Architecture:** One dict gains four entries in `src/usali/stats_promote.py`;
two tests in `tests/test_stats_promote.py` pin the behavior and the
cross-module label set. Design:
[`2026-09-20-skytouch-statistics-periods-design.md`](../design/2026-09-20-skytouch-statistics-periods-design.md).

**Tech Stack:** Python 3.13, SQLAlchemy, pytest with the `db_session` /
`founding_org` fixtures from `tests/conftest.py` (Postgres via testcontainers,
needs Docker).

---

### Task 1: Pin the defect, then fix the map

**Files:**
- Modify: `src/usali/stats_promote.py:12-15`
- Test: `tests/test_stats_promote.py`

- [ ] **Step 1: Write the two failing tests**

Append to `tests/test_stats_promote.py` (the file already imports `date`,
`Decimal`, `select`, `pytest`, `UsaliStatisticFact`, `StatisticRecord`,
`promote_statistics`, `stage_statistics`; add the new imports at the top):

```python
import json
from pathlib import Path

from usali.adaptors import skytouch_hotel_statistics
from usali.adaptors.pdf import Word
from usali.stats_promote import _CANONICAL_PERIODS
```

```python
def _skytouch_fixture_records() -> list[StatisticRecord]:
    # The committed words of the real Standard Audit Pack section: five curated
    # metrics x five period columns = 25 records, all with pms_source SKYTOUCH.
    words = [
        Word(text=x["text"], x0=x["x0"], top=x["top"])
        for x in json.loads(
            Path("tests/fixtures/skytouch_hotel_statistics_words.json").read_text()
        )
    ]
    return skytouch_hotel_statistics.parse_hotel_statistics(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )


def test_skytouch_day_and_mtd_facts_promote(db_session):
    recs = _skytouch_fixture_records()
    assert len(recs) == 25 and {r.period_label for r in recs} == {
        "ACTUAL", "PTD", "LY_PTD", "YTD", "LY_YTD"
    }
    stage_statistics(db_session, recs, source_file="pack.pdf", file_hash="st1")
    db_session.commit()
    result = promote_statistics(
        db_session, "mapping/statistics.yaml", source="SKYTOUCH",
        business_date=date(2026, 6, 21),
    )
    db_session.commit()
    # Every one of the 25 rows is a curated metric in a canonical period.
    assert (result.promoted, result.ignored, result.skipped) == (25, 0, 0)
    facts = db_session.execute(select(UsaliStatisticFact)).scalars().all()
    by_key = {(f.metric_code, f.period, f.is_prior_year): f.value for f in facts}
    assert by_key[("ROOMS_OCCUPIED", "DAY", False)] == Decimal("62")      # ACTUAL
    assert by_key[("ADR", "MTD", False)] == Decimal("90.10")              # PTD
    assert by_key[("ADR", "MTD", True)] == Decimal("92.25")               # LY_PTD
    assert by_key[("ROOM_REVENUE", "YTD", True)] == Decimal("435800.00")  # LY_YTD
    assert {f.period for f in facts} == {"DAY", "MTD", "YTD"}


def test_every_skytouch_period_label_is_canonical():
    # The adapter's labels and the promote map live in different modules; this
    # is the pin that fails if either side changes alone.
    missing = set(skytouch_hotel_statistics._PERIODS) - set(_CANONICAL_PERIODS)
    assert not missing, f"SkyTouch period labels with no canonical period: {missing}"
```

- [ ] **Step 2: Run them and watch both fail for the right reason**

Run: `uv run pytest tests/test_stats_promote.py -q -p no:cacheprovider`

Expected: `test_skytouch_day_and_mtd_facts_promote` FAILS at the
`(result.promoted, result.ignored, result.skipped) == (25, 0, 0)` line with
`(5, 20, 0)`; `test_every_skytouch_period_label_is_canonical` FAILS with
`{'ACTUAL', 'PTD', 'LY_PTD', 'LY_YTD'}`. The two pre-existing tests still pass.
If the first test fails anywhere else, stop and report; the fixture or the
adapter is not what the plan assumes.

- [ ] **Step 3: Extend the map and reword its comment**

Replace lines 12-15 of `src/usali/stats_promote.py` with:

```python
# Canonical reporting periods, keyed by the label each adapter stages. Opera
# and AutoClerk stage DAY/Today, MONTH/MTD, YEAR/YTD; SkyTouch stages its
# column headings (ACTUAL, PTD, LY_PTD, YTD, LY_YTD) and already sets
# is_prior_year on the LY_* rows, which is copied through unchanged. Anything
# not listed (Yesterday, Tomorrow, ...) stays stage-only by design; the model
# is lenient, these are KPIs not ledger data. The SkyTouch label set is pinned
# against this table in
# tests/test_stats_promote.py::test_every_skytouch_period_label_is_canonical.
_CANONICAL_PERIODS = {
    "DAY": "DAY", "Today": "DAY", "ACTUAL": "DAY",
    "MONTH": "MTD", "MTD": "MTD", "PTD": "MTD", "LY_PTD": "MTD",
    "YEAR": "YTD", "YTD": "YTD", "LY_YTD": "YTD",
}
```

- [ ] **Step 4: Run the module, then the gates**

Run: `uv run pytest tests/test_stats_promote.py -q -p no:cacheprovider`
Expected: 4 passed.

Run: `uv run ruff check && uv run mypy --strict src && uv run pytest -q -p no:cacheprovider`
Expected: ruff `All checks passed!`, mypy `Success`, pytest all green
(main has 2309 passed / 4 skipped; expect 2311 passed / 4 skipped; the 4 skips are the onnxruntime-gated face-model tests).

- [ ] **Step 5: Commit**

```bash
git add src/usali/stats_promote.py tests/test_stats_promote.py
git commit -m "fix(stats): SkyTouch ACTUAL/PTD/LY_* periods promote as DAY/MTD/YTD

The adapter stages its column headings; the promote map knew only the
Opera/AutoClerk labels, so every SkyTouch row but the plain YTD column was
ignored and no DAY or MTD statistic fact existed for a SkyTouch property.
Pinned on the real-pack fixture and as a cross-module label-set check."
```

(Append the session's attribution trailer lines to the commit message.)

### Task 2: Optional verification against the real pack

Only if `~/Desktop/Sample Hotel/Skytouch/All_Night_Audit_Reports_NM070_STANDARD AUDIT PACK_2026-06-21.pdf`
exists. Never commit it or anything derived from it.

- [ ] **Step 1:** Extract the Hotel Statistics section with the repo's own
  readers and count periods:

```bash
uv run python - <<'PY'
from pathlib import Path
from datetime import date
from usali.adaptors.pdf import extract_pages
from usali.adaptors.pack import split_pack
from usali.adaptors.skytouch_hotel_statistics import parse_hotel_statistics, extract_business_date
from usali.stats_promote import _CANONICAL_PERIODS
from collections import Counter
pdf = Path.home() / "Desktop/Sample Hotel/Skytouch/All_Night_Audit_Reports_NM070_STANDARD AUDIT PACK_2026-06-21.pdf"
pages = extract_pages(pdf)
for s in split_pack(pages):
    if "HOTEL STATISTICS" in s.title.upper():
        recs = parse_hotel_statistics(s.words, property_id="NM070", business_date=extract_business_date(s.words))
        c = Counter(r.period_label for r in recs)
        print(c, "uncanonical:", {p for p in c if p not in _CANONICAL_PERIODS})
PY
```

  Expected: five period labels, `uncanonical: set()`. If the function names in
  `pdf.py` / `pack.py` differ, adapt the script; report the exact output. Do not
  edit source for this task.
