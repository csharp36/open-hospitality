# Track B / B4 — open-items checklist (BACKEND) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer "what is left to set up?" for a tenant by probing what is
actually configured, persisting only dismissals.

**Architecture:** A module-level `ITEMS` closed set in `checklist.py`, each item
owning a `probe(session) -> bool` that runs one scoped query under the caller's
already-org-bound session. Status is computed per request — `done` from the
probe, else `dismissed` if an `org_checklist_override` row exists, else `open`.
A new `checklist_api.py` router exposes the read plus idempotent PUT/DELETE
dismissal endpoints.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, pydantic,
Postgres 16 via testcontainers, `uv`.

Implements [`docs/design/2026-08-30-track-b-b4-open-items-checklist-design.md`](../design/2026-08-30-track-b-b4-open-items-checklist-design.md).
The frontend is a separate plan, written after this lands.
Branch: `feat/track-b-b4-checklist`.

---

## Gates (run for EVERY task before committing)

```bash
uv run pytest -q            # full suite (testcontainers Postgres, ~9 min)
uv run mypy src             # src-only, never tests/
uv run ruff check src tests
```

Fixtures are synthetic — no real PII, phone numbers, or credentials.

## Grounding facts (verified against the code — do not re-guess)

- **Alembic head is `b1d0pmsinterest`** (`migrations/versions/b1d0pmsinterest_pms_interest_request.py`).
  The single test hardcoding the head literal is
  `tests/test_l4_org_grants.py:358` — it asserts `get_heads() == ["b1d0pmsinterest"]`
  and MUST be updated in Task 2. Other head tests assert only `len(get_heads()) == 1`.
- **`OrgScoped`** (`models.py:33`) supplies `org_id` as a plain indexed FK.
  `OrgSettings` (`models.py:467`) shows the override idiom when `org_id` must be
  part of the primary key — copy that shape.
- **The RLS template** is `migrations/versions/l5a0orgsettings_org_settings.py`:
  `ENABLE` + `FORCE ROW LEVEL SECURITY`, then a policy named exactly `org_wall`
  using `_PREDICATE` built from `usali.tenancy.RLS_ORG_VAR`. The app role's DML
  grant arrives automatically via the DEFAULT PRIVILEGES that `l2a0rlswall`
  recorded — **no grant boilerplate**.
- **Router prefixes:** `portal_api.py:693` is `APIRouter(prefix="/api")`;
  `property_config_api.py:75` is `/api/properties`; `crm_api.py:45` is
  `/api/crm`. The new router is `/api/checklist`.
- **`request_session_factory` lives in `usali.auth:384`, NOT `usali.tenancy`**
  (corrected 2026-08-30 — an earlier draft of this plan had it wrong; every
  feature router imports it from `usali.auth`). `current_org_id` IS in
  `usali.tenancy:128`.
- **The session idiom for a feature router** (`property_config_api.py:78`):
  ```python
  def _session(request: Request) -> Session:
      return request_session_factory(request)()
  ```
  used as `with _session(request) as session:` inside each endpoint.
- **`require_grants`** (`auth.py:282`) returns a dependency yielding a
  `Principal`. `ORG_ADMIN = "org_admin"` (`auth.py:194`). Precedent:
  `crm_api.py:43`, `payroll_run_api.py:48`.
- **`Principal.subject`** (`auth.py:44`) is the Keycloak subject — the value for
  `created_by`.
- **Models the probes query:** `IngestBatch` (`models.py:91`), `RoomInventory`
  (`models.py:1320`, has `property_id`), `FiscalCalendar` (`models.py:1379`, has
  `property_id`), `RoleAssignment` (`models.py:792`, has `keycloak_subject`),
  `OrgSettings` (`models.py:467`, has `crm_provider`), `Property`.
- **`test_tables_registered`** (`tests/test_models.py:12`) asserts the exact set
  of table names in `Base.metadata`. Adding a model without updating it fails.
- **Tenancy invariants** live in two places, and BOTH matter:
  - `tests/test_migration_on_populated_data.py`: `_L1_ORG_INDEPENDENT` (line
    1291) lists tables that legitimately carry no `org_id`.
    `org_checklist_override` is `OrgScoped`, so it does **NOT** go in that set.
  - `tests/test_l2_rls_wall.py::test_the_rls_inventory_is_complete_and_forced`
    (line 428) hardcodes the **complete expected set** of org-walled tables as
    a literal. It is deliberately exact and never sampled, so a new org-scoped
    table does **NOT** join it automatically — you must add its name in the
    same commit, as `l5a0orgsettings`, `m1a0propcfg`, and `m2a0perffoundations`
    each did. **Corrected 2026-08-30:** an earlier draft of this plan claimed
    the inventory tests pick the table up automatically. They do not. Task 2's
    gate command must therefore include `tests/test_l2_rls_wall.py`.
- **The upsert idiom** is `sqlalchemy.dialects.postgresql.insert(...)` +
  `.on_conflict_do_nothing(...)`; precedent at `stage.py:64`, `ledger_stage.py:52`.
- **Ruff** runs the pre-0.16 default set (`pyproject.toml` `[tool.ruff.lint]`) —
  `E`/`F` only. An unused function parameter is not flagged.
- **Test auth helpers:** `verifier, mint = make_authkit()` (`tests/authkit.py:30`,
  note the order), `grant_role(db_session, "org_admin", sub=..., org_id=1)`
  (`tests/grants.py:20`), and `DEFAULT_ORG_ALIAS`. Copy the `_client(...)`
  factory from `tests/test_property_config_api.py:22`.

## File structure

| File | Responsibility |
|---|---|
| `src/usali/models.py` (modify) | `OrgChecklistOverride` — the only persisted state |
| `migrations/versions/b2a0checklist_org_checklist_override.py` (create) | table + RLS policy |
| `src/usali/checklist.py` (create) | `ChecklistItem`, `ItemStatus`, `ITEMS`, the probes, `evaluate()` — pure over a `Session`, no HTTP |
| `src/usali/checklist_api.py` (create) | the router: read + dismissal endpoints, pydantic models |
| `src/usali/server.py` (modify) | mount the router with `operator_gates` |
| `tests/test_checklist.py` (create) | `evaluate()` precedence + every probe |
| `tests/test_checklist_api.py` (create) | endpoints, refusals, idempotency |
| `tests/test_checklist_tenancy.py` (create) | two-org isolation of dismissals |

---

## Task 1: The `OrgChecklistOverride` model

**Files:**
- Modify: `src/usali/models.py`
- Modify: `tests/test_models.py:12` (the registered-tables set)
- Test: `tests/test_checklist.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_checklist.py`:

```python
from usali.models import Base, OrgChecklistOverride


def test_override_is_org_scoped_with_composite_pk():
    table = Base.metadata.tables["org_checklist_override"]
    assert {c.name for c in table.primary_key.columns} == {"org_id", "item_key"}
    assert table.c.org_id.nullable is False
    assert table.c.note.nullable is True
    # The CHECK is the schema mirror of ITEMS (design §5).
    checks = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert "ck_org_checklist_override_item_key" in checks


def test_override_model_is_orgscoped():
    from usali.models import OrgScoped
    assert issubclass(OrgChecklistOverride, OrgScoped)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checklist.py -v`
Expected: FAIL — `ImportError: cannot import name 'OrgChecklistOverride'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/usali/models.py`, immediately after the `OrgSettings` class:

```python
class OrgChecklistOverride(OrgScoped, Base):
    """A dismissed onboarding open item (Track B/B4, D-B4.1). The ONLY
    persisted checklist state: every item's done/open status is DERIVED by
    probing what is actually configured, so there is no stored status to go
    stale. Presence of a row means dismissed; un-dismissing is a row delete
    (no `state` column — with one legal value it could only say one thing).

    `org_id` is part of the composite primary key and the FK to
    `organization`, the `OrgSettings` "org-scoped by its own key" shape. Both
    L2 walls therefore confine it automatically — the ORM criteria hook and
    the `org_wall` RLS policy.

    A dismissal LOSES to a probe that says done (D-B4.4): `checklist.evaluate`
    consults this table only when the probe reports the item still open.
    """

    __tablename__ = "org_checklist_override"
    # The CHECK is the SCHEMA MIRROR of the keys in usali.checklist.ITEMS —
    # kept literal on purpose so the DB refuses an unknown key independently
    # of the app import (the org_settings.crm_provider discipline). Adding an
    # item means editing ITEMS *and* this literal plus its migration.
    __table_args__ = (
        CheckConstraint(
            "item_key IN ('first_report', 'room_inventory', 'fiscal_calendar', "
            "'payroll', 'accounting', 'demand_feed', 'team')",
            name="ck_org_checklist_override_item_key",
        ),
    )

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organization.org_id", name="fk_org_checklist_override_org"),
        primary_key=True,
    )
    item_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

Then add `"org_checklist_override",` to the set in `tests/test_models.py:14`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_checklist.py tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/usali/models.py tests/test_models.py tests/test_checklist.py
git commit -m "feat(checklist): the dismissal override, the only stored state"
```

---

## Task 2: The migration

**Files:**
- Create: `migrations/versions/b2a0checklist_org_checklist_override.py`
- Modify: `tests/test_l4_org_grants.py:358` (the head literal)

- [ ] **Step 1: Write the failing test**

Update the head literal in `tests/test_l4_org_grants.py:358`:

```python
    assert ScriptDirectory.from_config(cfg).get_heads() == ["b2a0checklist"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_l4_org_grants.py::test_l4_is_the_single_alembic_head -v`
Expected: FAIL — the head is still `b1d0pmsinterest`

- [ ] **Step 3: Write minimal implementation**

Create `migrations/versions/b2a0checklist_org_checklist_override.py`:

```python
"""B4: the onboarding open-items checklist — `org_checklist_override`.

The checklist itself is DERIVED (usali.checklist probes what is actually
configured), so this table stores only the one fact nothing can derive: that
a tenant dismissed an optional item. Presence of a row means dismissed.

Joins the L2 database wall on the same terms as every other org-scoped table:
ENABLE/FORCE ROW LEVEL SECURITY plus the `org_wall` policy keyed on the
transaction-local `app.org_id` (the l2a0rlswall predicate, reused verbatim so
the two cannot drift). The app role's DML grant arrives automatically through
the DEFAULT PRIVILEGES l2a0rlswall recorded — no grant boilerplate here.

The `item_key` CHECK is the schema mirror of usali.checklist.ITEMS, literal on
purpose so the DB refuses an unknown key independently of the app import.

Downgrade drops the policy and the table: a dismissal is operator input a
re-seed does not need to reconstruct.
"""

from alembic import op
import sqlalchemy as sa

from usali.tenancy import RLS_ORG_VAR

revision = "b2a0checklist"
down_revision = "b1d0pmsinterest"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "org_checklist_override",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_org_checklist_override_org"),
            primary_key=True,
        ),
        sa.Column("item_key", sa.String(length=40), primary_key=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_key IN ('first_report', 'room_inventory', 'fiscal_calendar', "
            "'payroll', 'accounting', 'demand_feed', 'team')",
            name="ck_org_checklist_override_item_key",
        ),
    )
    op.execute("ALTER TABLE org_checklist_override ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_checklist_override FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON org_checklist_override "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY {_POLICY} ON org_checklist_override")
    op.drop_table("org_checklist_override")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_l4_org_grants.py tests/test_migration_on_populated_data.py tests/test_l2_rls_wall.py -v`
Expected: PASS. `test_l2_rls_wall.py` will FAIL until you add
`"org_checklist_override"` to the hardcoded `expected` set in
`test_the_rls_inventory_is_complete_and_forced` (line 428) and extend its
docstring in the established style — that literal is part of this task.

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/b2a0checklist_org_checklist_override.py tests/test_l4_org_grants.py
git commit -m "feat(checklist): migrate org_checklist_override behind the org wall"
```

---

## Task 3: `evaluate()` and the status precedence

Pure logic first, against an injected registry — no real probes yet, so the
precedence rules are pinned independently of what any probe queries.

**Files:**
- Create: `src/usali/checklist.py`
- Test: `tests/test_checklist.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_checklist.py`:

```python
from usali.checklist import ChecklistItem, evaluate
from usali.models import Organization, OrgChecklistOverride


def _item(key, *, done, required=False):
    return ChecklistItem(
        key=key, title=f"T {key}", description=f"D {key}",
        required=required, where="/setup", probe=lambda _session: done,
    )


def test_open_when_probe_says_not_done(db_session, founding_org):
    [row] = evaluate(db_session, items=(_item("payroll", done=False),))
    assert row.status == "open"


def test_done_when_probe_says_done(db_session, founding_org):
    [row] = evaluate(db_session, items=(_item("payroll", done=True),))
    assert row.status == "done"


def test_dismissed_when_an_override_exists_and_probe_is_open(db_session, founding_org):
    db_session.add(OrgChecklistOverride(org_id=1, item_key="payroll", created_by="s"))
    db_session.commit()
    [row] = evaluate(db_session, items=(_item("payroll", done=False),))
    assert row.status == "dismissed"


def test_done_outranks_a_dismissal(db_session, founding_org):
    """D-B4.4: the operator dismissed payroll, then actually connected it."""
    db_session.add(OrgChecklistOverride(org_id=1, item_key="payroll", created_by="s"))
    db_session.commit()
    [row] = evaluate(db_session, items=(_item("payroll", done=True),))
    assert row.status == "done"


def test_a_raising_probe_degrades_only_that_item(db_session, founding_org):
    """Design §8: loud but contained — and never `done`."""
    def _boom(_session):
        raise RuntimeError("probe exploded")

    bad = ChecklistItem(key="payroll", title="T", description="D", required=False,
                        where="/setup", probe=_boom)
    rows = evaluate(db_session, items=(bad, _item("team", done=True)))
    by_key = {r.key: r for r in rows}
    assert by_key["payroll"].status == "error"
    assert by_key["payroll"].status != "done"
    assert by_key["team"].status == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checklist.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usali.checklist'`

- [ ] **Step 3: Write minimal implementation**

Create `src/usali/checklist.py`:

```python
"""The onboarding open-items checklist (Track B/B4, D8.2).

Status is DERIVED, never stored: each item owns a `probe(session) -> bool`
that answers "is this configured for the active org?" against the caller's
already-org-bound session, so both tenancy walls apply and a probe cannot
observe another tenant's rows. The only persisted state is a dismissal
(`OrgChecklistOverride`), and a dismissal LOSES to a probe that says done
(D-B4.4) — an operator who dismissed payroll in August and connected it in
March must see `done`, not a stale "dismissed".

`ITEMS` is the closed set, the `CRM_PROVIDERS` idiom: one place to read the
whole checklist. Its keys are mirrored by a literal CHECK on
`org_checklist_override.item_key` (models.py) so the DB refuses an unknown
key independently of this import.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from usali.models import (
    FiscalCalendar,
    IngestBatch,
    OrgChecklistOverride,
    OrgSettings,
    Property,
    RoleAssignment,
    RoomInventory,
)

logger = logging.getLogger("usali.checklist")

Probe = Callable[[Session], bool]

DONE = "done"
OPEN = "open"
DISMISSED = "dismissed"
ERROR = "error"


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    title: str
    description: str
    required: bool
    where: str  # the SPA route that closes this item
    probe: Probe


@dataclass(frozen=True)
class ItemStatus:
    key: str
    title: str
    description: str
    required: bool
    where: str
    status: str
    detail: str | None = None


def evaluate(
    session: Session, items: Sequence[ChecklistItem] | None = None
) -> list[ItemStatus]:
    """One ItemStatus per registered item, computed under `session`'s org."""
    registry = ITEMS if items is None else items
    dismissed = {
        key for (key,) in session.execute(select(OrgChecklistOverride.item_key))
    }
    out: list[ItemStatus] = []
    for item in registry:
        try:
            done = item.probe(session)
        except Exception as exc:  # design §8: loud, but contained
            logger.exception("checklist probe failed for %s", item.key)
            out.append(_status(item, ERROR, detail=type(exc).__name__))
            continue
        if done:
            status = DONE
        elif item.key in dismissed:
            status = DISMISSED
        else:
            status = OPEN
        out.append(_status(item, status))
    return out


def _status(item: ChecklistItem, status: str, *, detail: str | None = None) -> ItemStatus:
    return ItemStatus(
        key=item.key, title=item.title, description=item.description,
        required=item.required, where=item.where, status=status, detail=detail,
    )


ITEMS: tuple[ChecklistItem, ...] = ()  # filled in Task 4
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_checklist.py -v`
Expected: PASS (5 new tests)

- [ ] **Step 5: Commit**

```bash
git add src/usali/checklist.py tests/test_checklist.py
git commit -m "feat(checklist): derive status, and let done outrank a dismissal"
```

---

## Task 4: The seven probes and the registry

**Files:**
- Modify: `src/usali/checklist.py`
- Test: `tests/test_checklist.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_checklist.py`:

```python
from datetime import date

from usali.checklist import ITEMS, evaluate
from usali.models import (
    FiscalCalendar, IngestBatch, OrgSettings, Property, RoomInventory,
)
from tests.grants import grant_role


def _status_of(db_session, key):
    return {r.key: r for r in evaluate(db_session)}[key].status


def test_registry_keys_match_the_schema_mirror(db_session):
    """models.py's CHECK literal and ITEMS must not drift (design §5)."""
    from usali.models import Base
    table = Base.metadata.tables["org_checklist_override"]
    [check] = [
        c for c in table.constraints
        if getattr(c, "name", None) == "ck_org_checklist_override_item_key"
    ]
    in_check = {
        part.strip().strip("'")
        for part in str(check.sqltext).split("(")[-1].rstrip(")").split(",")
    }
    assert in_check == {item.key for item in ITEMS}


def test_first_report_is_open_then_done(db_session, founding_org):
    assert _status_of(db_session, "first_report") == "open"
    db_session.add(IngestBatch(org_id=1, pms_source="OPERA", report_type="trial_balance",
                               source_file="f.pdf", file_hash="h"))
    db_session.commit()
    assert _status_of(db_session, "first_report") == "done"


def test_room_inventory_needs_at_least_one_property(db_session, founding_org):
    """An org with no properties must NOT satisfy the probe vacuously."""
    assert _status_of(db_session, "room_inventory") == "open"


def test_room_inventory_done_only_when_every_property_has_a_row(db_session, founding_org):
    db_session.add_all([
        Property(property_id="HISJ", org_id=1, name="H", pms_source="OPERA"),
        Property(property_id="SSSJ", org_id=1, name="S", pms_source="OPERA"),
    ])
    db_session.add(RoomInventory(org_id=1, property_id="HISJ",
                                 effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    assert _status_of(db_session, "room_inventory") == "open"  # SSSJ still missing
    db_session.add(RoomInventory(org_id=1, property_id="SSSJ",
                                 effective_date=date(2026, 1, 1), total_rooms=90))
    db_session.commit()
    assert _status_of(db_session, "room_inventory") == "done"


def test_fiscal_calendar_done_when_every_property_has_a_row(db_session, founding_org):
    db_session.add(Property(property_id="HISJ", org_id=1, name="H", pms_source="OPERA"))
    db_session.commit()
    assert _status_of(db_session, "fiscal_calendar") == "open"
    db_session.add(FiscalCalendar(org_id=1, property_id="HISJ",
                                  calendar_type="calendar_month",
                                  fiscal_year_start_month=1, week_start_weekday=None))
    db_session.commit()
    assert _status_of(db_session, "fiscal_calendar") == "done"


def test_demand_feed_reads_org_settings(db_session, founding_org):
    db_session.merge(OrgSettings(org_id=1, crm_provider=""))
    db_session.commit()
    assert _status_of(db_session, "demand_feed") == "open"
    db_session.merge(OrgSettings(org_id=1, crm_provider="delphi"))
    db_session.commit()
    assert _status_of(db_session, "demand_feed") == "done"


def test_team_needs_a_second_subject(db_session, founding_org):
    grant_role(db_session, "org_admin", sub="founder", org_id=1)
    assert _status_of(db_session, "team") == "open"
    grant_role(db_session, "accountant", sub="second-human", org_id=1)
    assert _status_of(db_session, "team") == "done"


def test_payroll_and_accounting_ignore_process_wide_settings(db_session, founding_org):
    """D-B4.3: a deployment-wide credential is not THIS tenant's connection.
    Both stay open until OH-17 gives them per-tenant config."""
    assert _status_of(db_session, "payroll") == "open"
    assert _status_of(db_session, "accounting") == "open"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checklist.py -v`
Expected: FAIL — `KeyError: 'first_report'` (ITEMS is still empty)

- [ ] **Step 3: Write minimal implementation**

Replace the `ITEMS: tuple[ChecklistItem, ...] = ()` placeholder at the bottom of
`src/usali/checklist.py` with the probes and the registry:

```python
def _every_property_has(session: Session, column: object) -> bool:
    """True when the org has at least one property AND every one of them has a
    row carrying `column`. The at-least-one guard matters: `all()` over an
    empty property list is vacuously true, which would report a
    partially-provisioned tenant as configured."""
    properties = {pid for (pid,) in session.execute(select(Property.property_id))}
    if not properties:
        return False
    covered = {pid for (pid,) in session.execute(select(distinct(column)))}
    return properties <= covered


def _probe_first_report(session: Session) -> bool:
    return session.execute(select(IngestBatch.batch_id).limit(1)).first() is not None


def _probe_room_inventory(session: Session) -> bool:
    return _every_property_has(session, RoomInventory.property_id)


def _probe_fiscal_calendar(session: Session) -> bool:
    return _every_property_has(session, FiscalCalendar.property_id)


def _probe_payroll(session: Session) -> bool:
    """D-B4.3: deliberately ignores `settings.payroll_provider`. A
    process-wide credential is not this tenant's connection, so the honest
    answer for a real tenant is "not connected". OH-17 replaces this body."""
    return False


def _probe_accounting(session: Session) -> bool:
    """D-B4.3, as `_probe_payroll`. OH-17 replaces this body."""
    return False


def _probe_demand_feed(session: Session) -> bool:
    row = session.execute(select(OrgSettings.crm_provider)).scalar_one_or_none()
    return bool(row)


def _probe_team(session: Session) -> bool:
    count = session.execute(
        select(func.count(distinct(RoleAssignment.keycloak_subject)))
    ).scalar_one()
    return count > 1


ITEMS: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        key="first_report", title="Upload your first PMS report",
        description="Drop a night-audit export to see your USALI statement.",
        required=True, where="/upload", probe=_probe_first_report,
    ),
    ChecklistItem(
        key="room_inventory", title="Set sellable room inventory",
        description="Occupancy, ADR and RevPAR divide by this — they cannot be "
                    "computed without it.",
        required=True, where="/property-config", probe=_probe_room_inventory,
    ),
    ChecklistItem(
        key="fiscal_calendar", title="Define the fiscal calendar",
        description="Calendar-month or 4-4-5, per property.",
        required=True, where="/property-config", probe=_probe_fiscal_calendar,
    ),
    ChecklistItem(
        key="payroll", title="Connect payroll",
        description="Optional. Compare estimated labor cost against the actual "
                    "gross-to-net from your provider.",
        required=False, where="/payroll", probe=_probe_payroll,
    ),
    ChecklistItem(
        key="accounting", title="Connect QuickBooks Online",
        description="Optional. Push the journal entry behind your statement.",
        required=False, where="/qbo", probe=_probe_accounting,
    ),
    ChecklistItem(
        key="demand_feed", title="Connect a demand feed",
        description="Optional. Pull group and event demand from Delphi or Tripleseat.",
        required=False, where="/schedule", probe=_probe_demand_feed,
    ),
    ChecklistItem(
        key="team", title="Invite your team",
        description="Optional. Add a second operator so you are not the only "
                    "person who can log in.",
        required=False, where="/employees", probe=_probe_team,
    ),
)
```

Move the `ITEMS` definition below the probes (a module-level tuple cannot
reference functions defined after it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_checklist.py -v`
Expected: PASS (all probe tests plus the mirror test)

- [ ] **Step 5: Commit**

```bash
git add src/usali/checklist.py tests/test_checklist.py
git commit -m "feat(checklist): the seven probes, and the registry they close over"
```

---

## Task 5: `GET /api/checklist`

**Files:**
- Create: `src/usali/checklist_api.py`
- Modify: `src/usali/server.py`
- Test: `tests/test_checklist_api.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_checklist_api.py`:

```python
from fastapi.testclient import TestClient

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import IngestBatch, Organization
from usali.server import create_app


def _client(db_engine, tmp_path, verifier) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app)


def _org(db_session):
    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.commit()


def _admin_headers(mint, db_session, sub="cl-admin"):
    grant_role(db_session, "org_admin", sub=sub, org_id=1)
    return {"Authorization": f"Bearer {mint(roles=['org_admin'], sub=sub)}"}


def test_get_checklist_reports_open_items(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/checklist", headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    body = r.json()
    assert body["all_clear"] is False
    assert body["open_count"] == len(body["items"])
    first = {i["key"]: i for i in body["items"]}["first_report"]
    assert first["status"] == "open"
    assert first["required"] is True
    assert first["where"] == "/upload"


def test_get_checklist_marks_done_items(db_engine, db_session, tmp_path):
    _org(db_session)
    db_session.add(IngestBatch(org_id=1, pms_source="OPERA", report_type="trial_balance",
                               source_file="f.pdf", file_hash="h"))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/checklist", headers=_admin_headers(mint, db_session))
    items = {i["key"]: i for i in r.json()["items"]}
    assert items["first_report"]["status"] == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checklist_api.py -v`
Expected: FAIL — 404, the route does not exist

- [ ] **Step 3: Write minimal implementation**

Create `src/usali/checklist_api.py`:

```python
"""The onboarding checklist router (Track B/B4).

Its own module rather than more weight on `portal_api` (past 1200 lines).
Reading the checklist needs only the router's operator gate; DISMISSING an
item requires `org_admin`, because "we don't use payroll" is a standing
commitment about the tenant rather than a per-user preference.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from usali.auth import request_session_factory
from usali.checklist import OPEN, evaluate

router = APIRouter(prefix="/api/checklist")


def _session(request: Request) -> Session:
    return request_session_factory(request)()


class ItemModel(BaseModel):
    key: str
    title: str
    description: str
    required: bool
    where: str
    status: str
    detail: str | None = None


class ChecklistModel(BaseModel):
    items: list[ItemModel]
    open_count: int
    all_clear: bool


@router.get("")
def get_checklist(request: Request) -> ChecklistModel:
    """Every registered item with its DERIVED status for the active org."""
    with _session(request) as session:
        rows = evaluate(session)
    items = [ItemModel(**vars(row)) for row in rows]
    open_count = sum(1 for row in rows if row.status == OPEN)
    return ChecklistModel(
        items=items, open_count=open_count, all_clear=open_count == 0
    )
```

In `src/usali/server.py`, add the import beside the other routers:

```python
from usali.checklist_api import router as checklist_router
```

and mount it with the other operator-gated routers (next to
`app.include_router(property_config_router, dependencies=operator_gates)`):

```python
    app.include_router(checklist_router, dependencies=operator_gates)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_checklist_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/usali/checklist_api.py src/usali/server.py tests/test_checklist_api.py
git commit -m "feat(checklist): GET /api/checklist"
```

---

> **Caller constraint (added 2026-08-30 from Task 3's review).** `evaluate()`
> rolls back on a probe failure, so it must never be called on a session
> carrying an UNCOMMITTED write — a later probe raising would discard that
> write as collateral. Today's only caller is the `GET`, which writes nothing,
> and the dismissal endpoints below commit before returning. If a future
> handler wants to write and then return a fresh checklist in one response, it
> must commit first or evaluate on a separate read.

## Task 6: Idempotent dismissal — `PUT` / `DELETE`

**Files:**
- Modify: `src/usali/checklist_api.py`
- Test: `tests/test_checklist_api.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_checklist_api.py`:

```python
def test_dismissing_an_optional_item_hides_it_from_the_open_count(
    db_engine, db_session, tmp_path
):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    before = c.get("/api/checklist", headers=h).json()["open_count"]
    r = c.put("/api/checklist/payroll/dismissal", json={"note": "we use a bureau"},
              headers=h)
    assert r.status_code == 204
    body = c.get("/api/checklist", headers=h).json()
    assert body["open_count"] == before - 1
    assert {i["key"]: i for i in body["items"]}["payroll"]["status"] == "dismissed"


def test_dismissal_is_idempotent(db_engine, db_session, tmp_path):
    """D-B4.5: two browser sessions dismissing at once must not 500."""
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    assert c.put("/api/checklist/payroll/dismissal", headers=h).status_code == 204
    assert c.put("/api/checklist/payroll/dismissal", headers=h).status_code == 204


def test_undismissing_is_idempotent_too(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    c.put("/api/checklist/payroll/dismissal", headers=h)
    assert c.delete("/api/checklist/payroll/dismissal", headers=h).status_code == 204
    assert c.delete("/api/checklist/payroll/dismissal", headers=h).status_code == 204
    body = c.get("/api/checklist", headers=h).json()
    assert {i["key"]: i for i in body["items"]}["payroll"]["status"] == "open"


def test_dismissing_a_required_item_refuses_loudly(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.put("/api/checklist/room_inventory/dismissal",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 422
    assert "required" in r.json()["detail"]


def test_unknown_item_key_is_404(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.put("/api/checklist/no_such_item/dismissal",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 404


def test_a_non_admin_operator_cannot_dismiss(db_engine, db_session, tmp_path):
    _org(db_session)
    grant_role(db_session, "accountant", sub="bookkeeper", org_id=1)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["accountant"], sub="bookkeeper")
    r = c.put("/api/checklist/payroll/dismissal",
              headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checklist_api.py -v`
Expected: FAIL — 405, PUT is not allowed on a route that does not exist

- [ ] **Step 3: Write minimal implementation**

Add to `src/usali/checklist_api.py` — extend the imports:

```python
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from usali.auth import (
    ORG_ADMIN, Principal, request_session_factory, require_grants,
)
from usali.checklist import ITEMS, OPEN, evaluate
from usali.models import OrgChecklistOverride
from usali.tenancy import current_org_id
```

then append:

```python
require_checklist_admin = require_grants(ORG_ADMIN)

_BY_KEY = {item.key: item for item in ITEMS}


class DismissRequest(BaseModel):
    note: str | None = None


def _item_or_404(key: str):
    item = _BY_KEY.get(key)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown checklist item")
    return item


@router.put("/{key}/dismissal", status_code=204)
def dismiss(
    key: str,
    request: Request,
    body: DismissRequest | None = None,
    principal: Principal = Depends(require_checklist_admin),
) -> Response:
    """Record that this tenant is opting out of an OPTIONAL item.

    Idempotent (D-B4.5): concurrent browser sessions must not collide on the
    composite key, so a repeat is ON CONFLICT DO NOTHING — which also keeps
    the FIRST dismisser's audit row, the decision that actually happened.
    """
    item = _item_or_404(key)
    if item.required:
        raise HTTPException(
            status_code=422,
            detail=f"{key} is required and cannot be dismissed",
        )
    with _session(request) as session:
        session.execute(
            pg_insert(OrgChecklistOverride)
            .values(
                org_id=current_org_id(session),
                item_key=key,
                note=(body.note if body else None),
                created_by=principal.subject,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "item_key"])
        )
        session.commit()
    return Response(status_code=204)


@router.delete("/{key}/dismissal", status_code=204)
def undismiss(
    key: str,
    request: Request,
    principal: Principal = Depends(require_checklist_admin),
) -> Response:
    """Reopen a dismissed item. Deleting an absent override is a no-op."""
    _item_or_404(key)
    with _session(request) as session:
        session.execute(
            delete(OrgChecklistOverride).where(OrgChecklistOverride.item_key == key)
        )
        session.commit()
    return Response(status_code=204)
```

> `current_org_id(session)` is `tenancy.py:128` — it returns the session's
> bound org and refuses loudly (`MissingOrgContext`) if there is none. Setting
> `org_id` explicitly satisfies the RLS `WITH CHECK`, which requires it to
> equal the session org. The `DELETE` needs no `org_id` filter: both walls
> scope it to the active org already.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_checklist_api.py -v`
Expected: PASS (all six new tests)

- [ ] **Step 5: Commit**

```bash
git add src/usali/checklist_api.py tests/test_checklist_api.py
git commit -m "feat(checklist): idempotent dismissal, and a loud refusal on required items"
```

---

## Task 7: Two-org isolation

**Files:**
- Create: `tests/test_checklist_tenancy.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_checklist_tenancy.py`:

```python
"""One org's dismissal must be invisible to another — the L2 walls, applied
to the checklist. Uses the shared world and the exact idiom of
test_l7_two_org_walk.py:41."""

from usali.checklist import evaluate
from usali.db import make_session_factory
from usali.models import OrgChecklistOverride
from usali.tenancy import bind_org_context


def test_a_dismissal_does_not_leak_across_orgs(two_tenant_world, app_role_engine):
    w = two_tenant_world
    factory = make_session_factory(app_role_engine)

    with factory() as s:
        bind_org_context(s, 1)
        s.add(OrgChecklistOverride(org_id=1, item_key="payroll", created_by="a"))
        s.commit()

    with factory() as s:
        bind_org_context(s, 1)
        assert {r.key: r.status for r in evaluate(s)}["payroll"] == "dismissed"

    with factory() as s:
        bind_org_context(s, w.org2_id)
        assert {r.key: r.status for r in evaluate(s)}["payroll"] == "open"
```

> `two_tenant_world` (`conftest.py:107`) builds org 1 beside a provisioned
> org 2 and returns a namespace carrying `org2_id`; `app_role_engine` is the
> non-owner engine RLS actually applies to. `bind_org_context` is called
> INSIDE the `with`, not as a context manager — copy that shape exactly.
> Note the world sets org 1's `crm_provider = "delphi"`, so `demand_feed`
> reads `done` for org 1 there; this test uses `payroll`, which is unaffected.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checklist_tenancy.py -v`
Expected: it should PASS immediately — the walls do this work. If it FAILS,
that is a real isolation bug and takes priority over everything below.

- [ ] **Step 3: No implementation needed**

This test pins behaviour the L2 walls already provide. Its value is regression
protection: a future refactor that reads overrides on an un-instrumented
session would break it.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_checklist_tenancy.py
git commit -m "test(checklist): pin that a dismissal cannot cross the org wall"
```

---

## Task 8: Full gates and the OpenAPI contract

**Files:**
- Modify: whatever the gates flag

- [ ] **Step 1: Run every gate**

```bash
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

Expected: all three clean. `mypy` is the likely complainer — `ItemModel(**vars(row))`
converts a dataclass to a pydantic model by keyword; if it objects, write the
seven fields out explicitly rather than loosening the type.

There is no generated OpenAPI client to refresh: `frontend/src/generated/`
holds only `releaseNotes.ts` and `package.json` has no generate script. The
frontend plan hand-writes its types, as the other pages do.

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "chore(checklist): gates green"
```

---

## Definition of done

- `GET /api/checklist` returns seven items with derived statuses, an
  `open_count`, and `all_clear`.
- `PUT`/`DELETE` on `/{key}/dismissal` are idempotent, org-admin gated, refuse
  a required key with 422 and an unknown key with 404.
- A dismissal loses to a probe that says `done`.
- A raising probe degrades only its own item, never to `done`.
- `org_checklist_override` sits behind the `org_wall` policy and a dismissal
  cannot cross orgs.
- All three gates pass.

## Follow-on

`docs/plans/2026-08-30-track-b-b4-checklist-frontend.md` — the `/setup` page,
the dashboard card that retires at `all_clear`, and the sidebar badge. Written
after this lands.
