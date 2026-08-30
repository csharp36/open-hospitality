# OH-17 — per-tenant integration config (BACKEND) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each tenant hold its own payroll / accounting / demand-feed
credentials, so the three integration checklist items become closeable and
"does QBO exist for this tenant?" becomes answerable.

**Architecture:** One `OrgScoped` table, `org_integration_credential`, whose
row carries the provider **and** its credentials so the two cannot drift
(D-OH17.1). Secrets are `EncryptedString` (ADR-005). A new `integrations.py`
owns the single resolution seam — given an org-bound session factory it
returns a configured adapter or `None`. `OrgSettings` is deleted, its
`crm_provider` column absorbed. `QboClient` gains a `TokenStore` port so the
rotating refresh token is persisted per tenant instead of living in process
memory.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, pydantic,
Postgres 16 via testcontainers, `uv`.

Implements [`docs/design/2026-08-30-oh17-per-tenant-integration-config-design.md`](../design/2026-08-30-oh17-per-tenant-integration-config-design.md).
The frontend (`/integrations` page) is a separate plan, written after this
lands — the B1/B4 precedent.
Branch: `feat/oh17-per-tenant-integration-config`.

---

## Gates (run for EVERY task before committing)

```bash
uv run pytest -q            # full suite (testcontainers Postgres, ~9 min)
uv run mypy src             # src-only, never tests/
uv run ruff check src tests
```

Fixtures are synthetic — no real PII, phone numbers, or credentials. Never
commit anything from `~/Desktop/Sample Hotel`.

## Grounding facts (verified against the code — do not re-guess)

- **Alembic head is `b2a0checklist`**
  (`migrations/versions/b2a0checklist_org_checklist_override.py:25`). The one
  test hardcoding the literal is `tests/test_l4_org_grants.py:358` —
  `assert ScriptDirectory.from_config(cfg).get_heads() == ["b2a0checklist"]`.
  Task 2 updates it. Other head tests assert only `len(get_heads()) == 1`.
- **Adding an `OrgScoped` table touches four hand-maintained lists**, none of
  which update themselves. This slice ALSO removes a table, so two of them
  change twice:
  1. `tests/test_l2_rls_wall.py:449-451` — the literal `expected` set. Add
     `org_integration_credential`, remove `org_settings`.
  2. `tests/test_models.py:33` — the exact `Base.metadata` name set. Same two
     edits.
  3. `tests/test_l4_org_grants.py:358` — the head literal.
  4. `_L1_ORG_INDEPENDENT` in `tests/test_migration_on_populated_data.py` —
     **do NOT touch**; this table is `OrgScoped`.
  Miss list 1 and the suite goes red. Miss the *policy* in the migration and
  the table has no tenant wall at all — the silent one.
- **The RLS migration template** is
  `migrations/versions/l5a0orgsettings_org_settings.py`: `ENABLE` **and**
  `FORCE ROW LEVEL SECURITY`, then a policy named exactly `org_wall` with both
  `USING` and `WITH CHECK`, predicate built from `usali.tenancy.RLS_ORG_VAR`.
  **Add no GRANT** — `l2a0rlswall` recorded DEFAULT PRIVILEGES covering future
  tables.
- **`OrgScoped`** (`models.py:33`) gives `org_id` as a plain indexed FK.
  `OrgChecklistOverride` (`models.py:507`) is the idiom to copy when `org_id`
  must be part of a composite primary key.
- **`EncryptedString`** is `usali.crypto.EncryptedString` (`crypto.py:125`),
  already imported by `models.py:26`.
- **The ORM read wall is SELECT-only by design** (`tenancy.py:18-21`), so
  `update()` / `delete()` ride the DB wall alone. The `org_wall` policy has no
  `FOR` clause, so it covers every verb — an unfiltered `delete()` scoped only
  by business key is still tenant-safe.
- **`Settings` fields that MUST NOT move** (D-OH17.3): every `*_base_url`, plus
  `qbo_client_id` and `qbo_client_secret` — those identify our Intuit
  *application*, not a tenant.
- **The Gusto e2e depends on the bare defaults.** `scripts/e2e_backend.py:399-401`:
  *"Mock Gusto for the pay-run e2e. No env needed: the settings defaults
  already point the GustoAdapter at 127.0.0.1:9300 with the static 'mock'
  token, and payroll_provider defaults to gusto."* A seed rule that only fires
  on non-default env silently breaks `payrun.spec.ts`. See D-OH17.15.
- **`USALI_CRM_PROVIDER` is set explicitly** by `scripts/demo.sh:91`,
  `scripts/cloud/deploy_app.sh:119` and `scripts/cloud/smoke.sh:57`; unset, the
  seed prints the honest "skipped" note at `scripts/demo_seed.py:838`. Keep
  both behaviours.
- **Nothing else writes `OrgSettings`.** The only writer is
  `ensure_default_org` (`mapping/property_registry.py:117`), for org 1. The
  only readers are `crm_api._active_org_crm_provider` (`crm_api.py:59`) and
  `checklist._probe_demand_feed` (`checklist.py:179`). That complete
  enumeration is what makes D-OH17.14's "drop, don't migrate" safe.

## Naming contract (used identically in every task below)

```python
# usali/integrations.py
PAYROLL = "payroll"; ACCOUNTING = "accounting"; DEMAND_FEED = "demand_feed"
INTEGRATIONS: tuple[str, ...]
PROVIDERS: tuple[ProviderSpec, ...]
def spec_for(integration: str, provider: str) -> ProviderSpec | None
def credential_for(session: Session, integration: str) -> OrgIntegrationCredential | None
def has_credential(session: Session, integration: str) -> bool
def resolve_payroll(factory: SessionFactory) -> PayrollProvider | None
def resolve_qbo(factory: SessionFactory) -> QboClient | None
def resolve_crm_feed(factory: SessionFactory) -> CrmFeed | None
def connected_provider(session: Session, integration: str) -> str
```

`resolve_*` take an **org-bound session factory**, not a session: `resolve_qbo`
must hand the same factory to `DbTokenStore` so rotation can write back in its
own short transaction. The three are uniform so no call site has to remember
which shape a given integration takes. `has_credential` / `credential_for` /
`connected_provider` take a session, because their callers (the checklist
probes, the `crm_api` handler) already hold one.

---

## Task 1: The `OrgIntegrationCredential` model, and deleting `OrgSettings`

**Files:**
- Modify: `src/usali/models.py:467-504` (delete `OrgSettings`), add the new model after it
- Modify: `tests/test_models.py:33`
- Test: `tests/test_integrations.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_integrations.py`:

```python
"""OH-17: per-tenant integration credentials (design D-OH17.1, D-OH17.5)."""

from usali.models import Base, OrgIntegrationCredential


def test_the_table_is_registered():
    assert "org_integration_credential" in Base.metadata.tables


def test_org_settings_is_gone():
    """D-OH17.1: crm_provider was OrgSettings' only column, so absorbing it
    into the credential row leaves an empty table — and an empty table is
    where the next drift grows back."""
    assert "org_settings" not in Base.metadata.tables
    assert not hasattr(__import__("usali.models", fromlist=["x"]), "OrgSettings")


def test_org_id_is_part_of_the_primary_key():
    """The OrgChecklistOverride shape: org-scoped by its own composite key,
    so both L2 walls confine it automatically."""
    pk = {c.name for c in OrgIntegrationCredential.__table__.primary_key}
    assert pk == {"org_id", "integration"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integrations.py -v`
Expected: FAIL — `ImportError: cannot import name 'OrgIntegrationCredential'`

- [ ] **Step 3: Delete `OrgSettings` and add the new model**

In `src/usali/models.py`, delete the whole `class OrgSettings(OrgScoped, Base)`
block (currently `:467-504`) and put this in its place:

```python
class OrgIntegrationCredential(OrgScoped, Base):
    """One tenant's credentials for ONE integration (OH-17, D-OH17.1).

    The row IS the connection: it carries the provider AND its credentials, so
    the two cannot drift. A tenant structurally cannot pick a provider without
    supplying credentials for it. This replaces `OrgSettings`, whose
    `crm_provider` said WHICH provider while `Settings` held its keys — two
    places, one of which was not even per-tenant.

    `org_id` is part of the composite primary key and the FK to `organization`
    (the `OrgChecklistOverride` shape), so both L2 walls confine it
    automatically — the ORM criteria hook and the `org_wall` RLS policy.

    Secrets are `EncryptedString` (ADR-005, AES-256-GCM under a key the server
    HOLDS). That regime and not ADR-004's blind vault because Intuit rotates
    the QBO refresh token on every grant, server-side, with no browser in the
    loop: the server must write the new token back, which a blind-at-rest
    envelope cannot do (D-OH17.2).

    Identifiers (`realm_id`, `company_id`, `client_id`) stay PLAINTEXT
    deliberately — they are not secrets, and reading them during a support
    conversation is worth more than encrypting a company id.

    `connected_at` / `connected_by` record the write EVENT. No probe reads
    them: a `verified_at` consulted as status would be the stored copy D-B4.1
    forbids.

    The CHECK is the SCHEMA MIRROR of `usali.integrations.PROVIDERS` — kept
    literal on purpose so the DB refuses a malformed credential row
    independently of the app import (the `org_checklist_override.item_key`
    discipline). Its "must be NULL" half is not decoration: it is what stops a
    stale `api_key` surviving a switch from Tripleseat to Delphi. If PROVIDERS
    changes, this and the b3a0integcred migration CHECK change with it.
    """

    __tablename__ = "org_integration_credential"
    __table_args__ = (
        CheckConstraint(
            "(integration = 'payroll' AND provider = 'gusto'"
            "  AND api_token IS NOT NULL AND company_id IS NOT NULL"
            "  AND refresh_token IS NULL AND realm_id IS NULL"
            "  AND client_id IS NULL AND client_secret IS NULL"
            "  AND subscription_key IS NULL AND api_key IS NULL)"
            " OR (integration = 'payroll' AND provider = 'adp'"
            "  AND client_id IS NOT NULL AND client_secret IS NOT NULL"
            "  AND refresh_token IS NULL AND realm_id IS NULL"
            "  AND api_token IS NULL AND company_id IS NULL"
            "  AND subscription_key IS NULL AND api_key IS NULL)"
            " OR (integration = 'accounting' AND provider = 'qbo'"
            "  AND refresh_token IS NOT NULL AND realm_id IS NOT NULL"
            "  AND api_token IS NULL AND company_id IS NULL"
            "  AND client_id IS NULL AND client_secret IS NULL"
            "  AND subscription_key IS NULL AND api_key IS NULL)"
            " OR (integration = 'demand_feed' AND provider = 'delphi'"
            "  AND subscription_key IS NOT NULL"
            "  AND refresh_token IS NULL AND realm_id IS NULL"
            "  AND api_token IS NULL AND company_id IS NULL"
            "  AND client_id IS NULL AND client_secret IS NULL"
            "  AND api_key IS NULL)"
            " OR (integration = 'demand_feed' AND provider = 'tripleseat'"
            "  AND api_key IS NOT NULL"
            "  AND refresh_token IS NULL AND realm_id IS NULL"
            "  AND api_token IS NULL AND company_id IS NULL"
            "  AND client_id IS NULL AND client_secret IS NULL"
            "  AND subscription_key IS NULL)",
            name="ck_org_integration_credential_provider_fields",
        ),
    )

    org_id: Mapped[int] = mapped_column(
        ForeignKey("organization.org_id", name="fk_org_integration_credential_org"),
        primary_key=True,
    )
    integration: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))

    realm_id: Mapped[str | None] = mapped_column(String(64))
    refresh_token: Mapped[str | None] = mapped_column(EncryptedString)
    api_token: Mapped[str | None] = mapped_column(EncryptedString)
    company_id: Mapped[str | None] = mapped_column(String(64))
    client_id: Mapped[str | None] = mapped_column(String(128))
    client_secret: Mapped[str | None] = mapped_column(EncryptedString)
    subscription_key: Mapped[str | None] = mapped_column(EncryptedString)
    api_key: Mapped[str | None] = mapped_column(EncryptedString)

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    connected_by: Mapped[str] = mapped_column(String(64))  # actor keycloak subject
```

- [ ] **Step 4: Fix the two now-broken imports**

`src/usali/crm_api.py:33` and `src/usali/checklist.py:30` both import
`OrgSettings`. Leave them broken **only** until Tasks 6 and 7 — instead, to
keep the tree importable, change each import now to
`OrgIntegrationCredential` and stub the two readers:

In `src/usali/crm_api.py`, replace the body of `_active_org_crm_provider`:

```python
def _active_org_crm_provider(session: Session) -> str:
    """The demand provider for the request's ACTIVE org. OH-17 Task 6 replaces
    this with integrations.connected_provider; this interim body preserves the
    exact semantics (row absent => '' => feature OFF)."""
    row = session.execute(
        select(OrgIntegrationCredential.provider).where(
            OrgIntegrationCredential.integration == "demand_feed"
        )
    ).scalar_one_or_none()
    return row or ""
```

In `src/usali/checklist.py`, replace `_probe_demand_feed`:

```python
def _probe_demand_feed(session: Session) -> bool:
    row = session.execute(
        select(OrgIntegrationCredential.provider).where(
            OrgIntegrationCredential.integration == "demand_feed"
        )
    ).scalar_one_or_none()
    return bool(row)
```

Update both import lines to name `OrgIntegrationCredential` instead of
`OrgSettings`.

- [ ] **Step 5: Update the model-registry literal**

`tests/test_models.py:33` — replace `"org_settings",` with
`"org_integration_credential",`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_integrations.py tests/test_models.py -v`
Expected: PASS. (`tests/test_l2_rls_wall.py` will still fail until Task 2 —
that is expected and is fixed there.)

- [ ] **Step 7: Commit**

```bash
git add src/usali/models.py src/usali/crm_api.py src/usali/checklist.py \
        tests/test_models.py tests/test_integrations.py
git commit -m "feat(oh17): org_integration_credential model, retiring OrgSettings"
```

---

## Task 2: The migration, the seed bridge, and the `OrgSettings` call-site migration

> **Amended mid-execution (2026-08-30).** As first written this task was the
> migration alone, and the seed bridge was Task 8. That sequencing was WRONG:
> `tests/conftest.py` imports `ensure_default_org` from
> `mapping/property_registry.py`, which imports `OrgSettings` — so from the
> moment Task 1 deleted that model, **the entire suite failed to collect**, not
> merely the RLS tests Task 1 predicted. Tasks 2-7 would each have run blind.
> The seed bridge (old Task 8) and the six test files that construct
> `OrgSettings` are therefore folded in here, because "the schema change lands
> everywhere and the suite is green again" is one job, not two. Old Task 8 is
> now a pointer to this task.
>
> **Execution notes (2026-08-30), recorded because they contradict the steps
> below.** Four things in this task were wrong as written:
> 1. `tests/orgworld.py:29` is a SECOND collection-breaker — `conftest.py`
>    imports `build_two_tenant_world` from it too. Steps 1-2 cannot fail for
>    their stated reason until BOTH it and `property_registry.py` are fixed,
>    so Step 8 and the orgworld half of Step 10 have to run first.
> 2. Step 7 is unreachable for the same reason: by the time collection works
>    the seed is already rewritten, so the seed tests pass on first run.
>    Mutation testing was substituted — each test was proved to bite by
>    breaking the seed three different ways and watching the right test fail.
> 3. Step 9 names `tests/test_crm_api.py`, **which does not exist**. The crm
>    seam coverage lives in `test_j4_crm_pull.py`, `test_j5_demand_surface.py`
>    and `test_l5_per_org_stores.py`.
> 4. Step 11 under-enumerated: dropping the table also invalidated
>    `tests/test_j3_crm_adapters.py:353` and
>    `tests/test_l1_org_wall_migration.py:176`. Both fixed.
>
> Also load-bearing and easy to miss: `Settings.qbo_realm_id` /
> `qbo_refresh_token` default to `"mock"`, not `None`. Had either been `None`,
> the CHECK's `realm_id IS NOT NULL` would make `ensure_default_org` raise in
> every test using the `founding_org` fixture.

**Files:**
- Create: `migrations/versions/b3a0integcred_org_integration_credential.py`
- Modify: `tests/test_l4_org_grants.py:358`
- Modify: `tests/test_l2_rls_wall.py:449-451`
- Modify: `src/usali/mapping/property_registry.py:23`, `:109-121` (the seed)
- Modify: `src/usali/crm_feed.py:50` (a comment naming the retired table)
- Modify: the six test files that construct `OrgSettings` — `tests/orgworld.py:29,52`,
  `tests/test_l5_per_org_stores.py:47,217`, `tests/test_j4_crm_pull.py:38,55`,
  `tests/test_checklist.py:10,184,187`, `tests/test_l7_two_org_walk.py:35,59,61,74`,
  `tests/test_j5_demand_surface.py:29,39`
- Test: `tests/test_property_registry.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_integrations.py`:

```python
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def test_the_check_refuses_a_gusto_row_carrying_an_api_key(db_session, founding_org):
    """D-OH17.5: the DB refuses a malformed credential row independently of
    the app import. The 'must be NULL' half is what stops a stale api_key
    surviving a switch from Tripleseat to Delphi."""
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, api_token, company_id, api_key, connected_by) "
            "VALUES (1, 'payroll', 'gusto', 'x', 'c1', 'leftover', 'sub')"
        ))
        db_session.flush()


def test_the_check_refuses_a_row_with_no_secret(db_session, founding_org):
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, connected_by) "
            "VALUES (1, 'demand_feed', 'delphi', 'sub')"
        ))
        db_session.flush()


def test_the_check_refuses_a_provider_from_another_integration(db_session, founding_org):
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, realm_id, refresh_token, connected_by) "
            "VALUES (1, 'demand_feed', 'qbo', 'r1', 'tok', 'sub')"
        ))
        db_session.flush()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integrations.py -k check_refuses -v`
Expected: FAIL — `relation "org_integration_credential" does not exist`

- [ ] **Step 3: Write the migration**

Create `migrations/versions/b3a0integcred_org_integration_credential.py`:

```python
"""OH-17: per-tenant integration credentials, and the retirement of org_settings.

The row IS the connection (D-OH17.1): provider AND credentials together, so a
tenant cannot pick a provider without supplying credentials for it. That
absorbs `org_settings.crm_provider` — its only column — so `org_settings` is
dropped here rather than left standing empty.

The org_settings rows are NOT carried forward (D-OH17.14). The matching secret
lives in env, and a data migration that reads env is fragile. Safe by
enumeration, not assumption: the only writer was `ensure_default_org` for org
1, and no SPA page ever wrote crm_provider — so the only row that can exist is
org 1's, which the seed reconstructs from the same env. This is the posture
l5a0orgsettings' own downgrade already recorded: "pure config a re-seed
reconstructs from env/operator input — not the I6 carry-rows-through case."

Joins the L2 database wall on the same terms as every other org-scoped table:
ENABLE/FORCE ROW LEVEL SECURITY plus the `org_wall` policy keyed on the
transaction-local `app.org_id`, predicate reused verbatim from l2a0rlswall so
the two cannot drift. No GRANT — the DEFAULT PRIVILEGES l2a0rlswall recorded
cover future tables.

The CHECK is the schema mirror of `usali.integrations.PROVIDERS`, kept literal
so the DB refuses a malformed row independently of the app import.
"""

from alembic import op
import sqlalchemy as sa

from usali.tenancy import RLS_ORG_VAR

revision = "b3a0integcred"
down_revision = "b2a0checklist"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"

_CHECK = (
    "(integration = 'payroll' AND provider = 'gusto'"
    "  AND api_token IS NOT NULL AND company_id IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND subscription_key IS NULL AND api_key IS NULL)"
    " OR (integration = 'payroll' AND provider = 'adp'"
    "  AND client_id IS NOT NULL AND client_secret IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND subscription_key IS NULL AND api_key IS NULL)"
    " OR (integration = 'accounting' AND provider = 'qbo'"
    "  AND refresh_token IS NOT NULL AND realm_id IS NOT NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND subscription_key IS NULL AND api_key IS NULL)"
    " OR (integration = 'demand_feed' AND provider = 'delphi'"
    "  AND subscription_key IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND api_key IS NULL)"
    " OR (integration = 'demand_feed' AND provider = 'tripleseat'"
    "  AND api_key IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND subscription_key IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "org_integration_credential",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey(
                "organization.org_id", name="fk_org_integration_credential_org"
            ),
            primary_key=True,
        ),
        sa.Column("integration", sa.String(length=20), primary_key=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("realm_id", sa.String(length=64), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("api_token", sa.Text(), nullable=True),
        sa.Column("company_id", sa.String(length=64), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("subscription_key", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # String(64), matching every other actor column in models.py
        # (created_by, enrolled_by, approved_by, ...). A Keycloak subject is a
        # UUID, so 64 is ample.
        sa.Column("connected_by", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            _CHECK, name="ck_org_integration_credential_provider_fields"
        ),
    )
    op.execute("ALTER TABLE org_integration_credential ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_integration_credential FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON org_integration_credential "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )

    # D-OH17.1 / D-OH17.14: crm_provider is absorbed, rows are not carried.
    op.execute(f"DROP POLICY {_POLICY} ON org_settings")
    op.drop_table("org_settings")


def downgrade() -> None:
    op.create_table(
        "org_settings",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_org_settings_org"),
            primary_key=True,
        ),
        sa.Column(
            "crm_provider",
            sa.String(length=20),
            server_default="",
            nullable=False,
        ),
        sa.CheckConstraint(
            "crm_provider IN ('', 'delphi', 'tripleseat')",
            name="ck_org_settings_crm_provider",
        ),
    )
    op.execute("ALTER TABLE org_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON org_settings "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )
    op.execute(f"DROP POLICY {_POLICY} ON org_integration_credential")
    op.drop_table("org_integration_credential")
```

Note the encrypted columns are `sa.Text()` in the migration: `EncryptedString`
is a `TypeDecorator` storing base64 as TEXT (`crypto.py:125`), so the DDL type
is TEXT while the ORM applies the crypto.

- [ ] **Step 4: Update the two hardcoded test literals**

`tests/test_l4_org_grants.py:358`:

```python
    assert ScriptDirectory.from_config(cfg).get_heads() == ["b3a0integcred"]
```

`tests/test_l2_rls_wall.py:449-451` — remove `"org_settings"` and add the new
table, and extend the docstring above it:

```python
    expected = set(_l2.RLS_TABLES) | {
        "room_inventory", "out_of_order_room", "fiscal_calendar",
        "property_stat_config", "ingestion_coverage", "org_checklist_override",
        "org_integration_credential",
    }
```

In the same test's docstring, replace the sentence beginning "L5 adds one more
org-scoped table (org_settings)…" with:

```
    OH-17 replaces org_settings with org_integration_credential
    (b3a0integcred): the credential row absorbed crm_provider, so the old
    table is dropped and the new one carries its own RLS — enumerated here so
    the inventory stays exact, not sampled.
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_integrations.py tests/test_l2_rls_wall.py tests/test_l4_org_grants.py tests/test_migration_on_populated_data.py -v`
Expected: PASS

- [ ] **Step 6: Write the failing test**

Add to `tests/test_property_registry.py`:

```python
def test_the_seed_writes_a_payroll_row_under_the_bare_defaults(db_session):
    """D-OH17.15. scripts/e2e_backend.py:399 relies on the Gusto defaults
    being a WORKING local config with no env set — a seed rule that only
    fired on non-default env would silently break payrun.spec.ts."""
    ensure_default_org(db_session)
    row = db_session.execute(text(
        "SELECT provider FROM org_integration_credential "
        "WHERE org_id = 1 AND integration = 'payroll'"
    )).scalar_one()
    assert row == "gusto"


def test_the_seed_writes_no_demand_feed_row_when_the_provider_is_unset(db_session):
    """'' is the OFF sentinel, and it stays off — demo.sh sets it explicitly."""
    ensure_default_org(db_session)
    assert db_session.execute(text(
        "SELECT count(*) FROM org_integration_credential "
        "WHERE integration = 'demand_feed'"
    )).scalar_one() == 0


def test_a_reseed_does_not_overwrite_an_operator_set_row(db_session):
    """The crm_ref find-or-create posture: a bare re-seed must never blank a
    credential an operator connected by hand."""
    ensure_default_org(db_session)
    db_session.execute(text(
        "UPDATE org_integration_credential SET company_id = 'operator-chosen' "
        "WHERE integration = 'payroll'"
    ))
    db_session.commit()
    ensure_default_org(db_session)
    assert db_session.execute(text(
        "SELECT company_id FROM org_integration_credential "
        "WHERE integration = 'payroll'"
    )).scalar_one() == "operator-chosen"
```

- [ ] **Step 7: Run test to verify it fails**

Run: `uv run pytest tests/test_property_registry.py -k seed -v`
Expected: FAIL — zero rows; the seed still writes `org_settings`

- [ ] **Step 8: Replace the seed block**

In `src/usali/mapping/property_registry.py`, change the import at `:23` from
`OrgSettings` to `OrgIntegrationCredential`, and replace the
`insert(OrgSettings)` block (`:109-121`) with:

```python
    # OH-17 (D-OH17.15): seed org 1's integration credentials from the
    # process-wide env, ON FIRST INSERT ONLY — the crm_ref / wage_jurisdiction
    # find-or-create posture, so a bare re-seed never blanks a credential an
    # operator connected by hand. At runtime every adapter reads THIS row,
    # never env; an env fallback for org != 1 is the mutant L5 killed.
    #
    # This is a BRIDGE, not a connect action: it reproduces exactly what each
    # default means today and does NOT run the connect endpoint's verification.
    # Do NOT "improve" it into seeding only when env differs from the committed
    # mock defaults — scripts/e2e_backend.py:399 states that the Gusto defaults
    # ARE the working local config with no env set, and that rule would break
    # payrun.spec.ts silently.
    settings = get_settings()
    seeds: list[dict[str, Any]] = [
        {
            "org_id": org_id, "integration": "payroll",
            "provider": settings.payroll_provider,
            "connected_by": _SEED_SUBJECT,
            **(
                {"api_token": settings.gusto_api_token,
                 "company_id": settings.gusto_company_id}
                if settings.payroll_provider == "gusto"
                else {"client_id": settings.adp_client_id,
                      "client_secret": settings.adp_client_secret}
            ),
        },
        {
            "org_id": org_id, "integration": "accounting", "provider": "qbo",
            "realm_id": settings.qbo_realm_id,
            "refresh_token": settings.qbo_refresh_token,
            "connected_by": _SEED_SUBJECT,
        },
    ]
    # The demand feed keeps its OFF sentinel: '' means no row at all, so an
    # unset USALI_CRM_PROVIDER still produces demo_seed.py's honest "skipped"
    # note rather than a connection to nothing.
    if settings.crm_provider == "delphi":
        seeds.append({
            "org_id": org_id, "integration": "demand_feed", "provider": "delphi",
            "subscription_key": settings.delphi_subscription_key,
            "connected_by": _SEED_SUBJECT,
        })
    elif settings.crm_provider == "tripleseat":
        seeds.append({
            "org_id": org_id, "integration": "demand_feed",
            "provider": "tripleseat", "api_key": settings.tripleseat_api_key,
            "connected_by": _SEED_SUBJECT,
        })
    for values in seeds:
        session.execute(
            insert(OrgIntegrationCredential)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["org_id", "integration"])
        )
```

Add near the module constants:

```python
# The `connected_by` recorded for env-seeded rows: no human connected these,
# and attributing them to one would be a lie in the audit trail.
_SEED_SUBJECT = "seed:env"
```

Add `from typing import Any` to the imports if absent.

- [ ] **Step 9: Run the tests**

Run: `uv run pytest tests/test_property_registry.py tests/test_crm_api.py -v`
Expected: PASS

- [ ] **Step 10: Migrate the six test files that construct `OrgSettings`**

Each sets org 1's `crm_provider`; under the new schema that means inserting or
removing a `demand_feed` credential row. Add ONE shared helper rather than six
open-coded inserts — `tests/orgworld.py` is the natural home since it already
holds the two-org fixtures:

```python
def set_demand_feed(session, provider: str, *, org_id: int = 1) -> None:
    """Point one org's demand feed at `provider`, or disconnect it when
    `provider` is ''. The OH-17 replacement for `update(OrgSettings)
    .values(crm_provider=...)`: the credential row IS the connection, so
    'off' is the ABSENCE of a row rather than an empty string."""
    session.execute(
        delete(OrgIntegrationCredential).where(
            OrgIntegrationCredential.org_id == org_id,
            OrgIntegrationCredential.integration == "demand_feed",
        )
    )
    if provider:
        secret = ("subscription_key" if provider == "delphi" else "api_key")
        session.add(OrgIntegrationCredential(
            org_id=org_id, integration="demand_feed", provider=provider,
            connected_by="test", **{secret: "mock"},
        ))
    session.flush()
```

Then repoint the six call sites. Note `tests/test_l7_two_org_walk.py:61,74`
COUNT `OrgSettings` rows to assert per-org isolation — count
`OrgIntegrationCredential` rows scoped to `integration == "demand_feed"`
instead, so the assertion keeps meaning the same thing.

- [ ] **Step 11: Fix the two stale references to the retired table**

`src/usali/crm_feed.py:50` and the docstring of `build_two_tenant_world`
(`tests/orgworld.py:47`, "org 1 already exists with its `org_settings` row").

`crm_feed.py:50` names "the org_settings CHECK (models.OrgSettings and the
l5a0orgsettings migration)" as the schema mirror of `CRM_PROVIDERS`. That table
is gone; the mirror is now `ck_org_integration_credential_provider_fields` on
`org_integration_credential`. A comment pointing at a dropped table is worse
than none.

- [ ] **Step 12: Run the FULL suite — it must collect and pass**

Run: `uv run pytest -q && uv run mypy src && uv run ruff check src tests`
Expected: PASS. This is the task's real acceptance criterion: the suite has
not been collectable since Task 1, and this is where that ends.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/b3a0integcred_org_integration_credential.py \
        tests/ src/usali/mapping/property_registry.py src/usali/crm_feed.py
git commit -m "feat(oh17): b3a0integcred migration, seed bridge, and call-site migration"
```

---

## Task 3: The provider registry

**Files:**
- Create: `src/usali/integrations.py`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_integrations.py`:

```python
from usali import integrations as integ


def test_every_spec_names_at_least_one_secret():
    for spec in integ.PROVIDERS:
        assert spec.secret_fields, spec.provider


def test_specs_cover_exactly_the_three_integrations():
    assert {s.integration for s in integ.PROVIDERS} == set(integ.INTEGRATIONS)


def test_spec_for_is_keyed_on_the_pair_not_the_provider_alone():
    """'qbo' is only legal under 'accounting' — the pair is the key, which is
    what the DB CHECK also enforces."""
    assert integ.spec_for("accounting", "qbo") is not None
    assert integ.spec_for("demand_feed", "qbo") is None


def test_the_registry_mirrors_the_crm_provider_closed_set():
    """crm_feed.CRM_PROVIDERS stays the source for demand-feed provider names;
    a new adapter must not be reachable here without being added there."""
    from usali.crm_feed import CRM_PROVIDERS
    feed = {s.provider for s in integ.PROVIDERS if s.integration == integ.DEMAND_FEED}
    assert feed == set(CRM_PROVIDERS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integrations.py -k registry or spec -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usali.integrations'`

- [ ] **Step 3: Write the registry**

Create `src/usali/integrations.py`:

```python
"""Per-tenant integration credentials and adapter resolution (OH-17).

The ONE place that answers "what is this tenant connected to, and with what?"
Every adapter in the app is built from here, from the active org's
`org_integration_credential` row — never from process-wide `Settings`, which
holds only deployment config now (base URLs, and our own Intuit application
id/secret). A process-wide credential is not THIS tenant's connection.

`PROVIDERS` is the closed set, the `CRM_PROVIDERS` idiom: one place to read
which credentials each provider needs. It is MIRRORED by the CHECK on
`org_integration_credential` (models.py + the b3a0integcred migration) so the
DB refuses a malformed row independently of this import. Adding a provider
means editing PROVIDERS *and* that literal plus its migration.

The `resolve_*` functions take an org-bound SESSION FACTORY, not a session:
`resolve_qbo` hands the same factory to `DbTokenStore`, which must write the
rotated refresh token back in its own short transaction (D-OH17.7). All three
take the same shape so no call site has to remember which is which.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import OrgIntegrationCredential

PAYROLL = "payroll"
ACCOUNTING = "accounting"
DEMAND_FEED = "demand_feed"

# The schema mirror of `org_integration_credential.integration`'s legal set,
# which is itself the three integration keys in `usali.checklist.ITEMS`.
INTEGRATIONS: tuple[str, ...] = (PAYROLL, ACCOUNTING, DEMAND_FEED)


@dataclass(frozen=True)
class ProviderSpec:
    """What one provider needs on its credential row.

    `secret_fields` are the EncryptedString columns and are NEVER returned on
    the wire; `plain_fields` are identifiers (a realm, a company id) that are
    not secrets and that the read endpoint does echo, because being able to
    see which QBO company a tenant is pointed at is the whole value of the
    read surface."""

    integration: str
    provider: str
    secret_fields: tuple[str, ...]
    plain_fields: tuple[str, ...]

    @property
    def fields(self) -> tuple[str, ...]:
        return self.secret_fields + self.plain_fields


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(PAYROLL, "gusto", ("api_token",), ("company_id",)),
    ProviderSpec(PAYROLL, "adp", ("client_secret",), ("client_id",)),
    ProviderSpec(ACCOUNTING, "qbo", ("refresh_token",), ("realm_id",)),
    ProviderSpec(DEMAND_FEED, "delphi", ("subscription_key",), ()),
    ProviderSpec(DEMAND_FEED, "tripleseat", ("api_key",), ()),
)

# Every credential column, so a write can null out the ones its provider does
# not use. Derived rather than repeated: a hand-written second list is exactly
# the drift the CHECK's "must be NULL" half exists to catch, and it would be
# caught only at the DB, one layer too late to give a good error.
ALL_CREDENTIAL_FIELDS: tuple[str, ...] = tuple(
    sorted({f for spec in PROVIDERS for f in spec.fields})
)


def spec_for(integration: str, provider: str) -> ProviderSpec | None:
    """The spec for one (integration, provider) PAIR, or None if illegal.

    Keyed on the pair, never the provider alone: 'qbo' is legal under
    'accounting' and nowhere else, which is the same rule the DB CHECK
    enforces."""
    for spec in PROVIDERS:
        if spec.integration == integration and spec.provider == provider:
            return spec
    return None


def credential_for(
    session: Session, integration: str
) -> OrgIntegrationCredential | None:
    """The active org's row for one integration, or None.

    The session is org-bound, so both L2 walls confine this SELECT to exactly
    the active org — there is no org_id parameter to pass wrong, and no env
    fallback for org != 1 in particular (the mutant L5 killed). The WHERE
    narrows to the integration only; the org half is the walls'."""
    return session.execute(
        select(OrgIntegrationCredential).where(
            OrgIntegrationCredential.integration == integration
        )
    ).scalar_one_or_none()


def has_credential(session: Session, integration: str) -> bool:
    """Is this integration connected for the active org?

    The checklist probe (D-OH17.8). Deliberately a PRESENCE check and not a
    live provider call: the checklist is read on every page load via the
    sidebar badge, so a probe that dialled out would put two-to-five outbound
    calls on the SPA's critical path and paint the page red during any
    provider outage. Honesty is enforced on the WRITE path instead — a
    credential that does not authenticate never becomes a row."""
    return credential_for(session, integration) is not None


def connected_provider(session: Session, integration: str) -> str:
    """The provider name, or '' when not connected. '' degrades exactly as the
    old `org_settings.crm_provider` OFF sentinel did."""
    row = credential_for(session, integration)
    return row.provider if row is not None else ""
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_integrations.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/usali/integrations.py tests/test_integrations.py
git commit -m "feat(oh17): the provider registry and credential lookup"
```

---

## Task 4: `QboClient` gains a `TokenStore` port

**Files:**
- Modify: `src/usali/qbo_client.py:112-160` (docstring + `__init__`), `:170-180` (`_refresh`)
- Modify: every construction site — `src/usali/server.py:130-139`, and the test/CLI callers found by the grep in Step 3
- Test: `tests/test_qbo_push.py`

> **Corrected before dispatch (2026-08-30).** This task originally named
> `tests/test_qbo_client.py` and helpers `qbo_mock_url` / `_minimal_je`. **None
> of those exist** — the same defect class as Task 2's `test_crm_api.py`. The
> real QBO client tests live in `tests/test_qbo_push.py`, whose fixtures are
> `mock_app` (an in-process ASGI mock, not a URL) and `qbo`, with
> `_bootstrap_refresh_token(mock_app)` taking the app rather than a URL. A
> journal entry is posted through `push_day(db_session, qbo, property_id=...,
> business_date=DAY)`, not a bare `post_journal_entry`. The code below is
> rewritten against those.
>
> **Corrected AGAIN during execution.** That rewrite was still wrong, in the
> way that matters most: it pushed `HISJ` twice, but `push_day`
> (`qbo_push.py:305-307`) short-circuits on an already-pushed ledger row with a
> matching `request_hash` and **returns before ever calling the client**. The
> rebuilt client would never have refreshed — never opened a connection — so
> the test would have passed against an implementation that dropped the
> rotation entirely. A vacuous test for the exact bug the task exists to fix.
> (The tuple also contained `"already_pushed"`; the real literal is
> `"already-pushed"`, with a hyphen.) The shipped test pushes **`SSSJ`**, a
> genuinely new (property, date) that reaches QBO, asserts `== "pushed"`, and
> pins the rotation COUNT — so it fails if no refresh happened. Do not
> "simplify" it back to one property.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_qbo_push.py`:

```python
class RecordingTokenStore:
    """A TokenStore that keeps its value, so a test can rebuild a client the
    way a NEW PROCESS would and prove the rotation lineage survived."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.stores: list[str] = []

    def load(self) -> str:
        return self.token

    def store(self, refresh_token: str) -> None:
        self.token = refresh_token
        self.stores.append(refresh_token)


def _client_on(mock_app: Any, store: Any) -> QboClient:
    return QboClient(
        "http://mock-qbo", "client", "secret", REALM, store,
        transport=SyncASGITransport(mock_app),
    )


def test_rotation_is_written_through_to_the_store(db_session, seed_six_pdfs, mock_app):
    """D-OH17.7. The lazy first refresh consumes and rotates the bootstrap
    token, so the store must have been written or the NEXT refresh is dead."""
    store = RecordingTokenStore(_bootstrap_refresh_token(mock_app))
    result = push_day(db_session, _client_on(mock_app, store),
                      property_id="HISJ", business_date=DAY)
    assert result.status == "pushed"
    assert store.stores, "the rotated refresh token was never persisted"


def test_a_rebuilt_client_can_still_refresh(db_session, seed_six_pdfs, mock_app):
    """The regression test for the bug `qbo_client.py:112` documents against
    itself: rotation used to live in client memory, so a process restart lost
    it and the next push invalid_grant'd against a token Intuit had already
    consumed. Rebuilding from the SAME store is exactly what a restart does."""
    store = RecordingTokenStore(_bootstrap_refresh_token(mock_app))
    first = push_day(db_session, _client_on(mock_app, store),
                     property_id="HISJ", business_date=DAY)
    assert first.status == "pushed"

    # A fresh client, as a restarted process would build: same store, new
    # instance, no in-memory state carried over.
    rebuilt = _client_on(mock_app, store)
    rebuilt._http.headers["X-Mock-Fault"] = "expired-once:test-401"
    second = push_day(db_session, rebuilt, property_id="HISJ",
                      business_date=DAY)
    assert second.status in ("pushed", "already_pushed")


def test_static_store_keeps_the_old_in_memory_behaviour():
    store = StaticTokenStore("tok")
    assert store.load() == "tok"
    store.store("rotated")
    assert store.load() == "rotated"
```

Import `StaticTokenStore` alongside the existing `QboClient` import. The
`X-Mock-Fault: expired-once` header is the existing idiom for forcing a 401 →
refresh → retry (`tests/test_qbo_push.py:319`); it is what makes the rebuilt
client actually exercise a refresh rather than riding a cached access token.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_qbo_push.py -k rotation or rebuilt or static_store -v`
Expected: FAIL — `ImportError: cannot import name 'StaticTokenStore'`

- [ ] **Step 3: Add the port and rewire `__init__` / `_refresh`**

In `src/usali/qbo_client.py`, add above `class QboClient`:

```python
class TokenStore(Protocol):
    """Where one tenant's QBO refresh token lives across calls (OH-17,
    D-OH17.7).

    Intuit rotates the refresh token on EVERY grant, so whoever holds it must
    be able to write the new one back. Before OH-17 that holder was process
    memory, which meant a restart lost the rotation and the next push
    invalid_grant'd against a spent token. The DB-backed implementation
    (`usali.integrations.DbTokenStore`) makes the lineage per-tenant and
    durable; `StaticTokenStore` preserves the old in-memory behaviour for the
    mock and for tests."""

    def load(self) -> str: ...
    def store(self, refresh_token: str) -> None: ...


class StaticTokenStore:
    """In-memory token store — dev, tests, and the `usali qbo-mock` loop.
    Rotation survives for the client's lifetime and no longer."""

    def __init__(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token

    def load(self) -> str:
        return self._refresh_token

    def store(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token
```

Add `Protocol` to the `typing` import at `qbo_client.py:25`.

Change `QboClient.__init__`'s fifth parameter from `refresh_token: str` to
`token_store: TokenStore`, and its body from
`self._refresh_token = refresh_token` to `self._tokens = token_store`.

Replace the two `_refresh` lines:

```python
        resp = self._http.post(
            _TOKEN_PATH,
            data={"grant_type": "refresh_token", "refresh_token": self._tokens.load()},
            headers={"Authorization": self._basic_auth},
        )
```

and

```python
        self._access_token = payload["access_token"]
        # Intuit rotates the refresh token on every grant; persist the new one
        # or the NEXT refresh fails with invalid_grant. Through the store, so
        # the lineage outlives this process (D-OH17.7) — `post_journal_entry`
        # already holds the instance lock around this, and DbTokenStore takes
        # a row lock, which is what serializes ACROSS processes.
        self._tokens.store(payload["refresh_token"])
```

Update the class docstring's "persisted IN MEMORY ONLY" paragraph to describe
the port, keeping the thread-safety paragraph as is.

- [ ] **Step 4: Update every construction site**

Run: `grep -rn "QboClient(" src tests scripts`

Wrap each existing `refresh_token` argument in `StaticTokenStore(...)`. The
one in `src/usali/server.py:130-139` is replaced wholesale in Task 6 — leave
it compiling here by wrapping it the same way.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_qbo_push.py tests/test_portal_qbo_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/usali/qbo_client.py tests/test_qbo_push.py
git commit -m "feat(oh17): TokenStore port so QBO rotation can outlive the process"
```

---

## Task 5: Adapter resolution

> **Open design question, raised by Task 4's review (2026-08-30) — settle it,
> do not inherit it.** D-OH17.7 says `DbTokenStore` takes `SELECT … FOR UPDATE`
> so two concurrent pushes cannot fork the rotation lineage across processes.
> A row lock taken *inside* `store()` **cannot** deliver that: the critical
> section is `load()` → HTTP grant → `store()`, and a lock acquired in `store()`
> covers only the tail. Holding it across the grant requires `load()` to open a
> transaction that `store()` commits — i.e. a transaction held open across an
> outbound HTTP call, which needs a statement timeout and is invisible from
> `QboClient`.
>
> Pick one and say which in the code:
> **(a)** `load()` opens the transaction and takes `FOR UPDATE`; `store()`
> commits it. Honours D-OH17.7, at the cost of a transaction spanning the
> grant — set a lock timeout and document why the span is deliberate.
> **(b)** Drop the cross-process serialization claim. A concurrent double
> refresh is then possible and the loser gets `invalid_grant` on its next push
> — recoverable, since the winner's token is in the row. Correct the D-OH17.7
> wording and the `_refresh` comment in `qbo_client.py` to match.
>
> (a) is what the design promised; (b) is simpler and may be enough for pilot
> throughput. Either is defensible — shipping the promise without the mechanism
> is not.


**Files:**
- Modify: `src/usali/integrations.py`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_integrations.py`:

```python
def test_resolve_returns_none_when_not_connected(org_bound_factory):
    assert integ.resolve_payroll(org_bound_factory) is None
    assert integ.resolve_qbo(org_bound_factory) is None
    assert integ.resolve_crm_feed(org_bound_factory) is None


def test_resolve_payroll_builds_the_named_adapter(org_bound_factory, db_session):
    _connect(db_session, "payroll", "gusto", api_token="t", company_id="c")
    provider = integ.resolve_payroll(org_bound_factory)
    assert type(provider).__name__ == "GustoAdapter"


def test_resolve_crm_feed_builds_the_named_adapter(org_bound_factory, db_session):
    _connect(db_session, "demand_feed", "tripleseat", api_key="k")
    assert type(integ.resolve_crm_feed(org_bound_factory)).__name__ == "TripleseatAdapter"


def test_credentials_are_encrypted_at_rest(db_session, founding_org):
    """ADR-005: a DB dump must not yield the token. The ORM decrypts, so the
    assertion reads the raw column."""
    _connect(db_session, "demand_feed", "delphi", subscription_key="s3cret")
    db_session.commit()
    raw = db_session.execute(text(
        "SELECT subscription_key FROM org_integration_credential "
        "WHERE integration = 'demand_feed'"
    )).scalar_one()
    assert raw != "s3cret"
    assert "s3cret" not in raw
```

Add the shared helper to the same file:

```python
def _connect(session, integration, provider, **fields):
    """Insert a credential row directly, bypassing the API — for tests about
    resolution rather than about the write path."""
    session.add(OrgIntegrationCredential(
        org_id=1, integration=integration, provider=provider,
        connected_by="test-subject", **fields,
    ))
    session.flush()
```

`org_bound_factory` is an org-bound `SessionFactory` for org 1. If
`tests/conftest.py` has no such fixture, add one built from the existing
`db_engine` with `OrgBoundSessionFactory(make_session_factory(db_engine), 1)`,
following how `tests/test_l3_active_org.py` builds one.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integrations.py -k resolve -v`
Expected: FAIL — `AttributeError: module 'usali.integrations' has no attribute 'resolve_payroll'`

- [ ] **Step 3: Implement resolution**

Append to `src/usali/integrations.py`:

```python
class DbTokenStore:
    """The QBO refresh token, held on the tenant's credential row (D-OH17.7).

    Each call opens its OWN short session off the org-bound factory rather
    than joining the caller's request transaction: a push holds its
    transaction for the whole HTTP call, and taking the row lock for that long
    would serialize unrelated work. `load` takes `FOR UPDATE`, so two
    concurrent pushes — in one process or across workers — cannot both spend
    the same token and hand one of them a spurious invalid_grant. The
    in-process `threading.Lock` on QboClient cannot do that; only the row lock
    reaches across processes."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    def load(self) -> str:
        with self._factory() as session:
            token = session.execute(
                select(OrgIntegrationCredential.refresh_token)
                .where(OrgIntegrationCredential.integration == ACCOUNTING)
                .with_for_update()
            ).scalar_one_or_none()
            if token is None:
                raise IntegrationNotConfigured(ACCOUNTING)
            return token

    def store(self, refresh_token: str) -> None:
        with self._factory() as session:
            session.execute(
                update(OrgIntegrationCredential)
                .where(OrgIntegrationCredential.integration == ACCOUNTING)
                .values(refresh_token=refresh_token)
            )
            session.commit()


class IntegrationNotConfigured(Exception):
    """This tenant has no credential for the integration. Raised only where a
    caller has already decided a connection must exist; the `resolve_*`
    functions return None instead, because "not connected" is an ordinary
    state their callers refuse loudly on their own terms."""

    def __init__(self, integration: str) -> None:
        super().__init__(f"{integration} is not connected for this tenant")
        self.integration = integration


def resolve_payroll(factory: SessionFactory) -> PayrollProvider | None:
    settings = get_settings()
    with factory() as session:
        row = credential_for(session, PAYROLL)
        if row is None:
            return None
        if row.provider == "gusto":
            return GustoAdapter(
                base_url=settings.gusto_base_url,
                api_token=row.api_token or "",
                company_id=row.company_id or "",
            )
        if row.provider == "adp":
            return AdpAdapter(
                base_url=settings.adp_base_url,
                client_id=row.client_id or "",
                client_secret=row.client_secret or "",
            )
        raise RuntimeError(f"unknown payroll provider {row.provider!r}")


def resolve_qbo(factory: SessionFactory) -> QboClient | None:
    """Base URL and OUR Intuit application id/secret stay process-wide
    (D-OH17.3) — they identify the app, not the tenant. Only the realm and the
    rotating refresh token are per-tenant."""
    settings = get_settings()
    with factory() as session:
        row = credential_for(session, ACCOUNTING)
        if row is None:
            return None
        realm_id = row.realm_id or ""
    return QboClient(
        settings.qbo_base_url,
        settings.qbo_client_id,
        settings.qbo_client_secret,
        realm_id,
        DbTokenStore(factory),
    )


def resolve_crm_feed(factory: SessionFactory) -> CrmFeed | None:
    settings = get_settings()
    with factory() as session:
        row = credential_for(session, DEMAND_FEED)
        if row is None:
            return None
        if row.provider == "delphi":
            return DelphiAdapter(
                base_url=settings.delphi_base_url,
                subscription_key=row.subscription_key or "",
            )
        if row.provider == "tripleseat":
            return TripleseatAdapter(
                base_url=settings.tripleseat_base_url,
                api_key=row.api_key or "",
            )
        raise RuntimeError(f"unknown crm provider {row.provider!r}")
```

Add the imports this needs at the top of the module:

```python
from sqlalchemy import select, update

from usali.adp_adapter import AdpAdapter
from usali.config import get_settings
from usali.crm_feed import CrmFeed
from usali.db import SessionFactory
from usali.delphi_adapter import DelphiAdapter
from usali.gusto_adapter import GustoAdapter
from usali.payroll_provider import PayrollProvider
from usali.qbo_client import QboClient
from usali.tripleseat_adapter import TripleseatAdapter
```

If `SessionFactory` is not exported from `usali.db`, import it from wherever
`server.py` imports it — check `grep -n "SessionFactory" src/usali/server.py`
and match that import exactly.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_integrations.py -v && uv run mypy src`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/usali/integrations.py tests/test_integrations.py
git commit -m "feat(oh17): resolve adapters from the tenant's credential row"
```

---

## Task 6: Rewire the call sites

**Files:**
- Modify: `src/usali/server.py:130-139`, `:142-186`, `:200-217`, `:384`, `:431-451`
- Modify: `src/usali/portal_api.py:98-110`
- Modify: `src/usali/payroll_run_api.py:62-64`
- Modify: `src/usali/crm_api.py:59-72` and the 503 text at `:137-141`
- Test: `tests/test_crm_api.py`, `tests/test_payroll_run_api.py`, `tests/test_qbo_push.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_crm_api.py`:

```python
def test_the_refusal_names_the_integrations_page_not_the_env_var(crm_client):
    """OH-17: USALI_CRM_PROVIDER is no longer the switch, so naming it in the
    refusal would send an operator to a lever that does nothing. ADR-010 wants
    a named blocker — it has to name the RIGHT one."""
    resp = crm_client.post("/api/crm/refresh", json={"property": "p1"})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "/integrations" in detail
    assert "USALI_CRM_PROVIDER" not in detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_crm_api.py -k names_the_integrations_page -v`
Expected: FAIL — detail still reads "set USALI_CRM_PROVIDER to delphi or tripleseat"

- [ ] **Step 3: Rewire `server.py`**

Delete `_qbo_client_from_settings` (`:130-139`), `_payroll_provider_from_settings`
(`:187-196`), `_crm_feed_for_provider` (`:200-217`) and `_shared_by_key`
(`:168-186`). Keep `_shared` — the face engine still uses it.

Delete the two fail-fast blocks in `create_app` that validate
`settings.payroll_provider` and `settings.crm_provider` (`:341-352`): neither
is a runtime switch any more, so refusing to boot on them would refuse to boot
on a value nothing reads.

Replace the three `app.state` assignments with factory-shaped seams that take
the request's org-bound factory:

```python
    # Integration seams (OH-17). All three resolve from the ACTIVE ORG's
    # credential row, so they take the request's org-bound session factory
    # rather than being memoized per process. `_shared` is deliberately gone
    # here: it existed because QBO's rotated refresh token lived in client
    # memory, and DbTokenStore moved that lineage into the database. Tests
    # inject a factory returning a fake.
    app.state.get_qbo_client = qbo_client_factory or integrations.resolve_qbo
    app.state.get_payroll_provider = (
        payroll_provider_factory or integrations.resolve_payroll
    )
    app.state.get_crm_feed = crm_feed_factory or integrations.resolve_crm_feed
```

Change the three `create_app` parameter annotations to match:

```python
    qbo_client_factory: Callable[[SessionFactory], QboClient | None] | None = None,
    payroll_provider_factory: Callable[[SessionFactory], PayrollProvider | None] | None = None,
    crm_feed_factory: Callable[[SessionFactory], CrmFeed | None] | None = None,
```

Add `from usali import integrations` to the imports; drop the now-unused
`AdpAdapter`, `GustoAdapter`, `DelphiAdapter`, `TripleseatAdapter` and
`CRM_PROVIDERS` imports if nothing else in `server.py` references them (check
with `grep -n`).

- [ ] **Step 4: Rewire the three consumers**

`src/usali/portal_api.py:98-110` — replace `_get_qbo_client`:

```python
def _get_qbo_client(request: Request) -> QboClient:
    """This tenant's QBO client, built from its own credential row (OH-17).

    No longer one shared instance for the app's lifetime: the refresh-token
    rotation lineage that `_shared` existed to protect now lives on the row
    (D-OH17.7), so a per-request client is correct and a shared one would be
    wrong the moment a second tenant pushed."""
    client: QboClient | None = request.app.state.get_qbo_client(
        request_session_factory(request)
    )
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="QuickBooks Online is not connected for this tenant — "
                   "connect it on /integrations",
        )
    return client
```

`src/usali/payroll_run_api.py:62-64` — replace `_provider`:

```python
def _provider(request: Request) -> PayrollProvider:
    provider: PayrollProvider | None = request.app.state.get_payroll_provider(
        request_session_factory(request)
    )
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="payroll is not connected for this tenant — "
                   "connect it on /integrations",
        )
    return provider
```

`src/usali/crm_api.py` — replace `_active_org_crm_provider` (the interim body
from Task 1) with a delegation, and update both call sites:

```python
def _active_org_crm_provider(session: Session) -> str:
    """The demand provider for the request's ACTIVE org (OH-17). '' when the
    org has no credential row — feature OFF, exactly as the old
    org_settings.crm_provider sentinel degraded."""
    return connected_provider(session, DEMAND_FEED)
```

Both `refresh_demand` and `get_demand` currently call
`request.app.state.get_crm_feed(provider_name)`. Change both to
`request.app.state.get_crm_feed(request_session_factory(request))`.

And the 503 text at `:137-141`:

```python
            refused(
                503,
                "no demand feed is connected for this tenant — connect "
                "Delphi or Tripleseat on /integrations",
            )
```

- [ ] **Step 5: Update the test fakes**

Every test injecting `crm_feed_factory=lambda provider: fake` or
`payroll_provider_factory=lambda: fake` now receives a factory. Run
`grep -rn "crm_feed_factory\|payroll_provider_factory\|qbo_client_factory" tests scripts`
and change each lambda to take one argument and ignore it, e.g.
`crm_feed_factory=lambda _factory: fake`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -q && uv run mypy src`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/usali/server.py src/usali/portal_api.py src/usali/payroll_run_api.py \
        src/usali/crm_api.py tests/
git commit -m "feat(oh17): resolve every integration from the active org"
```

---

## Task 7: The checklist edit, and retiring the tripwire

**Files:**
- Modify: `src/usali/checklist.py:167-186` (probes), `:189-192` (`_OH17_REASON`), `:213-231` (three items)
- Modify: `tests/test_checklist.py:198-224`

- [ ] **Step 1: Write the failing test**

In `tests/test_checklist.py`, DELETE
`test_the_integration_items_have_no_connect_surface_yet` (`:215-224`) and
`test_payroll_and_accounting_ignore_process_wide_settings` (`:198-202`), and
add:

```python
def test_every_item_has_a_connect_surface():
    """D-OH17.12, the mirror image of the tripwire this replaces. OH-17 gave
    all three integration items a real `where`, so a null one now means a
    regression rather than an honest gap."""
    assert [i.key for i in ITEMS if i.where is None] == []


def test_the_integration_items_route_to_integrations():
    by_key = {i.key: i for i in ITEMS}
    for key in ("payroll", "accounting", "demand_feed"):
        assert by_key[key].where == "/integrations"
        assert by_key[key].unavailable_reason is None


def test_payroll_and_accounting_read_the_credential_row(db_session, founding_org):
    """D-OH17.8: the probe is a presence check on what is actually configured
    for THIS tenant — still derived (D-B4.1), never a stored status."""
    assert _status_of(db_session, "payroll") == "open"
    assert _status_of(db_session, "accounting") == "open"
    _connect(db_session, "payroll", "gusto", api_token="t", company_id="c")
    _connect(db_session, "accounting", "qbo", realm_id="r", refresh_token="tok")
    assert _status_of(db_session, "payroll") == "done"
    assert _status_of(db_session, "accounting") == "done"
```

Import `_connect` from `tests/test_integrations.py`, or move it to
`tests/conftest.py` if both modules need it — one helper, not two.

`test_where_and_unavailable_reason_are_paired` STAYS. It is satisfied
vacuously now, and that is the point: it is what stops a future null-`where`
item slipping in unexplained.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checklist.py -v`
Expected: FAIL — `assert ['payroll', 'accounting', 'demand_feed'] == []`

- [ ] **Step 3: Rewrite the three probes**

In `src/usali/checklist.py`, replace the three probe bodies:

```python
def _probe_payroll(session: Session) -> bool:
    """OH-17 (D-OH17.8): the tenant's OWN credential row, not
    `settings.payroll_provider`. A presence check, not a live provider call —
    the checklist is read on every page load via the sidebar badge, and a
    probe that dialled out would put the SPA's critical path behind a provider.
    A credential that does not authenticate never becomes a row, because the
    connect endpoint verifies before it writes."""
    return has_credential(session, PAYROLL)


def _probe_accounting(session: Session) -> bool:
    """D-OH17.8, as `_probe_payroll`."""
    return has_credential(session, ACCOUNTING)


def _probe_demand_feed(session: Session) -> bool:
    return has_credential(session, DEMAND_FEED)
```

Replace the `OrgIntegrationCredential` / `select` imports the interim Task 1
bodies needed with
`from usali.integrations import ACCOUNTING, DEMAND_FEED, PAYROLL, has_credential`,
and drop `select` from the SQLAlchemy import if no other probe uses it (
`_probe_first_report`, `_every_property_has` and `_probe_team` still do — so
keep it).

- [ ] **Step 4: Delete `_OH17_REASON` and restore the three `where` values**

Delete the whole `_OH17_REASON` block (`:189-192`). In `ITEMS`, for each of
`payroll`, `accounting` and `demand_feed`, change
`where=None, probe=..., unavailable_reason=_OH17_REASON` to
`where="/integrations", probe=...` — dropping the `unavailable_reason`
argument entirely so it falls back to its `None` default.

Leave `ChecklistItem.where` and `ItemStatus.where` as `str | None`, and leave
the `unavailable_reason` field in place. D-B4.8's paired invariant is a
permanent property of the registry, not scaffolding for this one gap.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_checklist.py tests/test_checklist_api.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/usali/checklist.py tests/test_checklist.py
git commit -m "feat(oh17): the three integration items get a connect surface"
```

---

## Task 8: The org-1 seed bridge — MERGED INTO TASK 2

Folded into Task 2 mid-execution. See the amendment note there: the suite
could not collect while `property_registry.py` still imported the deleted
`OrgSettings`, so the seed had to land with the migration rather than five
tasks later. Kept as a numbered stub so the task numbering in the commit log
and in this document stay aligned.

---

## Task 9: A real `verify()` on each adapter

**Files:**
- Modify: `src/usali/payroll_provider.py` (protocol + `InMemoryPayrollProvider`)
- Modify: `src/usali/crm_feed.py` (protocol + `InMemoryCrmFeed`)
- Modify: `src/usali/gusto_adapter.py`, `src/usali/adp_adapter.py`,
  `src/usali/delphi_adapter.py`, `src/usali/tripleseat_adapter.py`
- Test: `tests/test_gusto_adapter.py`, `tests/test_adp_adapter.py`,
  `tests/test_delphi_adapter.py`, `tests/test_tripleseat_adapter.py`

**Why this task exists.** D-OH17.8 makes the checklist honest by verifying a
credential before storing it. There is currently **nothing to call**:
`capabilities()` on all four adapters (`gusto_adapter.py:49`,
`adp_adapter.py:62`, `delphi_adapter.py:77`, `tripleseat_adapter.py:72`) is a
local declaration that touches no network, so using it as the verification
would make D-OH17.8 quietly false — a `done` over an unauthenticated
credential, the exact drift the decision exists to stop. Each adapter needs one
real, read-only, authenticated call.

Each provider's cheapest such call differs, and none of them writes:

| Adapter | Verification call |
|---|---|
| ADP | the OAuth client-credentials grant (`_token()`, `adp_adapter.py:99`) — already exists, proves id+secret |
| Gusto | `GET /v1/companies/{company_id}` — proves token AND that the company id is reachable |
| Delphi | a one-day `fetch_demand` against the org's first `crm_ref` |
| Tripleseat | a one-day `fetch_demand` against the org's first `crm_ref` |

- [ ] **Step 1: Write the failing test**

Add to `tests/test_adp_adapter.py`:

```python
def test_verify_succeeds_against_the_mock(adp_mock_url):
    AdpAdapter(base_url=adp_mock_url, client_id="mock", client_secret="mock").verify()


def test_verify_raises_on_bad_credentials(adp_mock_url):
    with pytest.raises(ProviderError):
        AdpAdapter(base_url=adp_mock_url, client_id="wrong", client_secret="wrong").verify()
```

Add the mirror pair to `tests/test_gusto_adapter.py` (bad `api_token`), and to
`tests/test_delphi_adapter.py` / `tests/test_tripleseat_adapter.py` (bad key,
`verify(external_ref=...)`), using each module's existing mock fixture. Do not
build a second mock harness.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_adp_adapter.py -k verify -v`
Expected: FAIL — `AttributeError: 'AdpAdapter' object has no attribute 'verify'`

- [ ] **Step 3: Add `verify` to the two ports**

In `src/usali/payroll_provider.py`, add to the `PayrollProvider` Protocol:

```python
    def verify(self) -> None:
        """Prove these credentials authenticate, reading nothing and writing
        nothing (OH-17, D-OH17.8). Raises ProviderError on failure.

        Exists because the connect endpoint must refuse a credential that
        cannot authenticate, and `capabilities()` is a local declaration that
        would have proved nothing. Read-only by contract: this runs against a
        tenant's real payroll account on a button press, so it must never
        create, update or submit anything."""
        ...
```

and to `InMemoryPayrollProvider`:

```python
    def verify(self) -> None:
        return None
```

In `src/usali/crm_feed.py`, add to the `CrmFeed` Protocol:

```python
    def verify(self, external_ref: str) -> None:
        """Prove these credentials authenticate against one property's feed
        (OH-17, D-OH17.8). Raises CrmFeedError on failure. Needs a ref because
        every real CRM read is property-scoped — there is no account-level
        ping to call instead."""
        ...
```

and to `InMemoryCrmFeed`:

```python
    def verify(self, external_ref: str) -> None:
        return None
```

- [ ] **Step 4: Implement `verify` on the four adapters**

`src/usali/adp_adapter.py` — the grant already exists:

```python
    def verify(self) -> None:
        """The client-credentials grant IS the verification: it proves the id
        and secret authenticate, and it is what every other call does lazily
        anyway. Nothing is read and nothing is written."""
        self._token()
```

`src/usali/gusto_adapter.py`:

```python
    def verify(self) -> None:
        """Read the company the token is scoped to. Proves BOTH halves of the
        credential — a valid token against a company id that is not reachable
        is still a broken connection, and it fails here rather than on the
        tenant's first pay run."""
        try:
            resp = self._http.get(f"/v1/companies/{self._company_id}")
        except httpx.HTTPError as exc:
            raise ProviderError(f"gusto unreachable: {type(exc).__name__}") from exc
        self._raise_for(resp, "verify", include_detail=True)
```

Match `_raise_for`'s real signature at `gusto_adapter.py:74` — read it and pass
what it actually takes. `include_detail=True` is safe here and only here: the
request carries no PII, so unlike the sync path there is no SSN for a
validation error to echo back.

`src/usali/delphi_adapter.py` and `src/usali/tripleseat_adapter.py`:

```python
    def verify(self, external_ref: str) -> None:
        """One narrow demand fetch — the same call the pull will make, over the
        smallest possible window. A provider that answers this will answer the
        real pull."""
        today = date.today()
        self.fetch_demand(external_ref, today, today)
```

- [ ] **Step 5: Add the Gusto mock endpoint**

`src/usali/gusto_mock.py` has no `GET /v1/companies/{id}`. Add one that 200s
for the configured company and 404s otherwise, and 401s on a bad bearer —
mirroring how the mock already treats the token on its other routes.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_gusto_adapter.py tests/test_adp_adapter.py tests/test_delphi_adapter.py tests/test_tripleseat_adapter.py -v && uv run mypy src`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/usali/payroll_provider.py src/usali/crm_feed.py src/usali/gusto_adapter.py \
        src/usali/adp_adapter.py src/usali/delphi_adapter.py \
        src/usali/tripleseat_adapter.py src/usali/gusto_mock.py tests/
git commit -m "feat(oh17): a real verify() on each adapter, so connect can refuse"
```

---

## Task 10: The read / connect / disconnect router

**Files:**
- Create: `src/usali/integrations_api.py`
- Modify: `src/usali/server.py` (include the router)
- Test: `tests/test_integrations_api.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_integrations_api.py`:

```python
def test_read_never_returns_a_secret(integrations_client, db_session):
    _connect(db_session, "payroll", "gusto", api_token="s3cret", company_id="c1")
    db_session.commit()
    body = integrations_client.get("/api/integrations").json()
    payroll = next(i for i in body["items"] if i["integration"] == "payroll")
    assert payroll["connected"] is True
    assert payroll["provider"] == "gusto"
    assert payroll["identifiers"] == {"company_id": "c1"}
    assert "s3cret" not in integrations_client.get("/api/integrations").text


def test_connect_verifies_before_it_persists(integrations_client, failing_verifier):
    """D-OH17.8: a typo'd key must be a 422, not a `done` over an integration
    that 502s on first use. This is the assertion the whole write path exists
    for."""
    resp = integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "delphi", "subscription_key": "wrong"},
    )
    assert resp.status_code == 422
    assert integrations_client.get("/api/integrations").json()["items"]
    body = integrations_client.get("/api/integrations").json()
    feed = next(i for i in body["items"] if i["integration"] == "demand_feed")
    assert feed["connected"] is False


def test_connect_refuses_a_provider_from_another_integration(integrations_client):
    resp = integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "qbo", "realm_id": "r", "refresh_token": "t"},
    )
    assert resp.status_code == 422


def test_disconnect_is_a_noop_on_an_absent_row(integrations_client):
    assert integrations_client.delete("/api/integrations/payroll").status_code == 204


def test_a_non_admin_cannot_connect(integrations_client_gm):
    resp = integrations_client_gm.put(
        "/api/integrations/demand_feed",
        json={"provider": "tripleseat", "api_key": "k"},
    )
    assert resp.status_code == 403
```

Build `integrations_client` / `integrations_client_gm` on the existing
authenticated-client fixture pattern in `tests/test_checklist_api.py`, with an
org_admin and a property_gm principal respectively. `failing_verifier` injects
a verification callable that raises — see Step 3.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integrations_api.py -v`
Expected: FAIL — 404 on every route

- [ ] **Step 3: Write the router**

Create `src/usali/integrations_api.py`:

```python
"""The per-tenant integration connect surface (OH-17).

Its own module rather than more weight on `portal_api` (past 1200 lines), the
call `checklist_api` already made. Every route is org_admin: connecting a
tenant's accounting system is a standing commitment about the TENANT, the same
reasoning that gates checklist dismissal.

NO SECRET IS EVER RETURNED. The read echoes only the non-secret identifiers
(realm, company id, client id) — being able to see WHICH QBO company a tenant
is pointed at is the value of the read surface; being able to read the token
back is only a liability. Re-entering a key is how you change it. This is
ADR-004's blind-read posture applied to a store the server can technically
decrypt.

VERIFY BEFORE PERSIST (D-OH17.8): the checklist probe is a cheap presence
check, so a row that cannot authenticate would be a `done` over an integration
that 502s on first use — the drift D-B4.1 and D8.3 exist to prevent. The
write path is where that is stopped, by making one live provider call.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from usali.auth import ORG_ADMIN, Principal, request_session_factory, require_grants
from usali.crm_feed import CrmFeedError
from usali.integrations import (
    ALL_CREDENTIAL_FIELDS,
    INTEGRATIONS,
    credential_for,
    spec_for,
)
from usali.models import AuditEvent, OrgIntegrationCredential, Property
from usali.payroll_provider import ProviderError
from usali.qbo_client import QboError
from usali.tenancy import current_org_id

router = APIRouter(prefix="/api/integrations")

require_integration_admin = require_grants(ORG_ADMIN)

# What a failed verification can raise. Each adapter's own error type, so a
# provider failure is a 422 while a genuine bug still surfaces as a 500.
_VERIFY_ERRORS = (CrmFeedError, ProviderError, QboError)


def _session(request: Request) -> Session:
    return request_session_factory(request)()


class IntegrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str
    connected: bool
    provider: str | None
    identifiers: dict[str, str]
    connected_at: str | None


class IntegrationsModel(BaseModel):
    items: list[IntegrationModel]


@router.get("")
def get_integrations(request: Request) -> IntegrationsModel:
    with _session(request) as session:
        rows = {i: credential_for(session, i) for i in INTEGRATIONS}
    items = []
    for integration in INTEGRATIONS:
        row = rows[integration]
        if row is None:
            items.append(IntegrationModel(
                integration=integration, connected=False, provider=None,
                identifiers={}, connected_at=None,
            ))
            continue
        spec = spec_for(integration, row.provider)
        # A row whose pair is unknown cannot happen — the DB CHECK refuses it
        # — but reading `spec.plain_fields` off None would be a 500 rather than
        # a legible failure if it ever did.
        plain = spec.plain_fields if spec is not None else ()
        items.append(IntegrationModel(
            integration=integration, connected=True, provider=row.provider,
            identifiers={f: getattr(row, f) for f in plain if getattr(row, f)},
            connected_at=row.connected_at.isoformat(),
        ))
    return IntegrationsModel(items=items)


class ConnectRequest(BaseModel):
    # extra="allow": the credential fields differ per provider, and they are
    # validated against the provider's spec below rather than by a union of
    # five models. An unknown field is refused there, so nothing is smuggled
    # through — the check is just later and gives a better message.
    model_config = ConfigDict(extra="allow")

    provider: str


def _first_crm_ref(session: Session) -> str | None:
    """Any property in this org carrying a crm_ref. Every real CRM read is
    property-scoped, so verification needs one; which property is immaterial,
    because the credential is org-wide."""
    return session.execute(
        select(Property.crm_ref).where(Property.crm_ref.is_not(None)).limit(1)
    ).scalar_one_or_none()


def _verify(
    request: Request, integration: str, provider: str, values: dict[str, Any]
) -> None:
    """One live provider call, so a credential that cannot authenticate never
    becomes a row (D-OH17.8). Injected in tests via app.state.verify_integration."""
    with _session(request) as session:
        crm_ref = _first_crm_ref(session)
    request.app.state.verify_integration(integration, provider, values, crm_ref)


@router.put("/{integration}", status_code=204)
def connect(
    integration: str,
    body: ConnectRequest,
    request: Request,
    principal: Principal = Depends(require_integration_admin),
) -> Response:
    if integration not in INTEGRATIONS:
        raise HTTPException(status_code=404, detail="unknown integration")
    spec = spec_for(integration, body.provider)
    if spec is None:
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider!r} is not a provider for {integration}",
        )
    supplied = body.model_dump(exclude={"provider"})
    missing = [f for f in spec.fields if not supplied.get(f)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider} needs {', '.join(sorted(missing))}",
        )
    unknown = [f for f in supplied if f not in spec.fields]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider} does not take {', '.join(sorted(unknown))}",
        )
    try:
        _verify(request, integration, body.provider, supplied)
    except _VERIFY_ERRORS as exc:
        # The provider's own message only — these adapters are built never to
        # put a response body in an exception (crm_feed.CrmFeedError,
        # payroll_provider.ProviderError both say so), so this cannot leak one.
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider} rejected these credentials: {exc}",
        ) from exc

    # Every field this provider does not use is explicitly nulled: a stale
    # api_key surviving a switch from Tripleseat to Delphi is exactly what the
    # CHECK's "must be NULL" half refuses, and PUT is a full replace.
    values: dict[str, Any] = {f: None for f in ALL_CREDENTIAL_FIELDS}
    values.update({f: supplied[f] for f in spec.fields})
    with _session(request) as session:
        session.execute(
            pg_insert(OrgIntegrationCredential)
            .values(
                org_id=current_org_id(session), integration=integration,
                provider=body.provider,
                connected_at=datetime.now(timezone.utc),
                connected_by=principal.subject, **values,
            )
            .on_conflict_do_update(
                index_elements=["org_id", "integration"],
                set_={
                    "provider": body.provider,
                    "connected_at": datetime.now(timezone.utc),
                    "connected_by": principal.subject, **values,
                },
            )
        )
        session.add(AuditEvent(
            actor_subject=principal.subject, action="integration_connected",
            resource_type="integration", resource_id=integration,
        ))
        session.commit()
    return Response(status_code=204)


@router.delete("/{integration}", status_code=204)
def disconnect(
    integration: str,
    request: Request,
    principal: Principal = Depends(require_integration_admin),
) -> Response:
    """Deleting an absent row is a 204 no-op, matching checklist_api.undismiss."""
    if integration not in INTEGRATIONS:
        raise HTTPException(status_code=404, detail="unknown integration")
    with _session(request) as session:
        session.execute(
            delete(OrgIntegrationCredential).where(
                OrgIntegrationCredential.integration == integration
            )
        )
        session.add(AuditEvent(
            actor_subject=principal.subject, action="integration_disconnected",
            resource_type="integration", resource_id=integration,
        ))
        session.commit()
    return Response(status_code=204)
```

- [ ] **Step 4: Wire the router and the default verifier**

In `src/usali/server.py`, add beside the other routers:

```python
    app.include_router(integrations_router, dependencies=operator_gates)
```

and, beside the other seams:

```python
    # OH-17: the connect-time verification call (D-OH17.8). One live provider
    # call, so a credential that cannot authenticate never becomes a row.
    # Tests inject a fake that raises or passes.
    app.state.verify_integration = (
        verify_integration or integrations.verify_credentials
    )
```

with this added to `create_app`'s signature:

```python
    verify_integration: (
        Callable[[str, str, dict[str, Any], str | None], None] | None
    ) = None,
```

Add to `src/usali/integrations.py`:

```python
def verify_credentials(
    integration: str, provider: str, values: dict[str, Any], crm_ref: str | None
) -> None:
    """Prove a credential authenticates BEFORE it is stored (D-OH17.8).

    Builds the adapter from the supplied values plus deployment config and
    calls its `verify()` (Task 9). Raises the adapter's own error type on
    failure, which the router turns into a 422. Nothing is written — not here,
    and not by any `verify()` it calls.

    Dispatches on the PROVIDER passed in, never on which fields happen to be
    present: the router has already validated the pair against `spec_for`, and
    inferring a provider from its field names would silently pick the wrong
    adapter the first time two providers share a field name."""
    settings = get_settings()
    if provider == "gusto":
        GustoAdapter(
            base_url=settings.gusto_base_url,
            api_token=values["api_token"],
            company_id=values["company_id"],
        ).verify()
        return
    if provider == "adp":
        AdpAdapter(
            base_url=settings.adp_base_url,
            client_id=values["client_id"],
            client_secret=values["client_secret"],
        ).verify()
        return
    if provider in ("delphi", "tripleseat"):
        if crm_ref is None:
            # Every real CRM read is property-scoped, so with no property
            # carrying a crm_ref there is nothing to verify against. Refuse
            # loudly rather than storing an unverified credential and letting
            # the checklist call it done (ADR-010).
            raise CrmFeedError(
                "no property in this workspace has a crm_ref, so the feed "
                "cannot be verified — declare one in mapping/properties.yaml "
                "and re-seed first"
            )
        feed = (
            DelphiAdapter(
                base_url=settings.delphi_base_url,
                subscription_key=values["subscription_key"],
            )
            if provider == "delphi"
            else TripleseatAdapter(
                base_url=settings.tripleseat_base_url,
                api_key=values["api_key"],
            )
        )
        feed.verify(crm_ref)
        return
    # ACCOUNTING/qbo is verified by completing the OAuth grant itself (Task
    # 11) — there is no paste-a-key path that could reach here.
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_integrations_api.py -v && uv run mypy src`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/usali/integrations_api.py src/usali/integrations.py src/usali/server.py \
        tests/test_integrations_api.py
git commit -m "feat(oh17): connect/disconnect endpoints that verify before persisting"
```

---

## Task 11: The QBO OAuth pair

**Files:**
- Modify: `src/usali/crypto.py` (add `oauth_state_key`)
- Modify: `src/usali/config.py` (add `qbo_authorize_url`)
- Modify: `src/usali/integrations_api.py`
- Modify: `src/usali/server.py` (mount the callback outside the operator gates)
- Test: `tests/test_integrations_oauth.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_integrations_oauth.py`:

```python
import time

from usali.integrations_api import sign_state, verify_state


def test_a_forged_state_is_refused():
    assert verify_state("1:sub:9999999999:deadbeef") is None


def test_an_expired_state_is_refused():
    assert verify_state(sign_state(org_id=1, subject="sub", now=time.time() - 3600)) is None


def test_a_valid_state_round_trips():
    assert verify_state(sign_state(org_id=7, subject="sub")) == (7, "sub")


def test_the_callback_writes_under_the_org_named_in_state(oauth_client, db_session):
    """D-OH17.11: the callback has NO bearer token and NO active-org header, so
    `state` is the only carrier of tenant identity. It must therefore be
    unforgeable, and it must bind the row to the org INSIDE it and no other."""
    state = sign_state(org_id=1, subject="admin-sub")
    resp = oauth_client.get(
        f"/api/integrations/accounting/callback?code=good&realmId=r1&state={state}"
    )
    assert resp.status_code in (200, 307)
    row = db_session.execute(text(
        "SELECT org_id, realm_id FROM org_integration_credential "
        "WHERE integration = 'accounting'"
    )).one()
    assert row == (1, "r1")


def test_the_callback_refuses_a_bad_state_without_saying_which_way(oauth_client):
    """Forged, expired and missing must be indistinguishable: the difference
    is an oracle about other tenants' in-flight grants."""
    bad = oauth_client.get(
        "/api/integrations/accounting/callback?code=good&realmId=r1&state=forged"
    )
    assert bad.status_code == 400
    assert bad.json()["detail"] == "invalid authorization state"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integrations_oauth.py -v`
Expected: FAIL — `ImportError: cannot import name 'sign_state'`

- [ ] **Step 3: Add the state key**

In `src/usali/crypto.py`, beside `_PHOTO_HKDF_INFO`:

```python
# Domain-separation label for the OAuth `state` signing key (OH-17,
# D-OH17.11). A fixed info string so this derivation can never collide with
# the photo keys' — same master, different purpose, and the labels are what
# keep them apart.
_OAUTH_STATE_HKDF_INFO = b"usali-integration-oauth-state-v1"


def oauth_state_key() -> bytes:
    """The HMAC key protecting the integration OAuth `state` parameter.

    HKDF-derived from the master field-encryption key rather than configured
    separately (the `_photo_key` precedent): OH-17 introduces no new
    deployment secret, and production already fail-fasts on the committed
    dev-default master."""
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=_OAUTH_STATE_HKDF_INFO,
    ).derive(_key())
```

- [ ] **Step 4: Add the setting**

In `src/usali/config.py`, beside the other QBO fields:

```python
    # Intuit's CONSENT host — a different host from the API base URL above.
    # Defaults to the local mock; go-live points this at
    # https://appcenter.intuit.com/connect/oauth2 via USALI_QBO_AUTHORIZE_URL.
    qbo_authorize_url: str = "http://127.0.0.1:9200/connect/oauth2"
```

The redirect URI is NOT a new setting: it is
`f"{settings.public_base_url}/api/integrations/accounting/callback"`, built
from the existing `public_base_url`, which exists precisely because a request's
Host header is attacker-controlled behind a proxy.

- [ ] **Step 5: Add the two routes**

Append to `src/usali/integrations_api.py`:

```python
_STATE_TTL_SECONDS = 600


def sign_state(*, org_id: int, subject: str, now: float | None = None) -> str:
    """`org_id:subject:expiry:hmac` (D-OH17.11).

    Deliberately NOT single-use against a nonce store. Replay is already dead:
    the other half of the callback is Intuit's `code`, which is single-use AT
    INTUIT, so a replayed state necessarily carries a spent code and the token
    exchange refuses it. A nonce table would add a row, a migration and a
    reaper to re-block something already blocked."""
    expiry = int((now if now is not None else time.time()) + _STATE_TTL_SECONDS)
    payload = f"{org_id}:{subject}:{expiry}"
    mac = hmac.new(oauth_state_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{mac}"


def verify_state(state: str) -> tuple[int, str] | None:
    """(org_id, subject), or None for anything wrong. ONE None for every
    failure mode on purpose — forged, expired and malformed must be
    indistinguishable to the caller, or the refusal becomes an oracle about
    other tenants' in-flight grants."""
    parts = state.rsplit(":", 3)
    if len(parts) != 4:
        return None
    org_raw, subject, expiry_raw, mac = parts
    payload = f"{org_raw}:{subject}:{expiry_raw}"
    expected = hmac.new(
        oauth_state_key(), payload.encode(), hashlib.sha256
    ).hexdigest()
    # compare_digest, never ==: a timing-variable comparison on a MAC is the
    # textbook forgery oracle.
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        if int(expiry_raw) < time.time():
            return None
        return int(org_raw), subject
    except ValueError:
        return None


class AuthorizeUrlModel(BaseModel):
    url: str


@router.get("/accounting/authorize")
def authorize(
    request: Request,
    principal: Principal = Depends(require_integration_admin),
) -> AuthorizeUrlModel:
    """The Intuit consent URL for the ACTIVE org.

    Returns the URL rather than 302-ing: the SPA navigates the top-level
    window itself, so the fetch seam in `api/client.ts` and its one-shot
    `redirectToLogin` latch are never asked to follow a cross-origin redirect."""
    settings = get_settings()
    with _session(request) as session:
        org_id = current_org_id(session)
    params = urlencode({
        "client_id": settings.qbo_client_id,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "redirect_uri": f"{settings.public_base_url}"
                        "/api/integrations/accounting/callback",
        "state": sign_state(org_id=org_id, subject=principal.subject),
    })
    return AuthorizeUrlModel(url=f"{settings.qbo_authorize_url}?{params}")


@router.get("/accounting/callback")
def callback(request: Request, code: str, realmId: str, state: str) -> Response:
    """Complete the grant and store the tenant's realm + refresh token.

    Mounted OUTSIDE the operator gates: this arrives as a top-level browser
    navigation with no bearer token and no active-org header, so
    `require_operator` and `require_active_org` would both refuse it. All of
    its authorization therefore comes from the signed `state` — which is why
    the signature and the TTL are load-bearing here rather than defence in
    depth."""
    verified = verify_state(state)
    if verified is None:
        raise HTTPException(status_code=400, detail="invalid authorization state")
    org_id, subject = verified
    try:
        refresh_token = request.app.state.exchange_qbo_code(code)
    except QboError as exc:
        raise HTTPException(
            status_code=400, detail=f"QuickBooks refused the grant: {exc}"
        ) from exc

    factory = OrgBoundSessionFactory(request.app.state.db_session_factory, org_id)
    values: dict[str, Any] = {f: None for f in ALL_CREDENTIAL_FIELDS}
    values.update({"realm_id": realmId, "refresh_token": refresh_token})
    with factory() as session:
        session.execute(
            pg_insert(OrgIntegrationCredential)
            .values(
                org_id=org_id, integration="accounting", provider="qbo",
                connected_at=datetime.now(timezone.utc), connected_by=subject,
                **values,
            )
            .on_conflict_do_update(
                index_elements=["org_id", "integration"],
                set_={
                    "provider": "qbo",
                    "connected_at": datetime.now(timezone.utc),
                    "connected_by": subject, **values,
                },
            )
        )
        session.add(AuditEvent(
            actor_subject=subject, action="integration_connected",
            resource_type="integration", resource_id="accounting",
        ))
        session.commit()
    return RedirectResponse(url="/integrations?connected=accounting", status_code=307)
```

Add the imports: `hashlib`, `hmac`, `time`, `from urllib.parse import urlencode`,
`from fastapi.responses import RedirectResponse`,
`from usali.config import get_settings`,
`from usali.crypto import oauth_state_key`,
`from usali.tenancy import OrgBoundSessionFactory`.

- [ ] **Step 6: Mount the callback outside the gates**

The router carries `require_integration_admin` per route, so `callback` — which
takes no `principal` — is already ungated at the route level. But
`create_app` includes the router with `dependencies=operator_gates`, which
would still refuse it. Split it: register the callback on its own router
included with **no** dependencies.

In `integrations_api.py`, declare `callback_router = APIRouter(prefix="/api/integrations")`
and decorate `callback` with `@callback_router.get("/accounting/callback")`.
In `server.py`:

```python
    app.include_router(integrations_router, dependencies=operator_gates)
    # The Intuit callback carries no token and no active-org header (D-OH17.11);
    # its ONLY authorization is the signed `state` it verifies itself.
    app.include_router(integrations_callback_router)
```

Add the code-exchange seam beside the others in `create_app`:

```python
    app.state.exchange_qbo_code = exchange_qbo_code or _exchange_qbo_code_from_settings
```

with a `_exchange_qbo_code_from_settings(code: str) -> str` helper that POSTs
the authorization-code grant to `settings.qbo_base_url + _TOKEN_PATH` with the
Basic auth header `QboClient` already builds, and returns `refresh_token`.
Reuse `QboClient`'s `_TOKEN_PATH` and `_error_message` rather than re-deriving
either.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_integrations_oauth.py -v && uv run mypy src`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/usali/crypto.py src/usali/config.py src/usali/integrations_api.py \
        src/usali/server.py tests/test_integrations_oauth.py
git commit -m "feat(oh17): QBO OAuth connect flow with signed state"
```

---

## Task 12: Two-org isolation, contention, and the decryption failure mode

**Files:**
- Modify: `src/usali/integrations.py` (map `InvalidTag` to a named refusal)
- Modify: `src/usali/portal_api.py`, `src/usali/payroll_run_api.py`,
  `src/usali/crm_api.py` (surface it as 503)
- Test: `tests/test_integrations.py`

Design §7 and §8 each name a case the earlier tasks do not reach: a credential
that cannot be **decrypted**, and two pushes rotating the **same** token at
once. Both are failure modes rather than features, which is exactly why they
need a task of their own rather than a line in someone else's.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_integrations.py`:

```python
def _factory_for(db_engine, org_id):
    """An org-bound session factory — the precedent at
    tests/test_l2_rls_wall.py:224. Built explicitly rather than taken from a
    fixture because `two_tenant_world` returns a SimpleNamespace of ids
    (org2_id, org2_admin, org2_emp_id), not session factories."""
    return OrgBoundSessionFactory(make_session_factory(db_engine), org_id)


def test_one_org_cannot_read_anothers_credentials(db_engine, two_tenant_world):
    """The claim OH-17 has to earn: a tenant's credentials are unreachable
    from another tenant's session. Asserted through BOTH walls, because the
    ORM criteria hook is SELECT-only (tenancy.py:18-21) and a table whose RLS
    policy was forgotten would still pass an ORM-only test."""
    org_a = _factory_for(db_engine, 1)
    org_b = _factory_for(db_engine, two_tenant_world.org2_id)
    with org_a() as session:
        _connect(session, "demand_feed", "delphi", subscription_key="a-secret")
        session.commit()
    with org_b() as session:
        assert integ.credential_for(session, "demand_feed") is None
        assert session.execute(text(
            "SELECT count(*) FROM org_integration_credential"
        )).scalar_one() == 0


def test_one_org_cannot_overwrite_anothers_credentials(db_engine, two_tenant_world):
    """The RLS WITH CHECK half: a write naming another org's row is refused or
    invisible, never silently redirected."""
    org_a = _factory_for(db_engine, 1)
    org_b = _factory_for(db_engine, two_tenant_world.org2_id)
    with org_a() as session:
        _connect(session, "payroll", "gusto", api_token="t", company_id="c")
        session.commit()
    with org_b() as session:
        session.execute(text(
            "UPDATE org_integration_credential SET api_token = 'stolen'"
        ))
        session.commit()
    with org_a() as session:
        assert integ.credential_for(session, "payroll").api_token == "t"
```

`db_engine` must be the APP-ROLE engine for the RLS half to mean anything — a
superuser session bypasses RLS entirely. Use `app_role_engine`
(`tests/conftest.py:95`) wherever the assertion is about the database wall
rather than the ORM hook, exactly as `tests/test_l2_rls_wall.py` does.


`two_tenant_world` already exists in `tests/conftest.py:107` — use it, do not
create a second two-org fixture.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_integrations.py -k two_org or another -v`
Expected: FAIL if the fixture is missing; PASS once wired — the wall is
already built by Task 2's migration. A failure here means the `org_wall`
policy did not land, which is the silent gap the tenant-table checklist warns
about.

- [ ] **Step 3: Cover contention and undecryptable credentials**

Add to `tests/test_integrations.py`:

```python
def test_two_concurrent_pushes_do_not_fork_the_token_lineage(org_bound_factory, db_session):
    """Design §8. QboClient's threading.Lock serializes THREADS in one
    process; only DbTokenStore's SELECT ... FOR UPDATE reaches across
    workers. Without the row lock both callers spend the same refresh token
    and Intuit invalid_grants the loser."""
    _connect(db_session, "accounting", "qbo", realm_id="r", refresh_token="t0")
    db_session.commit()

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def push() -> None:
        try:
            barrier.wait(timeout=5)
            client = integ.resolve_qbo(org_bound_factory)
            client.post_journal_entry(_minimal_je(), request_id="r")
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=push) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)
    assert errors == []


def test_an_undecryptable_credential_refuses_loudly(org_bound_factory, db_session):
    """Design §7. ADR-005 records that rotating field_encryption_key makes
    existing ciphertext undecryptable; this is where a tenant meets that. It
    must be a NAMED refusal, never a fallback to env and never a silent
    'not connected' — the latter would quietly re-open the checklist item and
    invite the operator to reconnect something that is merely unreadable."""
    db_session.execute(text(
        "INSERT INTO org_integration_credential "
        "(org_id, integration, provider, subscription_key, connected_by) "
        "VALUES (1, 'demand_feed', 'delphi', 'not-valid-ciphertext', 'sub')"
    ))
    db_session.commit()
    with pytest.raises(integ.CredentialUnreadable) as caught:
        integ.resolve_crm_feed(org_bound_factory)
    assert "demand_feed" in str(caught.value)
```

Add `import threading` to the test module.

- [ ] **Step 4: Implement the named refusal**

In `src/usali/integrations.py`:

```python
class CredentialUnreadable(Exception):
    """A stored credential could not be decrypted (ADR-005: rotating
    `field_encryption_key` makes existing ciphertext undecryptable — there is
    no envelope/versioning yet).

    Deliberately NOT folded into "not connected". A tenant whose credential is
    merely unreadable would otherwise see the checklist item re-open and be
    invited to reconnect an integration that is fine, and the real cause —
    a key rotation — would never be named. ADR-010: absence degrades to a
    named blocker."""

    def __init__(self, integration: str) -> None:
        super().__init__(
            f"{integration} credentials could not be decrypted "
            "(field_encryption_key may have been rotated)"
        )
        self.integration = integration
```

Wrap each `credential_for` read inside the three `resolve_*` functions and
`DbTokenStore.load`:

```python
    try:
        row = credential_for(session, PAYROLL)
    except InvalidTag as exc:
        raise CredentialUnreadable(PAYROLL) from exc
```

with `from cryptography.exceptions import InvalidTag` imported at the top.
`EncryptedString` decrypts on attribute load, so the raise can also surface on
first field access — put the `try` around both the query and the field reads
the function performs.

In each of the three consumers (`portal_api._get_qbo_client`,
`payroll_run_api._provider`, `crm_api`'s two handlers), catch it beside the
`None` branch and raise a 503 whose detail is `str(exc)` — the message already
names the integration and the likely cause, and carries no secret.

- [ ] **Step 5: Run the full suite and the gates**

```bash
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

- [ ] **Step 6: Commit**

```bash
git add src/usali/integrations.py src/usali/portal_api.py \
        src/usali/payroll_run_api.py src/usali/crm_api.py tests/test_integrations.py
git commit -m "feat(oh17): name the undecryptable-credential and contention cases"
```

---

## Task 13: Roadmap deltas

**Files:**
- Modify: `docs/ROADMAP.md` §2.1, §6, §7
- Modify: `.github/roadmap.yml` (OH-17)

- [ ] **Step 1: Update `docs/ROADMAP.md`**

In §2.1, replace the "Three separate connect surfaces are waiting on this"
paragraph and the tripwire paragraph with a shipped note naming the design doc
and `/integrations`. In §6, mark row 2 shipped. In §7, mark open decision 3
settled by D-OH17.2 and cross-reference the design.

- [ ] **Step 2: Update `.github/roadmap.yml`**

Set **OH-17** `status: shipped`. §8 of the ROADMAP records that nothing
enforces the two files agreeing, and that OH-18 already drifted for two
commits by exactly this omission — a status edit in the doc alone is half an
edit.

- [ ] **Step 3: Commit**

```bash
git add docs/ROADMAP.md .github/roadmap.yml
git commit -m "docs(oh17): per-tenant integration config is shipped"
```

---

## Review notes to carry into the frontend plan

- Delphi/Tripleseat verification needs a property carrying a `crm_ref`
  (Task 9). A workspace with none gets a named refusal rather than an
  unverified credential, so the page must render that 422 as a real
  instruction ("declare a crm_ref first"), not as "wrong key".
- The exact `identifiers` keys the read endpoint returns per provider — the
  page renders them, and they come from `ProviderSpec.plain_fields`.
- `qbo_mock` needs NO change for the code exchange: it already handles
  `grant_type=authorization_code` and mints tokens (`qbo_mock.py:298`,
  verified 2026-08-30). What it lacks is a `/connect/oauth2` CONSENT page —
  irrelevant to the backend (the `authorize` endpoint only builds a URL
  string; nothing follows it), but the frontend e2e will need a stub if it
  drives the full round trip in a browser.
