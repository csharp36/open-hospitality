# Track A — the "aha" front door + anonymous parse-preview (design)

Status: **DESIGN / approved in brainstorm 2026-08-16.** The first
buildable slice of the self-service onboarding milestone (**OH-1**).
Depends on and inherits the constraints of **D8**
([`2026-08-16-data-posture-progressive-onboarding-design.md`](2026-08-16-data-posture-progressive-onboarding-design.md)).
Plan doc follows via the writing-plans skill.

## 1. Goal & north star

Let an anonymous hotel operator drop the report their PMS already emails
them and — in seconds, with no account and **nothing stored** — see it
mapped to a real USALI P&L, balanced, with their pulse (Occ/ADR/RevPAR)
on top. This is the marketing front door's centerpiece and the product's
first proof of the north star: *free the owner's time; show them money.*

**Why this is the first slice:** it is the single highest-visibility
piece that **needs no multi-tenancy** — because it persists nothing,
there is no tenant to isolate and no data at rest to protect. It pilots
**publicly** the day it ships, while the multi-tenancy foundation
(Track B) proceeds in parallel.

## 2. Scope

**In scope:**
1. **Front-door shell** — a public marketing hero on `demo.mandati.ai`
   with login upper-right, in the resolved visual skin (§7).
2. **Anonymous parse-preview** — drop/pick a PDF → in-memory parse →
   PII/PAN redaction → result screen (pulse → P&L-that-balances → proof
   strip) → detect-silently-then-confirm. Persists nothing.
3. **Edge states** — recognized-but-unsupported ("notify me"); unreadable
   ("see a sample that works" loads a synthetic report).
4. **Lead capture** — the "Save & automate" and "notify me" CTAs capture
   an email (and, for unsupported, which PMS) onto an **early-access
   allowlist**. This is Track A's *only* persistence, and it is marketing
   contact data — never hotel financial data or a tenant.
5. **Abuse guards** for the unauthenticated upload (§9).
6. **GDPR posture** (§10).
7. **A design-token set** (the skin) reused by Tracks B/C.

**Out of scope (deliberately):** real accounts, OTP, save/automate
backend, multi-tenancy (Track B); inbound email (Track C, deferred);
daily recap email (needs OH-12); full marketing content pages
(pricing/features/about — a light follow-on); the thin pricing stub
(YAGNI for now).

## 3. Decisions locked in the brainstorm

- **Front door = "A with a twist":** instant unauthenticated preview;
  authentication (OTP) only appears when the user wants to *save/continue*
  — which is Track B, so in Track A the save CTA is early-access capture.
- **Payload hierarchy:** the pulse (KPIs) leads; the USALI P&L that
  **ties out** sits right behind it; the "we understood your file" proof
  is a quiet strip. The visible **"🔒 nothing saved"** line and the
  **"✓ ties out"** reconciliation line are load-bearing trust signals.
- **Edge handling:** detect the PMS *silently* and let the user correct
  us ("Recognized: X — not right?"). The strict "declared-X-but-detected-Y"
  mismatch flag lives later in real onboarding (a declared PMS exists to
  contradict). Every miss becomes a next step, never a dead-end.
- **Visual direction:** warm-hospitality palette (cream/terracotta, serif
  display) disciplined by precise type (tight tracking, hairline rules,
  **monospace for every figure**). Warm at the door, exact at the numbers.
- **Environment:** public persist-nothing surface; no gate needed (D8
  env-topology). The pilot's invite gate belongs to the real prod env
  (Track B), not here.

## 4. Architecture overview

Two independently-testable halves plus a tiny lead store. The preview
**reuses the existing pure parse path** and adds one pure mapping step;
it never touches the tenant database or an ORM session.

```
                    ┌─────────────────────────── FRONTEND (public route) ──────────────────────────┐
  Browser  ── drop ─▶  FrontDoor page → DropZone → POST /api/preview → PreviewResult renderer        │
                    │                                          │ (unsupported/unreadable) → EdgeState │
                    │   "notify me" / "get early access" ──────┴──▶ POST /api/leads                   │
                    └──────────────────────────────────────────────────────────────────────────────┘
                                         │ multipart PDF (in memory)          │ {email, pms?, source}
   ┌──────────────────────────── BACKEND (ungated routers on server.py) ─────────────────────────────┐
   │  POST /api/preview  (stateless, abuse-guarded)                     POST /api/leads               │
   │    extract_pages ─▶ detect() ─▶ adapter.parse_* ─▶ preview_map ─▶ redact ─▶ PreviewPayload(JSON)  │
   │      (pdf.py)        (detect.py)   (adaptors/*)     (NEW pure)     (NEW)                          │
   │    reuse ▲──────────── all pure, no DB ────────────▲   loads mapping/*.yaml in-memory            │
   │                                                                    minimal `early_access_lead`   │
   └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 5. Components & boundaries

### 5a. Reused as-is (pure, no DB) — no changes
- `src/usali/adaptors/pdf.py` — `extract_pages` / `extract_words` / `cluster_rows`.
- `src/usali/detect.py` — `detect(words, registry)` with the in-repo
  `_REPORT_SIGNATURES` registry.
- `src/usali/adaptors/*` — the Opera / AutoClerk / SkyTouch `parse_*`
  functions returning `StagedRecord` / `StatisticRecord`. Called with a
  placeholder `property_id="PREVIEW"` and the business date extracted from
  the report; mapping is keyed by `(pms_source, trx_code)`, so the
  placeholder property never affects results.

### 5a-bis. `src/usali/recognition.py` — NEW, pure
**Why:** `detect()` only matches *supported* report signatures
(`_REPORT_SIGNATURES` → an adapter). A dropped **HotelKey** report has no
such signature, so without this it would fall to `unreadable` — we could
never say "this looks like HotelKey, notify me." This adds a **second,
lower tier**: a `recognition` registry of known-but-unsupported vendor
header phrases → a vendor *name only* (no adapter). Consulted **only when
`detect()` finds no supported signature**.
**Interface:** `recognize_vendor(words) -> str | None`.
**Consequence:** the drop outcome is a three-way — supported (parse) →
recognized vendor (unsupported edge) → nothing recognized (unreadable
edge). This realizes the onboarding doc's "placeholder for every known
PMS" as a real, testable step.

### 5b. `src/usali/preview.py` — NEW, pure
**What:** map parsed records → a `PreviewPayload` (KPIs, USALI P&L lines,
coverage/proof, reconciliation status) using the **in-memory mapping
dictionary loaded from `mapping/*.yaml`**, mirroring `transform()`'s
mapping+reconciliation logic *without* a `Session`.
**Interface:** `build_preview(detection, records) -> PreviewPayload`.
**Depends on:** the mapping YAMLs (read once, cached); no DB, no network.
**Why separate from `transform.py`:** `transform()` is intrinsically
DB-coupled (reads staged rows + `UsaliMappingDictionary`, writes facts).
The preview needs the same *mapping semantics* over in-memory records
with zero persistence — a genuinely different unit, not a flag on the old
one.
**Payload is aggregate-by-construction:** it emits amounts-by-USALI-line
and computed KPIs only. It never echoes raw folio/description tokens —
the first line of PII defense (§8).

### 5c. `src/usali/redaction.py` — NEW, pure
**What:** a defensive scrub over any free-text string fields in the
payload (e.g., an "unmapped code label"): detect and mask card PANs
(Luhn-checked digit runs) and obvious guest-name patterns before return.
**Interface:** `redact(payload) -> PreviewPayload`.
**Why:** §5b makes the payload aggregate, but redaction is the belt to
that suspenders — D8.4 says minimize at the boundary, defensively.

### 5d. `POST /api/preview` — NEW ungated route (in `server.py`)
Mounted like `kiosk_router` (no `operator_gates`). Stateless:
`extract_pages(in-memory PDF) → detect → adapter.parse_* → build_preview
→ redact → JSON`. Enforces the abuse guards (§9). Reads the upload into a
**bounded in-memory buffer**; never writes the PDF to disk or DB; drops
all intermediates on return. On any parse failure returns a typed
edge-state response (§11), not a 500.

### 5e. `POST /api/leads` + `early_access_lead` table — NEW
Minimal capture: `{email, pms_requested?, source: "early_access"|"notify_pms"}`.
The **only** Track A persistence. Not tenant-scoped (no tenant exists);
marketing contact data under its own consent + deletion path (§10). Rate-
limited; email-format validated; delivered-once idempotence by email+source.

### 5f. Frontend (public, unauthenticated) — `frontend/src/pages`
- `FrontDoor` route — hero + login-upper-right + `DropZone`.
- `DropZone` — drag/drop + file-pick; posts multipart to `/api/preview`;
  client-side size/type pre-check for fast feedback.
- `PreviewResult` — renders the payload: pulse KPI strip → USALI P&L with
  the "✓ ties out" line → proof chips; the "🔒 nothing saved" banner;
  "Recognized: X — not right?" confirm; "Save & automate → get early
  access" CTA.
- `EdgeState` — unsupported ("notify me" → `/api/leads`) and unreadable
  ("see a sample" loads a bundled **synthetic** report through the same
  render path).
- **Design tokens** — the resolved skin as Tailwind theme extensions
  (§7), the reusable output for later tracks.

## 6. Data flow

**Happy:** drop → `/api/preview` → detect hits a signature → adapter
parses → `build_preview` maps + reconciles → `redact` → payload →
`PreviewResult`. Nothing persisted.
**Recognized-but-unsupported:** `detect()` finds no supported signature,
but `recognize_vendor()` matches a known vendor phrase → typed
`unsupported` response with the vendor name → `EdgeState` "notify me".
**Unreadable:** no supported signature *and* no recognized vendor / not a
PDF → typed `unreadable` response → `EdgeState` hints + synthetic sample.

## 7. Visual skin (design tokens)

Palette: canvas `#f7f2ea`, ink `#33291f`, muted `#7a6a55`, line
`#e6dccd`, card `#fbf7f1`, single accent terracotta `#bd5b3d`.
Type: serif display (tight tracking) for headings; humanist sans
(already `@fontsource-variable/inter`) for prose; **monospace for every
figure**. Components: hairline rules, 8px radii, terracotta used sparingly
(one accent per view). Encoded as Tailwind v4 theme tokens so Tracks B/C
inherit the same system.

## 8. Data posture (D8.4, made concrete)

1. **In-memory only.** The PDF is read into a bounded buffer; no temp
   file, no DB row, no log line carrying file bytes. Intermediates are
   dropped when the request returns.
2. **Aggregate-by-construction.** `build_preview` emits sums-by-USALI-line
   and computed KPIs — not raw folio lines — so guest names / PANs do not
   survive aggregation in the normal path.
3. **Defensive redaction.** `redact()` scrubs PAN (Luhn) and name-shaped
   tokens from any residual free-text field before serialization.
4. **No preview telemetry with content.** Metrics may count previews and
   record detected source/report-type; never file content or figures.

## 9. Abuse guards (unauthenticated upload)

- **Rate limit** per client IP (and a global ceiling) on `/api/preview`
  and `/api/leads`.
- **Size cap** (e.g. 10 MB) enforced *before* buffering; reject larger.
- **Type check** — declared `application/pdf` *and* `%PDF` magic bytes.
- **Parse timeout / resource bound** on the pdfplumber pass; a
  pathological PDF fails closed to the `unreadable` edge state, not a hang.
- **Page ceiling** — cap pages parsed for a preview (a night-audit pack is
  bounded; refuse absurd page counts).

## 10. GDPR posture

- **Preview:** processing is **transient and at the user's explicit
  request**, with **no storage, no profiling, no retention**. A visible
  notice states this ("processed in your browser session, never stored").
  The EU market opens cleanly here precisely because there is nothing at
  rest to argue over.
- **Leads:** email is personal data → captured with clear purpose consent,
  minimal fields, and a working deletion/unsubscribe path from day one.

## 11. Error handling

`/api/preview` returns a typed discriminated result, never a bare 500:
`{status: "ok", payload}` | `{status: "unsupported", vendor}` |
`{status: "unreadable", hints[]}`. Abuse-guard rejections return 413
(too large) / 415 (wrong type) / 429 (rate) with a friendly frontend
mapping. The frontend never shows a stack trace; every terminal state has
a next step.

## 12. Testing strategy

- **`preview.py` unit** — synthetic records → expected KPIs, P&L lines,
  reconciliation flag; the "3 need review" coverage count. Reuse the
  **synthetic** SkyTouch/Opera/AutoClerk word-fixtures already in
  `tests/fixtures/` — **never** a real PMS file (fictitious-by-construction).
- **`recognition.py` unit** — a synthetic HotelKey-shaped header returns
  the vendor name; a supported (SkyTouch) header returns `None` (so
  `detect()` wins); noise returns `None`.
- **`redaction.py` unit** — adversarial inputs (a Luhn-valid PAN and a
  name embedded in a free-text field) are masked; aggregate fields pass
  through unchanged.
- **`/api/preview` endpoint** — happy + both edge states + each abuse
  guard (413/415/429/timeout) + a proof that nothing is written (no DB
  rows, no temp files) after a preview.
- **`/api/leads`** — capture, validation, idempotence, deletion path.
- **Frontend** — component tests for `PreviewResult`/`EdgeState`; a
  Playwright e2e (`frontend/e2e/`) for drop → result and drop-garbage →
  unreadable, against a synthetic fixture.

## 13. Handoff to Track B

The "Save & automate" CTA is the seam: in Track A it captures early
access; in Track B it becomes signup → OTP → real tenant. The preview's
`PreviewPayload` and the parse path are reused there to show the same
"aha" *inside* the authenticated flow — no rework.

## 14. Open questions (small, non-blocking)

- Rate-limit backing store on a single Cloud Run instance vs. shared
  (in-process token bucket is fine for the public preview at pilot scale;
  revisit if it scales out).
- Exact PAN/name redaction ruleset — start conservative (Luhn PAN + digit
  runs); tune against synthetic adversarial fixtures, never real data.
