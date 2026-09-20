# `HOTEL STATISTICS` detect collision — standalone fix (plan)

Ships D-OH22.1 of
[`2026-09-08-oh22-hotelkey-design.md`](../design/2026-09-08-oh22-hotelkey-design.md)
on its own, per §8 decision 3 (2026-09-20). Nothing else from OH-22 comes with
it: **`HOTELKEY` is NOT registered** in `detect._REPORT_SIGNATURES`, because
`supported_pms_sources()` derives the signup dropdown from that table and §3's
blocked path is unresolved.

## The defect, proven

Run 2026-09-20 against the real sample
(`~/Desktop/Sample Hotel/HotelKey/Hotel Statistics - HK.pdf`, never committed):

```
>>> _parse_preview_sync(hotelkey_pdf_bytes)
{'status': 'unsupported', 'vendor': 'Skytouch', 'reason': 'no_preview_for_report'}
```

A HotelKey customer using `/try` is told their report is SkyTouch. Path:
`server._parse_preview_sync` → `detect_report_signature(words)` with no title
→ header window → `"HOTEL STATISTICS"` is in the first 120 words → first match
is the SkyTouch row. The registered-property path (`detect()`) is not reachable
for HotelKey today, since no property can be registered as `HOTELKEY`.

The two header windows, as extracted (`extract_pages` + `split_pack`):

```
HotelKey  : Summit Lodge Redstone, TX Date: Aug 13, 2026 RDQSM Report Run Date: Aug 14 2026 S
            Report Run Time: 10:04:20 AM MOCK DATA User: Sample DEVUSER RDQSM Hotel Statistics
            Room Statistics Description Actual Today M-T-D LY-M-T-D Y-T-D LY-T-D Total Rooms ...
SkyTouch  : Hotel Statistics Property Name: Econo Lodge Business Date: 6/21/2026
            Property Code: NM070 Shift: 4 User:* Room Statistics 6/21/2026 PTD Last Year PTD ...
```

Both print `User:`, so that is not a discriminator. `Property Name:` is on every
SkyTouch page (real pack, the committed fixture
`tests/fixtures/skytouch_hotel_statistics_words.json`, the generator
`scripts/gen_skytouch_mock_fixtures.py:217`, and every hand-built SkyTouch
header in `tests/test_detect.py`). HotelKey prints the property as a bare name
on line 1 and the code on line 2, and stamps `Report Run Date:` /
`Report Run Time:` top-right.

## Change 1 — `src/usali/detect.py`: signatures gain an optional header anchor

Replace the `(phrase, (pms_source, report_type))` tuples with a small frozen
dataclass:

```python
@dataclass(frozen=True)
class _Signature:
    phrase: str                      # uppercased; matched against title or header window
    pms_source: str
    report_type: str
    anchors: tuple[str, ...] = ()    # uppercased; ALL must appear in the header window
```

Only the `HOTEL STATISTICS` row gets an anchor: `anchors=("PROPERTY NAME:",)`.
Every other row stays anchorless, so their matching is unchanged.

`detect_report_signature(words, title)`:

- `haystack` is the title when present, else the header window (unchanged).
- `header_text` is ALWAYS the header window, `words[:_HEADER_WORD_LIMIT]`
  uppercased, regardless of whether a title was given. Anchors are checked
  against it. A pack section's `words` start at that page's top row, so the
  banner is inside the window on the title path too.
- The result is the first row whose phrase is in `haystack` AND whose anchors
  are all in `header_text`. First-match-wins is otherwise preserved.

`supported_pms_sources()` iterates `sig.pms_source`. `detect()` is unchanged
except for whatever the new return shape needs (it should need nothing: keep
returning `tuple[str, str] | None`).

Comments: per the repo rule on cross-boundary claims, the anchor's comment
names the enforcement point and the test, not a guarantee. Something like:
"`PROPERTY NAME:` is the SkyTouch page banner; HotelKey titles its statistics
report identically and does not print it. Pinned both ways in
`tests/test_detect_signature.py::test_hotelkey_statistics_does_not_match_skytouch`
and `::test_skytouch_statistics_still_matches_with_its_banner`."

## Change 2 — `src/usali/recognition.py`: name HotelKey from its stamp

After change 1 the HotelKey PDF falls through to `recognize_vendor`, whose
`HOTELKEY` phrase does not appear anywhere in the sample, so the customer
would get `unreadable` instead of `unsupported / HotelKey`. Rows become
`(phrases: tuple[str, ...], name)` with ALL phrases required; existing rows
become one-tuples; add `(("REPORT RUN DATE:", "REPORT RUN TIME:"), "HotelKey")`.
Keep the existing `("HOTELKEY",)` row too.

Recognition is advisory (it picks a display name for an "unsupported"
message and nothing else), so requiring both stamps is about not naming the
wrong vendor, not about parsing safety.

## Tests (write first, watch them fail, then implement)

`tests/test_detect_signature.py`, using the existing `_words` helper, with
headers in the samples' exact shape:

1. `test_hotelkey_statistics_does_not_match_skytouch` — HotelKey window
   (as above, through `Y-T-D LY-T-D`) → `None`. **Must fail before the fix**
   with `("SKYTOUCH", "hotel_statistics")`.
2. `test_skytouch_statistics_still_matches_with_its_banner` — SkyTouch window
   → `("SKYTOUCH", "hotel_statistics")`; and the same words with
   `title="Hotel Statistics"` → the same. Regression pin for the working source.
3. `test_anchor_is_checked_on_the_title_path_too` — `title="Hotel Statistics"`
   but words WITHOUT `Property Name:` → `None`. This is the test that dies if
   the anchor check is skipped when a title is given.
4. `test_anchorless_rows_are_unchanged` — one anchorless row (e.g.
   `HOTEL JOURNAL SUMMARY`) matches without any banner present.

`tests/test_detect.py`: `test_skytouch_hotel_statistics_is_registered` already
carries `Property Name:` and must stay green untouched. Add
`test_skytouch_property_uploading_hotelkey_statistics_fails_as_unrecognised`:
`detect(hotelkey_words, _ST_REGISTRY)` raises `ValueError` matching
`"report type"`, not the `"registered for"` cross-check message.

`tests/test_recognition.py`: HotelKey stamp (both phrases) → `"HotelKey"`;
`Report Run Date:` alone → `None`; the existing single-phrase rows still work.

`tests/test_preview_api.py`: mirror `test_preview_unsupported_vendor`'s
monkeypatch with the HotelKey statistics window and assert exactly
`{"status": "unsupported", "vendor": "HotelKey", "reason": "vendor_not_supported"}`.
This is the customer-visible pin; before the fix it returns
`vendor: "Skytouch"`.

`tests/test_b1_signup_api.py::…supported_pms_sources…` stays green: the
supported set must not change.

## Verification against the real samples (not committed)

Run and paste the output into the PR description:

```
uv run python - <<'EOF'
from usali.adaptors.pdf import extract_pages
from usali.adaptors.pack import split_pack
from usali.detect import detect_report_signature
from usali.server import _parse_preview_sync
hk = "/Users/csharpl/Desktop/Sample Hotel/HotelKey/Hotel Statistics - HK.pdf"
sk = "/Users/csharpl/Desktop/Sample Hotel/Skytouch/All_Night_Audit_Reports_NM070_STANDARD AUDIT PACK_2026-06-21.pdf"
print("hotelkey preview:", _parse_preview_sync(open(hk, "rb").read()))
for s in split_pack(extract_pages(sk)):
    if s.title == "Hotel Statistics":
        print("skytouch pack section:", detect_report_signature(s.words, s.title))
EOF
```

Expected: `{'status': 'unsupported', 'vendor': 'HotelKey', 'reason': 'vendor_not_supported'}`
and `('SKYTOUCH', 'hotel_statistics')`.

Then the full CI set, not targeted modules: `uv run ruff check`,
`uv run mypy --strict src`, `uv run pytest -q` (needs Docker for
testcontainers; ~10 minutes).

## Review

Two reviewers, told to disagree:

- **Mutation lens** (own worktree): delete the anchor tuple; skip the anchor
  check on the title path; make recognition require only one stamp. Each
  mutation must fail a named test. Report any that survives.
- **Reading lens**: every comment in the diff — is it local, or does it cite a
  test by name that exists? Does `supported_pms_sources()` still return
  exactly the same set? Is any existing SkyTouch fixture or generator header
  missing `Property Name:`?

## Out of scope, on purpose

- Registering `HOTELKEY` (gated on OH-22 §3; would change the signup dropdown).
- Any HotelKey parser.
- Roadmap files (design §7: none until §8 is settled — and §8 is now settled,
  but the roadmap delta belongs to the OH-22 slice, not this fix).
- The design doc itself is untracked and under the author's review; note the
  shipped PR number in the report so D-OH22.1 can be annotated at commit time.
