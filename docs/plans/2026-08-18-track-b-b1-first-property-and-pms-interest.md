# Track B / B1 Part-2 — first-property wiring + PMS-interest (BACKEND) plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> to implement this plan task-by-task (fresh subagent per task, full red→green→commit
> TDD loop, review between tasks). Steps use `- [ ]` checkboxes.

**Goal:** Make the signup `/complete` endpoint create the workspace's first
property from the fields it already accepts, and — when the owner names an
unsupported PMS — capture a de-duped demand request and route it to an admin,
instead of silently dropping the data.

**Architecture:** `provision_tenant` runs on the least-privilege provisioner
session (D-B7) which cannot write `property`; so the first property is created
on a **separate app-role session bound to the new org** (`bind_org_context`),
after the provisioner commit. Unsupported-PMS requests land in a new
not-`OrgScoped` `pms_interest_request` table (platform-level demand data) and,
when new, trigger a `Notifier` email to a configured admin address.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, pydantic /
pydantic-settings, Postgres 16 via testcontainers, `uv`. Secrets via `secrets`.

Implements the BACKEND half of
[`docs/design/2026-08-18-track-b-b1-signup-frontend-design.md`](../design/2026-08-18-track-b-b1-signup-frontend-design.md)
(§5). The frontend SPA is a separate Part-2 plan, written after this lands.
Branch: `feat/track-b-b1-signup-frontend` (cut off `feat/onboarding-track-b`).

## Gates (run for EVERY task before committing)

```bash
uv run pytest -q            # full suite (testcontainers Postgres, ~9 min)
uv run mypy src             # src-only, never tests/
uv run ruff check src tests
```

Fixtures are synthetic — no real PII, phone numbers, or credentials.

## Grounding facts (verified against the code — do not re-guess)

- `Property(OrgScoped, Base)` (`src/usali/models.py`): PK `property_id: str`
  (`String(50)`, **global** PK, assigned not auto); required-without-default:
  `name`, `pms_source`. `timezone` has `server_default="America/Los_Angeles"`;
  `wage_jurisdiction: str | None` nullable; `crm_ref` nullable; `created_at`
  server-default. A bare property (no room-inventory / fiscal / property_config
  child rows) is a valid insert. `UniqueConstraint("org_id","property_id")`.
- `bind_org_context(session, org_id) -> Session` (`src/usali/tenancy.py:167`)
  instruments the session + binds it to one org; the `SET LOCAL` lands on the
  first query in the transaction. `OrgBoundSessionFactory.__call__` is exactly
  `bind_org_context(self._factory(), self._org_id)` — mirror that.
- `seed_properties` (`src/usali/mapping/property_registry.py:120`) shows the
  in-repo Property-write idiom: a Core `insert(Property).values(...)` setting
  `org_id` explicitly. Reuse that idiom (RLS `WITH CHECK` enforces
  `org_id == SET LOCAL org`, so set `org_id` explicitly).
- `signup_api.complete()` (`src/usali/signup_api.py`) today: step 1 APP session
  (validate + OTP verify + `invites.claim`), step 2 PROVISIONER session
  (`provision_tenant` only; the one place `provisioner_session_factory` is
  opened), except→`invites.revert_claim`, step 3 APP session
  (`invites.mark_consumed_org`), returns `{"org_alias": ...}` with `201`.
  `request.app.state.db_session_factory` is the UNBOUND base app-role factory.
- `CompleteRequest` already declares `property_name` (1–200), `pms_source`
  (1–20), `wage_jurisdiction` (1–10), `password` (min 8). No `timezone` field
  yet.
- Migration head after Part-1 is **`b1c0otp`**. The single test hardcoding the
  head literal is `tests/test_l4_org_grants.py::test_l4_is_the_single_alembic_head`
  (currently `== ["b1c0otp"]`). Other head tests assert only `len(get_heads())==1`.
- Not-`OrgScoped` tables (plain `Base`, no `org_wall` policy) get app-role DML
  automatically via `l2a0rlswall`'s `ALTER DEFAULT PRIVILEGES`, and stay out of
  the RLS inventory test **as long as their policy is not named `org_wall`**
  (they add none). See the Part-1 `invite`/`otp_challenge` migrations for the
  exact pattern.
- Config seam fields live in `src/usali/config.py`'s `Settings` (env prefix
  `USALI_`); Part-1 added `notifier`, `public_base_url`, `signup_*` there.
- Signup API tests (`tests/test_b1_signup_api.py`) provide `_signup_client(...)`
  (builds the app on the real app-role + provisioner-role sessions),
  `_make_invite(db_url, email)`, and the `_founding_committed` fixture. Reuse them.

Migration chain after this plan: `b1c0otp → b1d0pmsinterest`. Final head: **`b1d0pmsinterest`**.

## File structure

```
MOD  src/usali/mapping/property_registry.py   # + create_first_property()
MOD  src/usali/models.py                        # + PmsInterestRequest (plain Base)
NEW  src/usali/pms_interest.py                  # record_request() (+ _normalize)
NEW  migrations/versions/b1d0pmsinterest_pms_interest_request.py
MOD  src/usali/config.py                        # + admin_notify_email
MOD  src/usali/signup_api.py                    # CompleteRequest + complete() branch
MOD  tests/test_l4_org_grants.py                # head literal -> b1d0pmsinterest

NEW  tests/test_b1_first_property.py            # Task 1
NEW  tests/test_b1_pms_interest.py              # Task 3
MOD  tests/test_b1_signup_api.py                # Tasks 4-6 (schema + both /complete paths)
```

---

## Task 1 — `create_first_property` helper

**Files:** Modify `src/usali/mapping/property_registry.py`; Test `tests/test_b1_first_property.py`.

Inserts one `Property` under an **org-bound** session and returns the generated,
globally-unique `property_id` (`slugify(name)` + 4 hex, retried under a SAVEPOINT
on the rare PK collision so the outer transaction/org-bind survives).

- [ ] **Step 1: Write the failing test** — `tests/test_b1_first_property.py`

```python
"""create_first_property: inserts a bare property under the org-bound session,
generates a unique property_id, defaults timezone when omitted."""

from sqlalchemy import select

from usali.mapping.property_registry import create_first_property, ensure_default_org
from usali.models import Property
from usali.tenancy import FOUNDING_ORG_ID, bind_org_context


def test_creates_a_property_under_the_bound_org(db_session):
    ensure_default_org(db_session)
    bind_org_context(db_session, FOUNDING_ORG_ID)
    pid = create_first_property(
        db_session, FOUNDING_ORG_ID,
        name="Sunset Inn", pms_source="opera", wage_jurisdiction="US-CA",
    )
    db_session.commit()
    row = db_session.execute(
        select(Property).where(Property.property_id == pid)
    ).scalar_one()
    assert row.name == "Sunset Inn" and row.pms_source == "opera"
    assert row.org_id == FOUNDING_ORG_ID and row.wage_jurisdiction == "US-CA"
    assert pid.startswith("sunset-inn-")


def test_defaults_timezone_when_omitted(db_session):
    ensure_default_org(db_session)
    bind_org_context(db_session, FOUNDING_ORG_ID)
    pid = create_first_property(db_session, FOUNDING_ORG_ID,
                                name="No Tz Hotel", pms_source="autoclerk")
    db_session.commit()
    row = db_session.execute(
        select(Property).where(Property.property_id == pid)).scalar_one()
    assert row.timezone == "America/Los_Angeles"  # server default


def test_generated_ids_are_unique_across_calls(db_session):
    ensure_default_org(db_session)
    bind_org_context(db_session, FOUNDING_ORG_ID)
    a = create_first_property(db_session, FOUNDING_ORG_ID, name="Dup", pms_source="opera")
    b = create_first_property(db_session, FOUNDING_ORG_ID, name="Dup", pms_source="opera")
    db_session.commit()
    assert a != b
```

> **Resolve at implementation:** confirm the `db_session` fixture (`tests/conftest.py`)
> is usable for an org-bound write after `bind_org_context` (it is the same
> session type the invites/otp tests write through). Confirm `ensure_default_org`
> and `FOUNDING_ORG_ID` import paths (used across the suite). If `db_session` is
> already founding-bound by the fixture, the explicit `bind_org_context` is a
> harmless re-bind to the same org.

- [ ] **Step 2: Run it (RED)** — `uv run pytest -q tests/test_b1_first_property.py`
  Expected: FAIL (`create_first_property` does not exist).

- [ ] **Step 3: Implement** in `src/usali/mapping/property_registry.py`

```python
import re
import secrets

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

# (Property is already imported in this module for seed_properties.)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "property")[:40]


def create_first_property(
    session: Session,
    org_id: int,
    *,
    name: str,
    pms_source: str,
    wage_jurisdiction: str | None = None,
    timezone: str | None = None,
) -> str:
    """Insert the workspace's first property under an ORG-BOUND session (the
    caller must have called bind_org_context(session, org_id) first — the
    provisioner role cannot write `property`, D-B7). Returns the generated,
    globally-unique property_id. `timezone`/`wage_jurisdiction` fall back to the
    column defaults / NULL when omitted. The caller commits."""
    base = _slugify(name)
    for _ in range(5):
        property_id = f"{base}-{secrets.token_hex(2)}"
        values: dict[str, object] = {
            "property_id": property_id, "org_id": org_id,
            "name": name, "pms_source": pms_source,
        }
        if wage_jurisdiction is not None:
            values["wage_jurisdiction"] = wage_jurisdiction
        if timezone is not None:
            values["timezone"] = timezone
        try:
            with session.begin_nested():  # SAVEPOINT: a collision rolls back to
                session.execute(insert(Property).values(**values))  # here only
            return property_id
        except IntegrityError:
            continue  # astronomically rare 4-hex collision — try a new suffix
    raise RuntimeError(
        f"could not generate a unique property_id for {name!r} after 5 attempts"
    )
```

> **Resolve at implementation:** confirm `Session` and `Property` are already
> imported at the top of `property_registry.py` (seed_properties uses both); add
> only the missing imports (`re`, `secrets`, `insert`, `IntegrityError`).

- [ ] **Step 4: Run tests (GREEN)** — `uv run pytest -q tests/test_b1_first_property.py`
- [ ] **Step 5: Full gates + commit**

```bash
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): create_first_property helper (org-bound, unique id, retry)"
```

---

## Task 2 — `PmsInterestRequest` model + migration `b1d0pmsinterest`

**Files:** Modify `src/usali/models.py`, `tests/test_l4_org_grants.py`; Create the migration.

Not-`OrgScoped` (platform-level demand read across orgs by an admin — same
rationale as `invite`/`otp`). No `org_wall` policy; app-role DML via l2 default
privileges. `UNIQUE(org_id, normalized_pms)` is the per-org de-dupe.

- [ ] **Step 1: Write the failing test** — add to `tests/test_l4_org_grants.py`
  the head-literal bump (change the existing assertion):

```python
    assert ScriptDirectory.from_config(cfg).get_heads() == ["b1d0pmsinterest"]
```

  (No separate model unit test here — Task 3 exercises the table through the
  service. This step's RED is the head-literal test failing until the migration
  exists.)

- [ ] **Step 2: Run it (RED)** — `uv run pytest -q tests/test_l4_org_grants.py`
  Expected: FAIL (head is still `b1c0otp`).

- [ ] **Step 3a: Model** — add to `src/usali/models.py` (plain `Base`, near `Invite`):

```python
class PmsInterestRequest(Base):
    """A captured request for a PMS we don't support yet (Track B/B1 Part-2).
    NOT OrgScoped — platform-level demand data an admin reads ACROSS orgs (same
    rationale as Invite/OtpChallenge). `normalized_pms` is the de-dupe key
    (lowercased, non-alphanumerics stripped); UNIQUE(org_id, normalized_pms)
    stops one org spamming the same PMS while admins aggregate demand by
    normalized_pms."""

    __tablename__ = "pms_interest_request"
    __table_args__ = (
        UniqueConstraint("org_id", "normalized_pms",
                         name="uq_pms_interest_org_norm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organization.org_id", name="fk_pms_interest_org"),
        nullable=True,
    )
    email: Mapped[str] = mapped_column(String(320))
    raw_pms: Mapped[str] = mapped_column(String(60))
    normalized_pms: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(12), server_default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

- [ ] **Step 3b: Migration** — `migrations/versions/b1d0pmsinterest_pms_interest_request.py`

```python
"""Track B/B1 Part-2: the pms_interest_request table. NOT OrgScoped — no
org_wall RLS policy: platform-level demand data. usali_app gets DML via
l2a0rlswall's ALTER DEFAULT PRIVILEGES (future tables), so no grant here."""

import sqlalchemy as sa
from alembic import op

revision = "b1d0pmsinterest"
down_revision = "b1c0otp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pms_interest_request",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_pms_interest_org"),
                  nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("raw_pms", sa.String(length=60), nullable=False),
        sa.Column("normalized_pms", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=12), server_default="new", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "normalized_pms",
                            name="uq_pms_interest_org_norm"),
    )


def downgrade() -> None:
    op.drop_table("pms_interest_request")
```

> **Resolve at implementation:** confirm `UniqueConstraint`, `ForeignKey`,
> `Integer`, `String`, `DateTime`, `func`, `Mapped`, `mapped_column`, `datetime`
> are already imported in `models.py` (Part-1's Invite used all of them). If the
> full suite flags an inventory test (`tests/test_models.py::test_tables_registered`
> or `tests/test_migration_on_populated_data.py`), add `"pms_interest_request"`
> there exactly as Part-1 added `invite`/`otp_challenge` (read the failing
> assertion first).

- [ ] **Step 4: Run (GREEN)** — `uv run pytest -q tests/test_l4_org_grants.py tests/test_l2_rls_wall.py`
- [ ] **Step 5: Full gates + commit**

```bash
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): pms_interest_request model + migration (not-OrgScoped)"
```

---

## Task 3 — `pms_interest.record_request` service

**Files:** Create `src/usali/pms_interest.py`; Test `tests/test_b1_pms_interest.py`.

Normalizes the free-text PMS name and upserts, returning `(row, is_new)`.

- [ ] **Step 1: Write the failing test** — `tests/test_b1_pms_interest.py`

```python
"""record_request: normalizes the PMS name, de-dupes per (org, normalized),
allows the same PMS across orgs, reports is_new."""

from sqlalchemy import select

from usali import pms_interest
from usali.models import PmsInterestRequest
from usali.mapping.property_registry import ensure_default_org
from usali.tenancy import FOUNDING_ORG_ID


def test_normalize_collapses_spacing_case_and_punctuation():
    n = pms_interest._normalize
    assert n("HotelKey") == n("hotel key") == n("Hotel-Key!") == "hotelkey"


def test_records_and_dedupes_within_an_org(db_session):
    ensure_default_org(db_session)
    row1, new1 = pms_interest.record_request(
        db_session, org_id=FOUNDING_ORG_ID, email="a@example.test", raw_pms="HotelKey")
    db_session.commit()
    assert new1 is True and row1.normalized_pms == "hotelkey"
    _, new2 = pms_interest.record_request(
        db_session, org_id=FOUNDING_ORG_ID, email="a@example.test", raw_pms="hotel key")
    db_session.commit()
    assert new2 is False  # same (org, normalized) -> de-duped
    count = db_session.execute(
        select(PmsInterestRequest).where(
            PmsInterestRequest.normalized_pms == "hotelkey")).scalars().all()
    assert len(count) == 1


def test_same_pms_different_org_is_a_distinct_request(db_session):
    ensure_default_org(db_session)
    _, a = pms_interest.record_request(
        db_session, org_id=FOUNDING_ORG_ID, email="a@example.test", raw_pms="SkyTouch")
    _, b = pms_interest.record_request(
        db_session, org_id=None, email="b@example.test", raw_pms="skytouch")
    db_session.commit()
    assert a is True and b is True  # (1,'skytouch') and (NULL,'skytouch') differ
```

> **Resolve at implementation:** `UNIQUE(org_id, normalized_pms)` with a NULL
> `org_id` — Postgres treats NULLs as distinct, so two NULL-org rows for the
> same PMS would both insert. Signup always passes a real `org_id` (the new
> tenant), so this is fine; the `org_id=None` case is only the pre-org path and
> is out of scope here. Keep the test's NULL case as written (it asserts
> distinctness, which holds).

- [ ] **Step 2: Run it (RED)** — `uv run pytest -q tests/test_b1_pms_interest.py`

- [ ] **Step 3: Implement** — `src/usali/pms_interest.py`

```python
"""PMS-interest capture (Track B/B1 Part-2). When a signing-up owner names a PMS
we don't support, we record a de-duped demand signal instead of dropping it.
NOT OrgScoped — an admin reads it across orgs. Does NOT commit; the caller owns
the transaction (the signup path records it in the same request as provisioning)."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import PmsInterestRequest


def _normalize(raw_pms: str) -> str:
    """De-dupe key: lowercase, strip everything non-alphanumeric, so 'HotelKey',
    'hotel key', and 'Hotel-Key!' all collapse to 'hotelkey'. Fuzzy/alias
    matching (e.g. 'cloud beds' ~ 'cloudbeds pms') is a later refinement."""
    return re.sub(r"[^a-z0-9]+", "", raw_pms.lower())


def record_request(
    session: Session, *, org_id: int | None, email: str, raw_pms: str
) -> tuple[PmsInterestRequest, bool]:
    """Record a PMS-interest request, de-duped on (org_id, normalized_pms).
    Returns (row, is_new). is_new is False when this (org, PMS) was already
    captured — the caller notifies the admin only on is_new."""
    normalized = _normalize(raw_pms)
    existing = session.execute(
        select(PmsInterestRequest).where(
            PmsInterestRequest.org_id == org_id,
            PmsInterestRequest.normalized_pms == normalized,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    row = PmsInterestRequest(
        org_id=org_id, email=email, raw_pms=raw_pms, normalized_pms=normalized,
    )
    session.add(row)
    session.flush()
    return row, True
```

> **Resolve at implementation:** the `existing` lookup with `org_id == None`
> generates `IS NULL` in SQLAlchemy only if written as `.is_(None)`. Signup
> always passes a real `org_id`, so `== org_id` is correct for the live path;
> the equality form is fine for non-NULL org_ids. Leave as `== org_id`.

- [ ] **Step 4: Run (GREEN)** — `uv run pytest -q tests/test_b1_pms_interest.py`
- [ ] **Step 5: Full gates + commit**

```bash
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): pms_interest.record_request (normalize + de-dupe)"
```

---

## Task 4 — config `admin_notify_email` + `CompleteRequest` schema

**Files:** Modify `src/usali/config.py`, `src/usali/signup_api.py`; Test add to `tests/test_b1_signup_api.py`.

- [ ] **Step 1: Write the failing test** — add to `tests/test_b1_signup_api.py`

```python
def test_complete_rejects_other_pms_without_a_name(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]
    r = client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "W", "workspace_alias": "w-x",
        "property_name": "P", "pms_source": "other",  # no pms_other_name
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "passw0rd",
    })
    assert r.status_code == 422


def test_complete_rejects_unknown_pms_source(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]
    r = client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "W", "workspace_alias": "w-y",
        "property_name": "P", "pms_source": "sabre-x",  # not a member of the set
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "passw0rd",
    })
    assert r.status_code == 422
```

- [ ] **Step 2: Run it (RED)** — `uv run pytest -q tests/test_b1_signup_api.py -k "rejects"`
  Expected: FAIL (today `pms_source` accepts any 1–20 string; no `pms_other_name`).

- [ ] **Step 3a: config** — add to `Settings` in `src/usali/config.py`
  (near the other signup settings):

```python
    # Where unsupported-PMS demand requests are routed (Track B/B1 Part-2).
    # Empty in dev -> the ConsoleNotifier just logs it; a real address in prod.
    admin_notify_email: str = ""
```

- [ ] **Step 3b: `CompleteRequest`** — in `src/usali/signup_api.py`, replace the
  three PMS/property-related fields + add a validator and `timezone`:

```python
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_SUPPORTED_PMS = ("opera", "autoclerk")


class CompleteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    otp: str = Field(min_length=1, max_length=12)
    workspace_name: str = Field(min_length=1, max_length=200)
    workspace_alias: str = Field(min_length=1, max_length=63)
    property_name: str = Field(min_length=1, max_length=200)
    pms_source: Literal["opera", "autoclerk", "other"]
    pms_other_name: str | None = Field(default=None, min_length=1, max_length=60)
    wage_jurisdiction: str = Field(min_length=1, max_length=10)
    timezone: str | None = Field(default=None, min_length=1, max_length=50)
    cell: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=200)

    @model_validator(mode="after")
    def _other_requires_name(self) -> "CompleteRequest":
        if self.pms_source == "other" and not self.pms_other_name:
            raise ValueError("pms_other_name is required when pms_source is 'other'")
        return self
```

> **Resolve at implementation:** keep the existing `_ALIAS_RE` handler check and
> the password/alias comments already in the file. `Literal` makes an unknown
> `pms_source` a 422 automatically. A pydantic `ValueError` in a `model_validator`
> surfaces as a 422 through FastAPI — confirm with the RED→GREEN of the two tests
> above. Import `Literal` and `model_validator` (the file already imports
> `BaseModel, Field`).

- [ ] **Step 4: Run (GREEN)** — `uv run pytest -q tests/test_b1_signup_api.py -k "rejects"`
- [ ] **Step 5: Full gates + commit**

```bash
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): CompleteRequest pms_source enum + pms_other_name + timezone; admin_notify_email"
```

---

## Task 5 — `/complete` supported-PMS path creates the first property

**Files:** Modify `src/usali/signup_api.py`; Test add to `tests/test_b1_signup_api.py`.

- [ ] **Step 1: Write the failing test** — add to `tests/test_b1_signup_api.py`

```python
def test_complete_creates_the_first_property(db_url, tmp_path, _founding_committed):
    from usali.db import make_engine as me
    from usali.db import make_session_factory as msf
    from usali.models import Property

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    kc = InMemoryKeycloakAdmin()
    client = _signup_client(db_url, tmp_path, notifier=notifier, kc=kc)
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]

    done = client.post("/api/signup/complete", json={
        "token": raw, "otp": code,
        "workspace_name": "New Owner Group", "workspace_alias": "new-owner-group",
        "property_name": "Owner Hotel", "pms_source": "opera",
        "wage_jurisdiction": "US-CA", "timezone": "America/New_York",
        "cell": "+15550000000", "password": "chosen-password",
    })
    assert done.status_code == 201, done.text
    assert done.json()["pms_supported"] is True

    su = msf(me(db_url))  # superuser session sees across orgs
    with su() as s:
        from usali.models import Organization
        from sqlalchemy import select
        org = s.execute(select(Organization).where(
            Organization.kc_org_alias == "new-owner-group")).scalar_one()
        prop = s.execute(select(Property).where(
            Property.org_id == org.org_id)).scalar_one()
        assert prop.name == "Owner Hotel" and prop.pms_source == "opera"
        assert prop.wage_jurisdiction == "US-CA"
        assert prop.timezone == "America/New_York"
```

- [ ] **Step 2: Run it (RED)** — `uv run pytest -q tests/test_b1_signup_api.py -k first_property`
  Expected: FAIL (no property created; `pms_supported` missing).

- [ ] **Step 3: Implement** — in `signup_api.py`, add imports and rework the
  post-provision section of `complete()` (after step 2's `provision_tenant`
  commit, before step 3):

```python
# top of file:
from usali.mapping.property_registry import create_first_property
from usali.tenancy import bind_org_context

# ... inside complete(), replacing the current step-3-only tail:
    # STEP 2b (APP role, bound to the NEW org): create the first property. The
    # provisioner role cannot write `property` (D-B7), so this is a fresh
    # app-role session bound to result.org_id. Supported PMS only; "other" is
    # handled in Task 6.
    pms_supported = payload.pms_source in ("opera", "autoclerk")
    if pms_supported:
        with factory() as session:
            bind_org_context(session, result.org_id)
            create_first_property(
                session, result.org_id,
                name=payload.property_name,
                pms_source=payload.pms_source,
                wage_jurisdiction=payload.wage_jurisdiction,
                timezone=payload.timezone,
            )
            session.commit()

    # STEP 3 (APP role): record which tenant the invite became (audit).
    with factory() as session:
        invites.mark_consumed_org(session, payload.token, result.org_id)
        session.commit()
    return {"org_alias": payload.workspace_alias, "pms_supported": pms_supported}
```

> **Resolve at implementation:** `factory = request.app.state.db_session_factory`
> is already bound to a local in `complete()`; reuse it. The `return` type
> annotation on `complete()` is `dict[str, str]` — widen to
> `dict[str, str | bool]` (or `dict[str, object]`) for the added `pms_supported`.
> Confirm the confinement test still passes (the property write uses the APP
> `factory`, NOT `prov_factory`, so the provisioner is still opened exactly once).

- [ ] **Step 4: Run (GREEN)** — `uv run pytest -q tests/test_b1_signup_api.py`
- [ ] **Step 5: Full gates + commit**

```bash
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): /complete creates the first property for a supported PMS"
```

---

## Task 6 — `/complete` unsupported-PMS path captures + routes demand

**Files:** Modify `src/usali/signup_api.py`; Test add to `tests/test_b1_signup_api.py`.

- [ ] **Step 1: Write the failing test** — add to `tests/test_b1_signup_api.py`

```python
def test_complete_other_pms_records_interest_and_notifies_admin(
    db_url, tmp_path, _founding_committed
):
    from usali.db import make_engine as me
    from usali.db import make_session_factory as msf
    from usali.models import Organization, PmsInterestRequest, Property
    from sqlalchemy import select

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin(), admin_email="ops@example.test")
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]

    done = client.post("/api/signup/complete", json={
        "token": raw, "otp": code,
        "workspace_name": "Sky Group", "workspace_alias": "sky-group",
        "property_name": "Sky Hotel", "pms_source": "other",
        "pms_other_name": "SkyTouch",
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "passw0rd",
    })
    assert done.status_code == 201, done.text
    assert done.json()["pms_supported"] is False

    su = msf(me(db_url))
    with su() as s:
        org = s.execute(select(Organization).where(
            Organization.kc_org_alias == "sky-group")).scalar_one()
        # No property was created for an unsupported PMS.
        assert s.execute(select(Property).where(
            Property.org_id == org.org_id)).scalar_one_or_none() is None
        # A de-duped interest row was recorded.
        req = s.execute(select(PmsInterestRequest).where(
            PmsInterestRequest.org_id == org.org_id)).scalar_one()
        assert req.raw_pms == "SkyTouch" and req.normalized_pms == "skytouch"
    # The admin was emailed (new request).
    assert any(e["to"] == "ops@example.test" and "SkyTouch" in e["body"]
               for e in notifier.emails)
```

> **Resolve at implementation:** `_signup_client` needs an `admin_email` kwarg
> that sets `USALI_ADMIN_NOTIFY_EMAIL` (or passes a settings override into
> `create_app`). Add it to the helper: mirror how the helper already wires
> settings/env for the app. If `create_app` reads `admin_notify_email` from
> settings, set the env var for the client's process the way other config-driven
> tests do; else thread it through. Keep the default (no admin_email) behaving as
> today.

- [ ] **Step 2: Run it (RED)** — `uv run pytest -q tests/test_b1_signup_api.py -k other_pms`

- [ ] **Step 3: Implement** — extend the step-2b branch in `complete()`:

```python
    from usali import pms_interest  # or top-of-file import

    pms_supported = payload.pms_source in ("opera", "autoclerk")
    if pms_supported:
        with factory() as session:
            bind_org_context(session, result.org_id)
            create_first_property(
                session, result.org_id,
                name=payload.property_name, pms_source=payload.pms_source,
                wage_jurisdiction=payload.wage_jurisdiction, timezone=payload.timezone,
            )
            session.commit()
    else:
        # Unsupported PMS: no property; capture de-duped demand + route to admin.
        # pms_interest_request is not-OrgScoped, so no org binding is needed.
        with factory() as session:
            _, is_new = pms_interest.record_request(
                session, org_id=result.org_id, email=invite_email,
                raw_pms=payload.pms_other_name or "",
            )
            session.commit()
        admin_email = request.app.state.admin_notify_email
        if is_new and admin_email:
            request.app.state.notifier.send_email(
                to=admin_email,
                subject="New PMS request from a signup",
                body=(f"Org {payload.workspace_alias} ({invite_email}) requested "
                      f"PMS: {payload.pms_other_name}"),
            )
```

> **Resolve at implementation:** expose `admin_notify_email` on `app.state` in
> `create_app` (set `app.state.admin_notify_email = settings.admin_notify_email`
> next to the other signup wiring) so the endpoint reads it without re-reading
> settings. `invite_email` is already captured in step 1. Move the
> `pms_interest` import to the top of the file with the others. Confirm the
> provisioner is still opened exactly once (this branch uses the APP `factory`).

- [ ] **Step 4: Run (GREEN)** — `uv run pytest -q tests/test_b1_signup_api.py`
- [ ] **Step 5: Full gates + commit**

```bash
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): /complete captures + routes unsupported-PMS demand"
```

---

## Self-review checklist

- [ ] §5a CompleteRequest: `pms_source` Literal enum, `pms_other_name` conditional
  (422 without it), `timezone` optional — Task 4.
- [ ] §5b first property on an org-bound app session (never the provisioner);
  unique `property_id` with retry; timezone default when omitted — Tasks 1, 5.
- [ ] §5c `pms_interest_request` not-OrgScoped table + migration `b1d0pmsinterest`
  + `record_request` normalize/de-dupe — Tasks 2, 3.
- [ ] §5d `/complete` branch: supported→property, other→interest+notify;
  `pms_supported` in the response — Tasks 5, 6.
- [ ] Confinement preserved: every new write uses the APP `factory`; the
  provisioner is still opened exactly once (Part-1 confinement test green).
- [ ] Migration chain linear `b1c0otp → b1d0pmsinterest`, `get_heads()` length 1;
  head literal bumped; any inventory tests updated.
- [ ] Admin notified only on a NEW (org, PMS) request; empty `admin_notify_email`
  → ConsoleNotifier logs it (no crash).
- [ ] mypy `complete()` return type widened for `pms_supported`.

## Deviations
None anticipated. The one design nuance carried into code: property creation and
provisioning are two commits (the D-B7 split); a property-creation failure after
a successful provision leaves a property-less workspace the owner completes
post-login (D8) — not reverted, because the org already exists.
