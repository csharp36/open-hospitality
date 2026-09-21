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

The rule is calibrated on the committed corpus, not proved. The review of
the first cut (2026-09-20) disproved the soundness argument that stood here
with counterexamples in both directions, and the design records them as
the rule's known defeaters rather than pretending they do not exist:

- *A single report that fails the probe but resolves as a section.* The
  whole-file haystack is the 120-word window; the title haystack is a
  subset of it. First match over the larger text is not monotone with the
  smaller: a standalone SkyTouch Hotel Journal Summary that prints a
  `Rate Plan` column heading before its own title matches AutoClerk
  `rate_plan` whole-file (the issue #78 shape), trips the cross-check, and
  then resolves by title on the pack path. Two pages of it, with the
  column-heading row as page 2's top row, would ingest 20 of 33 words as
  a `transformed` batch. Same family: any multi-page report whose first
  120 words carry no signature phrase but whose page 2 top row is the
  title.
- *A pack that passes the probe.* Reorder the committed pack so the Hotel
  Journal Summary page is bound first and whole-file `detect` returns
  `SKYTOUCH hotel_journal STDEMO`; the router would hand all 187 words,
  four reports' worth, to the journal adapter. Today's sample is routed
  correctly because A/R Aging and the Cancellation List are bound first.

Neither shape is in the corpus, and the alternative router (a section
count) corrupts a file that is. The probe-first rule stays because it is
the best cheap rule available and degrades, in the second case, to what
`process_file` does today. The docstring says the same in one sentence.

Failure semantics of the probe itself: reading the bytes into words can
raise things that are not `ValueError` (a corrupt or encrypted PDF raises
pdfplumber's `PdfminerException`). Those are not routing signals. The
words are read outside the routing `try`; if reading fails the file goes
to `process_file`, which owns the quarantine contract (records the
`failed` batch, moves the file, raises `ProcessingError`). Only
`ValueError` from `detect` selects the pack path. Pinned in
`tests/test_process_document.py::test_a_corrupt_pdf_is_quarantined_by_the_single_report_path`.

Cost accepted: the file is read twice on the single-report path (once for
the probe, once inside `process_file`). The two callers are seeds over eight
small files. Threading pre-read words into `process_file` would touch its
signature and every caller for no user-facing gain.

**D2. Both scripts call `process_document`; the seed base loads every
dictionary a sample or an upload can need.** `_seed_base` and the e2e backend
load `opera.yaml` and `autoclerk.yaml` only. Without `skytouch.yaml` the pack's
journal rows land in `MappingException` instead of becoming facts (transform
records unmapped rows rather than failing, so the batch is `transformed` and
carries nothing); measured in review: 4 SkyTouch exception rows and 0 SkyTouch
facts with the load absent, 0 and 4 with it present. `hotelkey.yaml` is loaded
for parity: the committed HotelKey sample is a statistics report and touches
no dictionary row, so that load changes nothing today and is there for a
HotelKey financial upload against the seeded world. Both loads are upserts
(`load_mappings` uses `on_conflict_do_update` against the dictionary's unique
key), so `_seed_base` re-running on every cloud deploy is safe; a triple run
was measured clean. Pinned in
`tests/test_demo_seed_documents.py::test_seed_documents_ingests_every_sample_including_the_pack`
(zero exception rows, SkyTouch facts present).

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

1. `test_process_document.py`: pack sample routes to the pack path (exactly
   two results, both filed to `processed_dir`, no failed batch); manager
   report never touches `process_pack`; an XLSX whose probe WOULD raise (no
   properties seeded) still never touches `process_pack` and fails through
   `process_file`; a PDF that fails both ways raises one `ProcessingError`
   naming both reasons with one `failed` batch and the file in `failed_dir`;
   a `%PDF-` file of garbage is quarantined with one `failed` batch.
   `tests/adaptors/test_pack.py` (or the module that already tests
   `split_pack`) pins by name that the manager report splits into four
   sections of which one resolves by title.
2. `test_demo_seed_documents.py` as in D3; fails on main at the pack.
3. `scripts/e2e_backend.py` uses the same helper and dictionaries.
4. Full pytest, ruff, `mypy --strict src` green (venv synced with `--extra
   dev --extra face`).
