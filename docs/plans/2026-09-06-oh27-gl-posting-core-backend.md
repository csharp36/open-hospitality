# OH-27 GL Posting Core (backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the existing balanced journal-entry plans a home: five OrgScoped GL tables, a posting engine with a two-source registry, audited period close, a trial balance, the QBO push re-pointed at the journal, and the CI parity gate for the later SOS cutover.

**Architecture:** The JE builder moves from `qbo_push.py` into a new `gl_posting.py` that reads the chart from a per-org `gl_account` table (system accounts found by `system_role`, not number), posts through an idempotency ledger with reverse-and-repost in open periods, and refuses closed periods loudly. Balance and immutability are enforced in Postgres (the repo's first trigger; REVOKE per the `l2a0rlswall` idiom). `qbo_push` shrinks to exporting posted entries.

**Tech Stack:** SQLAlchemy 2 + Alembic + Postgres RLS (backend), FastAPI + pydantic, Typer CLI, pytest with the `two_tenant_world` / `app_role_engine` fixtures.

**Design doc:** `docs/design/2026-09-06-oh27-gl-posting-core-design.md` (D-OH27.1–12).
**ADR:** `docs/adr/adr-011-gl-posting-model.md` (accepted).

**Branch:** `feat/oh27-gl-posting-core` — create from `main`; the branch's first commit flips `OH-27` to `in-progress` in `.github/roadmap.yml` (§10 of the design).

**Frontend:** NOT in this plan. The `/gl` page gets its own plan after these API shapes survive execution (the OH-17 backend/frontend split precedent).

---

## Plan-level decisions (executor context)

Two calls made while planning, consistent with the design but not spelled out there:

- **GL is off until the chart is seeded.** `post_and_record` returns `skipped`
  (no ledger row) when the org has no `gl_account` rows. New orgs get the
  chart at provisioning (Task 5); existing orgs opt in via
  `usali gl-seed-chart`. This is the D8.3 posture — an unconfigured module is
  *off*, never half-on — and it keeps the ingestion hook (Task 11) from
  spraying `failed` rows through every existing pipeline test.
- **`payroll_accrual` posts daily labor-fact estimates**, not pay-run actuals.
  `UsaliLaborFact.est_cost` is the estimate written on timecard approval
  (`labor.py`); `PayRun` carries actuals. The source aggregates `est_cost`
  per department per (property, business_date) into lines against ONE
  Schedule-14 expense account found by role (`labor_wages_expense`), credited
  to `accrued_payroll` — per-department GL accounts and actual-vs-estimate
  truing-up are explicitly later work (design §9).

## The five hand-maintained literals this plan must touch

Verified against current code; each task names its edits, this is the map:

| Literal | Where | Trips when |
|---|---|---|
| `test_tables_registered` exact set | `tests/test_models.py:12-71` | any new model exists |
| RLS inventory `expected` set | `tests/test_l2_rls_wall.py:450-454` (asserted `:482`) | any new tenant table has a policy |
| Alembic head literal | `tests/test_l4_org_grants.py:352-358` (`== ["b3a0integcred"]`) | any new migration |
| `org_indexes()` exact set | `tests/test_l1_org_wall_migration.py:171-186` | a table uses the mixin's plain `org_id` (all four journal-side tables here; `gl_account`'s composite PK does not) |
| Predicate cross-pin | `tests/test_l2_rls_wall.py:523-532` (`_m2._PREDICATE == _l2._PREDICATE` etc.) | the new migration declares its own `_PREDICATE` (it must, verbatim) |

`_L1_ORG_INDEPENDENT` (`tests/test_migration_on_populated_data.py:1291-1294`)
gets **no** edit — the new tables are all org-scoped.

## File Structure

**Created**

- `mapping/gl_accounts_usali.yaml` — the USALI chart template (D-OH27.1).
- `src/usali/gl_chart.py` — template loading and idempotent per-org seeding.
- `src/usali/gl_posting.py` — the engine: source registry, plan builders,
  idempotency ledger, period state/close/reopen, typed refusals.
- `src/usali/gl_api.py` — `/api/gl` router.
- `migrations/versions/g1a0glcore_gl_posting_core.py` — five tables, RLS,
  the balance trigger, the immutability REVOKEs.
- `tests/test_gl_models.py`, `tests/test_gl_wall.py`, `tests/test_gl_chart.py`,
  `tests/test_gl_posting.py`, `tests/test_gl_periods.py`,
  `tests/test_gl_reporting.py`, `tests/test_gl_api.py`,
  `tests/test_gl_parity.py`.

**Modified**

- `src/usali/models.py` — five new models.
- `src/usali/fiscal.py` — `config_for(session, property_id)` (the one DB
  helper; `property_config_api._fiscal_config` becomes its caller).
- `src/usali/property_config_api.py` — use `fiscal.config_for`.
- `src/usali/qbo_push.py` — builder moves out; `push_day` reads the journal.
- `src/usali/ingestion.py` — the posting hook in `_process_section`.
- `src/usali/labor.py` + `src/usali/timecard_api.py` — `promote_timecard`
  returns the (property, date) set it wrote; approval posts `payroll_accrual`.
- `src/usali/signup_api.py` (or wherever `provision_tenant` lives — locate
  with `grep -rn "def provision_tenant" src/`) — chart seeding at provisioning.
- `src/usali/cli.py` — `gl-seed-chart`, `gl-post`.
- `src/usali/server.py` — mount `gl_api.router` under `operator_gates`.
- `tests/test_models.py`, `tests/test_l2_rls_wall.py`,
  `tests/test_l4_org_grants.py`, `tests/test_l1_org_wall_migration.py` — the
  literals above.
- `.github/roadmap.yml` — OH-27 → `in-progress` (first commit).

---

### Task 0: Branch and roadmap status

- [ ] **Step 1: Branch**

```bash
git checkout main && git pull --ff-only && git checkout -b feat/oh27-gl-posting-core
```

- [ ] **Step 2: Flip OH-27 to in-progress**

In `.github/roadmap.yml`, on the `OH-27` entry only, change `status: planned`
to `status: in-progress`.

- [ ] **Step 3: Commit**

```bash
git add .github/roadmap.yml
git commit -m "docs(oh27): the posting core is under way"
```

---

### Task 1: The five models

**Files:**
- Modify: `src/usali/models.py` (append after `QboPushLedger`, ~line 315)
- Modify: `tests/test_models.py:12-71`
- Test: `tests/test_gl_models.py`

- [ ] **Step 1: Write the failing registry test**

`tests/test_models.py`'s `test_tables_registered` is the tripwire; run it
first to see it green, then add the five names to its literal set:

```python
        "gl_account",
        "journal_entry",
        "journal_line",
        "gl_posting_ledger",
        "gl_period_event",
```

Run: `pytest tests/test_models.py::test_tables_registered -v`
Expected: FAIL — the metadata does not yet contain the five names.

- [ ] **Step 2: Write the shape tests**

Create `tests/test_gl_models.py`:

```python
"""Shape pins for the GL tables (design D-OH27.1..3, D-OH27.12).

These are deliberate literals: the migration mirrors these shapes by hand
(the b3a0integcred convention), so a drifting model shows up here first.
"""

from usali import models


def test_gl_account_is_keyed_by_org_and_code():
    pk = [c.name for c in models.GlAccount.__table__.primary_key.columns]
    assert pk == ["org_id", "account_code"]


def test_gl_account_types_are_the_closed_set():
    ck = next(
        c for c in models.GlAccount.__table__.constraints
        if c.name == "ck_gl_account_type"
    )
    for t in ("asset", "contra_asset", "liability", "equity", "income", "expense"):
        assert t in ck.sqltext.text


def test_journal_line_posting_is_debit_or_credit_and_positive():
    names = {c.name for c in models.JournalLine.__table__.constraints}
    assert "ck_journal_line_posting" in names
    assert "ck_journal_line_amount_positive" in names


def test_journal_entry_supports_composite_children():
    names = {c.name for c in models.JournalEntry.__table__.constraints}
    assert "uq_journal_entry_org" in names  # the (org_id, entry_id) target


def test_posting_ledger_grain_is_property_date_source():
    uq = next(
        c for c in models.GlPostingLedger.__table__.constraints
        if c.name == "uq_gl_posting_ledger_org_row"
    )
    assert [c.name for c in uq.columns] == [
        "org_id", "property_id", "business_date", "source_type"
    ]
```

Run: `pytest tests/test_gl_models.py -v`
Expected: FAIL — `AttributeError: module 'usali.models' has no attribute 'GlAccount'`

- [ ] **Step 3: Add the models**

Append to `src/usali/models.py` (after `QboPushLedger`; `Index` joins the
existing `sqlalchemy` import list if not present):

```python
class GlAccount(OrgScoped, Base):
    """One chart-of-accounts row per org (ADR-011 §1). Seeded from
    mapping/gl_accounts_usali.yaml at provisioning; orgs extend by adding
    rows. `system_role` is how the engine finds structural accounts
    (D-OH27.2) — unique per org where set, so renumbering is free and
    deleting a role-bearing account is refused in gl_api, the enforcement
    point for that rule."""

    __tablename__ = "gl_account"
    __table_args__ = (
        CheckConstraint(
            "account_type IN ('asset', 'contra_asset', 'liability', "
            "'equity', 'income', 'expense')",
            name="ck_gl_account_type",
        ),
        Index(
            "uq_gl_account_org_role",
            "org_id",
            "system_role",
            unique=True,
            postgresql_where=text("system_role IS NOT NULL"),
        ),
    )

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organization.org_id", name="fk_gl_account_org"),
        primary_key=True,
    )
    account_code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    account_type: Mapped[str] = mapped_column(String(20))
    usali_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usali_major_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    usali_sub_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    usali_line_item: Mapped[str | None] = mapped_column(String(100), nullable=True)
    system_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JournalEntry(OrgScoped, Base):
    """One posting event (ADR-011 §2, §3). Append-only: the g1a0glcore
    migration REVOKEs UPDATE and DELETE from the application role, so
    correction is a reversal entry (`reversal_of`), never an edit."""

    __tablename__ = "journal_entry"
    __table_args__ = (
        UniqueConstraint("org_id", "entry_id", name="uq_journal_entry_org"),
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_journal_entry_property_org",
        ),
    )

    entry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    property_id: Mapped[str] = mapped_column(String(50))
    business_date: Mapped[date] = mapped_column(Date)
    source_type: Mapped[str] = mapped_column(String(30))
    source_hash: Mapped[str] = mapped_column(String(64))
    memo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reversal_of: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entry.entry_id", name="fk_journal_entry_reversal"),
        nullable=True,
    )
    posted_by: Mapped[str] = mapped_column(String(64))
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class JournalLine(OrgScoped, Base):
    """One side of one entry. Direction lives in `posting`, never in the
    sign — amounts are strictly positive (the JeLine convention, which is
    also what the QBO export body requires). `fact_id` is the drill-through
    link for the pms_daily source; payroll_accrual lines carry NULL and
    name their department in `memo` (design D-OH27.3)."""

    __tablename__ = "journal_line"
    __table_args__ = (
        CheckConstraint(
            "posting IN ('Debit', 'Credit')", name="ck_journal_line_posting"
        ),
        CheckConstraint("amount > 0", name="ck_journal_line_amount_positive"),
        ForeignKeyConstraint(
            ["org_id", "entry_id"],
            ["journal_entry.org_id", "journal_entry.entry_id"],
            name="fk_journal_line_entry_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "account_code"],
            ["gl_account.org_id", "gl_account.account_code"],
            name="fk_journal_line_account_org",
        ),
    )

    line_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(BigInteger)
    account_code: Mapped[str] = mapped_column(String(20))
    posting: Mapped[str] = mapped_column(String(6))
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 4))
    memo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("usali_financial_fact.fact_id", name="fk_journal_line_fact"),
        nullable=True,
    )


class GlPostingLedger(OrgScoped, Base):
    """One row per (property, business date, source) posting outcome — the
    QboPushLedger precedent (D-OH27.6). `entry_id` is the CURRENT entry for
    that grain; `source_hash` is its content hash, so an unchanged re-run is
    a no-op and a changed one reverses-and-reposts. `message` carries the
    latest refusal and clears on success; `status` says whether a current
    entry exists. The unique constraint is the concurrency arbiter: the
    loser of a simultaneous post raises IntegrityError, loudly."""

    __tablename__ = "gl_posting_ledger"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "property_id", "business_date", "source_type",
            name="uq_gl_posting_ledger_org_row",
        ),
        CheckConstraint(
            "status IN ('posted', 'failed')", name="ck_gl_posting_ledger_status"
        ),
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_gl_posting_ledger_property_org",
        ),
        ForeignKeyConstraint(
            ["org_id", "entry_id"],
            ["journal_entry.org_id", "journal_entry.entry_id"],
            name="fk_gl_posting_ledger_entry_org",
        ),
    )

    posting_ledger_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    property_id: Mapped[str] = mapped_column(String(50))
    business_date: Mapped[date] = mapped_column(Date)
    source_type: Mapped[str] = mapped_column(String(30))
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(10))
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GlPeriodEvent(OrgScoped, Base):
    """Close/reopen events per (property, fiscal period) — append-only like
    the journal; a period's state is the LAST event (D-OH27.7, the D-B4.1
    derived-not-stored rule). This table IS the audit record for close:
    actor, reason, timestamp live here, not in a parallel AuditEvent."""

    __tablename__ = "gl_period_event"
    __table_args__ = (
        CheckConstraint("event IN ('close', 'reopen')", name="ck_gl_period_event_kind"),
        ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_gl_period_event_property_org",
        ),
    )

    period_event_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    property_id: Mapped[str] = mapped_column(String(50))
    period_key: Mapped[str] = mapped_column(String(10))
    event: Mapped[str] = mapped_column(String(10))
    actor_subject: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

Note `GlAccount` hand-declares `org_id` in its composite PK (the
`OrgIntegrationCredential` shape, `models.py:536-539`); the other four take
the mixin's plain column, which is what obligates their `ix_<table>_org_id`
entries in Task 2. Add `Decimal` to the module's imports
(`from decimal import Decimal`) if absent, and `Index` / `text` to the
sqlalchemy imports if absent.

- [ ] **Step 4: Run both test files**

Run: `pytest tests/test_gl_models.py tests/test_models.py::test_tables_registered -v`
Expected: PASS (the models exist; no DB yet needed for either).

- [ ] **Step 5: Commit**

```bash
git add src/usali/models.py tests/test_models.py tests/test_gl_models.py
git commit -m "feat(oh27): the five GL tables as models"
```

---

### Task 2: The migration — tables, RLS, the balance trigger, the immutability REVOKEs

**Files:**
- Create: `migrations/versions/g1a0glcore_gl_posting_core.py`
- Modify: `tests/test_l4_org_grants.py:352-358` (head literal)
- Modify: `tests/test_l2_rls_wall.py:450-454` (inventory) and `:523-532` (predicate cross-pin)
- Modify: `tests/test_l1_org_wall_migration.py:171-186` (`org_indexes()` set)

- [ ] **Step 1: Update the four literals and watch them fail**

- `tests/test_l4_org_grants.py:352-358`: the head literal becomes
  `["g1a0glcore"]`.
- `tests/test_l2_rls_wall.py` `expected` set: add the five table names.
- `tests/test_l2_rls_wall.py:523-532`: add
  `assert _g1._PREDICATE == _l2._PREDICATE` with the import alias the
  neighbors use (`from migrations.versions import g1a0glcore_gl_posting_core as _g1`
  — match the exact import style `_m1`/`_m2` use at the top of that test file).
- `tests/test_l1_org_wall_migration.py` `org_indexes()` set: add
  `"ix_journal_entry_org_id"`, `"ix_journal_line_org_id"`,
  `"ix_gl_posting_ledger_org_id"`, `"ix_gl_period_event_org_id"`
  (NOT `gl_account` — composite PK, per the comment at `:175-178`).

Run: `pytest tests/test_l4_org_grants.py::test_l4_is_the_single_alembic_head -v`
Expected: FAIL — head is still `b3a0integcred`.

- [ ] **Step 2: Write the migration**

Create `migrations/versions/g1a0glcore_gl_posting_core.py`. Follow
`b3a0integcred`'s conventions exactly: models are NOT imported; shapes are
mirrored by hand; `_PREDICATE` is a module-local literal built on
`RLS_ORG_VAR`; RLS DDL and grants go through `op.execute`.

```python
"""OH-27 G1: the GL posting core tables (design D-OH27.1..7, D-OH27.12).

Five org-scoped tables — gl_account, journal_entry, journal_line,
gl_posting_ledger, gl_period_event — each ENABLE + FORCE RLS with the
org_wall policy, predicate byte-identical to l2a0rlswall's (pinned by
tests/test_l2_rls_wall.py's cross-pin test).

Two firsts for this chain, both ADR-011 enforcement points:

- ck_journal_entry_balanced: a DEFERRABLE INITIALLY DEFERRED constraint
  trigger on journal_line — at commit, every touched entry's debits must
  equal its credits exactly. The builder checks first with a friendlier
  error; this trigger is the wall, and it firing at all is a bug report.
- REVOKE UPDATE, DELETE on journal_entry, journal_line, gl_period_event
  from the app role (the l2a0rlswall REVOKE idiom): append-only is a grant,
  not a convention. gl_posting_ledger keeps UPDATE — it points at the
  current entry and is MEANT to move.
"""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import APP_DB_ROLE, RLS_ORG_VAR

revision = "g1a0glcore"
down_revision = "b3a0integcred"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"

_TABLES = (
    "gl_account",
    "journal_entry",
    "journal_line",
    "gl_posting_ledger",
    "gl_period_event",
)

_IMMUTABLE = ("journal_entry", "journal_line", "gl_period_event")


def _wall(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def upgrade() -> None:
    op.create_table(
        "gl_account",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_gl_account_org"),
            primary_key=True,
        ),
        sa.Column("account_code", sa.String(length=20), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("account_type", sa.String(length=20), nullable=False),
        sa.Column("usali_schedule_id", sa.Integer(), nullable=True),
        sa.Column("usali_major_category", sa.String(length=100), nullable=True),
        sa.Column("usali_sub_category", sa.String(length=100), nullable=True),
        sa.Column("usali_line_item", sa.String(length=100), nullable=True),
        sa.Column("system_role", sa.String(length=40), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "account_type IN ('asset', 'contra_asset', 'liability', "
            "'equity', 'income', 'expense')",
            name="ck_gl_account_type",
        ),
    )
    op.create_index(
        "uq_gl_account_org_role",
        "gl_account",
        ["org_id", "system_role"],
        unique=True,
        postgresql_where=sa.text("system_role IS NOT NULL"),
    )

    op.create_table(
        "journal_entry",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_journal_entry_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("entry_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("memo", sa.String(length=255), nullable=True),
        sa.Column(
            "reversal_of",
            sa.BigInteger(),
            sa.ForeignKey("journal_entry.entry_id", name="fk_journal_entry_reversal"),
            nullable=True,
        ),
        sa.Column("posted_by", sa.String(length=64), nullable=False),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "entry_id", name="uq_journal_entry_org"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_journal_entry_property_org",
        ),
    )
    op.create_index("ix_journal_entry_org_id", "journal_entry", ["org_id"])

    op.create_table(
        "journal_line",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_journal_line_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("line_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("account_code", sa.String(length=20), nullable=False),
        sa.Column("posting", sa.String(length=6), nullable=False),
        sa.Column("amount", sa.Numeric(15, 4), nullable=False),
        sa.Column("memo", sa.String(length=255), nullable=True),
        sa.Column(
            "fact_id",
            sa.BigInteger(),
            sa.ForeignKey("usali_financial_fact.fact_id", name="fk_journal_line_fact"),
            nullable=True,
        ),
        sa.CheckConstraint("posting IN ('Debit', 'Credit')", name="ck_journal_line_posting"),
        sa.CheckConstraint("amount > 0", name="ck_journal_line_amount_positive"),
        sa.ForeignKeyConstraint(
            ["org_id", "entry_id"],
            ["journal_entry.org_id", "journal_entry.entry_id"],
            name="fk_journal_line_entry_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "account_code"],
            ["gl_account.org_id", "gl_account.account_code"],
            name="fk_journal_line_account_org",
        ),
    )
    op.create_index("ix_journal_line_org_id", "journal_line", ["org_id"])

    op.create_table(
        "gl_posting_ledger",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_gl_posting_ledger_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "posting_ledger_id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "property_id", "business_date", "source_type",
            name="uq_gl_posting_ledger_org_row",
        ),
        sa.CheckConstraint(
            "status IN ('posted', 'failed')", name="ck_gl_posting_ledger_status"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_gl_posting_ledger_property_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "entry_id"],
            ["journal_entry.org_id", "journal_entry.entry_id"],
            name="fk_gl_posting_ledger_entry_org",
        ),
    )
    op.create_index("ix_gl_posting_ledger_org_id", "gl_posting_ledger", ["org_id"])

    op.create_table(
        "gl_period_event",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_gl_period_event_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "period_event_id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("period_key", sa.String(length=10), nullable=False),
        sa.Column("event", sa.String(length=10), nullable=False),
        sa.Column("actor_subject", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("event IN ('close', 'reopen')", name="ck_gl_period_event_kind"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_gl_period_event_property_org",
        ),
    )
    op.create_index("ix_gl_period_event_org_id", "gl_period_event", ["org_id"])

    for table in _TABLES:
        _wall(table)

    # The balance wall (ADR-011 §4): checked at COMMIT so multi-line entries
    # can be inserted line by line inside one transaction.
    op.execute(
        """
        CREATE FUNCTION gl_entry_balance_check() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE imbalance numeric;
        BEGIN
            SELECT COALESCE(
                SUM(CASE WHEN posting = 'Credit' THEN amount ELSE -amount END), 0
            )
            INTO imbalance FROM journal_line WHERE entry_id = NEW.entry_id;
            IF imbalance <> 0 THEN
                RAISE EXCEPTION
                    'journal entry % is out of balance by %', NEW.entry_id, imbalance;
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ck_journal_entry_balanced
        AFTER INSERT ON journal_line
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION gl_entry_balance_check()
        """
    )

    # Append-only is a grant, not a convention (ADR-011 §2).
    for table in _IMMUTABLE:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {APP_DB_ROLE}")


def downgrade() -> None:
    for table in _IMMUTABLE:
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {APP_DB_ROLE}")
    op.execute("DROP TRIGGER ck_journal_entry_balanced ON journal_line")
    op.execute("DROP FUNCTION gl_entry_balance_check()")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY {_POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.drop_table(table)
```

Before writing, confirm `APP_DB_ROLE` is importable from `usali.tenancy`
(`grep -n "APP_DB_ROLE" src/usali/tenancy.py`); if the symbol lives only in
`l2a0rlswall`, import it the way that migration defines it and mirror here.

- [ ] **Step 3: Run the migration-sensitive suites**

Run: `pytest tests/test_l4_org_grants.py tests/test_l2_rls_wall.py tests/test_l1_org_wall_migration.py tests/test_models.py tests/test_migration_on_populated_data.py -q`
Expected: PASS. (`conftest.py`'s `db_url` fixture runs `alembic upgrade head`
against a fresh Postgres container, so the migration executes on every test
session.)

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/g1a0glcore_gl_posting_core.py tests/test_l4_org_grants.py tests/test_l2_rls_wall.py tests/test_l1_org_wall_migration.py
git commit -m "feat(oh27): the g1a0glcore migration — five walls, one trigger, three revokes"
```

---

### Task 3: The wall tests — isolation, immutability, balance

**Files:**
- Test: `tests/test_gl_wall.py`

These pin the three Postgres-level guarantees. Pattern-match
`tests/test_integrations.py:519-657` for the fixtures and helpers.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gl_wall.py`:

```python
"""The GL's three database walls (ADR-011 §2, §4; design D-OH27.4, .12).

Isolation: the org_wall policy on all five tables, proven through the app
role with raw SQL (the DB wall alone). Immutability: UPDATE/DELETE on the
journal is refused BY GRANT for the app role. Balance: an unbalanced entry
is refused at COMMIT by the deferred constraint trigger.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from usali.models import GlAccount, JournalEntry, JournalLine
from usali.tenancy import FOUNDING_ORG_ID, OrgBoundSessionFactory
from sqlalchemy.orm import sessionmaker


def _factory(engine, org_id):
    return OrgBoundSessionFactory(sessionmaker(bind=engine), org_id)


def _seed_minimal_books(factory, property_id):
    """Two accounts and one balanced 100.00 entry for the given org."""
    with factory() as s:
        s.add(GlAccount(account_code="4000", name="Room Revenue",
                        account_type="income", is_active=True))
        s.add(GlAccount(account_code="1210", name="Guest Ledger Clearing",
                        account_type="asset", is_active=True,
                        system_role="guest_ledger_clearing"))
        s.flush()
        entry = JournalEntry(property_id=property_id,
                             business_date=date(2026, 7, 7),
                             source_type="test", source_hash="x" * 64,
                             posted_by="test")
        s.add(entry)
        s.flush()
        s.add(JournalLine(entry_id=entry.entry_id, account_code="4000",
                          posting="Credit", amount=Decimal("100.0000")))
        s.add(JournalLine(entry_id=entry.entry_id, account_code="1210",
                          posting="Debit", amount=Decimal("100.0000")))
        s.commit()
        return entry.entry_id


def test_one_org_cannot_read_anothers_journal(app_role_engine, two_tenant_world):
    org_a = _factory(app_role_engine, FOUNDING_ORG_ID)
    _seed_minimal_books(org_a, two_tenant_world.org1_property_id
                        if hasattr(two_tenant_world, "org1_property_id")
                        else "HOTEL001")
    org_b = _factory(app_role_engine, two_tenant_world.org2_id)
    with org_b() as s:
        for table in ("gl_account", "journal_entry", "journal_line"):
            n = s.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            assert n == 0, f"org 2 can see org 1 rows in {table}"


def test_the_journal_refuses_update_and_delete_by_grant(
    app_role_engine, founding_org
):
    org_a = _factory(app_role_engine, FOUNDING_ORG_ID)
    entry_id = _seed_minimal_books(org_a, "HOTEL001")
    with org_a() as s:
        with pytest.raises(Exception, match="permission denied"):
            s.execute(text(
                "UPDATE journal_entry SET memo = 'tampered' WHERE entry_id = :e"
            ), {"e": entry_id})
            s.commit()
    with org_a() as s:
        with pytest.raises(Exception, match="permission denied"):
            s.execute(text("DELETE FROM journal_line WHERE entry_id = :e"),
                      {"e": entry_id})
            s.commit()


def test_an_unbalanced_entry_is_refused_at_commit(app_role_engine, founding_org):
    org_a = _factory(app_role_engine, FOUNDING_ORG_ID)
    _seed_minimal_books(org_a, "HOTEL001")
    with org_a() as s:
        entry = JournalEntry(property_id="HOTEL001",
                             business_date=date(2026, 7, 8),
                             source_type="test", source_hash="y" * 64,
                             posted_by="test")
        s.add(entry)
        s.flush()
        s.add(JournalLine(entry_id=entry.entry_id, account_code="4000",
                          posting="Credit", amount=Decimal("100.0000")))
        # No balancing debit: the deferred trigger must refuse at COMMIT.
        with pytest.raises(Exception, match="out of balance"):
            s.commit()
```

Executor notes, to be resolved against the code (not left in the file):
`two_tenant_world`'s attribute names come from `tests/orgworld.py:91-131` —
use its real property/org fields, and use the founding org's seeded property
id the way `tests/test_skytouch_end_to_end.py` obtains one, rather than the
`"HOTEL001"` stand-in, if property FKs make the stand-in fail. The
`permission denied` and `out of balance` match-strings are Postgres's own.

Run: `pytest tests/test_gl_wall.py -v`
Expected: PASS if Tasks 1–2 are correct — write these first and treat any
failure as a Task 1/2 defect, not a test to loosen. (These tests are
verification of already-built walls, so unlike the feature tasks they go
green immediately; their value is that they fail when someone later weakens
a grant, drops the trigger, or forgets a policy.)

- [ ] **Step 2: Commit**

```bash
git add tests/test_gl_wall.py
git commit -m "test(oh27): the journal's three database walls, pinned"
```

---

### Task 4: The chart template and `gl_chart.py`

**Files:**
- Create: `mapping/gl_accounts_usali.yaml`
- Create: `src/usali/gl_chart.py`
- Test: `tests/test_gl_chart.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gl_chart.py`:

```python
"""Chart template and per-org seeding (design D-OH27.1, .2)."""

from usali import gl_chart
from usali.models import GlAccount


REQUIRED_ROLES = {"guest_ledger_clearing", "accrued_payroll", "labor_wages_expense"}


def test_the_template_carries_every_engine_role():
    accounts = gl_chart.load_template()
    roles = {a.system_role for a in accounts if a.system_role}
    assert REQUIRED_ROLES <= roles


def test_the_template_is_a_superset_of_the_qbo_chart():
    """Every code the QBO push has ever emitted must exist in the new chart,
    or re-pointing the push (Task 10) would orphan existing QBO books."""
    import yaml
    legacy = {str(e["account_code"]) for e in
              yaml.safe_load(open("mapping/qbo_accounts.yaml"))}
    codes = {a.account_code for a in gl_chart.load_template()}
    assert legacy <= codes


def test_seeding_is_idempotent_and_org_scoped(db_session, founding_org):
    first = gl_chart.seed_chart(db_session)
    again = gl_chart.seed_chart(db_session)
    assert first > 0 and again == 0  # second run inserts nothing
    row = db_session.get(GlAccount, (1, "4000"))
    assert row is not None and row.account_type == "income"


def test_seeding_does_not_resurrect_an_edited_account(db_session, founding_org):
    """The OH-17 seed lesson: a re-run must never overwrite operator edits."""
    gl_chart.seed_chart(db_session)
    row = db_session.get(GlAccount, (1, "4000"))
    row.name = "Rooms Revenue (renamed)"
    db_session.flush()
    gl_chart.seed_chart(db_session)
    assert db_session.get(GlAccount, (1, "4000")).name == "Rooms Revenue (renamed)"
```

Run: `pytest tests/test_gl_chart.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usali.gl_chart'`

- [ ] **Step 2: Write the template**

Create `mapping/gl_accounts_usali.yaml` — the successor to
`mapping/qbo_accounts.yaml` (whose header says it was always a placeholder
pending a real chart). Start from the legacy nine codes so the superset test
holds, add the two payroll roles and the USALI linkage the parity gate
(Task 12) joins on:

```yaml
# USALI (11th ed.) chart-of-accounts template — seeded per org by
# usali.gl_chart.seed_chart at provisioning and via `usali gl-seed-chart`.
#
# system_role is how the posting engine finds structural accounts
# (design D-OH27.2): renumber freely, the roles travel with the rows.
# usali_* fields are the join keys the SOS parity gate uses; income and
# expense accounts should carry them, clearing/liability accounts do not.
#
# This template is a starting chart, not a mandate: orgs extend and rename
# their own copies after seeding. Codes 1000-6000 are carried from
# mapping/qbo_accounts.yaml so books already pushed to QBO keep their codes.
- account_code: "1000"
  name: "Cash Clearing"
  account_type: asset
- account_code: "1100"
  name: "Credit Card Clearing"
  account_type: asset
- account_code: "1200"
  name: "Accounts Receivable Clearing"
  account_type: asset
- account_code: "1210"
  name: "Guest Ledger Clearing"
  account_type: asset
  system_role: guest_ledger_clearing
- account_code: "2100"
  name: "Sales & Occupancy Tax Payable"
  account_type: liability
- account_code: "2200"
  name: "Accrued Payroll"
  account_type: liability
  system_role: accrued_payroll
- account_code: "4000"
  name: "Room Revenue"
  account_type: income
  usali_schedule_id: 1
  usali_major_category: "Rooms"
- account_code: "4100"
  name: "Other Operated Departments Revenue"
  account_type: income
  usali_schedule_id: 2
  usali_major_category: "Other Operated Departments"
- account_code: "4200"
  name: "Miscellaneous Income"
  account_type: income
  usali_schedule_id: 4
  usali_major_category: "Miscellaneous Income"
- account_code: "5000"
  name: "Rooms Labor — Wages"
  account_type: expense
  usali_schedule_id: 14
  usali_major_category: "Labor"
  system_role: labor_wages_expense
- account_code: "6000"
  name: "Write-Offs & Bad Debt"
  account_type: expense
```

Before finalizing, diff the `usali_major_category` strings against the values
actually present in `mapping/opera.yaml` / `mapping/skytouch.yaml` and the
seeded `UsaliMappingDictionary` rows (`grep -n "major" mapping/opera.yaml | head`)
— the parity join in Task 12 is only as good as these strings, and inventing
them here instead of copying them is exactly the drift the mapping YAMLs
exist to prevent.

- [ ] **Step 3: Write `gl_chart.py`**

```python
"""Chart-of-accounts template loading and per-org seeding (D-OH27.1).

Seeding is insert-only and keyed on absence per (org, account_code): a
re-run inserts template rows the org lacks and NEVER updates a row that
exists — an operator's rename or deactivation survives every later seed
(the OH-17 env-seed lesson, applied from day one).
"""

from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import GlAccount
from usali.tenancy import current_org_id

_TEMPLATE = Path(__file__).resolve().parents[2] / "mapping" / "gl_accounts_usali.yaml"


@dataclass(frozen=True)
class TemplateAccount:
    account_code: str
    name: str
    account_type: str
    usali_schedule_id: int | None = None
    usali_major_category: str | None = None
    usali_sub_category: str | None = None
    usali_line_item: str | None = None
    system_role: str | None = None


def load_template(path: Path = _TEMPLATE) -> list[TemplateAccount]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be a YAML list of accounts")
    return [TemplateAccount(**{**e, "account_code": str(e["account_code"])}) for e in raw]


def seed_chart(session: Session, path: Path = _TEMPLATE) -> int:
    """Insert template accounts the active org lacks; return the count."""
    org_id = current_org_id(session)
    have = set(
        session.scalars(select(GlAccount.account_code)).all()
    )
    inserted = 0
    for acct in load_template(path):
        if acct.account_code in have:
            continue
        session.add(
            GlAccount(
                org_id=org_id,
                account_code=acct.account_code,
                name=acct.name,
                account_type=acct.account_type,
                usali_schedule_id=acct.usali_schedule_id,
                usali_major_category=acct.usali_major_category,
                usali_sub_category=acct.usali_sub_category,
                usali_line_item=acct.usali_line_item,
                system_role=acct.system_role,
                is_active=True,
            )
        )
        inserted += 1
    session.flush()
    return inserted
```

If `current_org_id(session)` (`src/usali/tenancy.py:128-137`) refuses on the
superuser `db_session` fixture, follow how `tests/test_gl_chart.py`'s
neighbors obtain an org-stamped session (`founding_org` + the ORM wall
stamps `org_id` on add) and let the wall supply `org_id` instead of passing
it — match whichever shape `_connect_bound` in `tests/test_integrations.py:532`
proves works.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_gl_chart.py -v`
Expected: PASS

- [ ] **Step 5: Wire provisioning**

Locate the tenant provisioner: `grep -rn "def provision_tenant" src/`. Inside
it, after the org and founding property exist and inside the same
transaction, add:

```python
from usali import gl_chart
gl_chart.seed_chart(session)
```

with a one-line comment: `# D-OH27.1: every new org starts with the USALI chart.`
Then extend whatever test pins `provision_tenant`'s effects
(`grep -rln "provision_tenant" tests/`) with an assertion that a freshly
provisioned org has `gl_account` rows.

- [ ] **Step 6: Run the provisioning suite and commit**

Run: `pytest tests/test_gl_chart.py $(grep -rln "provision_tenant" tests/) -q`
Expected: PASS

```bash
git add mapping/gl_accounts_usali.yaml src/usali/gl_chart.py tests/test_gl_chart.py src/usali/signup_api.py
git commit -m "feat(oh27): the USALI chart template, seeded per org, insert-only"
```

(Adjust the `git add` if `provision_tenant` lives elsewhere.)

---

### Task 5: `fiscal.config_for` — one loader for the calendar

**Files:**
- Modify: `src/usali/fiscal.py`
- Modify: `src/usali/property_config_api.py:82-90`
- Test: extend `tests/test_gl_periods.py` later; here, the existing fiscal suites

- [ ] **Step 1: Add the loader**

`fiscal.py`'s docstring says "pure functions"; the engine needs the one
impure helper, and duplicating `property_config_api._fiscal_config` into
`gl_posting.py` would be a second copy of the row→config mapping. Move it.
Append to `src/usali/fiscal.py`:

```python
def config_for(session, property_id: str) -> "FiscalConfig | None":
    """Load a property's FiscalCalendar row as a FiscalConfig, or None.

    The one impure function in this module, so every caller (property
    config API, the GL posting engine) shares a single row->config mapping.
    Pair with require_config() for the loud-refusal shape.
    """
    from usali.models import FiscalCalendar  # local: keep module import-light

    row = session.get(FiscalCalendar, property_id)
    if row is None:
        return None
    return FiscalConfig(
        calendar_type=row.calendar_type,
        fiscal_year_start_month=row.fiscal_year_start_month,
        week_start_weekday=row.week_start_weekday,
    )
```

- [ ] **Step 2: Re-point `property_config_api`**

Replace the body of `_fiscal_config` (`property_config_api.py:82-90`) with a
delegation:

```python
def _fiscal_config(session: Session, property_id: str) -> FiscalConfig | None:
    return fiscal.config_for(session, property_id)
```

(or delete `_fiscal_config` and update its two call sites to
`fiscal.config_for` directly — prefer the deletion if the module already
imports `fiscal`).

- [ ] **Step 3: Run the fiscal and property-config suites**

Run: `pytest tests/test_fiscal.py tests/test_property_config_api.py -q 2>/dev/null || pytest $(grep -rln "fiscal" tests/ | tr '\n' ' ') -q`
Expected: PASS, unchanged behavior.

- [ ] **Step 4: Commit**

```bash
git add src/usali/fiscal.py src/usali/property_config_api.py
git commit -m "refactor(oh27): one loader from FiscalCalendar row to FiscalConfig"
```

---### Task 6: The engine — sources, idempotency, reverse-and-repost

**Files:**
- Create: `src/usali/gl_posting.py`
- Modify: `src/usali/qbo_push.py` (builder moves out; re-export shims)
- Test: `tests/test_gl_posting.py`

This is the heart of the slice. The builder's economics move verbatim; what
is new around them is the chart-from-DB, the role lookup, the ledger, and
the reversal pair.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gl_posting.py`:

```python
"""The posting engine (design D-OH27.5, .6): idempotent per
(property, date, source); corrections are reversal+repost, never edits."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from usali import gl_chart, gl_posting
from usali.models import JournalEntry, JournalLine, GlPostingLedger, UsaliFinancialFact


def _post(session, property_id, business_date):
    return gl_posting.post_and_record(
        session,
        property_id=property_id,
        business_date=business_date,
        source_type="pms_daily",
        actor="test",
    )


def test_posting_without_a_chart_is_skipped(db_session, founding_org, seed_six_pdfs):
    prop, day = seed_six_pdfs.property_id, seed_six_pdfs.business_date
    out = _post(db_session, prop, day)
    assert out.status == "skipped"
    assert db_session.scalar(select(GlPostingLedger)) is None


def test_posting_is_idempotent(db_session, founding_org, seed_six_pdfs):
    gl_chart.seed_chart(db_session)
    prop, day = seed_six_pdfs.property_id, seed_six_pdfs.business_date
    first = _post(db_session, prop, day)
    assert first.status == "posted"
    second = _post(db_session, prop, day)
    assert second.status == "noop"
    assert second.entry_id == first.entry_id
    entries = db_session.scalars(select(JournalEntry)).all()
    assert len(entries) == 1


def test_changed_facts_reverse_and_repost(db_session, founding_org, seed_six_pdfs):
    gl_chart.seed_chart(db_session)
    prop, day = seed_six_pdfs.property_id, seed_six_pdfs.business_date
    first = _post(db_session, prop, day)
    # Change the economics: bump one fact by a dollar.
    fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
        )
    ).first()
    fact.amount = float(Decimal(str(fact.amount)) + Decimal("1"))
    db_session.flush()
    out = _post(db_session, prop, day)
    assert out.status == "reposted"
    entries = db_session.scalars(
        select(JournalEntry).order_by(JournalEntry.entry_id)
    ).all()
    assert len(entries) == 3  # original, its reversal, the correction
    assert entries[1].reversal_of == entries[0].entry_id
    ledger = db_session.scalar(select(GlPostingLedger))
    assert ledger.entry_id == entries[2].entry_id and ledger.status == "posted"


def test_the_journal_nets_to_a_fresh_plan_after_any_repost(
    db_session, founding_org, seed_six_pdfs
):
    """The reversal invariant: net per account across ALL entries for the
    grain equals the current plan's lines exactly."""
    gl_chart.seed_chart(db_session)
    prop, day = seed_six_pdfs.property_id, seed_six_pdfs.business_date
    _post(db_session, prop, day)
    fact = db_session.scalars(select(UsaliFinancialFact)).first()
    fact.amount = float(Decimal(str(fact.amount)) + Decimal("2.5"))
    db_session.flush()
    _post(db_session, prop, day)

    plan = gl_posting.build_pms_daily_plan(db_session, prop, day)
    want = {
        line.gl_account_code: (line.amount if line.posting == "Credit" else -line.amount)
        for line in plan.lines
    }
    got: dict[str, Decimal] = {}
    for line in db_session.scalars(select(JournalLine)).all():
        signed = line.amount if line.posting == "Credit" else -line.amount
        got[line.account_code] = got.get(line.account_code, Decimal("0")) + signed
    got = {k: v for k, v in got.items() if v != 0}
    assert got == want


def test_unmapped_gl_records_a_failed_row(db_session, founding_org, seed_six_pdfs):
    gl_chart.seed_chart(db_session)
    prop, day = seed_six_pdfs.property_id, seed_six_pdfs.business_date
    db_session.execute(
        UsaliFinancialFact.__table__.update().values(gl_account_code=None)
    )
    db_session.flush()
    out = _post(db_session, prop, day)
    assert out.status == "failed"
    ledger = db_session.scalar(select(GlPostingLedger))
    assert ledger.status == "failed" and "no GL account code" in ledger.message
```

Executor note: `seed_six_pdfs` (`tests/conftest.py:172-191`) may return
nothing usable as `.property_id`/`.business_date` — read the fixture; if it
yields no handle, query the first `(property_id, business_date)` from
`UsaliFinancialFact` at the top of each test instead. Keep the assertions.

Run: `pytest tests/test_gl_posting.py -v`
Expected: FAIL — `No module named 'usali.gl_posting'`

- [ ] **Step 2: Move the builder and write the engine**

Create `src/usali/gl_posting.py`. Move `JeLine`, `JePlan`, `UnmappedGlError`,
`_group_by_gl`, `_QUANT_4DP`, and the body of `build_journal_entry` here from
`qbo_push.py` — the bucketing, hashing, and balancing logic verbatim, with
two substitutions: account names come from the org's `gl_account` rows
instead of `_load_account_names(yaml)`, and the balancing account comes from
`role_account(session, "guest_ledger_clearing")` instead of the `"1210"`
constant. Then the engine around it:

```python
"""The GL posting engine (ADR-011 §3; design D-OH27.5, .6, .7).

Sources are a closed registry (the checklist/CRM_PROVIDERS idiom): each
owns a build function returning a balanced JePlan or None (nothing to
post). post_and_record() is the single entry point every caller shares —
ingestion hook, timecard approval, CLI, API — so idempotency, the period
gate, and the failure ledger behave identically everywhere.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali import fiscal
from usali.models import (
    GlAccount,
    GlPeriodEvent,
    GlPostingLedger,
    JournalEntry,
    JournalLine,
    UsaliFinancialFact,
    UsaliLaborFact,
)

# ... JeLine, JePlan, UnmappedGlError, _group_by_gl, _QUANT_4DP moved here ...


class SystemRoleMissingError(Exception):
    """The org's chart has no active account carrying the named role."""

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(
            f"the chart of accounts has no active account with system role "
            f"{role!r} — seed or edit the chart (usali gl-seed-chart)"
        )


class PeriodClosedError(Exception):
    """Posting dated inside a closed fiscal period (D-OH27.7)."""

    def __init__(self, property_id: str, period_key: str) -> None:
        self.period_key = period_key
        super().__init__(
            f"period {period_key} is closed for property {property_id}; "
            f"reopen it (with a reason) before posting into it"
        )


def chart(session: Session) -> dict[str, GlAccount]:
    rows = session.scalars(
        select(GlAccount).where(GlAccount.is_active.is_(True))
    ).all()
    return {row.account_code: row for row in rows}


def role_account(session: Session, role: str) -> GlAccount:
    row = session.scalar(
        select(GlAccount).where(
            GlAccount.system_role == role, GlAccount.is_active.is_(True)
        )
    )
    if row is None:
        raise SystemRoleMissingError(role)
    return row


def build_pms_daily_plan(
    session: Session, property_id: str, business_date: date
) -> "JePlan | None":
    """The moved build_journal_entry, with the chart read from gl_account
    and the balancing line found via role_account(). Returns None (rather
    than raising NoFactsError) when the day has no facts — 'nothing to
    post' is a source outcome, not an error, in the engine."""
    ...


def build_payroll_accrual_plan(
    session: Session, property_id: str, business_date: date
) -> "JePlan | None":
    """Debit labor_wages_expense per department (one line each, department
    named in the memo), credit accrued_payroll with the total. est_cost is
    the estimate labor.py wrote on timecard approval; truing to PayRun
    actuals is deliberately out of scope (design §9)."""
    rows = session.execute(
        select(UsaliLaborFact.department_id, UsaliLaborFact.est_cost).where(
            UsaliLaborFact.property_id == property_id,
            UsaliLaborFact.business_date == business_date,
        )
    ).all()
    if not rows:
        return None
    by_dept: dict[int | None, Decimal] = {}
    for dept, cost in rows:
        by_dept[dept] = by_dept.get(dept, Decimal("0")) + Decimal(str(cost))
    expense = role_account(session, "labor_wages_expense")
    liability = role_account(session, "accrued_payroll")
    total = sum(by_dept.values(), Decimal("0"))
    if total == 0:
        return None
    lines = [
        JeLine(
            gl_account_code=expense.account_code,
            account_name=expense.name,
            posting="Debit",
            amount=cost,
            memo=f"Labor accrual dept {dept if dept is not None else 'unassigned'} "
                 f"{business_date.isoformat()}",
        )
        for dept, cost in sorted(by_dept.items(), key=lambda kv: str(kv[0]))
        if cost != 0
    ]
    lines.append(
        JeLine(
            gl_account_code=liability.account_code,
            account_name=liability.name,
            posting="Credit",
            amount=total,
            memo=f"Accrued payroll {business_date.isoformat()}",
        )
    )
    return _finish_plan(property_id, business_date, lines)  # totals + hash,
    # the same canonical-hash tail build_journal_entry already has —
    # extract it as _finish_plan() during the move so both builders share it.


@dataclass(frozen=True)
class PostingSource:
    source_type: str
    build_plan: Callable[[Session, str, date], "JePlan | None"]


POSTING_SOURCES: tuple[PostingSource, ...] = (
    PostingSource("pms_daily", build_pms_daily_plan),
    PostingSource("payroll_accrual", build_payroll_accrual_plan),
)

_SOURCES = {s.source_type: s for s in POSTING_SOURCES}

OutcomeStatus = Literal["posted", "noop", "reposted", "skipped", "failed"]


@dataclass(frozen=True)
class PostOutcome:
    status: OutcomeStatus
    entry_id: int | None
    message: str | None


def period_state(session: Session, property_id: str, period_key: str) -> str:
    last = session.scalar(
        select(GlPeriodEvent.event)
        .where(
            GlPeriodEvent.property_id == property_id,
            GlPeriodEvent.period_key == period_key,
        )
        .order_by(GlPeriodEvent.period_event_id.desc())
        .limit(1)
    )
    return "closed" if last == "close" else "open"


def _write_entry(
    session: Session,
    plan: "JePlan",
    *,
    source_type: str,
    actor: str,
    reversal_of: int | None = None,
    flip: bool = False,
) -> JournalEntry:
    entry = JournalEntry(
        property_id=plan.property_id,
        business_date=plan.business_date,
        source_type=source_type,
        source_hash=plan.request_hash,
        posted_by=actor,
        reversal_of=reversal_of,
        memo=f"reversal of entry {reversal_of}" if flip else None,
    )
    session.add(entry)
    session.flush()
    for line in plan.lines:
        posting = line.posting
        if flip:
            posting = "Debit" if posting == "Credit" else "Credit"
        session.add(
            JournalLine(
                entry_id=entry.entry_id,
                account_code=line.gl_account_code,
                posting=posting,
                amount=line.amount,
                memo=line.memo,
                fact_id=getattr(line, "fact_id", None),
            )
        )
    session.flush()
    return entry


def _plan_of_entry(session: Session, entry: JournalEntry) -> "JePlan":
    """Rebuild a JePlan from a posted entry's lines — the reversal input,
    and (Task 10) the QBO export input."""
    ...


def post_and_record(
    session: Session,
    *,
    property_id: str,
    business_date: date,
    source_type: str,
    actor: str,
) -> PostOutcome:
    """The one entry point. Refusals become failed ledger rows, never
    exceptions — the ingestion hook commits in the same transaction as
    promotion, so a GL problem must not quarantine a parsed file."""
    if session.scalar(select(GlAccount).limit(1)) is None:
        return PostOutcome("skipped", None, "no chart of accounts; GL is off")

    source = _SOURCES[source_type]
    now = datetime.now(UTC)
    row = session.scalar(
        select(GlPostingLedger).where(
            GlPostingLedger.property_id == property_id,
            GlPostingLedger.business_date == business_date,
            GlPostingLedger.source_type == source_type,
        )
    )

    def _fail(message: str) -> PostOutcome:
        nonlocal row
        if row is None:
            row = GlPostingLedger(
                property_id=property_id,
                business_date=business_date,
                source_type=source_type,
                status="failed",
            )
            session.add(row)
        if row.entry_id is None:
            row.status = "failed"
        row.message = message[:500]
        row.posted_at = now
        session.flush()
        return PostOutcome("failed", row.entry_id, row.message)

    try:
        cfg = fiscal.require_config(fiscal.config_for(session, property_id))
        period = fiscal.period_containing(cfg, business_date)
        plan = source.build_plan(session, property_id, business_date)
        if plan is None:
            return PostOutcome("skipped", None, None)
        if row is not None and row.entry_id is not None:
            if row.source_hash == plan.request_hash:
                if row.message is not None:
                    row.message = None
                    session.flush()
                return PostOutcome("noop", row.entry_id, None)
        if period_state(session, property_id, period) == "closed":
            raise PeriodClosedError(property_id, period)
        if row is not None and row.entry_id is not None:
            prior = session.get(JournalEntry, row.entry_id)
            _write_entry(
                session,
                _plan_of_entry(session, prior),
                source_type=source_type,
                actor=actor,
                reversal_of=prior.entry_id,
                flip=True,
            )
            entry = _write_entry(session, plan, source_type=source_type, actor=actor)
            row.entry_id, row.source_hash = entry.entry_id, plan.request_hash
            row.status, row.message, row.posted_at = "posted", None, now
            session.flush()
            return PostOutcome("reposted", entry.entry_id, None)
        entry = _write_entry(session, plan, source_type=source_type, actor=actor)
        if row is None:
            row = GlPostingLedger(
                property_id=property_id,
                business_date=business_date,
                source_type=source_type,
                status="posted",
            )
            session.add(row)
        row.entry_id, row.source_hash = entry.entry_id, plan.request_hash
        row.status, row.message, row.posted_at = "posted", None, now
        session.flush()
        return PostOutcome("posted", entry.entry_id, None)
    except (
        UnmappedGlError,
        SystemRoleMissingError,
        PeriodClosedError,
        fiscal.FiscalCalendarNotConfigured,
    ) as exc:
        return _fail(str(exc))
```

`_plan_of_entry` reconstructs `JeLine`s from `JournalLine` rows and re-derives
the canonical hash with the shared `_finish_plan` tail; write it during the
move, next to the builder, and give `JeLine` an optional `fact_id: int | None = None`
field so `build_pms_daily_plan` can carry per-line fact provenance into
`journal_line.fact_id` (group-level lines carry the fact id only when a GL
group has exactly one fact; otherwise `None` — drill-through still reaches
facts via (property, date, account→USALI linkage)).

In `qbo_push.py`, keep working shims for the move (the `month_bounds`
re-export precedent, `qbo_push.py:46-48`):

```python
from usali.gl_posting import (  # moved to the engine in OH-27 Task 6;
    JeLine,                      # re-exported so P8-era callers keep working
    JePlan,
    UnmappedGlError,
)
```

and leave `build_journal_entry` as a thin delegation to
`gl_posting.build_pms_daily_plan` that raises `NoFactsError` on `None`
(preserving its documented contract) until Task 10 retires the fact path.

- [ ] **Step 3: Run the engine tests, then the old push suite**

Run: `pytest tests/test_gl_posting.py -v && pytest tests/test_qbo_push.py -q 2>/dev/null || pytest $(grep -rln "build_journal_entry" tests/ | tr '\n' ' ') -q`
Expected: PASS both — the move must not change the push's behavior yet.

- [ ] **Step 4: Commit**

```bash
git add src/usali/gl_posting.py src/usali/qbo_push.py tests/test_gl_posting.py
git commit -m "feat(oh27): the posting engine — two sources, one ledger, reversals not edits"
```

---

### Task 7: Period close and reopen

**Files:**
- Modify: `src/usali/gl_posting.py`
- Test: `tests/test_gl_periods.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gl_periods.py`:

```python
"""Close/reopen semantics (design D-OH27.7): derived state, loud refusal,
reopen requires a reason, close names the gaps."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from usali import gl_chart, gl_posting
from usali.models import GlPeriodEvent, JournalEntry, UsaliFinancialFact


def _setup(db_session, seed_six_pdfs):
    gl_chart.seed_chart(db_session)
    prop = db_session.scalar(select(UsaliFinancialFact.property_id))
    day = db_session.scalar(
        select(UsaliFinancialFact.business_date).where(
            UsaliFinancialFact.property_id == prop
        )
    )
    return prop, day


def test_close_refuses_new_postings_and_reopen_restores(
    db_session, founding_org, seed_six_pdfs
):
    prop, day = _setup(db_session, seed_six_pdfs)
    period = gl_posting.period_key_for(db_session, prop, day)
    gl_posting.close_period(db_session, property_id=prop, period_key=period,
                            actor="admin")
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "failed" and period in out.message

    gl_posting.reopen_period(db_session, property_id=prop, period_key=period,
                             actor="admin", reason="late audit pack")
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "posted"


def test_repost_into_a_closed_period_is_refused_and_the_entry_stands(
    db_session, founding_org, seed_six_pdfs
):
    prop, day = _setup(db_session, seed_six_pdfs)
    first = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    period = gl_posting.period_key_for(db_session, prop, day)
    gl_posting.close_period(db_session, property_id=prop, period_key=period,
                            actor="admin")
    fact = db_session.scalars(select(UsaliFinancialFact)).first()
    fact.amount = float(Decimal(str(fact.amount)) + Decimal("1"))
    db_session.flush()
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "failed"
    assert len(db_session.scalars(select(JournalEntry)).all()) == 1
    assert out.entry_id == first.entry_id  # the posted entry still stands


def test_reopen_requires_a_reason(db_session, founding_org, seed_six_pdfs):
    prop, day = _setup(db_session, seed_six_pdfs)
    period = gl_posting.period_key_for(db_session, prop, day)
    gl_posting.close_period(db_session, property_id=prop, period_key=period,
                            actor="admin")
    with pytest.raises(ValueError, match="reason"):
        gl_posting.reopen_period(db_session, property_id=prop,
                                 period_key=period, actor="admin", reason="")


def test_close_names_the_unposted_fact_dates(db_session, founding_org, seed_six_pdfs):
    prop, day = _setup(db_session, seed_six_pdfs)
    period = gl_posting.period_key_for(db_session, prop, day)
    gaps = gl_posting.close_period(db_session, property_id=prop,
                                   period_key=period, actor="admin")
    assert day in gaps  # nothing was posted before closing
    events = db_session.scalars(select(GlPeriodEvent)).all()
    assert [e.event for e in events] == ["close"]
```

Run: `pytest tests/test_gl_periods.py -v`
Expected: FAIL — `period_key_for` / `close_period` / `reopen_period` missing.

- [ ] **Step 2: Implement in `gl_posting.py`**

```python
def period_key_for(session: Session, property_id: str, day: date) -> str:
    cfg = fiscal.require_config(fiscal.config_for(session, property_id))
    return fiscal.period_containing(cfg, day)


def close_period(
    session: Session, *, property_id: str, period_key: str, actor: str
) -> list[date]:
    """Append a close event; return fact dates in the period with no
    current pms_daily entry, so closing over a gap is a visible choice
    (design D-OH27.7). Idempotent: closing a closed period is a no-op
    that still returns the gaps."""
    cfg = fiscal.require_config(fiscal.config_for(session, property_id))
    start, end = fiscal.resolve_period(cfg, period_key)
    fact_dates = set(
        session.scalars(
            select(UsaliFinancialFact.business_date)
            .where(
                UsaliFinancialFact.property_id == property_id,
                UsaliFinancialFact.business_date >= start,
                UsaliFinancialFact.business_date <= end,
            )
            .distinct()
        )
    )
    posted_dates = set(
        session.scalars(
            select(GlPostingLedger.business_date).where(
                GlPostingLedger.property_id == property_id,
                GlPostingLedger.source_type == "pms_daily",
                GlPostingLedger.status == "posted",
                GlPostingLedger.business_date >= start,
                GlPostingLedger.business_date <= end,
            )
        )
    )
    if period_state(session, property_id, period_key) != "closed":
        session.add(
            GlPeriodEvent(
                property_id=property_id, period_key=period_key,
                event="close", actor_subject=actor,
            )
        )
        session.flush()
    return sorted(fact_dates - posted_dates)


def reopen_period(
    session: Session, *, property_id: str, period_key: str, actor: str, reason: str
) -> None:
    if not reason or not reason.strip():
        raise ValueError("reopening a closed period requires a reason")
    if period_state(session, property_id, period_key) != "closed":
        return  # idempotent, like the D-B4.5 PUT shape
    session.add(
        GlPeriodEvent(
            property_id=property_id, period_key=period_key,
            event="reopen", actor_subject=actor, reason=reason.strip(),
        )
    )
    session.flush()
```

- [ ] **Step 3: Run and commit**

Run: `pytest tests/test_gl_periods.py tests/test_gl_posting.py -v`
Expected: PASS

```bash
git add src/usali/gl_posting.py tests/test_gl_periods.py
git commit -m "feat(oh27): period close as an append-only event log"
```

---

### Task 8: The hooks — ingestion, timecard approval, CLI

**Files:**
- Modify: `src/usali/ingestion.py:290-313` (`_process_section`)
- Modify: `src/usali/labor.py` (`promote_timecard` return), `src/usali/timecard_api.py:317-327`, `src/usali/cli.py:492-505` (`promote-labor`)
- Modify: `src/usali/cli.py` (new `gl-seed-chart`, `gl-post`)
- Test: extend `tests/test_gl_posting.py`; touch `tests/test_skytouch_end_to_end.py`

- [ ] **Step 1: Write the failing pipeline test**

Append to `tests/test_gl_posting.py`:

```python
def test_process_file_posts_when_the_chart_exists(db_session, founding_org, tmp_path):
    """The ingestion hook (design §3): promotion and posting land in ONE
    transaction, and a GL refusal records a failed ledger row without
    quarantining the file. Mirrors test_skytouch_end_to_end's setup."""
    from tests.test_skytouch_end_to_end import SAMPLE  # or copy its seed lines
    from usali import gl_chart
    from usali.ingestion import process_pack
    from usali.models import GlPostingLedger
    from sqlalchemy import select
    import shutil

    # seed schedules/mappings/properties exactly as the end-to-end test does,
    # then additionally:
    gl_chart.seed_chart(db_session)
    db_session.commit()

    drop = tmp_path / SAMPLE.name
    shutil.copy(SAMPLE, drop)
    results = process_pack(
        db_session, drop,
        processed_dir=tmp_path / "processed", failed_dir=tmp_path / "failed",
    )
    assert results  # the pack parsed as before
    rows = db_session.scalars(select(GlPostingLedger)).all()
    assert rows and all(r.source_type == "pms_daily" for r in rows)
```

Executor note: import or inline the sample-path and seeding lines from
`tests/test_skytouch_end_to_end.py:20-49` — copy them rather than importing
private names if the module does not expose them; the assertion block above
is the contract.

Run: `pytest tests/test_gl_posting.py::test_process_file_posts_when_the_chart_exists -v`
Expected: FAIL — no ledger rows (no hook yet).

- [ ] **Step 2: The ingestion hook**

In `src/usali/ingestion.py`, `_process_section` (`:290-313`), after
`batch.status = "transformed"` and before `return`:

```python
    # OH-27: promotion and posting land in the caller's one transaction.
    # post_and_record turns GL refusals into failed ledger rows rather than
    # exceptions, so a books problem never quarantines a parsed file; with
    # no chart seeded it returns "skipped" and writes nothing (GL is off).
    gl_posting.post_and_record(
        session,
        property_id=det.property_id,
        business_date=business_date,
        source_type="pms_daily",
        actor="ingestion",
    )
```

with `from usali import gl_posting` added to the module imports.

- [ ] **Step 3: The timecard hook**

In `src/usali/labor.py`, change `promote_timecard` to collect and return the
`(property_id, business_date)` pairs it inserts (it currently returns an
int): build a `written: set[tuple[str, date]]` in the insert loop
(`labor.py:178-188`, one add per aggregate — add each aggregate's key), and
`return written`. Update the two callers that consume the return value —
`timecard_api.py:327` and `cli.py:492-505` — to use `len(written)` where
they echoed a count.

Then in `src/usali/timecard_api.py`, immediately after the
`promote_timecard(...)` call in the approval handler (`:327`), before the
commit:

```python
    for prop, day in sorted(written):
        gl_posting.post_and_record(
            session, property_id=prop, business_date=day,
            source_type="payroll_accrual", actor=principal.subject,
        )
```

(matching the handler's actual principal variable name), and the same loop in
`cli.py`'s `promote-labor` with `actor="cli"`.

- [ ] **Step 4: The CLI commands**

Append to `src/usali/cli.py`, following the `seed-mappings` shape
(`cli.py:96-102`):

```python
@app.command("gl-seed-chart")
def gl_seed_chart_cmd() -> None:
    """Seed the USALI chart for the org (insert-only; edits survive)."""
    from usali import gl_chart

    with _session_factory()() as s:
        n = gl_chart.seed_chart(s)
        s.commit()
    typer.echo(f"Seeded {n} account(s)")


@app.command("gl-post")
def gl_post_cmd(
    property_id: str = typer.Argument(...),
    date_from: str = typer.Argument(..., help="YYYY-MM-DD"),
    date_to: str = typer.Argument(..., help="YYYY-MM-DD"),
) -> None:
    """Post (or re-post) every source for each date in the range —
    the D-OH27.8 backfill: the production engine, run over history."""
    from datetime import date as _date

    from usali import gl_posting

    start, end = _date.fromisoformat(date_from), _date.fromisoformat(date_to)
    if start > end:
        raise typer.BadParameter(f"{date_from} is after {date_to}")
    failures = 0
    with _session_factory()() as s:
        day = start
        while day <= end:
            for source in gl_posting.POSTING_SOURCES:
                out = gl_posting.post_and_record(
                    s, property_id=property_id, business_date=day,
                    source_type=source.source_type, actor="cli",
                )
                if out.status not in ("skipped",):
                    typer.echo(f"{day} {source.source_type}: {out.status}"
                               + (f" — {out.message}" if out.message else ""))
                if out.status == "failed":
                    failures += 1
            day += timedelta(days=1)
        s.commit()
    if failures:
        typer.echo(f"FAILED: {failures} posting(s) refused", err=True)
        raise typer.Exit(code=1)
```

(`timedelta` joins the module's datetime imports.)

- [ ] **Step 5: Run the affected suites**

Run: `pytest tests/test_gl_posting.py tests/test_skytouch_end_to_end.py $(grep -rln "promote_timecard" tests/ | tr '\n' ' ') -q`
Expected: PASS — the end-to-end tests still pass unchanged because their
orgs have no chart (the skip gate), and the timecard suites pass with the
new return shape.

- [ ] **Step 6: Commit**

```bash
git add src/usali/ingestion.py src/usali/labor.py src/usali/timecard_api.py src/usali/cli.py tests/test_gl_posting.py
git commit -m "feat(oh27): posting rides promotion — ingestion, approval, and the backfill CLI"
```

---

### Task 9: Trial balance in `reporting.py`

**Files:**
- Modify: `src/usali/reporting.py`
- Test: `tests/test_gl_reporting.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gl_reporting.py`:

```python
"""Trial balance from the journal (design D-OH27.9): per account per
period, exact totals, debits == credits always (the trigger guarantees it;
the report proves it end to end)."""

from decimal import Decimal

from sqlalchemy import select

from usali import gl_chart, gl_posting, reporting
from usali.models import UsaliFinancialFact


def test_trial_balance_balances_and_carries_names(
    db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session)
    prop = db_session.scalar(select(UsaliFinancialFact.property_id))
    days = sorted(set(db_session.scalars(
        select(UsaliFinancialFact.business_date).where(
            UsaliFinancialFact.property_id == prop
        )
    )))
    for day in days:
        gl_posting.post_and_record(
            db_session, property_id=prop, business_date=day,
            source_type="pms_daily", actor="test",
        )
    period = gl_posting.period_key_for(db_session, prop, days[0])
    tb = reporting.trial_balance(db_session, property_id=prop, period_key=period)
    assert tb.total_debits == tb.total_credits > Decimal("0")
    assert all(line.name for line in tb.lines)
    assert tb.date_from <= days[0] <= tb.date_to
```

Run: `pytest tests/test_gl_reporting.py -v`
Expected: FAIL — `reporting` has no `trial_balance`.

- [ ] **Step 2: Implement**

Append to `src/usali/reporting.py` (frozen-dataclass style, matching the
module):

```python
@dataclass(frozen=True)
class TrialBalanceLine:
    account_code: str
    name: str
    account_type: str
    debits: Decimal
    credits: Decimal


@dataclass(frozen=True)
class TrialBalanceReport:
    property_id: str
    period_key: str
    date_from: date
    date_to: date
    lines: list[TrialBalanceLine]
    total_debits: Decimal
    total_credits: Decimal


def trial_balance(
    session: Session, *, property_id: str, period_key: str
) -> TrialBalanceReport:
    """Journal-derived per-account totals for one fiscal period. Reads
    journal_line joined to journal_entry (for property + date scope) and
    gl_account (for name/type); reversals net out arithmetically, so no
    filtering is needed. Raises NoFactsError when the period holds no
    journal lines — the same typed signal the SOS uses."""
    from usali import fiscal
    from usali.models import GlAccount, JournalEntry, JournalLine

    cfg = fiscal.require_config(fiscal.config_for(session, property_id))
    date_from, date_to = fiscal.resolve_period(cfg, period_key)
    rows = session.execute(
        select(
            JournalLine.account_code,
            GlAccount.name,
            GlAccount.account_type,
            JournalLine.posting,
            JournalLine.amount,
        )
        .join(JournalEntry, JournalEntry.entry_id == JournalLine.entry_id)
        .join(GlAccount, GlAccount.account_code == JournalLine.account_code)
        .where(
            JournalEntry.property_id == property_id,
            JournalEntry.business_date >= date_from,
            JournalEntry.business_date <= date_to,
        )
    ).all()
    if not rows:
        raise NoFactsError(
            f"no journal lines for property {property_id} in {period_key}"
        )
    acc: dict[str, TrialBalanceLine] = {}
    for code, name, acct_type, posting, amount in rows:
        prior = acc.get(code)
        debits = (prior.debits if prior else Decimal("0")) + (
            Decimal(str(amount)) if posting == "Debit" else Decimal("0")
        )
        credits = (prior.credits if prior else Decimal("0")) + (
            Decimal(str(amount)) if posting == "Credit" else Decimal("0")
        )
        acc[code] = TrialBalanceLine(code, name, acct_type, debits, credits)
    lines = [acc[c] for c in sorted(acc)]
    return TrialBalanceReport(
        property_id=property_id,
        period_key=period_key,
        date_from=date_from,
        date_to=date_to,
        lines=lines,
        total_debits=sum((l.debits for l in lines), Decimal("0")),
        total_credits=sum((l.credits for l in lines), Decimal("0")),
    )
```

(The joins are org-safe without explicit org predicates because every table
involved sits behind the org wall — the same reasoning every existing
reporting query relies on.)

- [ ] **Step 3: Run and commit**

Run: `pytest tests/test_gl_reporting.py -v`
Expected: PASS

```bash
git add src/usali/reporting.py tests/test_gl_reporting.py
git commit -m "feat(oh27): the trial balance reads the journal"
```

---

### Task 10: The QBO push exports the journal

**Files:**
- Modify: `src/usali/qbo_push.py`
- Test: extend the existing QBO push suite (locate: `grep -rln "push_day" tests/`)

- [ ] **Step 1: Write the failing equivalence test**

In the QBO push test module, add:

```python
def test_the_journal_built_body_equals_the_fact_built_body(
    db_session, founding_org, seed_six_pdfs
):
    """D-OH27.10's proof that re-pointing changes plumbing, not books:
    for the same facts, the Intuit body built from the posted journal
    entry is line-for-line the body the fact path produced."""
    from sqlalchemy import select
    from usali import gl_chart, gl_posting, qbo_push
    from usali.models import JournalEntry, UsaliFinancialFact

    gl_chart.seed_chart(db_session)
    prop = db_session.scalar(select(UsaliFinancialFact.property_id))
    day = db_session.scalar(
        select(UsaliFinancialFact.business_date).where(
            UsaliFinancialFact.property_id == prop
        )
    )
    legacy_plan = gl_posting.build_pms_daily_plan(db_session, prop, day)
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    entry = db_session.get(JournalEntry, out.entry_id)
    assert qbo_push.journal_entry_body(
        gl_posting.plan_of_entry(db_session, entry)
    ) == qbo_push.journal_entry_body(legacy_plan)


def test_payroll_accrual_entries_are_not_pushed(db_session, founding_org):
    """D-OH27.10: owners' QBO books get exactly what they got before.
    push_day must select ONLY the pms_daily entry for the grain."""
    # Arrange a property/date that has BOTH a pms_daily and a payroll_accrual
    # entry posted, then assert the body push_day builds contains no line
    # from the accrual entry (no 'Accrued payroll' memo). Build the labor
    # fixture the way tests/test_gl_posting.py's payroll tests do.
```

(Fill the second test's body from the labor-fact fixture established in
Task 6's suite; its assertion is on the built body, before any client call.)

Run: FAIL — `plan_of_entry` is not public / `push_day` still builds from facts.

- [ ] **Step 2: Re-point `push_day`**

In `qbo_push.py`:

- Export `plan_of_entry` from `gl_posting` (rename `_plan_of_entry` public).
- `push_day` replaces `plan = build_journal_entry(...)` with:

```python
    entry = session.scalar(
        select(JournalEntry).where(
            JournalEntry.property_id == property_id,
            JournalEntry.business_date == business_date,
            JournalEntry.source_type == "pms_daily",
        ).order_by(JournalEntry.entry_id.desc()).limit(1)
    )
    if entry is None:
        # Not posted yet (or GL off for this org): fall back to the fact
        # path so the push works for orgs that have not adopted the GL.
        plan = build_journal_entry(
            session, property_id=property_id, business_date=business_date
        )
    else:
        current = session.scalar(
            select(GlPostingLedger.entry_id).where(
                GlPostingLedger.property_id == property_id,
                GlPostingLedger.business_date == business_date,
                GlPostingLedger.source_type == "pms_daily",
            )
        )
        plan = gl_posting.plan_of_entry(
            session, session.get(JournalEntry, current) if current else entry
        )
```

The `QboPushLedger` pushed/failed/stale lifecycle below that point is
untouched — `request_hash` still fingerprints the plan, so an already-pushed
identical entry replays and changed books surface as `stale` exactly as
before. The fact-path fallback keeps the D8.3 posture: an org with GL off
loses nothing it had.

- [ ] **Step 3: Run the full push suite and commit**

Run: `pytest $(grep -rln "push_day\|qbo_push" tests/ | tr '\n' ' ') -q`
Expected: PASS

```bash
git add src/usali/qbo_push.py src/usali/gl_posting.py tests/
git commit -m "feat(oh27): the QBO push exports the journal, facts as fallback"
```

---

### Task 11: The `/api/gl` router

**Files:**
- Create: `src/usali/gl_api.py`
- Modify: `src/usali/server.py:436-479` (mount under `operator_gates`)
- Test: `tests/test_gl_api.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gl_api.py`, pattern-matched on the existing integrations
API tests (locate the client/auth fixtures with
`grep -n "def client\|TestClient" tests/test_integrations_api.py | head`):

```python
"""The /api/gl surface: org_admin gates on mutation, closed periods are
409s, the trial balance mirrors reporting.trial_balance field-for-field."""
```

Tests to write (assertions named; fixture plumbing copied from the
integrations API suite):

- `test_accounts_get_lists_the_seeded_chart` — after `seed_chart`, `GET
  /api/gl/accounts` returns rows including `4000` with `account_type ==
  "income"`; non-admin principal gets 403.
- `test_role_bearing_accounts_refuse_deactivation` — `PUT` on the
  `guest_ledger_clearing` account with `is_active: false` → 422 naming the
  role (the D-OH27.2 enforcement point).
- `test_close_returns_gaps_and_reopen_requires_reason` — `PUT
  /api/gl/periods/{key}/close?property=...` → 200 with `unposted_dates`;
  `PUT .../reopen` with empty reason → 422; with reason → 204.
- `test_post_endpoint_reports_outcomes` — `POST /api/gl/post` for a seeded
  property/date range returns per-date `{date, source, status, message}`
  rows matching `PostOutcome`.
- `test_trial_balance_endpoint_mirrors_reporting` — response totals equal
  `reporting.trial_balance(...)`'s.

Run: `pytest tests/test_gl_api.py -v` — Expected: FAIL (no module).

- [ ] **Step 2: Implement `gl_api.py`**

```python
"""The /api/gl surface (design §5). Reads are operator-gated at mount;
mutations (chart edits, close/reopen, post) are org_admin-only, the
integrations_api precedent."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from usali import gl_chart, gl_posting, reporting
from usali.auth import ORG_ADMIN, Principal, request_session_factory, require_grants
from usali.models import GlAccount

router = APIRouter(prefix="/api/gl")
require_gl_admin = require_grants(ORG_ADMIN)


def _session(request: Request):
    return request_session_factory(request)()


class AccountModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_code: str
    name: str
    account_type: str
    system_role: str | None
    usali_schedule_id: int | None
    is_active: bool


class AccountPut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    account_type: str
    is_active: bool = True


class PeriodModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_key: str
    state: str
    unposted_dates: list[date]


class CloseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_key: str
    state: str
    unposted_dates: list[date]


class ReopenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class PostBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: str
    date_from: date
    date_to: date


class PostOutcomeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_date: date
    source_type: str
    status: str
    message: str | None


class TrialBalanceLineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_code: str
    name: str
    account_type: str
    debits: str
    credits: str


class TrialBalanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: str
    period_key: str
    date_from: date
    date_to: date
    lines: list[TrialBalanceLineModel]
    total_debits: str
    total_credits: str
```

Endpoints (each following the module's models; amounts serialize as
`str(Decimal)`, the `journal_entry_body` precedent — exact through JSON):

- `GET ""` → `list[AccountModel]` (admin-gated like the integrations GET).
- `PUT "/accounts/{account_code}"` → 204; loads the row, and if
  `row.system_role is not None and not body.is_active`, raises
  `HTTPException(422, detail=f"account carries system role {row.system_role}; ...")`.
  Creates the row when absent (an org extension).
- `GET "/trial-balance"` (`property` + `period` query params) → wraps
  `reporting.trial_balance` with the module-local `_run`-style mapping:
  `NoFactsError` → 404, `FiscalCalendarNotConfigured` → 422.
- `GET "/periods"` (`property`, optional `fiscal_year`) → derived state per
  period via `gl_posting.period_state` + the gap list `close_period`
  computes (extract the gap query into a helper both share).
- `PUT "/periods/{period_key}/close"` (`property` query) → `CloseResponse`.
- `PUT "/periods/{period_key}/reopen"` → 204; empty reason → 422 (the
  `ValueError` from `reopen_period` mapped).
- `POST "/post"` (`PostBody`) → `list[PostOutcomeModel]`, looping dates ×
  `POSTING_SOURCES` exactly as `gl-post` does, committing once.

All mutations take `principal: Annotated[Principal, Depends(require_gl_admin)]`
and pass `principal.subject` as `actor`.

- [ ] **Step 3: Mount it**

In `src/usali/server.py`, with the other operator-gated includes
(`:436-479`):

```python
from usali.gl_api import router as gl_router
...
    app.include_router(gl_router, dependencies=operator_gates)
```

- [ ] **Step 4: Run and commit**

Run: `pytest tests/test_gl_api.py -q`
Expected: PASS

```bash
git add src/usali/gl_api.py src/usali/server.py tests/test_gl_api.py
git commit -m "feat(oh27): the /api/gl surface — chart, periods, trial balance, post"
```

---

### Task 12: The parity gate

**Files:**
- Modify: `src/usali/reporting.py`
- Test: `tests/test_gl_parity.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gl_parity.py`:

```python
"""The D-OH27.9 gate, in CI from day one: the journal's revenue-side
statement must match the fact-derived SOS exactly on the seeded sample
data. The SOS is NOT re-pointed until this holds on real data too."""

from sqlalchemy import select

from usali import gl_chart, gl_posting, reporting
from usali.models import UsaliFinancialFact


def test_journal_and_facts_agree_on_the_seeded_samples(
    db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session)
    pairs = set(db_session.execute(
        select(UsaliFinancialFact.property_id, UsaliFinancialFact.business_date)
    ).all())
    for prop, day in sorted(pairs):
        gl_posting.post_and_record(
            db_session, property_id=prop, business_date=day,
            source_type="pms_daily", actor="test",
        )
    diffs = []
    for prop in sorted({p for p, _ in pairs}):
        days = sorted(d for p, d in pairs if p == prop)
        diffs.extend(reporting.sos_journal_parity(
            db_session, property_id=prop,
            date_from=days[0], date_to=days[-1],
        ))
    assert diffs == [], f"journal disagrees with the SOS: {diffs}"
```

Run: FAIL — `sos_journal_parity` does not exist.

- [ ] **Step 2: Implement `sos_journal_parity`**

Append to `reporting.py`:

```python
@dataclass(frozen=True)
class ParityDiff:
    gl_account_code: str
    fact_total: Decimal
    journal_total: Decimal


def sos_journal_parity(
    session: Session, *, property_id: str, date_from: date, date_to: date
) -> list[ParityDiff]:
    """Per GL account over the range: the facts' net amount vs the
    journal's net (credits positive), pms_daily entries only. The
    balancing (guest-ledger-clearing) account is excluded — it has no fact
    counterpart by construction. Empty list == parity."""
    from usali.models import GlAccount, JournalEntry, JournalLine

    fact_rows = session.execute(
        select(
            UsaliFinancialFact.gl_account_code,
            func.sum(UsaliFinancialFact.amount),
        )
        .where(
            UsaliFinancialFact.property_id == property_id,
            UsaliFinancialFact.business_date >= date_from,
            UsaliFinancialFact.business_date <= date_to,
            UsaliFinancialFact.gl_account_code.is_not(None),
        )
        .group_by(UsaliFinancialFact.gl_account_code)
    ).all()
    facts = {code: Decimal(str(total)).quantize(Decimal("0.0001"))
             for code, total in fact_rows}

    clearing = session.scalar(
        select(GlAccount.account_code).where(
            GlAccount.system_role == "guest_ledger_clearing"
        )
    )
    journal_rows = session.execute(
        select(JournalLine.account_code, JournalLine.posting, JournalLine.amount)
        .join(JournalEntry, JournalEntry.entry_id == JournalLine.entry_id)
        .where(
            JournalEntry.property_id == property_id,
            JournalEntry.source_type == "pms_daily",
            JournalEntry.business_date >= date_from,
            JournalEntry.business_date <= date_to,
        )
    ).all()
    journal: dict[str, Decimal] = {}
    for code, posting, amount in journal_rows:
        if code == clearing:
            continue
        signed = Decimal(str(amount)) if posting == "Credit" else -Decimal(str(amount))
        journal[code] = journal.get(code, Decimal("0")) + signed

    diffs = []
    for code in sorted(set(facts) | set(journal)):
        f = facts.get(code, Decimal("0"))
        j = journal.get(code, Decimal("0")).quantize(Decimal("0.0001"))
        if f != j:
            diffs.append(ParityDiff(code, f, j))
    return diffs
```

- [ ] **Step 3: Run everything and commit**

Run: `pytest tests/test_gl_parity.py -v && pytest -q`
Expected: parity PASSES and the full suite is green.

```bash
git add src/usali/reporting.py tests/test_gl_parity.py
git commit -m "feat(oh27): the parity gate — the journal must agree with the SOS"
```

---

### Task 13: Docs sync and finish

- [ ] **Step 1: ROADMAP annotation**

In `docs/ROADMAP.md` §3 Tier 0 row 3 (OH-27), append to the Why column:
`**Backend shipped**; the /gl page and the SOS cutover (parity-gated) follow.`
Do NOT flip `.github/roadmap.yml` to `shipped` — the catalogue entry promises
"keep the books", which includes the page; leave `in-progress`.

- [ ] **Step 2: Design-doc status**

In `docs/design/2026-09-06-oh27-gl-posting-core-design.md`, update the
Status line to record execution started and note any decision amended during
execution in the decisions log (the D-OH17.7 precedent: amendments are
recorded, not silently diverged from).

- [ ] **Step 3: Full suite, then hand off**

Run: `pytest -q && (cd frontend && npm test -- --run 2>/dev/null || true)`
Expected: backend fully green; frontend untouched.

```bash
git add docs/ROADMAP.md docs/design/2026-09-06-oh27-gl-posting-core-design.md
git commit -m "docs(oh27): the posting core backend is built; page and cutover follow"
```

Then use superpowers:finishing-a-development-branch — PR to `main`, review,
admin-merge per the repo's solo-PR convention.

---

## Self-review notes (kept for the executor)

- **Spec coverage:** D-OH27.1→T4, .2→T4/T6/T11, .3→T1, .4→T2/T3, .5→T6/T8,
  .6→T6, .7→T7, .8→T8 (`gl-post`), .9→T9/T12 (cutover itself deferred by
  design), .10→T10, .11→T6 (Decimal throughout), .12→T2/T3. Design §5's
  API→T11, §7 refusals→T6/T7/T11.
- **Known soft spots the executor must resolve against code, not guess:**
  the `two_tenant_world` attribute names (T3), the seeded property id used
  in wall tests (T3), `seed_six_pdfs`'s return shape (T6/T9/T12), the
  provisioner's location (T4), the QBO push test module's name (T10), and
  whether `current_org_id` works under the superuser test session (T4).
  Each is a read-first note in its task; none changes a contract.
- **Type consistency:** `JePlan`/`JeLine` live in `gl_posting` after T6 with
  `qbo_push` re-exports; `plan_of_entry` becomes public in T10; every amount
  crossing a boundary is `Decimal`, serialized as `str`.
