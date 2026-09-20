# Issue #138: the sample seeds route a pack through the single-report path (design)

**Date:** 2026-09-20. **Scope:** one routing helper in `ingestion.py`, its
two callers (`scripts/demo_seed.py`, `scripts/e2e_backend.py`), tests.

## The defect, measured

`scripts/demo_seed.py::_seed_documents` and `scripts/e2e_backend.py` (the
Playwright web server's seed) both glob `docs/reference/samples/*.pdf` and
hand every file to `process_file`. That path detects by the first 120 words
of the whole file. For `SkyTouch - Standard Audit Pack (mock).pdf` the window
is the A/R Aging and Cancellation List pages, and the Cancellation List's
`RATE PLAN` column heading matches the AutoClerk `rate_plan` signature
first. `mapping/properties.yaml` registers `REDSTONE TEST INN` as
`SKYTOUCH`, so `detect` raises its source cross-check error, `process_file`
records a `failed` batch, quarantines the file and raises `ProcessingError`,
and neither script catches it. The pack sample has been in that directory
since 2026-08-23 (`ef8b9bc`), so the local document seed and the Playwright
backend have been failing at that file for a month. Playwright is not in CI
(`ci.yml` has no e2e job), which is why nothing went red.

Detection of every committed sample on main, whole file versus per section
(`detect(section.words, registry, section.title)`; `-` means the section
resolves to nothing):

| sample | whole-file `detect` | sections recognized / total | words in recognized sections / total |
|---|---|---|---|
| Autoclerk - Manager Report | AUTOCLERK manager_report SSSJ | 1 / 4 | 104 / 336 |
| Autoclerk - Revenue by Rate Plan | AUTOCLERK rate_plan SSSJ | 0 / 1 | 0 / 337 |
| Autoclerk - Transaction Summary | AUTOCLERK transaction_summary SSSJ | 1 / 2 | 218 / 425 |
| HotelKey - Hotel Statistics (mock) | HOTELKEY hotel_statistics HKDEMO | 0 / 3 | 0 / 545 |
| Manager Flash - Opera | OPERA manager_flash HISJ | 0 / 1 | 0 / 535 |
| Market Code Statistics - Opera | OPERA market_stats HISJ | 0 / 1 | 0 / 745 |
| **SkyTouch - Standard Audit Pack (mock)** | **raises: STDEMO is SKYTOUCH, report looks like AUTOCLERK** | **2 / 4** | 133 / 187 |
| Trial Balance - Opera | OPERA trial_balance HISJ | 0 / 1 | 0 / 304 |

Two things the table settles. Whole-file detection succeeds for every single
report and fails only for the pack. And `split_pack` carves a multi-page
single report at every page-title change (the manager report's pages 2 to 4
carry column-heading rows as their "titles"), so `process_pack` on a single
report would ingest only the pages whose titles resolve and silently drop
the rest: 232 of the manager report's 336 words. Issue #138's fix sketch
("more than one detected section title") is therefore rejected here; a
section count cannot be the router.

## Decisions

**D1. One helper, `ingestion.process_document`, routes by trying the
single-report detection first.** Signature and contract:

```python
def process_document(session, path, *, processed_dir, failed_dir, edition=12) -> list[ProcessResult]
```

- Not a PDF (by magic bytes, `reader.is_pdf`): `process_file`, wrapped in a
  one-element list. Packs are PDF-only (`process_pack` calls
  `extract_pages`), so an XLSX never takes the pack path.
- PDF: `detect(read_words_from_bytes(data), load_registry(session))`. If it
  returns, `process_file`. If it raises `ValueError`, `process_pack`.
- If the pack path also fails, re-raise `ProcessingError` whose message
  carries both reasons, single-report first. `process_pack` has already
  recorded the `failed` batch and quarantined the file; the wrapper records
  nothing.

Why the rule is sound rather than lucky: whole-file and per-section
detection resolve the property the same way (the registry cross-check in
`detect`), and a section's words begin with that section's own header, which
for the first section is the same header the whole-file window reads. So a
file whose first section resolves by title also resolves whole-file, unless
the whole-file window matched an earlier signature that disagrees with the
property's source. That disagreement is exactly what a pack produces, because
its first page is whichever report the PMS bound first. The converse, a
single report that fails whole-file but resolves as a pack, needs a page
title to match a signature its own header window did not, which the
first-match rule in `detect_report_signature` does not allow. The behavior
is pinned, not asserted:
`tests/test_process_document.py::test_single_report_never_takes_the_pack_path`
monkeypatches `process_pack` to fail loudly and runs the manager report.

Cost accepted: the file is read twice on the single-report path (once for
the probe, once inside `process_file`). The two callers are seeds over eight
small files. Threading pre-read words into `process_file` would touch its
signature and every caller for no user-facing gain.

**D2. Both scripts call `process_document`; the seed base loads every
dictionary the samples need.** `_seed_base` and the e2e backend load
`opera.yaml` and `autoclerk.yaml` only. Without `skytouch.yaml` and
`hotelkey.yaml`, the pack and the HotelKey sample would now ingest with every
row landing in `MappingException` (transform records unmapped rows rather
than failing), which is a `transformed` batch that carries no facts. Both
scripts gain the two `load_mappings` calls. `test_skytouch_end_to_end.py` and
`test_hotelkey_end_to_end.py` load the same files the same way.

**D3. The demo seed's document step is pinned end to end.**
`tests/test_demo_seed_documents.py` runs `_seed_base` then `_seed_documents`
against the real sample directory and asserts every sample's content hash
has a `transformed` batch, no batch is `failed`, the pack yields exactly two
`SKYTOUCH` batches (journal and statistics), and a second run adds nothing.
That test fails on main with `ProcessingError` at the pack, which is the
issue reproduced.

## Not in scope

- The night-audit API keeps its two endpoints (single report, pack); the
  operator chooses there. The CLI `ingest` and `watch` commands stay on
  `process_file`; routing them is a product decision, not a seed fix.
- The `split_pack` page-title heuristic. It is the reason the router must be
  conservative, and it is unchanged.
- Playwright specs are not run here. Whether any of them pins a count that
  the two extra samples change is checked by reading them (plan Task 2) and
  reported, not fixed.

## Acceptance

1. `test_process_document.py`: pack sample routes to the pack path (two
   results, filed, no failed batch); manager report never touches
   `process_pack`; a HotelKey XLSX never touches `process_pack`; a PDF that
   fails both ways raises one `ProcessingError` naming both reasons with one
   `failed` batch and the file in `failed_dir`.
2. `test_demo_seed_documents.py` as in D3; fails on main at the pack.
3. `scripts/e2e_backend.py` uses the same helper and dictionaries.
4. Full pytest, ruff, `mypy --strict src` green (venv synced with `--extra
   dev --extra face`).
