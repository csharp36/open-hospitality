# Face-recognition timekeeping: legal grounding (researched 2026-07-21)

**Status: NOT BUILT.** The recorded posture stands — the punch photo is
"evidence for manager review, never a biometric template" — until the owner
makes an explicit go decision against this document. Matching a clock-in
photo to an on-file reference photo IS biometric processing the moment a
face template/embedding is computed; a bare photograph is not.

Employer of record: a private California LLC (two San Jose hotels). Both
properties `wage_jurisdiction = US-CA`.

**Re-check by 2026-12-01** — the CA 2025–26 session ends 2026-08-31 (a
BIPA-style successor to the dead SB 1189 could pass late), and the CPPA's
ADMT pre-use-notice deadline is 2027-01-01. Second reminder Q4 2027: risk
assessments for pre-2026 processing are due 2027-12-31 (CPPA summary filing
2028-04-01).

## 1. California: what the law actually requires

🟢 = verified against statute text; 🟡 = law-firm/secondary synthesis;
🔴 = unresolved, needs counsel.

- 🟢 **Biometric info is "sensitive personal information".** Civ. Code
  §1798.140(c) (biometric information: characteristics "used ... to
  establish individual identity") and §1798.140(ae) ("processing of
  biometric information for the purpose of uniquely identifying a
  consumer"). Employees are covered — the CPRA removed the HR carve-out
  effective 2023-01-01.
  https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=1798.140.&lawCode=CIV
- 🟢 **Notice, not opt-in consent, is CA's model.** §1798.100(a): notice
  at/before collection of categories, purposes, and (11 CCR §7012) the
  retention period or criteria. §1798.100(c): collection must be
  "reasonably necessary and proportionate". California does NOT require a
  signed release before capture — that is Illinois/Texas/Washington/
  Colorado. Employees keep access/deletion/correction/non-discrimination
  rights regardless.
  https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=1798.100.&lawCode=CIV
- 🟡 **"Right to limit" (§1798.121) may not even need a control** if the
  use fits the security/service carve-outs (§1798.140(e)(2)/(5)) — a match
  used solely to verify identity for attendance plausibly fits. 🔴 No CPPA
  guidance squarely on point; get counsel's read before relying on it.
- 🟢 **Applicability threshold first.** CCPA applies only if the LLC is a
  §1798.140(d) "business" (>$25M revenue, OR ≥100k consumers'/households'
  PI). Hotel GUEST data across two properties may cross 100k — the
  threshold analysis is the first document to write, because it switches
  the whole framework on or off.
- 🟢 **Enforcement: agency, not plaintiffs.** No private right of action
  except a data breach of unencrypted biometric data via §1798.150
  ($100–$750/person/incident). Otherwise CPPA/AG administrative penalties
  (~$2.7k/$8k per violation, CPI-adjusted; 30-day cure gone since 2023).
  No known CPPA/AG action over biometric timeclocks as of mid-2026.
- 🟢 **Labor Code §1051 (misdemeanor!).** Conditioning employment on being
  photographed to FURNISH the photo (or info about it) to another employer
  or third person, usable to the employee's detriment. Internal use is the
  textbook lawful case. 🔴 A third-party face-matching VENDOR arguably
  receives a "furnishing" — untested in CA appellate law (the better
  reading: a processor acting solely for the employer's internal purpose
  is not "any other employer or third person", but no court has said so).
  https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=1051.&lawCode=LAB

  **The line is WHO PROCESSES, not where the servers are** (owner
  pushback 2026-07-21, accepted — "on-premises" was the wrong frame):
  1. *Cloud-hosted infrastructure* (our stack on AWS/GCP, photos in S3,
     matching by OUR code on OUR instances, provider under its standard
     DPA as a §1798.100(d) service provider) — settled, low-risk; the
     `S3PhotoStore` seam is exactly this. Cloud hosting changes nothing.
  2. *Self-hosted matching model inside our deployment* — RECOMMENDED.
     No vendor ever sees a face, no §1051 ambiguity, no vendor
     model-training clause to police, portable unchanged to consent
     states.
  3. *Third-party face-recognition API* (Rekognition, Azure Face) —
     legally workable in CA with a §1798.100(d) service-provider contract
     carrying retention/deletion SLAs, but: it is the exact pattern the
     IL timeclock suits targeted (Cothron: scans "transmitted to a
     third-party vendor"), §1051 stays unresolved for it, and the
     contract must EXPLICITLY bar vendor model-training on our images
     (for AWS Rekognition that is an account-level opt-out to set, not a
     default). Acceptable fallback if tier 2 proves impractical.

  **If this product is ever hosted multi-tenant for other operators, WE
  are the third-party processor** other employers furnish photos to.
  That flips the obligations: a service-provider contract with each
  tenant, retention/deletion SLAs and no-training commitments as product
  features (not internal policy), per-tenant isolation of templates, and
  any tenant with Illinois employees imports BIPA's written-release
  regime into our onboarding flow. Design input now, not a blocker.
- 🟢 **CPPA risk-assessment regs (effective 2026-01-01).** Biometric
  identity verification is an enumerated trigger: NEW processing needs the
  risk assessment BEFORE deployment. If the match ever feeds a "significant
  decision" (pay docking, discipline) without human review, the ADMT regs
  add pre-use notice + opt-out from 2027-01-01 — keeping a human approver
  in the loop (the green/red checkbox design does exactly this) is what
  keeps us out of the automated-decision regime. Document that reasoning.
- 🟢 **SB 1189 (BIPA-style CA bill, would have added a private right of
  action) died 2022-11-30**; no successor active on the Jan-2026 tracker.
  Re-check at the date above.
  https://leginfo.legislature.ca.gov/faces/billStatusClient.xhtml?bill_id=202120220SB1189

## 2. Does this need to be configurable by state? YES — by posture

The obligations differ in KIND across states, not just degree:

| State | Before first capture | Private right of action | Notes |
|---|---|---|---|
| CA | Notice at collection | Breach only | This doc |
| IL (BIPA, 740 ILCS 14) | **Written release (opt-in)** + public retention policy | **Yes** — $1k/$5k per person per method (post-2024 fix; was per-scan: *Cothron v. White Castle*, ~$17B exposure) | The cautionary tale |
| TX (CUBI §503.001) | Informed consent | No (AG, $25k/violation) | Security justification EXPIRES at termination |
| WA (RCW 19.375) | Notice + consent | No (AG) | 🔴 employer-use scope unverified |
| CO (CPA + HB 24-1130, eff. 2025-07-01) | **Consent**; employment mostly can't be conditioned on it | No (AG) | First comprehensive-privacy law with employee-specific biometric consent |

So: encode it exactly like `overtime_rules` / `sick_leave_rules` — a
`biometric_rules_for(jurisdiction)` posture table where US-CA returns
notice-mode requirements and any UNKNOWN state REFUSES to enable the
feature rather than defaulting to CA's (weakest) posture. Both hotels are
CA today; the table exists so expansion can't silently run an IL-illegal
flow.

## 3. Build checklist (if the owner says go)

1. Threshold memo: is the LLC a §1798.140(d) business? (guest-data volume)
2. Employee **notice at collection** (standalone HR notice) BEFORE first
   reference-photo capture: category, purpose (identity verification for
   attendance), no sale/sharing, retention criteria.
3. **Retention policy**: delete template + reference photo at separation
   (≤30 days); benchmark to IL's "3 years or purpose-satisfied" as the
   strictest-plausible ceiling. No keep-for-rehire without documented
   justification and matching notice language.
4. **Risk assessment before deployment** (CPPA regs, biometric trigger).
5. **Matching by our own code, wherever hosted** (tier 2 above): a
   self-hosted embedding model inside our deployment. A third-party face
   API is the fallback, and only with a service-provider contract that
   sets retention/deletion and bars model-training on our images.
6. Encrypt templates at rest (a breach of unencrypted biometric data is
   the one private-right-of-action scenario).
7. **Match gates approval, never the punch** (wage law: hours worked get
   recorded regardless), a human approves every red/grey, overrides are
   audited per day — and that human-in-the-loop design is also the ADMT
   defense.
8. Threshold is model-calibrated and configurable, with a distinct GREY
   state (no face found / bad frame); red means "human looks", never
   "fraud" — face models carry demographic error-rate differences.

## 4. As built (Pillar F, 2026-07 — plan: the Pillar F face-match design)

The checklist above shipped as follows. This section is the operator-facing
map; the plan doc carries the decision reasoning.

**Architecture (tier 2, self-hosted, in-process).** SCRFD face detection +
ArcFace embeddings (insightface buffalo_l: `det_10g.onnx`,
`w600k_r50.onnx`) run CPU-only inside the API process via onnxruntime — no
network at inference. Model files are never committed:
`scripts/fetch_face_models.py` downloads them once, sha256-pinned, into
`models/face/` (gitignored). The Python `face` extra
(`uv sync --extra face`) carries onnxruntime/numpy/pillow; without it the
core install is unaffected and every face feature degrades cleanly.

**Settings** (all `USALI_`-prefixed env vars):

| setting | default | meaning |
|---|---|---|
| `BIOMETRIC_MATCHING_ENABLED` | `false` | the dark flag; gates matching AND enrollment |
| `FACE_MODEL_DIR` | `models/face` | where the fetch script puts the ONNX files |
| `FACE_MATCH_THRESHOLD` | `0.60` | min cosine for a verified/matched call |
| `FACE_MATCH_MARGIN` | `0.10` | top1−top2 ambiguity margin for 1:N identify |
| `BIOMETRIC_NOTICE_VERSION` | `""` | the notice-at-collection version stamped on enrollment; REQUIRED to enable in production |

**The posture table** (`src/usali/biometric_rules.py`) encodes US-CA
(notice-at-collection, retention ≤30 days after separation) and refuses
every other jurisdiction, including bare "US" — enrollment and kiosk
matching both check it per property, so the feature cannot switch on where
the consent regime is unencoded (IL would need a written-release workflow).

**Data model.** One `employee_face_template` row per employee: the
embedding (AES-256-GCM at rest via `field_encryption_key` — server-readable
because matching needs the vector, unlike the HPKE-sealed payroll PII),
`model_version`, and the `notice_version`/`notified_on` pair. Punches carry
`match_state` (verified | unverified | no_template | NULL = pre-matching)
+ `match_score`, CHECK-constrained. There is no read path for embeddings,
and no match events in the audit log — the score on the punch is the
record; enroll/replace/delete are the audited actions.

**The flows.**
- *Enrollment* (Employees page → Face, org_admin | property_gm with
  whole-person scope): camera capture or JPEG upload; server embeds,
  replaces the single row, stores the reference photo under
  `face-reference/{employee_id}.jpg` in the encrypted photo store.
  Termination deletes template + photo.
- *Kiosk* (camera-first when `GET /api/kiosk/config` says matching is on):
  capture → 1:N identify (stateless; probe discarded) → "Hi {name}" confirm
  tap → punch. The search fallback (min 3 chars, property-confined, capped)
  is always available and never blocks the punch — wage rule; it also
  renders when `/config` itself is unreachable. The punch endpoint
  re-verifies 1:1 server-side; the client can never assert "verified".
  Identify's response carries state + identity only (F8): the raw
  similarity never leaves the server (a hill-climbing oracle to a device
  token), and "nobody here is enrolled" is collapsed into `no_match` (a
  database fact the device population has no business reading). A damaged
  stored template — undecryptable after a key rotation, truncated —
  degrades exactly like an engine outage: identify answers 503 (→ search),
  the punch records with NULL match fields.
- *Approval* (Timecards): green/grey/red badges per punch; approve REFUSES
  a card with unverified punches until each is explicitly acknowledged, and
  every acknowledgment is audited per punch. Grey and pre-matching punches
  never gate (cold start must not deadlock approvals).

**Demo.** `scripts/demo.sh` runs with matching ON: it installs the face
extra, fetches the pinned models (fetch failure degrades to a search-only
kiosk), and the seed enrolls one hourly worker per property with the
committed SYNTHETIC faces (SFHQ; these people do not exist — see
tests/fixtures/faces/README.md). To see a green match, hold
`tests/fixtures/faces/person_a.jpg` (or `person_b.jpg`) up to the kiosk
camera — or enroll yourself against any demo employee from the Employees
page and punch as them. The seed also stamps the open demo punches with a
coherent story: verified for the enrolled stars, one red punch so the
approval gate demos out of the box, grey cold-start for everyone else, and
NULL on the closed history (recorded before matching existed). Real faces
must never be committed as fixtures or seeded as reference photos.
