# Ingestion-boundary redaction (Tier 0 #2) — design

**Date:** 2026-09-21. **Decided by:** the owner, 2026-09-21, resolving
`docs/ROADMAP.md` open decision 6: **redaction is destructive; raw bytes
never persist.** Built ahead of OH-23 (emailed intake), which the roadmap
makes dependent on this gate, so the email path inherits it rather than
adding a second raw-storage surface.

## 1. What the gate is about, measured

D8.4 of `docs/design/2026-08-16-data-posture-progressive-onboarding-design.md`
says guest identity and card PANs are stripped at the ingestion boundary so
"identity is stripped/tokenized before storage". Today that is true only of
the anonymous preview, which persists nothing. Every authenticated path
(`POST /ingest`, the night-audit upload, `usali process`, `usali watch`, the
seeds) writes the uploaded bytes to the inbox, processes them, and files the
raw file under `processed_dir` (or `failed_dir`) indefinitely.

What is in those files, measured 2026-09-21 on the real choiceADVANTAGE pack
at `~/Desktop/Sample Hotel/Skytouch/` (never committed):

| | |
|---|---|
| pages / sections | 42 / 26 |
| sections the registry recognizes | 2 (Hotel Journal Summary, Hotel Statistics) |
| words in recognized sections / total | 536 / 7,399 |
| sections with Luhn-valid 13-19 digit numbers | 5 (Credit Check List, Departure List, Guest Ledger, In House List, Rate Discrepancy) |

The product uses 7 percent of the pack. The other 93 percent is the guest
ledger, in-house, housekeeping, departure, no-show and credit-check lists:
guest identity by construction. The recognized sections carry labels and
figures only; their "name-shaped" tokens are `Total Occupied Rooms` and the
like. The HotelKey XLSX exports are the other shape: the settlement export
prints `Guest Name`, `First Name`, `Last Name`, `Account Name`, `Username`
and `Remarks` columns beside the figures, and every XLSX header block prints
`User: <operator>`.

Two facts about the store bound the change. Nothing reads a filed file after
processing (no download route, no page; only `usali watch` reads the inbox).
And on Cloud Run the three directories are container-local and vanish with
the revision, so today's retention is neither durable nor used; it is a
liability without a consumer.

Staged rows are already identity-free by adapter construction. The one
exception is deliberate and stays: `hotelkey_ar_aging` stages the Company
Name section's per-row labels (direct-bill account names) into
`ledger_label`, because the AR sub-ledger is what D-OH22.5 defers to the AR
slice. The gate does not make the retained artifact stricter than the staged
data; changing that is an AR decision, recorded in §6.

## 2. Decisions

**D1. The authenticated paths process from memory; no raw bytes reach disk
from an upload.** `POST /ingest` and `POST /{property}/night-audit/upload`
stop writing the payload to the inbox. The exclusive-create that guarded a
pending same-named inbox file goes with it; it protected a file that no
longer exists. Refusals before staging leave nothing behind, which the
night-audit tests already pin and which now holds trivially.

**D2. The ingestion API gains a bytes core; path functions are thin
wrappers that never move or delete the caller's file.**

```python
process_bytes(session, data, name, *, processed_dir, failed_dir, edition=12) -> ProcessResult
process_pack_bytes(...) -> list[ProcessResult]
process_document_bytes(...) -> list[ProcessResult]
process_file(session, path, ...)      # reads path, calls process_bytes; leaves path alone
process_pack(session, path, ...)      # likewise
process_document(session, path, ...)  # likewise
```

`_move` is deleted. `usali process <path>` leaves the operator's file where
it is. `usali watch` deletes the inbox file after processing, on either
outcome, because the artifact or the error record is now the trace and a
file left in the inbox would be re-read at the next start. The seeds and
`scripts/e2e_backend.py` copy samples into a temporary inbox and are
unaffected. `ProcessResult.destination` becomes the artifact path.

**D3. What is filed on success is a redacted words artifact, one per source
file.** `processed_dir/<stem>.<sha256[:8]>.redacted.json`:

```json
{
  "source_file": "night-audit-STDEMO-pack.pdf",
  "sha256": "<of the original bytes>",
  "bytes": 1239345,
  "received_at": "2026-09-21T09:14:02Z",
  "kind": "pack" | "single",
  "sections_dropped": ["A/R Aging", "Cancellation List", ...],
  "redaction": {"pans_masked": 0, "cells_dropped": 0, "header_cells_dropped": 1},
  "sections": [
    {"title": "Hotel Journal Summary", "pms_source": "SKYTOUCH", "report_type": "hotel_journal",
     "property_id": "STDEMO", "business_date": "2026-06-21",
     "words": [{"text": "...", "x0": 38.2, "top": 50.0}, ...]}
  ]
}
```

The `words` shape is the one the adapters consume and the committed
fixtures already use, so an artifact can be re-parsed by any future adapter
revision without the original file. A pack artifact holds its recognized
sections only; unrecognized sections are named in `sections_dropped` and
their words are discarded at the boundary. A single report is one section.
The hash is of the original bytes, so `IngestBatch.file_hash` keeps its
meaning and the seed's per-file skip keeps working.

**D4. What is filed on failure is an error record, never content.**
`failed_dir/<stem>.<sha256[:8]>.error.json` with `source_file`, `sha256`,
`bytes`, `received_at`, `error`. A failed parse is exactly the case where
the code cannot vouch for what the words contain, so nothing of them is
kept. The operator re-sends the file to debug. `IngestBatch` keeps its
`failed` row with the message as today.

**D5. Redaction rules, applied to the retained words only.** The adapters
parse the in-memory words unredacted (masking a label would break them;
the name regex in `redaction.py` matches `Total Occupied`), and staging
stays identity-free by adapter construction, pinned per adapter (for
example `tests/adaptors/test_hotelkey_xlsx.py::test_settlement_rows_are_transaction_grain_without_guest_names`).
Retention applies:

1. *PAN masking on every retained row.* Rows are `cluster_rows`; the row's
   words are joined with single spaces and scanned with the existing
   `_PAN_RUN` + Luhn check, so a card printed as four groups (`4111 1111
   1111 1111`) is caught even though it is four words. Every covered word
   becomes `••••`; the last covered word becomes `•••• <last4>`. Counted in
   `redaction.pans_masked`.
2. *XLSX column allowlist per report type.* For an XLSX report the policy
   names the header columns whose detail cells are retained; every other
   detail cell is dropped (an allowlist, so an unknown column is dropped,
   not kept). Settlement keeps `Account Category, Date, Time, Transaction
   Number, Folio Number, Room Number, Payment Type, Payment Description,
   Amount`. All Payments keeps `Payment Type, Amount`. AR aging keeps every
   column (§1, last paragraph). Title-block cells beginning `User:` are
   dropped for every XLSX report.
3. *PDF reports retain every word of a recognized section after rule 1.*
   The recognized report types are label-and-figure reports (§1); no
   per-column rule exists for them today, and the policy table makes that
   an explicit `keep_all` entry rather than a default.
4. *Name masking is not applied.* Guest identity is kept out by not
   retaining identity-bearing sections and columns, which is exact, rather
   than by the name regex, which is not.

**D6. The policy table is a closed set pinned against the pipeline
table.** `src/usali/retention.py` holds `RETENTION: dict[tuple[str, str],
Policy]` keyed exactly like `ingestion._PIPELINES`;
`tests/test_retention.py::test_every_pipeline_has_a_retention_policy` fails
when either side gains or loses a key. A new report type cannot ship
without saying what it retains.

**D7. Roadmap deltas.** `docs/ROADMAP.md` §2's "ingestion-boundary
redaction is preview-only" bullet and Tier 0 row 2 flip to shipped, open
decision 6 moves to the settled list, and `.github/roadmap.yml` gains
`OH-32 Ingestion-boundary redaction`, `shipped`, so the capability the bot
dedups against exists (Tier 0 row 2 had no id). OH-24's summary ("every
received audit pack is kept (redacted)") narrows to recognized reports in
the same edit; a future viewer that wants the whole pack must extend the
policy table section by section.

## 3. Rejected

- *Keep the raw file encrypted per org (ADR-005 key).* Full fidelity for a
  later viewer, but the card numbers stay at rest inside OH's footprint and
  every reader must decrypt. Rejected by the owner 2026-09-21.
- *Store raw, redact on promote.* Guarantees only what is already true of
  derived data; shrinks nothing. Contradicts D8.4 as written.
- *A redacted PDF.* pdfplumber does not write; rasterizing or rewriting
  content streams is a dependency and a fidelity problem for a file nothing
  reads. The words artifact is what the product parses.
- *Regex name masking on retained words.* Matches labels; see D5.4.

## 4. Data flow after the change

```
upload bytes ──> magic check ──> words (memory) ──> detect ──> handler(words) ──> stage/transform (DB, commit)
                                    │                                                  │
                                    └── retention policy ──> redacted words ──> processed_dir/*.redacted.json
   any failure: rollback, IngestBatch(failed), failed_dir/*.error.json, ProcessingError
```

The pack path splits pages in memory (`extract_pages_from_bytes`, new in
`adaptors/pdf.py`), validates every recognized section, stages under one
transaction, and files one artifact. The night-audit `_ingest_pack`
validation passes each section's title to `detect`, as `process_pack` does
(it did not; a section whose header window matches an earlier signature
was being skipped rather than recognized — the issue #78 shape inside the
upload validator). Pinned in `tests/test_night_audit.py`.

## 5. Tests that carry the guarantee

- `tests/test_ingestion_boundary.py::test_no_file_on_disk_carries_the_uploaded_bytes`:
  after `POST /ingest` and after a night-audit upload (single and pack),
  walk every file under the app's three directories and assert none has
  the payload's sha256 and none contains a 16-byte slice of the payload.
  This is the gate's own pin; the rest are supporting.
- `test_retention.py`: the pack artifact holds exactly the recognized
  sections and names the dropped ones; the settlement artifact has no
  `Guest Name` / `Username` / `Remarks` cell and no `User:` cell; a spaced
  PAN across four words is masked with last4 kept; a failed upload files an
  error record with no `words`; every `_PIPELINES` key has a policy.
- Existing filing assertions (18 sites) move from `<name>` to the artifact
  or error-record name.
- `usali watch` deletes the inbox file on success and on failure;
  `usali process` leaves its argument in place.
- Manual, recorded in the PR body, never committed: the real pack through
  `process_pack_bytes`; expected roughly 536 retained words in 2 sections,
  24 dropped section titles, `pans_masked == 0`, and a scan of the artifact
  for Luhn-valid runs returning none.

## 6. Out of scope, named

- AR aging's staged account names (§1). The AR slice decides whether
  direct-bill account names are counterparties or identity.
- A durable artifact store (GCS). Cloud Run's disk is ephemeral today for
  raw files and stays so for artifacts; OH-24 is where retention becomes a
  product feature and picks the bucket.
- The anonymous preview: unchanged, persists nothing.
- Hash-based duplicate refusal on upload. A re-sent file still creates a
  batch whose rows dedupe at the stage layer; refusing by hash is a
  night-audit UX decision, not a redaction one.
