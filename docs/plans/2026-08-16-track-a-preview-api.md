# Track A — Part 1: Anonymous parse-preview API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public, stateless `POST /api/preview` that turns an uploaded PMS PDF into a USALI P&L preview — **persisting nothing** — plus the pure units it needs.

**Architecture:** Reuse the existing pure parse path (`pdf.py → detect → adaptors/*`). Add a **signature-only** detection function (the anonymous preview has no property registry, so it cannot call `detect()`, which raises without one), a `recognition` step for known-but-unsupported vendors, a pure `preview` mapper (records → payload via the in-memory `mapping/*.yaml`, no DB), a defensive `redaction` pass, an in-process rate limiter, and one ungated endpoint. No ORM session, no persistence, no multi-tenancy.

**Tech Stack:** Python 3.12, FastAPI/Starlette, pydantic v2, pdfplumber, PyYAML, pytest. Design spec: [`docs/design/2026-08-16-track-a-front-door-preview-design.md`](../design/2026-08-16-track-a-front-door-preview-design.md). Data-posture constraints: D8 (`docs/design/2026-08-16-data-posture-progressive-onboarding-design.md`).

**Scope note.** This branch (`feat/onboarding-track-a`, off `main`) has **Opera + AutoClerk** adapters only — SkyTouch (PR #54) is unmerged. So the previewable set is the **financial family**: Opera `trial_balance` and AutoClerk `transaction_summary`. The KPI/statistics family and the SkyTouch pack are a later plan (they drop into the same dispatch). The frontend front-door UI is **Part 2** (separate plan; frontend recon already done).

**Standing rules:** fixtures are **synthetic / fictitious-by-construction** — never a real PMS file. `mypy --strict` (src only) + `ruff` must stay clean. Frequent commits.

---

## File structure

- `src/usali/detect.py` — MODIFY: extract `detect_report_signature(words)`; `detect()` delegates to it.
- `src/usali/recognition.py` — CREATE: `recognize_vendor(words)` over a known-but-unsupported vendor registry.
- `src/usali/preview.py` — CREATE: `PreviewPayload`, `PnlLine`, `build_financial_preview(...)`, in-memory mapping loader.
- `src/usali/redaction.py` — CREATE: `mask_pans`, `mask_names` (utility), `redact(payload)` defensive boundary pass.
- `src/usali/ratelimit.py` — CREATE: `RateLimiter` in-process token/sliding-window, injectable clock.
- `src/usali/adaptors/pdf.py` — MODIFY: add `extract_words_from_bytes(data)` (in-memory, no temp file); `extract_words` delegates.
- `src/usali/server.py` — MODIFY: add ungated `POST /api/preview` + a `RateLimiter` on `app.state`.
- Tests: `tests/test_detect_signature.py`, `tests/test_recognition.py`, `tests/test_preview.py`, `tests/test_redaction.py`, `tests/test_ratelimit.py`, `tests/test_preview_api.py`.

---

## Task 1: Signature-only detection

**Files:**
- Modify: `src/usali/detect.py`
- Test: `tests/test_detect_signature.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detect_signature.py
from usali.adaptors.pdf import Word
from usali.detect import detect, detect_report_signature


def _words(*texts: str) -> list[Word]:
    return [Word(text=t, x0=float(i), top=0.0) for i, t in enumerate(texts)]


def test_detect_report_signature_matches_supported_header():
    assert detect_report_signature(_words("OPERA", "TRIAL", "BALANCE")) == (
        "OPERA",
        "trial_balance",
    )


def test_detect_report_signature_none_when_unknown():
    assert detect_report_signature(_words("HotelKey", "Final", "Audit")) is None


def test_detect_still_resolves_property_via_registry():
    # regression: detect() unchanged for the persisting path
    words = _words("OPERA", "TRIAL", "BALANCE", "REDSTONE", "INN")
    registry = [{"match": "REDSTONE INN", "property_id": "RS1", "pms_source": "OPERA"}]
    det = detect(words, registry)
    assert (det.pms_source, det.report_type, det.property_id) == (
        "OPERA",
        "trial_balance",
        "RS1",
    )
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError: cannot import name 'detect_report_signature'`)

Run: `uv run pytest tests/test_detect_signature.py -q`

- [ ] **Step 3: Implement** — in `src/usali/detect.py`, add the function and delegate:

```python
def detect_report_signature(words: list[Word]) -> tuple[str, str] | None:
    """Match only the (pms_source, report_type) report signature from the header,
    WITHOUT resolving a property. Returns None if no supported signature matches.

    The anonymous preview uses this: it has no property registry, so it cannot
    call detect() (which raises unless a registered property resolves).
    """
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()
    return next((sig for phrase, sig in _REPORT_SIGNATURES if phrase in header_text), None)
```

Then refactor the head of `detect()` to reuse it:

```python
def detect(words: list[Word], registry: Sequence[Mapping[str, str]]) -> Detection:
    match = detect_report_signature(words)
    if match is None:
        raise ValueError("could not detect report type from PDF header")
    pms_source, report_type = match
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()
    for row in registry:
        ...  # unchanged property-resolution loop
```

- [ ] **Step 4: Run — expect PASS.** Then `uv run pytest tests/test_detect_registry.py -q` (regression) and `uv run mypy src && uv run ruff check src tests`.

- [ ] **Step 5: Commit** — `git commit -am "feat(detect): signature-only detection for the anonymous preview"`

---

## Task 2: Vendor recognition (known-but-unsupported)

**Files:**
- Create: `src/usali/recognition.py`
- Test: `tests/test_recognition.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recognition.py
from usali.adaptors.pdf import Word
from usali.recognition import recognize_vendor


def _words(*texts: str) -> list[Word]:
    return [Word(text=t, x0=float(i), top=0.0) for i, t in enumerate(texts)]


def test_recognizes_known_unsupported_vendor():
    assert recognize_vendor(_words("HotelKey", "Final", "Audit", "Report")) == "HotelKey"


def test_returns_none_for_supported_or_unknown():
    assert recognize_vendor(_words("OPERA", "TRIAL", "BALANCE")) is None  # supported -> detect wins
    assert recognize_vendor(_words("random", "invoice")) is None
```

- [ ] **Step 2: Run — expect FAIL** (module missing).

- [ ] **Step 3: Implement** `src/usali/recognition.py`:

```python
from usali.adaptors.pdf import Word

# Known-but-unsupported PMS vendor header phrases -> vendor display name.
# Consulted ONLY when detect_report_signature() returns None, so the preview
# can say "looks like HotelKey" instead of a blank "unreadable". Sourced from
# docs/reference/pms-variants.md. A vendor here that later gains an adapter is
# harmless: detect() matches first, so recognition is never reached for it.
_VENDOR_SIGNATURES: list[tuple[str, str]] = [
    ("HOTELKEY", "HotelKey"),
    ("SKYTOUCH", "SkyTouch"),
    ("CHOICEADVANTAGE", "SkyTouch"),
    ("CLOUDBEDS", "Cloudbeds"),
    ("MEWS", "Mews"),
    ("APALEO", "Apaleo"),
    ("VISUAL MATRIX", "Visual Matrix"),
    ("ROOMMASTER", "roomMaster"),
    ("WEBREZPRO", "WebRezPro"),
]
_HEADER_WORD_LIMIT = 120


def recognize_vendor(words: list[Word]) -> str | None:
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()
    return next((name for phrase, name in _VENDOR_SIGNATURES if phrase in header_text), None)
```

- [ ] **Step 4: Run — expect PASS** + mypy/ruff clean.

- [ ] **Step 5: Commit** — `git commit -am "feat(recognition): identify known-but-unsupported PMS vendors"`

---

## Task 3: Preview mapper (records → payload, no DB)

**Files:**
- Create: `src/usali/preview.py`
- Test: `tests/test_preview.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_preview.py
from datetime import date
from decimal import Decimal

from usali.preview import build_financial_preview
from usali.schemas import StagedRecord


def _rec(code: str, amount: str, desc: str | None = None) -> StagedRecord:
    return StagedRecord(
        property_id="PREVIEW",
        pms_source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pms_trx_code=code,
        pms_trx_desc=desc,
        raw_amount=Decimal(amount),
    )


def test_build_financial_preview_aggregates_and_counts():
    # 1000 = Rooms/Room Revenue (reviewed); 9004 = Settlements/Visa (reviewed);
    # 5105 = Miscellaneous Income/Parking (needs-review); 8888 = unmapped.
    records = [
        _rec("1000", "5487.00"),
        _rec("9004", "-5487.00"),
        _rec("5105", "120.00"),
        _rec("8888", "0.00", desc="mystery line"),
    ]
    p = build_financial_preview(
        source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        records=records,
    )
    lines = {(l.major, l.line_item): l.amount for l in p.pnl_lines}
    assert lines[("Operated Departments", "Room Revenue")] == Decimal("5487.00")
    assert lines[("Settlements", "Visa")] == Decimal("-5487.00")
    assert p.codes_recognized == 4
    assert p.codes_mapped == 3
    assert p.codes_needs_review == 2  # 5105 (needs-review) + 8888 (unmapped)
    assert p.net_total == Decimal("120.00")
    assert p.balanced is False


def test_balanced_true_when_net_zero():
    p = build_financial_preview(
        source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        records=[_rec("1000", "5487.00"), _rec("9004", "-5487.00")],
    )
    assert p.balanced is True
    assert p.net_total == Decimal("0")
```

- [ ] **Step 2: Run — expect FAIL** (module missing).

- [ ] **Step 3: Implement** `src/usali/preview.py`:

```python
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import yaml

from usali.schemas import StagedRecord

# mapping/*.yaml live at the repo root (src/usali/preview.py -> parents[2] == repo root).
_MAPPING_DIR = Path(__file__).resolve().parents[2] / "mapping"


@dataclass(frozen=True)
class PnlLine:
    major: str
    sub: str
    line_item: str
    amount: Decimal


@dataclass(frozen=True)
class Kpi:
    label: str
    value: Decimal


@dataclass(frozen=True)
class PreviewPayload:
    pms_source: str
    report_type: str
    business_date: date
    pnl_lines: list[PnlLine]
    kpis: list[Kpi] = field(default_factory=list)  # populated by the later stats family
    codes_recognized: int = 0
    codes_mapped: int = 0
    codes_needs_review: int = 0
    net_total: Decimal = Decimal("0")
    balanced: bool = False


@lru_cache(maxsize=None)
def _load_mapping(source: str, edition: int) -> dict[str, dict]:
    path = _MAPPING_DIR / f"{source.lower()}.yaml"
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return {
        str(r["code"]): r
        for r in rows
        if r.get("source") == source and r.get("edition") == edition
    }


def build_financial_preview(
    *,
    source: str,
    report_type: str,
    business_date: date,
    records: list[StagedRecord],
    edition: int = 12,
) -> PreviewPayload:
    mapping = _load_mapping(source, edition)

    buckets: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    seen: set[str] = set()
    mapped: set[str] = set()
    needs_review: set[str] = set()
    net_total = Decimal("0")

    for r in records:
        code = r.pms_trx_code
        seen.add(code)
        net_total += r.raw_amount
        m = mapping.get(code)
        if m is None:
            needs_review.add(code)  # unmapped -> a human should look
            continue
        mapped.add(code)
        if m.get("review_status") == "needs-review":
            needs_review.add(code)
        key = (m["major"], m.get("sub") or "", m["line_item"])
        buckets[key] += r.raw_amount

    pnl_lines = [
        PnlLine(major=k[0], sub=k[1], line_item=k[2], amount=v)
        for k, v in sorted(buckets.items())
    ]
    return PreviewPayload(
        pms_source=source,
        report_type=report_type,
        business_date=business_date,
        pnl_lines=pnl_lines,
        codes_recognized=len(seen),
        codes_mapped=len(mapped),
        codes_needs_review=len(needs_review),
        net_total=net_total,
        balanced=net_total == Decimal("0"),
    )
```

> **Honesty note (D8 reconciliation principle):** `balanced` is a strict
> net-to-zero check on the parsed rows, and `net_total` is exposed raw — the
> frontend shows "✓ ties out" only when `balanced` is true, never a fabricated
> claim. Unmapped codes are counted into `codes_needs_review` and **excluded**
> from `pnl_lines` (never silently bucketed).

- [ ] **Step 4: Run — expect PASS** + mypy/ruff clean. (The test relies on real rows in `mapping/opera.yaml`: `1000`, `9004`, `5105` — all present per the file; confirm codes if the assertion misses.)

- [ ] **Step 5: Commit** — `git commit -am "feat(preview): map parsed records to a USALI P&L payload (no DB)"`

---

## Task 4: Defensive redaction pass

**Files:**
- Create: `src/usali/redaction.py`
- Test: `tests/test_redaction.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_redaction.py
from datetime import date

from usali.preview import PnlLine, PreviewPayload
from usali.redaction import mask_names, mask_pans, redact


def test_mask_pans_masks_luhn_valid_card_only():
    assert mask_pans("paid 4111 1111 1111 1111 today") == "paid •••• 1111 today"
    # a non-Luhn 16-digit run is left alone (not a card)
    assert mask_pans("ref 1234 5678 9012 3456") == "ref 1234 5678 9012 3456"


def test_mask_names_masks_capitalized_name_pairs():
    assert mask_names("guest John Smith checked out") == "guest ••• checked out"


def test_redact_scrubs_pans_but_preserves_mapping_labels():
    payload = PreviewPayload(
        pms_source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pnl_lines=[PnlLine("Settlements", "Credit Card", "Visa 4111111111111111", "0")],  # type: ignore[arg-type]
    )
    out = redact(payload)
    assert "4111111111111111" not in out.pnl_lines[0].line_item
    # a clean label is untouched
    payload2 = PreviewPayload(
        pms_source="OPERA", report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pnl_lines=[PnlLine("Operated Departments", "Rooms", "Room Revenue", "0")],  # type: ignore[arg-type]
    )
    assert redact(payload2).pnl_lines[0].line_item == "Room Revenue"
```

- [ ] **Step 2: Run — expect FAIL** (module missing).

- [ ] **Step 3: Implement** `src/usali/redaction.py`:

```python
import re
from dataclasses import replace

from usali.preview import PnlLine, PreviewPayload

_PAN_RUN = re.compile(r"\b\d[\d -]{11,21}\d\b")
_NAME_PAIR = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_pans(text: str) -> str:
    """Mask credit-card-shaped digit runs that pass the Luhn check -> '•••• last4'."""

    def _sub(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return f"•••• {digits[-4:]}"
        return m.group(0)

    return _PAN_RUN.sub(_sub, text)


def mask_names(text: str) -> str:
    """Mask 'Firstname Lastname' pairs. NOT applied to mapping-authored labels
    (which are curated, never PII) — reserved for file-derived free text surfaced
    by future report families (e.g. an unmapped-line description)."""
    return _NAME_PAIR.sub("•••", text)


def redact(payload: PreviewPayload) -> PreviewPayload:
    """Defensive boundary pass (D8.4). The financial payload is aggregate-by-
    construction, so this is belt-to-suspenders: mask any PAN that leaked into a
    label. Name-masking is deliberately NOT run on curated mapping labels."""
    lines = [replace(l, line_item=mask_pans(l.line_item)) for l in payload.pnl_lines]
    return replace(payload, pnl_lines=lines)
```

- [ ] **Step 4: Run — expect PASS** + mypy/ruff clean.

- [ ] **Step 5: Commit** — `git commit -am "feat(redaction): defensive PAN/name scrub at the preview boundary"`

---

## Task 5: In-process rate limiter

**Files:**
- Create: `src/usali/ratelimit.py`
- Test: `tests/test_ratelimit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ratelimit.py
from usali.ratelimit import RateLimiter


def test_allows_up_to_max_then_blocks_then_recovers():
    now = [0.0]
    rl = RateLimiter(max_events=2, window_s=60.0, clock=lambda: now[0])
    assert rl.allow("ip") is True
    assert rl.allow("ip") is True
    assert rl.allow("ip") is False          # third within window -> blocked
    assert rl.allow("other") is True        # per-key isolation
    now[0] = 61.0
    assert rl.allow("ip") is True           # window elapsed -> recovers
```

- [ ] **Step 2: Run — expect FAIL** (module missing).

- [ ] **Step 3: Implement** `src/usali/ratelimit.py`:

```python
import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    """Per-key sliding-window limiter, in-process (fine for a single Cloud Run
    instance at pilot scale — see the Track A design §14)."""

    def __init__(
        self, *, max_events: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_events
        self._window = window_s
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        dq = self._events[key]
        while dq and now - dq[0] > self._window:
            dq.popleft()
        if len(dq) >= self._max:
            return False
        dq.append(now)
        return True
```

- [ ] **Step 4: Run — expect PASS** + mypy/ruff clean.

- [ ] **Step 5: Commit** — `git commit -am "feat(ratelimit): in-process sliding-window limiter"`

---

## Task 6: The `POST /api/preview` endpoint (ungated, stateless)

**Files:**
- Modify: `src/usali/adaptors/pdf.py` (add `extract_words_from_bytes`)
- Modify: `src/usali/server.py` (add the route + a `RateLimiter` on `app.state`)
- Test: `tests/test_preview_api.py`

- [ ] **Step 1: Add the in-memory PDF reader** in `src/usali/adaptors/pdf.py`, and delegate `extract_words`:

```python
import io  # at top

def extract_words_from_bytes(data: bytes) -> list[Word]:
    words: list[Word] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            page_offset = i * (page.height + 1000)
            for w in page.extract_words():
                words.append(
                    Word(text=w["text"], x0=float(w["x0"]), top=float(w["top"]) + page_offset)
                )
    return words


def extract_words(pdf_path: str | Path) -> list[Word]:
    return extract_words_from_bytes(Path(pdf_path).read_bytes())
```

- [ ] **Step 2: Write the failing endpoint test**

```python
# tests/test_preview_api.py
from pathlib import Path

# Reuse whatever app/client fixture the suite already exposes (see tests/conftest.py).
# The preview route needs NO database, so no seeding is required.

# A synthetic Opera trial-balance sample PDF (fictitious-by-construction). Use the
# same synthetic sample the e2e suite uploads; confirm the exact filename by listing
# docs/reference/samples/ and picking the Opera trial balance.
SAMPLE = Path("docs/reference/samples")  # bind the exact .pdf during implementation


def test_preview_ok_returns_pnl(client):
    pdf = (SAMPLE / "opera_trial_balance_synthetic.pdf").read_bytes()  # bind real name
    r = client.post("/api/preview", files={"file": ("audit.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["payload"]["pms_source"] == "OPERA"
    assert body["payload"]["pnl_lines"]
    assert "codes_needs_review" in body["payload"]


def test_preview_unreadable_for_non_pdf(client):
    r = client.post("/api/preview", files={"file": ("x.txt", b"not a pdf", "text/plain")})
    assert r.status_code == 415


def test_preview_too_large(client):
    big = b"%PDF-" + b"0" * (10 * 1024 * 1024 + 1)
    r = client.post("/api/preview", files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413


def test_preview_unsupported_vendor(client, monkeypatch):
    # a valid PDF whose header recognizes as HotelKey -> unsupported/notify-me
    import usali.server as srv
    monkeypatch.setattr(srv, "extract_words_from_bytes", lambda data: _hotelkey_words())
    r = client.post("/api/preview", files={"file": ("hk.pdf", b"%PDF-xx", "application/pdf")})
    assert r.json() == {"status": "unsupported", "vendor": "HotelKey", "reason": "vendor_not_supported"}


def _hotelkey_words():
    from usali.adaptors.pdf import Word
    return [Word(text=t, x0=float(i), top=0.0) for i, t in enumerate(["HotelKey", "Final", "Audit"])]
```

- [ ] **Step 3: Run — expect FAIL** (route 404).

- [ ] **Step 4: Implement** the route in `src/usali/server.py`. Add imports near the top, construct the limiter on `app.state` just before the SPA mount, and register the ungated route (NOT in `operator_gates`, like `kiosk_router`):

```python
# imports
from dataclasses import asdict
from usali.adaptors import autoclerk_transaction_summary, opera_trial_balance
from usali.adaptors.pdf import extract_words_from_bytes
from usali.detect import detect_report_signature
from usali.preview import PreviewPayload, build_financial_preview
from usali.ratelimit import RateLimiter
from usali.recognition import recognize_vendor
from usali.redaction import redact

_MAX_PREVIEW_BYTES = 10 * 1024 * 1024
_PREVIEW_ADAPTERS = {
    ("OPERA", "trial_balance"): (
        opera_trial_balance.parse_trial_balance,
        opera_trial_balance.extract_business_date,
    ),
    ("AUTOCLERK", "transaction_summary"): (
        autoclerk_transaction_summary.parse_transaction_summary,
        autoclerk_transaction_summary.extract_business_date,
    ),
}
_UNREADABLE_HINTS = [
    "Is it a night-audit / trial-balance report (not a single folio or a photo)?",
    "Is it the original PDF your PMS emailed, not a scan?",
]


def _payload_json(p: PreviewPayload) -> dict[str, object]:
    d = asdict(p)
    d["business_date"] = p.business_date.isoformat()
    d["net_total"] = str(p.net_total)
    for line in d["pnl_lines"]:
        line["amount"] = str(line["amount"])
    for kpi in d["kpis"]:
        kpi["value"] = str(kpi["value"])
    return d
```

Inside `create_app`, before the SPA mount:

```python
    app.state.preview_rate_limiter = RateLimiter(max_events=20, window_s=60.0)

    @app.post("/api/preview")  # PUBLIC: no operator_gates, no session, persists nothing
    async def preview(request: Request, file: UploadFile) -> dict[str, object]:
        limiter: RateLimiter = request.app.state.preview_rate_limiter
        client_ip = request.client.host if request.client else "unknown"
        if not limiter.allow(client_ip):
            raise HTTPException(status_code=429, detail="too many previews; try again shortly")
        if (file.content_type or "") not in ("application/pdf", "application/octet-stream"):
            raise HTTPException(status_code=415, detail="please upload a PDF")
        if file.size is not None and file.size > _MAX_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail="file too large")
        data = await file.read()
        if len(data) > _MAX_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail="file too large")
        if data[:5] != b"%PDF-":
            raise HTTPException(status_code=415, detail="please upload a PDF")
        try:
            words = extract_words_from_bytes(data)
        except Exception:
            return {"status": "unreadable", "hints": _UNREADABLE_HINTS}
        finally:
            del data  # nothing about the upload persists
        sig = detect_report_signature(words)
        if sig in _PREVIEW_ADAPTERS:
            parse_fn, date_fn = _PREVIEW_ADAPTERS[sig]
            try:
                business_date = date_fn(words)
                records = parse_fn(words, property_id="PREVIEW", business_date=business_date)
            except Exception:
                return {"status": "unreadable", "hints": _UNREADABLE_HINTS}
            payload = redact(
                build_financial_preview(
                    source=sig[0],
                    report_type=sig[1],
                    business_date=business_date,
                    records=records,
                )
            )
            return {"status": "ok", "payload": _payload_json(payload)}
        if sig is not None:
            return {"status": "unsupported", "vendor": sig[0].title(), "reason": "no_preview_for_report"}
        vendor = recognize_vendor(words)
        if vendor is not None:
            return {"status": "unsupported", "vendor": vendor, "reason": "vendor_not_supported"}
        return {"status": "unreadable", "hints": _UNREADABLE_HINTS}
```

> **Disk note:** Starlette may spool a large multipart upload to an OS temp file
> that it deletes on close. That transient spool is not *our* storage — no OH DB
> row, no inbox file, no log of bytes. The `del data` and the persist-nothing
> contract are about application state. If strict no-spool is required later, set
> the multipart spool threshold ≥ the size cap.

- [ ] **Step 5: Run — expect PASS.** Bind the real synthetic sample filename in the test (`ls docs/reference/samples`), then `uv run pytest tests/test_preview_api.py -q`. If no synthetic Opera trial-balance PDF exists, generate one with the same generator style as the SkyTouch mock (`scripts/gen_*_mock_fixtures.py`) — **synthetic only**.

- [ ] **Step 6: Full gates** — `uv run pytest -q && uv run mypy src && uv run ruff check src tests`.

- [ ] **Step 7: Commit** — `git commit -am "feat(api): public stateless /api/preview endpoint"`

---

## Deferred to later plans (not in Part 1)

- **Part 2 — front-door frontend** (separate plan; recon done): a public route (`router.tsx` + `RootShell.tsx` allowlist), a `PreviewPage` modeled on `UploadPage.tsx`, a `postPreview` client that **bypasses `authHeaders()`/`redirectToLogin()`**, the pulse→P&L→proof result renderer + edge states, the warm-but-precise Tailwind `@theme` tokens, vitest + an unauthenticated Playwright e2e (`test.use({ storageState: { cookies: [], origins: [] } })`).
- **Statistics/KPI family** — the pulse zone (Occ/ADR/RevPAR) from manager-flash / manager-report stats reports via `mapping/statistics.yaml`; and the **SkyTouch pack** once PR #54 merges (drops into `_PREVIEW_ADAPTERS` + `_REPORT_SIGNATURES`).
- **Lead capture (`/api/leads`)** — deliberately NOT here: its persistence collides with the org-scoped RLS/session machinery (every app session is org-bound via `require_active_org`). Storing non-tenant marketing rows needs a deliberate "where do public rows live under RLS?" decision — a small brainstorm before it becomes a plan.

## Self-review checklist (done)

- Spec coverage: every Part-1 in-scope item (signature detect, recognition, preview map, redaction, abuse guards, stateless endpoint) has a task. KPIs/leads/frontend are explicitly deferred with reasons.
- No placeholders: all steps carry real code; the one bind-at-implementation value (the synthetic sample PDF filename) is called out explicitly with how to resolve it.
- Type consistency: `PreviewPayload`/`PnlLine`/`Kpi` names and `build_financial_preview(...)` signature are identical across Tasks 3, 4, 6.
