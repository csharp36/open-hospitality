# Track B / B1 — invite-gated self-service signup: BACKEND plan

Status: **PLAN (2026-08-17).** Implements the backend of the approved spec
[`docs/design/2026-08-17-track-b-b1-invite-gated-signup-design.md`](../design/2026-08-17-track-b-b1-invite-gated-signup-design.md)
(scoping/decisions D-B1..D-B7:
[`docs/design/2026-08-17-track-b-self-service-onboarding-scoping.md`](../design/2026-08-17-track-b-self-service-onboarding-scoping.md)).
Branch: `feat/onboarding-track-b`.

> **The SIGNUP FRONTEND is a separate Part-2 plan (deferred).** This plan
> stops at the HTTP surface + CLI. The SPA invite-accept page, workspace form,
> OTP entry, and OIDC handoff (design §5f) are NOT in scope here.

## Goal

Turn the dormant `provision_tenant` primitive into a working, **invite-gated,
server-driven** signup backend: an approved owner clicks an emailed invite,
verifies their cell by SMS OTP, sets a password, and a new always-sensitive
tenant is provisioned with them as `org_admin` — with integrations honestly
off, no Open Hospitality staff in the loop. Every public request is
abuse-guarded and fail-closed; the one elevated DB credential the flow uses is
a **least-privilege role that cannot read a single row of any tenant's data**.

## Architecture

- **New least-privilege DB role `usali_provisioner` (D-B7).** Cluster-level
  (dev init / bootstrap / test fixture), granted INSERT+SELECT on **only**
  `organization` + `role_assignment`, with a **role-specific permissive RLS
  policy** on those two tables and **no grant on any tenant-data table**. It is
  the only credential the public completion path may use, and it is confined to
  running exactly `provision_tenant` + `invites.consume`.
- **`set_password` on the `KeycloakAdmin` seam** (real client + in-memory
  fake); `provision_tenant` gains a `password` param.
- **Two new not-`OrgScoped` tables + services**: `invite` (`src/usali/invites.py`)
  and `otp_challenge` (`src/usali/otp.py`, `OtpService`). Both precede any org,
  so they carry no `org_id` and no `org_wall` RLS policy (D-B3).
- **`Notifier` seam** (`src/usali/notifications.py`): config-selected like the
  payroll/CRM/photo-store seams; `ConsoleNotifier` dev default; tests inject a
  capturing fake.
- **In-process `RateLimiter`** (`src/usali/ratelimit.py`): sliding window,
  injectable clock. (Track A's isn't on this branch.)
- **Public, ungated signup router** in `server.py` (mounted like `kiosk_router`,
  no `operator_gates`): `GET /api/signup/invite/{token}`, `POST /api/signup/otp`,
  `POST /api/signup/complete`. A new `provisioner_session_factory` seam is
  reachable ONLY from the confined completion path.
- **`usali invite <email>` CLI**: create the invite, render the link, send it
  via the `Notifier`.

Everything below the endpoints reuses the built engine (`provision_tenant`, D1
walls, D2 org resolution). The new code is the *surface* + the confinement.

## Tech Stack

Python 3.12, FastAPI, SQLAlchemy 2.x (Declarative), Alembic, pydantic-settings
(`env_prefix="USALI_"`), Typer, httpx (`MockTransport` for KC unit tests),
Postgres 16 via `testcontainers`. `uv` runs everything. Secrets are hashed with
`hashlib.sha256`; tokens/codes come from `secrets`.

## REQUIRED SUB-SKILL: superpowers:subagent-driven-development

Execute this plan with **superpowers:subagent-driven-development**: dispatch
each numbered task to a subagent, one at a time, each doing the full
red→green→commit TDD loop below, and review each result before the next. Do not
batch tasks. Every task ends green on all three gates and a `git commit`.

## Gates (run for EVERY task before committing)

```bash
uv run pytest -q
uv run mypy src
uv run ruff check src tests
```

`mypy` is **src-only** (do not point it at `tests/`). Fixtures are synthetic —
never real PII, real phone numbers, or real credentials.

## Grounding facts verified against the code (do not re-guess)

- `provision_tenant(session, kc, *, org_name, org_alias, admin_username,
  admin_email, admin_full_name) -> ProvisionResult` refuses
  `is_org_instrumented(session)`, writes only `organization` + one org-wide
  `org_admin` `role_assignment`, **caller commits**
  (`src/usali/provisioning.py`).
- `KeycloakAdmin` Protocol + `KeycloakAdminClient` (real, `httpx`) +
  `InMemoryKeycloakAdmin` fake. `create_user(...)` sets
  `requiredActions:["UPDATE_PASSWORD"]`, no password, no `emailVerified`
  (`src/usali/keycloak_admin.py`).
- The serving process connects as the RLS-bound `usali_app` role.
  `app.state.db_session_factory` is the **unbound base** factory; sessions off
  it are NOT instrumented (so queries over non-`OrgScoped` tables need no org
  context). `l2a0rlswall` granted the app role DML on all **future** tables via
  `ALTER DEFAULT PRIVILEGES`, so `invite`/`otp_challenge` are readable/writable
  by `usali_app` with no extra grant (`src/usali/server.py`,
  `migrations/versions/l2a0rlswall_rls_wall.py`).
- Config-selected seams inject a kwarg into `create_app`, defaulting to a
  settings-built factory (mirror `_payroll_provider_from_settings`,
  `app.state.photo_store`).
- Non-`OrgScoped` tables (plain `Base`, no `org_wall` policy) do not appear in
  the `org_wall` RLS inventory, so `test_the_rls_inventory_is_complete_and_forced`
  in `tests/test_l2_rls_wall.py` stays green with the new tables **as long as
  their RLS policy name is NOT `org_wall`**.
- Migration head today is **`m2a0perffoundations`**. The one test that hardcodes
  the head literal is `tests/test_l4_org_grants.py::test_l4_is_the_single_alembic_head`
  (`== ["m2a0perffoundations"]`). `tests/test_l3_org_resolution.py::test_l3_sits_on_the_single_alembic_chain`
  and `tests/test_l2_rls_wall.py::test_l2_is_on_the_single_alembic_lineage`
  assert only `len(get_heads()) == 1` — they stay green but must be RUN to
  confirm. See "Deviations" for the correction to the "three tests" claim.
- `tests/test_l2_rls_wall.py::test_the_migration_refuses_without_the_app_role`
  upgrades a fresh container to `head` after creating ONLY the app role — so a
  new migration that also refuses without `usali_provisioner` will break it
  unless that test (and the `conftest.db_url` fixture) also create the
  provisioner role first. Both are updated in Task 1.

## File structure

```
NEW  src/usali/invites.py               # Invite service (create/validate/consume/revoke)
NEW  src/usali/otp.py                    # OtpService (issue/verify, fail-closed)
NEW  src/usali/notifications.py          # Notifier protocol + ConsoleNotifier
NEW  src/usali/ratelimit.py             # in-process sliding-window RateLimiter
NEW  src/usali/signup_api.py             # the ungated public signup router
NEW  migrations/versions/b1a0provrole_provisioner_role.py     # role grants + permissive policy
NEW  migrations/versions/b1b0invite_invite_table.py           # invite table
NEW  migrations/versions/b1c0otp_otp_challenge_table.py       # otp_challenge table

MOD  src/usali/models.py                 # + Invite, + OtpChallenge  (plain Base)
MOD  src/usali/keycloak_admin.py         # + set_password (Protocol, client, fake)
MOD  src/usali/provisioning.py           # + password param
MOD  src/usali/config.py                 # + notifier, provisioner creds, public_base_url, otp/rate knobs
MOD  src/usali/server.py                 # notifier + provisioner_session_factory seams; include signup router
MOD  src/usali/cli.py                    # + `usali invite <email>`
MOD  scripts/dev_pg_init.sql             # + usali_provisioner role (dev)
MOD  tests/orgwall.py                    # + ensure_provisioner_role / provisioner_role_url
MOD  tests/conftest.py                   # db_url fixture creates the provisioner role before migrating
MOD  tests/test_l4_org_grants.py         # head literal -> new head (each migration task)
MOD  tests/test_l2_rls_wall.py           # refuse-without-role test also creates provisioner role

NEW  tests/test_b1_provisioner_role.py   # Task 1
NEW  tests/test_b1_set_password.py       # Task 2
NEW  tests/test_b1_invites.py            # Task 3
NEW  tests/test_b1_otp.py                # Task 4
NEW  tests/test_b1_notifications.py      # Task 5
NEW  tests/test_b1_ratelimit.py          # Task 6
NEW  tests/test_b1_signup_api.py         # Task 7
NEW  tests/test_b1_invite_cli.py         # Task 8
NEW  tests/notifiers.py                  # CapturingNotifier fake (Task 5, reused by 7/8)
```

Migration chain: `m2a0perffoundations → b1a0provrole → b1b0invite → b1c0otp`.
Final head after this plan: **`b1c0otp`**.

---

## Task 1 — `usali_provisioner` least-privilege DB role (D-B7)

Cluster-level role creation (dev init + test fixture, note bootstrap), plus a
migration that GRANTs INSERT/SELECT on **only** `organization` +
`role_assignment` and adds a **role-specific permissive RLS policy** on those
two tables — with NO grant on any tenant-data table. The adversarial-review pin:
connected as `usali_provisioner`, INSERT into `organization` + `role_assignment`
succeeds but `SELECT` on a tenant-data table is DENIED.

### 1a. Failing test

Add `tests/test_b1_provisioner_role.py`:

```python
"""D-B7: the least-privilege provisioner DB role — it can write the two
cross-org provisioning tables and CANNOT read any tenant-data table."""

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

import pytest

from usali.mapping.property_registry import ensure_default_org
from tests.orgwall import provisioner_role_url

# The tenant-data table the deny pin probes. `employee` is the PII spine and is
# org_wall-policied + granted to usali_app only; the provisioner has NO grant on
# it at all, so the refusal is a table-privilege "permission denied", strictly
# stronger than an RLS empty-read. If `employee` is ever renamed, pick any other
# OrgScoped tenant table from src/usali/models.py (e.g. `punch`, `timecard`).
_TENANT_DATA_TABLE = "employee"


@pytest.fixture
def _founding(db_session):
    ensure_default_org(db_session)
    db_session.commit()


def test_provisioner_can_write_org_and_grant(db_url, _founding):
    """The provisioner role can INSERT a brand-new org row and its first
    org-wide org_admin grant — the exact two writes provision_tenant makes —
    through the role's permissive RLS policy (no BYPASSRLS)."""
    engine = create_engine(provisioner_role_url(db_url))
    try:
        with engine.begin() as conn:
            org_id = conn.execute(text(
                "INSERT INTO organization (name, kc_org_alias) "
                "VALUES ('Provisioner Probe Org', 'provisioner-probe') "
                "RETURNING org_id"
            )).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO role_assignment "
                    "(org_id, keycloak_subject, role, property_id, department_id) "
                    "VALUES (:org, 'kc-probe', 'org_admin', NULL, NULL)"
                ),
                {"org": org_id},
            )
            assert conn.execute(
                text("SELECT count(*) FROM organization WHERE org_id = :o"),
                {"o": org_id},
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_provisioner_cannot_read_tenant_data(db_url, _founding):
    """The adversarial pin: SELECT on a tenant-data table is DENIED. A stolen
    provisioner credential can mint junk empty orgs but cannot read one row of
    any tenant's real data."""
    engine = create_engine(provisioner_role_url(db_url))
    try:
        with pytest.raises(ProgrammingError, match="permission denied"):
            with engine.connect() as conn:
                conn.execute(text(f"SELECT * FROM {_TENANT_DATA_TABLE}"))
    finally:
        engine.dispose()


def test_provisioner_cannot_read_or_write_property(db_url, _founding):
    """Belt and suspenders: no read AND no write on another tenant-data table."""
    engine = create_engine(provisioner_role_url(db_url))
    try:
        with pytest.raises(ProgrammingError, match="permission denied"):
            with engine.connect() as conn:
                conn.execute(text("SELECT count(*) FROM property"))
    finally:
        engine.dispose()
```

Also, in `tests/test_l2_rls_wall.py::test_the_migration_refuses_without_the_app_role`,
add the provisioner-role creation just before the final `command.upgrade(cfg, "head")`
that is expected to SUCCEED (import `ensure_provisioner_role` from `tests.orgwall`):

```python
            # ... and creating the role makes the same upgrade converge.
            ensure_app_role(url)
            ensure_provisioner_role(url)   # b1a0provrole refuses without it too
            command.upgrade(cfg, "head")
```

### 1b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_provisioner_role.py
```

Fails: `provisioner_role_url` does not exist, and the role/grants/policy are not
migrated.

### 1c. Implement

**`tests/orgwall.py`** — add the role name, password, creator, and URL helper
(mirror `ensure_app_role`/`app_role_url`):

```python
PROVISIONER_ROLE = "usali_provisioner"
PROVISIONER_PASSWORD = "usali-provisioner-test"


def ensure_provisioner_role(url: str) -> None:
    """Create the least-privilege provisioner role (idempotently) before
    migrating. LOGIN only — no CREATEROLE/CREATEDB/BYPASSRLS: the whole point
    is that FORCE RLS applies and its grants are the only reach it has."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": PROVISIONER_ROLE},
            ).scalar()
            if exists is None:
                conn.execute(text(
                    f"CREATE ROLE {PROVISIONER_ROLE} LOGIN "
                    f"PASSWORD '{PROVISIONER_PASSWORD}'"
                ))
    finally:
        engine.dispose()


def provisioner_role_url(superuser_url: str) -> str:
    """The same database, connected as the least-privilege provisioner role."""
    return make_url(superuser_url).set(
        username=PROVISIONER_ROLE, password=PROVISIONER_PASSWORD
    ).render_as_string(hide_password=False)
```

**`tests/conftest.py`** — in the `db_url` fixture, create the provisioner role
right after `ensure_app_role(url)` and before `command.upgrade(cfg, "head")`:

```python
from tests.orgwall import (  # noqa: E402
    app_role_url, ensure_app_role, ensure_provisioner_role,
)
...
        ensure_app_role(url)
        ensure_provisioner_role(url)   # b1a0provrole refuses loudly without it
        os.environ["USALI_DB_URL"] = url
```

**`scripts/dev_pg_init.sql`** — append the dev role (idempotent guard, LOGIN
only), and a comment noting `scripts/cloud/bootstrap.sh` must create it in cloud
via `ensure_sql_user` exactly as it does `usali_app`:

```sql
-- D-B7 (Track B/B1): the least-privilege PROVISIONER role. LOGIN only — no
-- SUPERUSER, no BYPASSRLS, not the table owner. The b1a0provrole migration
-- grants it INSERT/SELECT on ONLY organization + role_assignment and a
-- role-specific permissive RLS policy on those two; it holds NO grant on any
-- tenant-data table, so a stolen credential can mint empty orgs but cannot read
-- a single tenant row. CREATE ROLE is cluster-level, so it cannot live in the
-- migration chain — the migration REFUSES to run until this role exists.
-- CLOUD: scripts/cloud/bootstrap.sh creates it the same way as usali_app.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'usali_provisioner') THEN
    CREATE ROLE usali_provisioner LOGIN PASSWORD 'usali_provisioner';
  END IF;
END
$$;
```

**`migrations/versions/b1a0provrole_provisioner_role.py`** — new migration:

```python
"""D-B7 (Track B/B1): the least-privilege usali_provisioner role's grants and a
role-specific permissive RLS policy on organization + role_assignment.

The public signup-completion path runs provision_tenant + invites.consume as
this role, which needs to WRITE the cross-org organization row + the first
org-wide org_admin grant but must be UNABLE to read any tenant's data. So:

  - GRANT INSERT, SELECT on ONLY organization + role_assignment (and USAGE on
    their identity sequences so the autoincrement PKs work). NOTHING else — no
    grant on any tenant-data table, no ALTER DEFAULT PRIVILEGES.
  - A PERMISSIVE policy `provisioner_wall` TO usali_provisioner on those two
    tables, USING(true) WITH CHECK(true). Postgres OR-combines permissive
    policies, and a policy restricted TO a role does not apply to usali_app —
    so the app role stays confined by org_wall while the provisioner may write
    a new org's own row + grant before any org context exists (the same reason
    provision_tenant needs an RLS-bypassing owner session; here it is a
    role-scoped permissive policy instead of BYPASSRLS).

CREATE ROLE is cluster-level (dev_pg_init.sql / bootstrap.sh / the test
fixture) — this migration REFUSES loudly when the role is missing, exactly like
l2a0rlswall does for usali_app.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1a0provrole"
down_revision = "m2a0perffoundations"
branch_labels = None
depends_on = None

PROVISIONER_ROLE = "usali_provisioner"
_POLICY = "provisioner_wall"
# The ONLY two tables the provisioner may touch. Both are cross-org by nature:
# organization is org-scoped by its own PK; role_assignment's first row for a
# new org cannot pass org_wall's WITH CHECK before any org context exists.
_TABLES = ("organization", "role_assignment")


def upgrade() -> None:
    conn = op.get_bind()
    if conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": PROVISIONER_ROLE},
    ).scalar() is None:
        raise RuntimeError(
            f"the provisioner database role {PROVISIONER_ROLE!r} does not exist "
            "— this migration's grants have nothing to land on. CREATE ROLE is "
            "cluster-level and lives outside the migration chain: "
            "scripts/cloud/bootstrap.sh provisions it in the cloud, "
            "scripts/dev_pg_init.sql in dev, and tests/orgwall.ensure_provisioner_role "
            "in the test container. Create the role, then re-run alembic."
        )
    for table in _TABLES:
        op.execute(f"GRANT INSERT, SELECT ON {table} TO {PROVISIONER_ROLE}")
        # The autoincrement PK's identity sequence (SERIAL) — INSERT needs USAGE.
        # Resolve the name from the catalog rather than hardcoding the *_seq
        # spelling, so an identity-column change cannot silently break the grant.
        seq = conn.execute(
            sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
            {"t": table, "c": "org_id" if table == "organization" else "assignment_id"},
        ).scalar()
        if seq is not None:
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {PROVISIONER_ROLE}")
        # Role-specific PERMISSIVE policy: OR-combined with org_wall, applies to
        # the provisioner role ONLY (usali_app is unaffected).
        op.execute(
            f"CREATE POLICY {_POLICY} ON {table} AS PERMISSIVE FOR ALL "
            f"TO {PROVISIONER_ROLE} USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"REVOKE INSERT, SELECT ON {table} FROM {PROVISIONER_ROLE}")
        seq = conn.execute(
            sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
            {"t": table, "c": "org_id" if table == "organization" else "assignment_id"},
        ).scalar()
        if seq is not None:
            op.execute(f"REVOKE USAGE, SELECT ON SEQUENCE {seq} FROM {PROVISIONER_ROLE}")
    # The role itself is cluster state owned by dev-init/bootstrap — not dropped.
```

**`tests/test_l4_org_grants.py`** — update the head literal:

```python
    assert ScriptDirectory.from_config(cfg).get_heads() == ["b1a0provrole"]
```

> **Resolve at implementation:** confirm `_TENANT_DATA_TABLE = "employee"` still
> names an `OrgScoped` table granted to `usali_app` but NOT the provisioner
> (grep `class Employee(OrgScoped` in `src/usali/models.py`). If renamed, pick
> another OrgScoped tenant table. Also confirm `pg_get_serial_sequence` returns
> non-NULL for `organization.org_id` and `role_assignment.assignment_id` in a
> migrated container (they are SQLAlchemy `autoincrement=True` SERIALs, so it
> should); if a table ever moves to GENERATED-identity, the `IF seq IS NOT NULL`
> guard already no-ops and you grant on the table's identity another way.

### 1d. Run gates (GREEN) & commit

```bash
uv run pytest -q tests/test_b1_provisioner_role.py tests/test_l2_rls_wall.py tests/test_l4_org_grants.py
uv run pytest -q
uv run mypy src
uv run ruff check src tests
git add -A && git commit -m "feat(b1): least-privilege usali_provisioner DB role + role-scoped RLS policy (D-B7)"
```

---

## Task 2 — `set_password` on the `KeycloakAdmin` seam + `provision_tenant` password

### 2a. Failing test

Add `tests/test_b1_set_password.py`:

```python
"""set_password on the KeycloakAdmin seam: clears UPDATE_PASSWORD, sets
emailVerified; the fake records it and raises on an unknown id; provision_tenant
threads a password through."""

import httpx
import pytest

from usali.keycloak_admin import (
    InMemoryKeycloakAdmin,
    KeycloakAdminClient,
    KeycloakAdminError,
)
from usali.provisioning import provision_tenant
from usali.tenancy import bind_org_context
from usali.mapping.property_registry import ensure_default_org


def test_fake_set_password_records_and_clears_required_action():
    kc = InMemoryKeycloakAdmin()
    sub = kc.create_user(
        username="owner", email="owner@example.test", full_name="Ow Ner",
        realm_roles=["org_admin"],
    )
    kc.set_password(sub, "s3cret-passphrase")
    user = kc.users[sub]
    assert user["password"] == "s3cret-passphrase"
    assert user["required_actions"] == []
    assert user["email_verified"] is True


def test_fake_set_password_unknown_id_raises():
    kc = InMemoryKeycloakAdmin()
    with pytest.raises(KeycloakAdminError, match="unknown user"):
        kc.set_password("nope", "x")


def test_real_client_set_password_calls_reset_and_clears_actions():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(204)

    client = KeycloakAdminClient(
        base_url="http://kc.local", realm="usali",
        client_id="usali-admin", client_secret="s",
        transport=httpx.MockTransport(handler),
    )
    client.set_password("sub-123", "pw")
    paths = [p for m, p in seen]
    assert "/admin/realms/usali/users/sub-123/reset-password" in paths
    # The user update that clears requiredActions + sets emailVerified.
    assert "/admin/realms/usali/users/sub-123" in paths


def test_provision_tenant_sets_the_admin_password(db_session):
    ensure_default_org(db_session)
    db_session.commit()
    kc = InMemoryKeycloakAdmin()
    result = provision_tenant(
        db_session, kc,
        org_name="Pw Org", org_alias="pw-org",
        admin_username="pw-admin", admin_email="pw@example.test",
        admin_full_name="Pw Admin", password="chosen-pw",
    )
    db_session.commit()
    assert kc.users[result.admin_subject]["password"] == "chosen-pw"
    assert kc.users[result.admin_subject]["email_verified"] is True
```

### 2b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_set_password.py
```

### 2c. Implement

**`src/usali/keycloak_admin.py`** — add to the `KeycloakAdmin` Protocol:

```python
    def set_password(self, subject_id: str, password: str) -> None:
        """Set a permanent password on an EXISTING user, clear the
        UPDATE_PASSWORD required action, and mark the email verified.

        provision_tenant calls this after creating the admin user: the
        create_user step deliberately leaves the account password-less with
        UPDATE_PASSWORD pending (an operator-provisioned user resets on first
        login); the SELF-SERVICE owner instead chooses their password at signup
        and has already proven the email by clicking the invite link, so the
        required action is cleared and emailVerified set here — the account is
        immediately usable, no reset email, no unverified-email gate."""
        ...
```

Add to `KeycloakAdminClient`:

```python
    def set_password(self, subject_id: str, password: str) -> None:
        headers = self._auth()
        r = self._http.put(
            f"/admin/realms/{self._realm}/users/{subject_id}/reset-password",
            headers=headers,
            json={"type": "password", "value": password, "temporary": False},
        )
        if r.status_code not in (204, 200):
            raise KeycloakAdminError(f"set password failed: {r.status_code}")
        # Clear the UPDATE_PASSWORD required action and mark the email verified:
        # the invite click proved the email, and the owner just set a real
        # password, so neither gate should stand between them and first login.
        r2 = self._http.put(
            f"/admin/realms/{self._realm}/users/{subject_id}",
            headers=headers,
            json={"requiredActions": [], "emailVerified": True},
        )
        if r2.status_code not in (204, 200):
            raise KeycloakAdminError(f"clear required actions failed: {r2.status_code}")
```

In `InMemoryKeycloakAdmin.create_user`, extend the stored dict so the fake
mirrors the real shape (add the three keys):

```python
        self.users[subject_id] = {
            "username": username, "email": email, "full_name": full_name,
            "realm_roles": realm_roles, "enabled": True,
            "required_actions": ["UPDATE_PASSWORD"],
            "password": None, "email_verified": False,
        }
```

Add the fake method:

```python
    def set_password(self, subject_id: str, password: str) -> None:
        user = self.users.get(subject_id)
        if user is None:
            raise KeycloakAdminError(f"set password failed: unknown user {subject_id!r}")
        user["password"] = password
        user["required_actions"] = []
        user["email_verified"] = True
```

**`src/usali/provisioning.py`** — add a `password` parameter and call it after
the user exists (adopt or create). Change the signature:

```python
def provision_tenant(
    session: Session,
    kc: KeycloakAdmin,
    *,
    org_name: str,
    org_alias: str,
    admin_username: str,
    admin_email: str,
    admin_full_name: str,
    password: str | None = None,
) -> ProvisionResult:
```

After step 3 (`kc.add_member(kc_org_id, admin_subject)`), before the DB writes:

```python
    # Self-service signup (Track B/B1): the owner chose a password and proved
    # their email via the invite click. Set it here (idempotent — a re-run just
    # re-sets the same password). Operator provisioning passes password=None and
    # keeps the create_user UPDATE_PASSWORD/reset-on-first-login posture.
    if password is not None:
        kc.set_password(admin_subject, password)
```

Update the module docstring's parameter list to mention `password` is optional
and only used by the self-service path.

### 2d. Gates & commit

```bash
uv run pytest -q tests/test_b1_set_password.py
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): KeycloakAdmin.set_password + provision_tenant password param"
```

---

## Task 3 — `invite` model + `src/usali/invites.py` + migration

Not-`OrgScoped` (D-B3): the invite precedes any tenant. Token is a bearer
secret — the raw value is shown once (in the emailed link), stored only hashed.

### 3a. Failing test

Add `tests/test_b1_invites.py`:

```python
"""Invite lifecycle: create -> valid -> consume -> invalid-on-reuse; expiry;
revoke. The raw token is never stored — only its SHA-256."""

from datetime import datetime, timedelta, timezone

from usali import invites
from usali.models import Invite
from usali.mapping.property_registry import ensure_default_org

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_create_returns_raw_token_and_stores_only_the_hash(db_session):
    invite, raw = invites.create_invite(db_session, "owner@example.test", now=_NOW)
    db_session.commit()
    assert raw and invite.token_hash != raw
    assert invites._hash_token(raw) == invite.token_hash
    assert invite.status == "pending"
    assert invite.email == "owner@example.test"


def test_validate_accepts_pending_unexpired_matching(db_session):
    _, raw = invites.create_invite(db_session, "a@example.test", now=_NOW)
    db_session.commit()
    found = invites.validate(db_session, raw, now=_NOW + timedelta(hours=1))
    assert found is not None and found.email == "a@example.test"


def test_validate_rejects_unknown_expired_and_consumed(db_session):
    invite, raw = invites.create_invite(
        db_session, "b@example.test", ttl=timedelta(hours=1), now=_NOW
    )
    db_session.commit()
    assert invites.validate(db_session, "not-a-real-token", now=_NOW) is None
    assert invites.validate(db_session, raw, now=_NOW + timedelta(hours=2)) is None
    invites.consume(db_session, invite, org_id=1)
    db_session.commit()
    assert invites.validate(db_session, raw, now=_NOW + timedelta(minutes=5)) is None


def test_consume_marks_status_and_records_org(db_session):
    ensure_default_org(db_session)
    invite, raw = invites.create_invite(db_session, "c@example.test", now=_NOW)
    db_session.commit()
    invites.consume(db_session, invite, org_id=1)
    db_session.commit()
    assert invite.status == "consumed"
    assert invite.consumed_org_id == 1


def test_revoke_makes_it_invalid(db_session):
    invite, raw = invites.create_invite(db_session, "d@example.test", now=_NOW)
    db_session.commit()
    invites.revoke(db_session, invite)
    db_session.commit()
    assert invites.validate(db_session, raw, now=_NOW) is None
```

### 3b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_invites.py
```

### 3c. Implement

**`src/usali/models.py`** — add near `Organization` (plain `Base`, NOT
`OrgScoped`, no RLS policy — D-B3). Import `timezone` is already available via
`datetime`. Add:

```python
class Invite(Base):
    """A one-time, expiring, invite-gate row (Track B/B1, D-B3/D-B4). NOT
    OrgScoped: an invite precedes any tenant, so it carries no org_id and no
    org_wall RLS policy. The raw token is a BEARER secret shown once in the
    emailed link and stored only hashed (SHA-256 hex). `consumed_org_id` is set
    on consume for audit — the tenant the invite became."""

    __tablename__ = "invite"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_invite_token_hash"),
        CheckConstraint(
            "status IN ('pending', 'consumed', 'revoked')",
            name="ck_invite_status",
        ),
    )

    invite_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320))
    token_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    consumed_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organization.org_id", name="fk_invite_consumed_org"),
        nullable=True,
    )
```

**`src/usali/invites.py`** — new service:

```python
"""Invite-gate service (Track B/B1, D-B4). The raw token is a bearer secret:
created once, returned once (for the emailed link), and stored only as its
SHA-256. Validation is fail-closed — unknown / expired / non-pending all return
None, and the caller refuses without an existence oracle.

These functions do NOT commit; the caller owns the transaction boundary (the
signup-completion path consumes the invite in the SAME transaction as
provision_tenant, so a provisioning failure leaves the invite pending)."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import Invite

_DEFAULT_TTL = timedelta(days=7)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_invite(
    session: Session,
    email: str,
    *,
    ttl: timedelta = _DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[Invite, str]:
    """Create a pending invite for `email`. Returns (row, raw_token); the raw
    token is shown to the caller ONCE and never stored in the clear."""
    moment = now or _now()
    raw_token = secrets.token_urlsafe(32)
    invite = Invite(
        email=email,
        token_hash=_hash_token(raw_token),
        status="pending",
        expires_at=moment + ttl,
    )
    session.add(invite)
    session.flush()
    return invite, raw_token


def validate(
    session: Session, raw_token: str, *, now: datetime | None = None
) -> Invite | None:
    """The pending, unexpired invite whose hash matches `raw_token`, or None.
    Fail-closed on every miss — the caller refuses naming nothing."""
    moment = now or _now()
    invite = session.execute(
        select(Invite).where(Invite.token_hash == _hash_token(raw_token))
    ).scalar_one_or_none()
    if invite is None or invite.status != "pending":
        return None
    if invite.expires_at <= moment:
        return None
    return invite


def consume(session: Session, invite: Invite, org_id: int) -> None:
    """Mark the invite consumed and record which tenant it became."""
    invite.status = "consumed"
    invite.consumed_org_id = org_id
    session.flush()


def revoke(session: Session, invite: Invite) -> None:
    invite.status = "revoked"
    session.flush()
```

**`migrations/versions/b1b0invite_invite_table.py`** — new migration. NO
`org_wall` policy (D-B3): the table is org-independent. `usali_app` gets DML on
it automatically via l2's `ALTER DEFAULT PRIVILEGES`. The provisioner has no
grant on it (invite writes happen on the app role, not the provisioner).

```python
"""Track B/B1 (D-B3): the invite-gate table. NOT OrgScoped — no org_id, no
org_wall RLS policy: an invite precedes any tenant. usali_app is granted DML on
it by l2a0rlswall's ALTER DEFAULT PRIVILEGES (future tables), so no grant here."""

import sqlalchemy as sa
from alembic import op

revision = "b1b0invite"
down_revision = "b1a0provrole"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invite",
        sa.Column("invite_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_invite_consumed_org"),
                  nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_invite_token_hash"),
        sa.CheckConstraint("status IN ('pending', 'consumed', 'revoked')",
                           name="ck_invite_status"),
    )


def downgrade() -> None:
    op.drop_table("invite")
```

**`tests/test_l4_org_grants.py`** — bump the head literal to `["b1b0invite"]`.

### 3d. Gates & commit

```bash
uv run pytest -q tests/test_b1_invites.py tests/test_l4_org_grants.py
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): invite model + service + migration (not-OrgScoped, D-B3)"
```

---

## Task 4 — `otp_challenge` model + `OtpService` + migration

Not-`OrgScoped`. Code stored hashed, expiring, attempt-limited, fail-closed. DB
store chosen over an in-process store (design §10) — simplest and testable.

### 4a. Failing test

Add `tests/test_b1_otp.py`:

```python
"""OtpService: issue/verify happy path; wrong/expired/exhausted fail closed;
the code is stored hashed, never in the clear."""

from datetime import datetime, timedelta, timezone

import pytest

from usali.otp import OtpService
from usali.models import OtpChallenge
from sqlalchemy import select

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
_PURPOSE = "signup_cell"
_TARGET = "+15550000000"  # synthetic


def test_issue_then_verify_succeeds_once(db_session):
    svc = OtpService()
    code = svc.issue(db_session, purpose=_PURPOSE, target=_TARGET, now=_NOW)
    db_session.commit()
    assert code.isdigit() and len(code) == 6
    # Stored hashed, not in the clear.
    row = db_session.execute(select(OtpChallenge)).scalar_one()
    assert row.code_hash != code
    assert svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                      code=code, now=_NOW + timedelta(minutes=1))
    db_session.commit()
    # A second verify with the same code fails (single-use).
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code=code, now=_NOW + timedelta(minutes=1))


def test_wrong_code_fails_and_counts_against_the_attempt_limit(db_session):
    svc = OtpService(max_attempts=3)
    code = svc.issue(db_session, purpose=_PURPOSE, target=_TARGET, now=_NOW)
    db_session.commit()
    for _ in range(3):
        assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                              code="000000", now=_NOW)
        db_session.commit()
    # Exhausted: even the RIGHT code now fails closed.
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code=code, now=_NOW)


def test_expired_code_fails_closed(db_session):
    svc = OtpService(ttl=timedelta(minutes=5))
    code = svc.issue(db_session, purpose=_PURPOSE, target=_TARGET, now=_NOW)
    db_session.commit()
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code=code, now=_NOW + timedelta(minutes=6))


def test_verify_with_no_challenge_fails_closed(db_session):
    svc = OtpService()
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code="123456", now=_NOW)
```

### 4b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_otp.py
```

### 4c. Implement

**`src/usali/models.py`** — add (plain `Base`, NOT `OrgScoped`):

```python
class OtpChallenge(Base):
    """A short-lived, hashed, attempt-limited one-time code (Track B/B1). NOT
    OrgScoped — it gates SIGNUP, before any tenant exists. The code is a bearer
    secret stored only as its SHA-256; `attempts` is the fail-closed counter."""

    __tablename__ = "otp_challenge"

    otp_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(String(30))
    target: Mapped[str] = mapped_column(String(200))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

**`src/usali/otp.py`** — new service:

```python
"""OTP challenge service (Track B/B1, folded in per D-B6). Numeric codes,
hashed at rest, expiring, attempt-limited, and single-use. Every failure mode —
no challenge, wrong code, expired, exhausted — returns False (fail-closed); the
caller refuses without saying which. Does NOT commit; the caller owns the
transaction so an attempt increment is durable even when it later refuses."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import OtpChallenge

_DEFAULT_TTL = timedelta(minutes=10)
_DEFAULT_MAX_ATTEMPTS = 5


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OtpService:
    def __init__(
        self,
        *,
        ttl: timedelta = _DEFAULT_TTL,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._ttl = ttl
        self._max_attempts = max_attempts

    def issue(
        self, session: Session, *, purpose: str, target: str,
        now: datetime | None = None,
    ) -> str:
        """Mint a fresh 6-digit code for (purpose, target), superseding any
        prior outstanding challenge for the same pair. Returns the raw code (to
        be sent via the Notifier); only its hash is stored."""
        moment = now or _now()
        # One live challenge per (purpose, target): a re-request invalidates the
        # previous code rather than leaving two valid.
        for stale in session.execute(
            select(OtpChallenge).where(
                OtpChallenge.purpose == purpose, OtpChallenge.target == target
            )
        ).scalars():
            session.delete(stale)
        code = f"{secrets.randbelow(1_000_000):06d}"
        session.add(OtpChallenge(
            purpose=purpose, target=target, code_hash=_hash_code(code),
            expires_at=moment + self._ttl, attempts=0,
        ))
        session.flush()
        return code

    def verify(
        self, session: Session, *, purpose: str, target: str, code: str,
        now: datetime | None = None,
    ) -> bool:
        """True iff (purpose, target) has a live, unexhausted challenge whose
        code matches. On a wrong code, increments attempts (fail-closed on
        exhaustion). On success, consumes the challenge (single-use)."""
        moment = now or _now()
        challenge = session.execute(
            select(OtpChallenge).where(
                OtpChallenge.purpose == purpose, OtpChallenge.target == target
            )
        ).scalar_one_or_none()
        if challenge is None:
            return False
        if challenge.expires_at <= moment or challenge.attempts >= self._max_attempts:
            return False
        if not hmac.compare_digest(challenge.code_hash, _hash_code(code)):
            challenge.attempts += 1
            session.flush()
            return False
        session.delete(challenge)  # single-use
        session.flush()
        return True
```

**`migrations/versions/b1c0otp_otp_challenge_table.py`** — new migration (no
`org_wall` policy; app-role DML via l2 default privileges):

```python
"""Track B/B1 (D-B6): the otp_challenge table. NOT OrgScoped — no org_id, no
org_wall RLS policy: OTP gates signup, before any tenant exists."""

import sqlalchemy as sa
from alembic import op

revision = "b1c0otp"
down_revision = "b1b0invite"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "otp_challenge",
        sa.Column("otp_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Index("ix_otp_challenge_purpose_target", "purpose", "target"),
    )


def downgrade() -> None:
    op.drop_table("otp_challenge")
```

**`tests/test_l4_org_grants.py`** — bump the head literal to `["b1c0otp"]`.

### 4d. Gates & commit

```bash
uv run pytest -q tests/test_b1_otp.py tests/test_l4_org_grants.py
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): otp_challenge model + OtpService (fail-closed) + migration"
```

---

## Task 5 — `notifications.py` Notifier seam

Config-selected like the payroll/CRM/photo-store seams. `ConsoleNotifier` is the
dev/test default; a new `USALI_NOTIFIER` setting selects it; `create_app` gets a
`notifier` kwarg and stashes `app.state.notifier`.

### 5a. Failing test

Add `tests/notifiers.py` (the capturing fake, reused by Tasks 7 & 8):

```python
"""A capturing Notifier fake for tests — records every message, sends nothing."""

from dataclasses import dataclass, field


@dataclass
class CapturingNotifier:
    emails: list[dict[str, str]] = field(default_factory=list)
    smses: list[dict[str, str]] = field(default_factory=list)

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        self.emails.append({"to": to, "subject": subject, "body": body})

    def send_sms(self, *, to: str, body: str) -> None:
        self.smses.append({"to": to, "body": body})
```

Add `tests/test_b1_notifications.py`:

```python
"""The Notifier seam: ConsoleNotifier is the config default; create_app accepts
an injected notifier and exposes it on app.state; an unknown USALI_NOTIFIER
value fails fast."""

import logging
from pathlib import Path

import pytest

from usali.config import Settings
from usali.notifications import ConsoleNotifier, notifier_from_settings
from usali.server import create_app
from tests.notifiers import CapturingNotifier


def test_console_notifier_logs_both_channels(caplog):
    n = ConsoleNotifier()
    with caplog.at_level(logging.INFO):
        n.send_email(to="a@example.test", subject="Hi", body="Body")
        n.send_sms(to="+15550000000", body="123456")
    text = " ".join(r.message for r in caplog.records)
    assert "a@example.test" in text and "+15550000000" in text


def test_notifier_from_settings_selects_console_by_default():
    assert isinstance(notifier_from_settings(Settings(notifier="console")), ConsoleNotifier)


def test_notifier_from_settings_rejects_unknown():
    with pytest.raises(RuntimeError, match="unknown notifier"):
        notifier_from_settings(Settings(notifier="carrier-pigeon"))


def test_create_app_uses_injected_notifier(tmp_path: Path):
    fake = CapturingNotifier()
    app = create_app(notifier=fake, dist_dir=tmp_path / "nope")
    assert app.state.notifier is fake
```

### 5b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_notifications.py
```

### 5c. Implement

**`src/usali/notifications.py`** — new module:

```python
"""Notification seam (Track B/B1, D-B6). A minimal Notifier interface + a dev
ConsoleNotifier that logs instead of sending. Real SMTP/SMS adapters are the B2
vendor matrix; this ships only the interface + the no-vendor default, selected
by configuration exactly like the payroll/CRM/photo-store seams."""

import logging
from typing import Protocol

logger = logging.getLogger("usali.notifications")


class Notifier(Protocol):
    def send_email(self, *, to: str, subject: str, body: str) -> None: ...
    def send_sms(self, *, to: str, body: str) -> None: ...


class ConsoleNotifier:
    """Dev/test default: logs the message (link/code visible in the console) and
    sends nothing. No vendor, fully testable."""

    def send_email(self, *, to: str, subject: str, body: str) -> None:
        logger.info("EMAIL to=%s subject=%s body=%s", to, subject, body)

    def send_sms(self, *, to: str, body: str) -> None:
        logger.info("SMS to=%s body=%s", to, body)


def notifier_from_settings(settings: "Settings") -> Notifier:  # noqa: F821
    """Config-selected notifier. Only 'console' ships in B1; the SMTP/SMS
    adapters are B2. An unknown name fails fast (the payroll-provider posture)."""
    if settings.notifier == "console":
        return ConsoleNotifier()
    raise RuntimeError(
        f"unknown notifier {settings.notifier!r} (expected console; SMTP/SMS "
        "adapters land in B2)"
    )
```

Add the `Settings` import at the top (`from usali.config import Settings`) and
change the annotation to `settings: Settings` — the string form above is only to
avoid a forward-import cycle if one exists; verify `config.py` does not import
`notifications` (it does not), so a direct import is clean.

**`src/usali/config.py`** — add the setting near the other seam settings:

```python
    # Notification/OTP delivery seam (Track B/B1, D-B6). Only "console" ships in
    # B1 (logs the link/code, no vendor); SMTP + one SMS vendor land in B2.
    notifier: str = "console"  # "console"

    # Public base URL the invite/signup links point at (usali invite CLI).
    public_base_url: str = "http://localhost:8100"

    # Provisioner DB role (D-B7): the signup-completion path connects as this
    # least-privilege role to write ONLY organization + role_assignment. In dev
    # these match scripts/dev_pg_init.sql; prod overrides via USALI_PROVISIONER_*.
    provisioner_db_role: str = "usali_provisioner"
    provisioner_db_password: str = "usali_provisioner"

    # Signup abuse guards (Track B/B1). Per-target OTP request ceiling and the
    # per-invite completion attempt ceiling, each over the sliding window below.
    signup_otp_max_per_window: int = 5
    signup_rate_window_seconds: int = 3600
```

**`src/usali/server.py`** — add the `notifier` kwarg + default. Import at top:
`from usali.notifications import Notifier, notifier_from_settings`. Add the
parameter to `create_app` (alongside the other seams):

```python
    notifier: Notifier | None = None,
```

and after `app.state.photo_store = ...`:

```python
    # Notification seam (B1/D-B6). Tests inject a capturing fake; the default is
    # config-selected (console-only in B1). One instance for the app's lifetime.
    app.state.notifier = notifier or notifier_from_settings(settings)
```

### 5d. Gates & commit

```bash
uv run pytest -q tests/test_b1_notifications.py
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): Notifier seam (ConsoleNotifier + config-selected + create_app kwarg)"
```

---

## Task 6 — In-process `RateLimiter`

Per-key sliding window with an injectable clock and eviction of expired keys.

### 6a. Failing test

Add `tests/test_b1_ratelimit.py`:

```python
"""In-process sliding-window rate limiter with an injectable clock."""

from usali.ratelimit import RateLimiter


def test_allows_up_to_the_limit_then_blocks():
    now = [1000.0]
    rl = RateLimiter(max_events=3, window_seconds=60, clock=lambda: now[0])
    assert [rl.allow("k") for _ in range(3)] == [True, True, True]
    assert rl.allow("k") is False


def test_window_slides_so_old_events_expire():
    now = [1000.0]
    rl = RateLimiter(max_events=2, window_seconds=60, clock=lambda: now[0])
    assert rl.allow("k") and rl.allow("k")
    assert rl.allow("k") is False
    now[0] += 61  # both events fall out of the window
    assert rl.allow("k") is True


def test_keys_are_independent():
    now = [0.0]
    rl = RateLimiter(max_events=1, window_seconds=10, clock=lambda: now[0])
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    assert rl.allow("a") is False


def test_expired_keys_are_evicted_to_bound_memory():
    now = [0.0]
    rl = RateLimiter(max_events=1, window_seconds=10, clock=lambda: now[0])
    rl.allow("gone")
    now[0] += 11
    rl.allow("here")
    assert "gone" not in rl._events  # evicted on the sweep
```

### 6b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_ratelimit.py
```

### 6c. Implement

**`src/usali/ratelimit.py`** — new module:

```python
"""A tiny in-process, per-key sliding-window rate limiter (Track B/B1). Single
serving process at the pilot scale, so an in-memory limiter is enough; the clock
is injectable for deterministic tests. Not durable and not shared across
processes — a deliberate pilot simplification, replaced by a shared store when
the deployment scales out."""

import threading
import time
from collections import defaultdict
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        max_events: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_events
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an attempt for `key`; True if it is within the window's
        ceiling, False otherwise. Sweeps expired timestamps (and empty keys) on
        every call so memory stays bounded by what is currently active."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            # Evict expired keys entirely (bound memory), not just the one hit.
            for k in [k for k, ts in self._events.items()
                      if not ts or ts[-1] <= cutoff]:
                del self._events[k]
            recent = [t for t in self._events[key] if t > cutoff]
            if len(recent) >= self._max:
                self._events[key] = recent
                return False
            recent.append(now)
            self._events[key] = recent
            return True
```

### 6d. Gates & commit

```bash
uv run pytest -q tests/test_b1_ratelimit.py
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): in-process sliding-window RateLimiter"
```

---

## Task 7 — Public signup router (ungated) in `server.py`

`GET /api/signup/invite/{token}`, `POST /api/signup/otp`, `POST /api/signup/complete`.
Wires invite + OTP + notifier. A new `provisioner_session_factory` seam (the
`usali_provisioner` creds) is reachable **only** by the confined completion path
that runs exactly `provision_tenant` + `invites.consume`. Abuse-guarded
(rate limits + invite required); fail-closed, no-oracle refusals.

### 7a. Failing test

Add `tests/test_b1_signup_api.py`:

```python
"""The ungated public signup router: two-org happy path through the real app
role + provisioner role, fail-closed refusals, and the confinement pin — the
only elevated session the endpoint opens is the provisioner one, only in
completion."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.notifiers import CapturingNotifier
from tests.orgwall import app_role_url, provisioner_role_url
from usali import invites
from usali.db import make_engine, make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import Employee, Organization
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.tenancy import bind_org_context


def _signup_client(db_url, tmp_path, *, notifier, kc, spy=None):
    """A serving app whose PUBLIC surfaces run as usali_app and whose
    provisioner seam runs as usali_provisioner. `spy` wraps the provisioner
    factory so a test can assert it is (or is not) opened."""
    verifier, _ = make_authkit()
    base = make_session_factory(make_engine(app_role_url(db_url)))
    prov = make_session_factory(make_engine(provisioner_role_url(db_url)))
    if spy is not None:
        prov = spy(prov)
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=base,
        provisioner_session_factory=prov,
        token_verifier=verifier,
        keycloak_admin=kc,
        photo_store=InMemoryPhotoStore(),
        notifier=notifier,
    )
    return TestClient(app)


@pytest.fixture
def _founding_committed(db_session):
    from usali.mapping.property_registry import ensure_default_org
    ensure_default_org(db_session)
    db_session.commit()


def _make_invite(db_url, email):
    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        _, raw = invites.create_invite(s, email)
        s.commit()
    return raw


def test_get_invite_returns_email_for_a_valid_token(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    client = _signup_client(db_url, tmp_path, notifier=CapturingNotifier(),
                            kc=InMemoryKeycloakAdmin())
    r = client.get(f"/api/signup/invite/{raw}")
    assert r.status_code == 200 and r.json()["email"] == "owner@example.test"


def test_get_invite_refuses_unknown_token_without_oracle(db_url, tmp_path, _founding_committed):
    client = _signup_client(db_url, tmp_path, notifier=CapturingNotifier(),
                            kc=InMemoryKeycloakAdmin())
    r = client.get("/api/signup/invite/not-a-real-token")
    assert r.status_code == 404


def test_otp_requires_a_valid_invite_and_sends_the_sms(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    ok = client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    assert ok.status_code == 204
    assert len(notifier.smses) == 1 and notifier.smses[0]["to"] == "+15550000000"
    bad = client.post("/api/signup/otp", json={"token": "nope", "cell": "+15550000000"})
    assert bad.status_code == 404


def test_happy_path_provisions_a_second_tenant_and_consumes_the_invite(
    db_url, tmp_path, _founding_committed
):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    kc = InMemoryKeycloakAdmin()
    client = _signup_client(db_url, tmp_path, notifier=notifier, kc=kc)

    assert client.post("/api/signup/otp",
                       json={"token": raw, "cell": "+15550000000"}).status_code == 204
    code = notifier.smses[-1]["body"]  # ConsoleNotifier would log it; fake exposes it
    # NOTE: the code delivered is the raw OTP; the fake records the sms body,
    # which the endpoint set to the code (see implementation).

    done = client.post("/api/signup/complete", json={
        "token": raw, "otp": code,
        "workspace_name": "New Owner Group", "workspace_alias": "new-owner-group",
        "property_name": "Owner Hotel", "pms_source": "opera",
        "wage_jurisdiction": "US-CA", "cell": "+15550000000",
        "password": "chosen-password",
    })
    assert done.status_code == 201, done.text
    alias = done.json()["org_alias"]
    assert alias == "new-owner-group"

    # A new org exists (seen on the superuser session), the invite is consumed,
    # and the KC user has a password + verified email.
    from usali.db import make_session_factory as msf
    from usali.db import make_engine as me
    su = msf(me(db_url))
    with su() as s:
        org = s.execute(select(Organization).where(
            Organization.kc_org_alias == "new-owner-group")).scalar_one()
        assert org.org_id != 1
        assert s.execute(select(func.count()).select_from(Employee)
                         .where(Employee.org_id == org.org_id)).scalar_one() == 0
    sub = next(iter(kc.users))
    assert any(u["password"] == "chosen-password" and u["email_verified"]
               for u in kc.users.values())

    # Reusing the invite is refused (single-use, no oracle).
    again = client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "x", "workspace_alias": "y",
        "property_name": "z", "pms_source": "opera", "wage_jurisdiction": "US-CA",
        "cell": "+15550000000", "password": "p",
    })
    assert again.status_code in (404, 409)


def test_complete_fails_closed_on_wrong_otp(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    client = _signup_client(db_url, tmp_path, notifier=CapturingNotifier(),
                            kc=InMemoryKeycloakAdmin())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    r = client.post("/api/signup/complete", json={
        "token": raw, "otp": "000000", "workspace_name": "x",
        "workspace_alias": "y", "property_name": "z", "pms_source": "opera",
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "p",
    })
    assert r.status_code == 403
    # The invite stays pending — a failed OTP must not consume it.
    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        assert invites.validate(s, raw) is not None


def test_the_endpoint_opens_the_provisioner_session_only_in_completion(
    db_url, tmp_path, _founding_committed
):
    """The confinement pin: GET invite and POST otp NEVER open the provisioner
    session; complete opens it exactly once."""
    opened: list[str] = []

    def spy(factory):
        def wrapped():
            opened.append("prov")
            return factory()
        return wrapped

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin(), spy=spy)

    client.get(f"/api/signup/invite/{raw}")
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    assert opened == []  # provisioner untouched by the read + otp paths

    code = notifier.smses[-1]["body"]
    client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "Only Once Group",
        "workspace_alias": "only-once-group", "property_name": "H",
        "pms_source": "opera", "wage_jurisdiction": "US-CA",
        "cell": "+15550000000", "password": "p",
    })
    assert opened == ["prov"]  # exactly one confined provisioning session
```

> **Resolve at implementation:** confirm `tests/authkit.py` exposes
> `make_authkit()` and `DEFAULT_ORG_ALIAS` (it does — used across the L7/L8
> tests). The signup router does NOT use auth, so `make_authkit`'s verifier is
> only there because `create_app` requires a `token_verifier`; any verifier is
> fine.

### 7b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_signup_api.py
```

### 7c. Implement

**`src/usali/signup_api.py`** — the ungated router. It reads seams off
`request.app.state`. The invite/OTP work runs on `db_session_factory` (app role,
non-`OrgScoped` tables need no org context); the completion runs on the
provisioner factory and is the ONLY place that factory is opened.

```python
"""Public, UNGATED signup surface (Track B/B1). Mounted like kiosk_router — no
operator_gates: these endpoints are reached by an unauthenticated owner holding
an invite token. Every refusal is fail-closed and names nothing (no existence
oracle). The one elevated credential — the usali_provisioner session — is opened
ONLY by /complete, which runs exactly provision_tenant + invites.consume."""

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from usali import invites
from usali.otp import OtpService
from usali.provisioning import provision_tenant

router = APIRouter(prefix="/api/signup")

_OTP_PURPOSE = "signup_cell"
# A workspace alias is the KC-org join key: a bounded, URL-safe identifier.
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class OtpRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    cell: str = Field(min_length=3, max_length=32)


class CompleteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    otp: str = Field(min_length=1, max_length=12)
    workspace_name: str = Field(min_length=1, max_length=200)
    workspace_alias: str = Field(min_length=2, max_length=63)
    property_name: str = Field(min_length=1, max_length=200)
    pms_source: str = Field(min_length=1, max_length=20)
    wage_jurisdiction: str = Field(min_length=1, max_length=10)
    cell: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=200)


def _refuse() -> HTTPException:
    # One 404 for every invite miss — unknown, expired, consumed, revoked all
    # look identical (no oracle over which invites exist).
    return HTTPException(status_code=404, detail="not found")


@router.get("/invite/{token}")
def get_invite(token: str, request: Request) -> dict[str, str]:
    factory = request.app.state.db_session_factory
    with factory() as session:
        invite = invites.validate(session, token)
        if invite is None:
            raise _refuse()
        return {"email": invite.email}


@router.post("/otp", status_code=204)
def send_otp(payload: OtpRequest, request: Request) -> None:
    limiter = request.app.state.signup_rate_limiter
    if not limiter.allow(f"otp:{payload.cell}"):
        raise HTTPException(status_code=429, detail="too many requests")
    factory = request.app.state.db_session_factory
    otp: OtpService = request.app.state.otp_service
    notifier = request.app.state.notifier
    with factory() as session:
        invite = invites.validate(session, payload.token)
        if invite is None:
            raise _refuse()
        code = otp.issue(session, purpose=_OTP_PURPOSE, target=payload.cell)
        session.commit()
    # Send AFTER commit so a delivered code always has a stored challenge.
    notifier.send_sms(to=payload.cell, body=code)


@router.post("/complete", status_code=201)
def complete(payload: CompleteRequest, request: Request) -> dict[str, str]:
    limiter = request.app.state.signup_rate_limiter
    if not limiter.allow(f"complete:{payload.token}"):
        raise HTTPException(status_code=429, detail="too many requests")
    if not _ALIAS_RE.match(payload.workspace_alias):
        raise HTTPException(status_code=422, detail="invalid workspace alias")

    factory = request.app.state.db_session_factory
    otp: OtpService = request.app.state.otp_service

    # Gate on the APP-role session (invite + OTP live on non-OrgScoped tables).
    # The OTP attempt increment is committed regardless of the verify outcome.
    with factory() as session:
        invite = invites.validate(session, payload.token)
        if invite is None:
            raise _refuse()
        verified = otp.verify(session, purpose=_OTP_PURPOSE,
                              target=payload.cell, code=payload.otp)
        session.commit()
    if not verified:
        raise HTTPException(status_code=403, detail="verification failed")

    kc = request.app.state.keycloak_admin
    # The CONFINED path: the ONLY place the provisioner session is opened. It
    # runs exactly provision_tenant + invites.consume, then commits. A KC/DB
    # failure rolls back and leaves the invite pending (idempotent re-try safe).
    prov_factory = request.app.state.provisioner_session_factory
    with prov_factory() as session:
        # Re-validate the invite on THIS session before consuming — the app-role
        # gate above and this write must agree on a still-pending invite.
        fresh = invites.validate(session, payload.token)
        if fresh is None:
            raise _refuse()
        result = provision_tenant(
            session, kc,
            org_name=payload.workspace_name,
            org_alias=payload.workspace_alias,
            admin_username=payload.workspace_alias,
            admin_email=invite.email,
            admin_full_name=invite.email,
            password=payload.password,
        )
        invites.consume(session, fresh, org_id=result.org_id)
        session.commit()
    return {"org_alias": payload.workspace_alias}
```

> **Resolve at implementation:** the provisioner session reads/writes `invite`
> (validate + consume). That requires the provisioner role to have SELECT +
> UPDATE on `invite`. Task 1's migration granted the provisioner **only**
> `organization` + `role_assignment` (deliberately — the deny pin). **Decision
> to bind here:** keep the invite `validate`+`consume` on the APP-role session,
> not the provisioner session — split the completion into (a) app-role txn:
> validate + OTP verify + `invites.consume`, then (b) provisioner txn:
> `provision_tenant` only. Prefer this split because it preserves the D-B7
> invariant that the provisioner touches ONLY the two provisioning tables.
> Concretely, move `invites.consume` into the app-role `with factory()` block
> AFTER a successful provision, OR consume in the same app-role transaction
> guarded by a "provision succeeded" flag. The trade-off (consume and provision
> in separate transactions) is acceptable because `provision_tenant` is
> find-or-create idempotent: if consume fails after provision, a re-run adopts
> the existing org and re-consumes. **Implement the split; do NOT grant the
> provisioner role any access to `invite`** — that would widen the least-
> privilege surface the whole task exists to keep narrow. Update the
> confinement test if the ordering of the two `with` blocks changes, keeping the
> assertion that the provisioner factory is opened exactly once.

**`src/usali/server.py`** — three wiring changes:

1. Imports: `from usali.signup_api import router as signup_router`,
   `from usali.otp import OtpService`, `from usali.ratelimit import RateLimiter`,
   `from usali.db import make_engine, make_session_factory` (already imported).

2. `create_app` gets a `provisioner_session_factory` kwarg and a default builder:

```python
def _provisioner_session_factory_from_settings(settings: Settings) -> SessionFactory:
    """The least-privilege provisioner session factory (D-B7): an UNBOUND base
    factory connected as usali_provisioner. Unbound on purpose — provision_tenant
    refuses an org-instrumented session; this role's permissive RLS policy lets
    its cross-org writes land without BYPASSRLS."""
    from sqlalchemy.engine import make_url
    prov_url = make_url(settings.db_url).set(
        username=settings.provisioner_db_role,
        password=settings.provisioner_db_password,
    ).render_as_string(hide_password=False)
    return make_session_factory(make_engine(prov_url))
```

Add the kwarg to `create_app(... provisioner_session_factory: SessionFactory | None = None, notifier: Notifier | None = None)` and, after the notifier wiring:

```python
    # Provisioner seam (D-B7): the confined signup-completion path's ONLY
    # elevated credential. Tests inject a factory on the provisioner role; the
    # default builds one from settings. Unbound (owner-style) — provision_tenant
    # refuses an instrumented session.
    app.state.provisioner_session_factory = (
        provisioner_session_factory
        or _provisioner_session_factory_from_settings(settings)
    )
    # OTP + rate-limit singletons for the public signup surface.
    app.state.otp_service = OtpService()
    app.state.signup_rate_limiter = RateLimiter(
        max_events=settings.signup_otp_max_per_window,
        window_seconds=settings.signup_rate_window_seconds,
    )
```

3. Include the router WITHOUT `operator_gates` (next to `kiosk_router`):

```python
    # Public, UNGATED signup surface (Track B/B1) — like kiosk_router, mounted
    # without operator_gates: an unauthenticated owner holding an invite reaches
    # it. Its own invite + OTP checks are the gate.
    app.include_router(signup_router)
```

### 7d. Gates & commit

```bash
uv run pytest -q tests/test_b1_signup_api.py
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): ungated public signup router + confined provisioner seam"
```

---

## Task 8 — `usali invite <email>` CLI

Mirrors `seed-roster`'s structure (session factory, loud failure). Creates the
invite, renders the link, and sends it via the `Notifier`.

> **Interface note (resolve at implementation):** the design says the CLI
> "mirrors seed-roster for KC access", but creating an invite touches no
> Keycloak — the invite is PRE-tenant and KC is only involved at
> `/complete`. So this command needs the **Notifier + a DB session**, not a
> `KeycloakAdmin`. Build the Notifier via `notifier_from_settings(get_settings())`.
> Follow seed-roster only for the command *shape* (typer command, session
> factory, `typer.Exit(1)` on failure). This is called out under "Deviations".

### 8a. Failing test

Add `tests/test_b1_invite_cli.py`:

```python
"""usali invite <email>: creates a pending invite and sends the link via the
Notifier; the CLI prints the invite link."""

from typer.testing import CliRunner

import usali.cli as cli
from tests.notifiers import CapturingNotifier
from usali import invites
from usali.db import make_engine, make_session_factory
from tests.orgwall import app_role_url
from sqlalchemy import select
from usali.models import Invite


def test_invite_command_creates_row_and_sends_link(db_url, monkeypatch, _cli_env):
    captured = CapturingNotifier()
    monkeypatch.setattr(cli, "_notifier", lambda: captured, raising=False)
    # Point the CLI's notifier factory at the fake (see implementation hook).
    result = CliRunner().invoke(cli.app, ["invite", "owner@example.test"])
    assert result.exit_code == 0, result.output
    assert "signup?token=" in result.output
    assert len(captured.emails) == 1 and captured.emails[0]["to"] == "owner@example.test"

    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        row = s.execute(select(Invite).where(Invite.email == "owner@example.test")).scalar_one()
        assert row.status == "pending"
```

> **Resolve at implementation:** the CLI builds its own session factory from
> `get_settings().db_url` (default owner creds `usali:usali`). The
> `_cli_env` fixture must point `USALI_DB_URL` at the test container
> (`monkeypatch.setenv("USALI_DB_URL", db_url)`) and the notifier at the fake.
> Mirror how the existing CLI tests (e.g. the cpa-pack CLI test referenced in
> the recent de-flake commit) set `USALI_DB_URL`; reuse that exact idiom rather
> than inventing a new one. If the existing CLI tests use a helper fixture for
> the DB env, reuse it and delete `_cli_env` from this test.

### 8b. Run it (RED)

```bash
uv run pytest -q tests/test_b1_invite_cli.py
```

### 8c. Implement

**`src/usali/cli.py`** — add imports and the command. Add near the other
imports:

```python
from usali import invites
from usali.notifications import Notifier, notifier_from_settings
```

Add a small seam so tests can inject the notifier (the seed commands build their
KC client inline; here we build the notifier through a module-level hook):

```python
def _notifier() -> Notifier:
    return notifier_from_settings(get_settings())
```

Add the command:

```python
@app.command("invite")
def invite_cmd(
    email: str = typer.Argument(..., help="Email address to invite (pilot gate)"),
) -> None:
    """Create a one-time invite for EMAIL and send the signup link (D-B4).

    Pilot invite origination: writes a pending invite row and emails the link
    via the configured Notifier (console in dev — the link is logged). A GA
    admin surface replaces this later; public signup CONSUMES the invite.
    """
    settings = get_settings()
    with _session_factory()() as s:
        invite, raw_token = invites.create_invite(s, email)
        s.commit()
    link = f"{settings.public_base_url.rstrip('/')}/signup?token={raw_token}"
    _notifier().send_email(
        to=email,
        subject="Your Open Hospitality workspace invite",
        body=f"You've been invited to create a workspace. Open: {link}",
    )
    typer.echo(f"Invited {email}: {link}")
```

> **Resolve at implementation:** `_session_factory()` returns a founding-org-
> bound factory. `Invite` is NOT `OrgScoped`, so writing it through a bound
> session is fine — the write-stamp wall only touches `OrgScoped` rows, and
> `invite` carries no `org_wall` policy. Confirm the CLI's default `db_url`
> role can INSERT `invite` (the owner `usali` can; in a locked-down deploy where
> the CLI runs as `usali_app`, the l2 default privileges cover it). No change
> needed; just verify in the test container.

### 8d. Gates & commit

```bash
uv run pytest -q tests/test_b1_invite_cli.py
uv run pytest -q && uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(b1): usali invite <email> CLI (create + send link via Notifier)"
```

---

## Self-review checklist

Run the full suite one final time (`uv run pytest -q && uv run mypy src &&
uv run ruff check src tests`) and confirm each of the following.

**Security / D-B7 (the sharp edges):**
- [ ] `usali_provisioner` is created LOGIN-only (no BYPASSRLS/SUPERUSER/CREATEROLE/CREATEDB) in `dev_pg_init.sql` and `tests/orgwall.ensure_provisioner_role`, and `bootstrap.sh` is noted for cloud.
- [ ] The migration grants the provisioner INSERT/SELECT on **only** `organization` + `role_assignment` (+ their identity sequences) and adds the role-scoped **permissive** `provisioner_wall` policy — NOTHING on any tenant-data table.
- [ ] `test_provisioner_cannot_read_tenant_data` passes: SELECT on `employee` is `permission denied` connected as the provisioner.
- [ ] The `provisioner_wall` policy name is NOT `org_wall`, so `test_the_rls_inventory_is_complete_and_forced` still passes unchanged.
- [ ] The signup-completion path opens the provisioner session in exactly one place; `invites.consume`/`validate` stay on the APP-role session so the provisioner never needs a grant on `invite` (the split in Task 7's resolution note is implemented).
- [ ] The confinement test asserts GET-invite and POST-otp open the provisioner factory ZERO times; complete opens it once.

**Correctness / fail-closed:**
- [ ] Invite tokens and OTP codes are stored only hashed (SHA-256); raw values appear once (link/SMS) and are never persisted in the clear.
- [ ] Every invite miss (unknown/expired/consumed/revoked) returns the SAME 404; OTP failure returns 403; neither is an existence oracle.
- [ ] A wrong/expired OTP leaves the invite `pending` (pinned); a completed signup marks it `consumed` with `consumed_org_id`.
- [ ] `provision_tenant(..., password=...)` sets the KC password AND clears UPDATE_PASSWORD + sets emailVerified (fake + real client both).
- [ ] Idempotent re-run: `provision_tenant` is find-or-create; a re-tried completion after a partial failure adopts the existing org.

**Tenancy invariants:**
- [ ] `Invite` and `OtpChallenge` are plain `Base` (NOT `OrgScoped`), carry no `org_id`, and have no `org_wall` RLS policy (D-B3).
- [ ] `usali_app` can read/write `invite` + `otp_challenge` via l2's `ALTER DEFAULT PRIVILEGES` (no new grant added).
- [ ] The migration chain is linear (`m2a0perffoundations → b1a0provrole → b1b0invite → b1c0otp`); `get_heads()` length stays 1 throughout.
- [ ] `tests/test_l4_org_grants.py` head literal ends at `["b1c0otp"]`; `tests/test_l3_org_resolution.py` and `tests/test_l2_rls_wall.py` single-head assertions still pass.
- [ ] `tests/conftest.py` `db_url` fixture and `tests/test_l2_rls_wall.py::test_the_migration_refuses_without_the_app_role` both create the provisioner role before upgrading to head.

**Seams / config:**
- [ ] `notifier`, `provisioner_session_factory` are `create_app` kwargs defaulting to settings-built factories (mirror `photo_store`/payroll seams).
- [ ] `USALI_NOTIFIER` selects the notifier; unknown value fails fast.
- [ ] The signup router is included WITHOUT `operator_gates` (ungated, like `kiosk_router`).

**Hygiene:**
- [ ] Each task ended with its own `git commit`; no task left the tree red on any of the three gates.
- [ ] `mypy` was run against `src` only; fixtures are synthetic (no real PII / phone numbers / secrets).
- [ ] The SIGNUP FRONTEND remains out of scope (Part-2 plan).

## Deviations from the stated facts (verified against the code)

- **"THREE tests assert the exact single head."** Only ONE test hardcodes the
  head *literal* — `tests/test_l4_org_grants.py::test_l4_is_the_single_alembic_head`
  (`== ["m2a0perffoundations"]`), which each migration task bumps. The l3 and l2
  head tests assert only `len(get_heads()) == 1` and stay green without a literal
  change; the plan RUNS all three but edits only l4. Additionally, a fourth
  head-touching test — `tests/test_l2_rls_wall.py::test_the_migration_refuses_without_the_app_role`
  — must be updated (create the provisioner role before its final upgrade-to-head),
  which the facts did not mention.
- **CLI "mirrors seed-roster for KC access."** Invite creation touches no
  Keycloak (the invite is pre-tenant; KC is engaged only at `/complete`), so the
  `usali invite` command needs the **Notifier + a DB session**, not a
  `KeycloakAdmin`. It mirrors seed-roster's command *shape* only.
- **Provisioner access to `invite`.** To hold the D-B7 line ("the provisioner
  can touch ONLY `organization` + `role_assignment`"), the plan keeps
  `invites.validate`/`consume` on the app-role session and gives the provisioner
  session `provision_tenant` alone — a small split from the design's "runs
  exactly provision_tenant + invite.consume [on the provisioner session]"
  phrasing, chosen so the least-privilege grant stays as narrow as the deny pin
  requires.
```
