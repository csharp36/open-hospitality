"""Tenant context — the ONE source of truth both L2 walls read.

Pillar L decision 2 (design/2026-08-01-d1-tenant-isolation-design.md):
two independent walls, either alone sufficient to stop a cross-org leak.

- *Application wall*: a per-session ``do_orm_execute`` hook adds
  ``with_loader_criteria(OrgScoped, org_id == current)`` (the documented
  mixin recipe) to every ORM SELECT that touches an org-scoped table —
  wherever the entity appears: result columns, aggregate-only FROMs,
  join-only entities, subqueries, and (by propagation) lazy loads. A
  handler that forgets to filter still gets filtered, and a session with
  NO org context refuses loudly (:class:`MissingOrgContext`) on its
  first org-scoped query, never returning a silently unscoped result.
  Plain-Core selects over org-scoped tables still hit the context
  REQUIREMENT (the structural traversal sees their tables); only the
  loader criteria cannot attach to them. ``text()`` statements bypass
  the hook entirely. The DB wall filters both. The READ wall is
  SELECT-only by design (decision 2): ORM ``update()``/``delete()``
  statements ride the DB wall alone, so no ORM-level READ pin catches a
  cross-org UPDATE/DELETE on an RLS-bypassing (superuser) engine — L4/L5 must
  not assume symmetric coverage there. INSERTs, however, DO have an
  application wall: the *write wall* (:func:`_stamp_wall`, L6a), a
  ``before_flush`` hook that stamps ``org_id`` from the session context
  on every pending ORM INSERT of an ``OrgScoped`` entity, so an org≠1
  session's INSERT lands in its OWN org instead of falling to the server
  default ('1') and dying on the DB wall's WITH CHECK (the pre-L6a
  fail-closed shape). It also refuses (:class:`OrgContextMismatch`) an
  INSERT that explicitly hardcodes a cross-org id, above the RLS WITH
  CHECK. This made org≠1 worlds writable — the hard prerequisite the
  plan's L6 bullet recorded before provisioning may create the first
  org≠1 tenant. Bulk/Core ``insert()`` statements bypass the flush and
  ride the DB wall alone (see :func:`_stamp_wall`).
- *Database wall*: per-table RLS (the ``l2a0rlswall`` migration) keyed
  on the transaction-local session variable :data:`RLS_ORG_VAR`; an
  ``after_begin`` hook issues the ``SET LOCAL`` equivalent
  (``set_config(..., true)``) at the top of every transaction. SET LOCAL
  is transaction-scoped, so pooled connections cannot bleed an org
  across requests; a transaction that never set it reads ZERO rows
  (fail-closed), because the policy compares against NULL.

One predicate, one function: both hooks obtain the org exclusively via
:func:`current_org_id` reading ``session.info`` — there is no second
place an org can come from, so the walls can disagree with each other
only by one of them being dropped (each drop is pinned by a test that
the OTHER wall cannot save).

Instrumentation is PER-SESSION (:func:`instrument_org_wall` /
:func:`bind_org_context`), not process-global: alembic, seeds run
against the owner role's un-instrumented sessions, and the test
fixtures' superuser sessions all keep their existing (unscoped)
behavior on purpose — the serving paths opt in explicitly through
:class:`OrgBoundSessionFactory`.
"""

from collections.abc import Callable
from typing import TypeAlias

from sqlalchemy import Table, event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import (
    ORMExecuteState,
    Session,
    SessionTransaction,
    with_loader_criteria,
)
from sqlalchemy.sql import Select, visitors

from usali.models import Organization, OrgScoped

# What every serving path actually needs from a "session factory": call
# it, get a Session. `sessionmaker[Session]` satisfies it, and so do the
# org-bound wrappers below — the honest annotation for `create_app`'s
# parameter and the routers' locals (which used to claim the concrete
# sessionmaker type while holding a wrapper).
SessionFactory: TypeAlias = Callable[[], Session]

# The RLS-bound database role the SERVING paths connect as: LOGIN only,
# not the table owner, no BYPASSRLS. CREATE ROLE is cluster-level, so
# the role is provisioned OUTSIDE the migration chain (dev compose init,
# scripts/cloud/bootstrap.sh, tests/orgwall.py) and the l2a0rlswall
# migration refuses to run until it exists. Alembic keeps the OWNER role.
APP_DB_ROLE = "usali_app"

# The transaction-local Postgres variable the RLS policies read:
# USING (org_id = current_setting('app.org_id', ...)).
RLS_ORG_VAR = "app.org_id"

# The transaction-local variable the l3a0aliaslookup policy reads: the
# ONE narrow door through which alias -> org_id resolution can see the
# `organization` row for an alias BEFORE any org is bound (the org_id
# policy would need the answer to grant the question). Set in exactly
# one place: resolve_org_id.
RLS_ALIAS_VAR = "app.org_alias"

# Where the org context lives on a session (session.info).
ORG_INFO_KEY = "org_id"
_INSTRUMENTED_KEY = "_org_wall_instrumented"

# The founding org — org 1 by construction (A2.1 seeds exactly one; the
# L1 backfill refused any other shape). Since L3, the REQUEST path binds
# the auth-resolved active org instead (auth.require_active_org); this
# constant remains for the paths that legitimately act within the
# founding org alone: the CLI, the demo seed, and the kiosk DEVICE
# surface (device-token auth carries no org claim — kiosk multi-org is
# deferred with the provisioning work, L6+).
FOUNDING_ORG_ID = 1


class MissingOrgContext(RuntimeError):
    """An org-scoped query ran on a session that never got an org.

    Raised INSTEAD of returning an unscoped result: a session that
    forgot its tenant must refuse loudly, never leak quietly.
    """


class OrgContextMismatch(RuntimeError):
    """An INSERT explicitly set an org_id that DIFFERS from the session org.

    The write wall's defense-in-depth above the RLS WITH CHECK: a handler
    that hardcodes a cross-org id fails here, BEFORE the flush reaches the
    DB, with a clean application error naming nothing sensitive — never a
    raw ProgrammingError/500 from the database wall. (An explicit org_id
    EQUAL to the session org passes through; an UNSET one is stamped.)
    """


def current_org_id(session: Session) -> int:
    """The org this session acts for — the single predicate both walls read."""
    try:
        return int(session.info[ORG_INFO_KEY])
    except KeyError:
        raise MissingOrgContext(
            "session has no org context — bind_org_context(session, org_id) "
            "must run before any org-scoped query; refusing loudly rather "
            "than returning an unscoped result"
        ) from None


def instrument_org_wall(session: Session) -> Session:
    """Attach both wall hooks to this session (idempotent).

    Instrumented-but-unbound is a legal state with fail-closed
    semantics: ORM org-scoped queries refuse (MissingOrgContext), raw
    SQL sees zero rows through RLS because no SET LOCAL was issued.
    """
    if not session.info.get(_INSTRUMENTED_KEY):
        event.listen(session, "do_orm_execute", _orm_wall)
        event.listen(session, "after_begin", _db_wall)
        event.listen(session, "before_flush", _stamp_wall)
        session.info[_INSTRUMENTED_KEY] = True
    return session


def is_org_instrumented(session: Session) -> bool:
    """True if the org walls are attached to this session (instrument_org_wall).

    The owner/superuser sessions alembic, seeds, and provisioning run on are
    NOT instrumented (they bypass RLS by construction); a request/org-bound
    session IS. :func:`usali.provisioning.provision_tenant` uses this to refuse
    an instrumented session with a diagnosable message rather than letting its
    cross-org DB writes fail deep in the walls.
    """
    return bool(session.info.get(_INSTRUMENTED_KEY))


def bind_org_context(session: Session, org_id: int) -> Session:
    """Instrument the session and bind it to one org.

    Rebinding MID-TRANSACTION composes fail-closed, not fail-open: the
    old org's SET LOCAL holds until the transaction ends while the ORM
    criteria already filter on the new org — an empty intersection, zero
    rows, until the next transaction re-issues SET LOCAL for the new
    binding. Bind before the first query and don't switch orgs mid-flight.
    """
    instrument_org_wall(session)
    session.info[ORG_INFO_KEY] = int(org_id)
    return session


_ORG_TABLE_NAMES: frozenset[str] | None = None


def _org_table_names() -> frozenset[str]:
    """Every org-scoped TABLE name, from the mapper registry (lazily —
    the registry must be fully configured before it is swept). The set
    FREEZES at first use: classes mapped after that are invisible to the
    traversal — irrelevant for the application (models.py maps everything
    at import), only for dynamically declared test classes."""
    global _ORG_TABLE_NAMES
    if _ORG_TABLE_NAMES is None:
        from usali.models import Base

        Base.registry.configure()
        _ORG_TABLE_NAMES = frozenset(
            mapper.local_table.name
            for mapper in Base.registry.mappers
            if issubclass(mapper.class_, (OrgScoped, Organization))
            and isinstance(mapper.local_table, Table)
        )
    return _ORG_TABLE_NAMES


def _touches_org_scoped(statement: Select[tuple[object, ...]]) -> bool:
    """Whether ANY org-scoped table appears anywhere in the statement —
    result columns, aggregate-only FROMs, joins, subqueries. Structural
    traversal, not `all_mappers`: the latter sees only top-level result
    entities and is blind to `select(func.count()).select_from(X)` and
    to entities that appear only in a join."""
    names = _org_table_names()
    for element in visitors.iterate(statement):
        if isinstance(element, Table) and element.name in names:
            return True
        table = getattr(element, "table", None)
        if isinstance(table, Table) and table.name in names:
            return True
    return False


def _orm_wall(execute_state: ORMExecuteState) -> None:
    """The application wall: org criteria on every ORM SELECT.

    The documented SQLAlchemy mixin recipe: ONE criteria option against
    `OrgScoped` covers every mixin entity wherever it appears in the
    statement — result columns, aggregate-only FROMs, join-only
    entities, subqueries — and propagates to lazy loads; `Organization`
    (org-scoped by its own PK, no mixin) gets the twin option. Column
    loads and relationship loads are skipped per the recipe (their
    statements are cached lambda constructs; the originating statement's
    options propagate to them).
    """
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.is_relationship_load
    ):
        return
    statement = execute_state.statement
    if not isinstance(statement, Select):
        # is_select is also true for non-Select shapes (FromStatement,
        # lambda constructs) that loader criteria cannot attach to — and
        # an assert would vanish under -O. The DB wall covers these.
        return
    if not _touches_org_scoped(statement):
        return  # org-free reference data needs no context
    session = execute_state.session
    org = current_org_id(session)  # no context -> MissingOrgContext, loudly
    execute_state.statement = statement.options(
        with_loader_criteria(
            OrgScoped, lambda cls: cls.org_id == org, include_aliases=True
        ),
        with_loader_criteria(
            Organization, lambda cls: cls.org_id == org, include_aliases=True
        ),
    )


def _db_wall(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    """The database wall's key: SET LOCAL the org at transaction begin.

    ``set_config(..., is_local => true)`` IS ``SET LOCAL`` — it expires
    with the transaction, so a pooled connection returns to the
    fail-closed state (policy vs NULL → zero rows) the moment the
    transaction ends. An unbound session sets nothing: RLS then yields
    zero rows rather than this hook guessing an org.
    """
    try:
        org = current_org_id(session)
    except MissingOrgContext:
        return
    connection.execute(
        text("SELECT set_config(:var, :org, true)"),
        {"var": RLS_ORG_VAR, "org": str(org)},
    )


def _stamp_wall(
    session: Session, flush_context: object, instances: object | None
) -> None:
    """The write wall: stamp/verify org_id on every pending ORM INSERT.

    The write-side twin of :func:`_orm_wall`, reading the SAME single
    predicate (:func:`current_org_id`) — one function, both walls, so
    they can disagree only by one being dropped. For each ``OrgScoped``
    object in ``session.new``:

    - org_id UNSET (None) → stamp it with the session org, so an org != 1
      session's INSERT lands in its OWN org rather than falling to the
      server default ('1') and dying on the DB wall's WITH CHECK. This is
      what makes org != 1 worlds writable (the L6 prerequisite).
    - org_id EXPLICITLY set and DIFFERENT from the session org → refuse
      LOUDLY (:class:`OrgContextMismatch`) BEFORE the flush hits the DB.
      Defense-in-depth above the RLS WITH CHECK: a hardcoded cross-org id
      fails with a clean app error, not a raw DB 500. (Equal is fine.)

    A session with NO org context refuses loudly (:class:`MissingOrgContext`
    from ``current_org_id``) the moment it tries to flush an org-scoped
    row — the write-side mirror of the read wall's refusal, never a
    silently unscoped write.

    ``Organization`` is EXEMPT by construction: it is org-scoped by its
    own PRIMARY KEY (``class Organization(Base)`` — NOT the ``OrgScoped``
    mixin), so ``isinstance(obj, OrgScoped)`` is False for it. Its
    ``org_id`` is the tenant IDENTITY being created (set explicitly by
    provisioning, L6b), not a reference to some other tenant — stamping
    or mismatch-checking it would break a founding-bound session creating
    org 2. The exemption is the smallest sound rule and is pinned.

    Scope: ORM unit-of-work INSERTs only. ANYTHING that skips the flush
    bypasses the stamp — Core ``session.execute(insert(...))`` statements
    AND the bulk ORM paths (``session.bulk_save_objects`` /
    ``session.bulk_insert_mappings`` and the other ``bulk_*``, which
    write straight to the DB without a per-object unit-of-work) — so none
    of them are stamped; the DB wall's WITH CHECK is their sole guard (a
    bulk ORM insert on an org≠1 session therefore surfaces a raw
    ProgrammingError from RLS, not a clean :class:`OrgContextMismatch`).
    This is the write-side analogue of the read-side Core-statement note
    on :func:`_orm_wall`; owner-side seeds/migrations use Core inserts and
    rely on the server default. UPDATEs/DELETEs carry no new org_id and
    ride the DB wall alone, as on the read side.
    """
    org: int | None = None  # resolved lazily, once, only if an OrgScoped row exists
    for obj in session.new:
        if not isinstance(obj, OrgScoped):
            continue  # Organization (org-scoped by its PK) and org-free rows
        if org is None:
            org = current_org_id(session)  # no context → MissingOrgContext, loudly
        if obj.org_id is None:
            obj.org_id = org
        elif int(obj.org_id) != org:
            raise OrgContextMismatch(
                "an INSERT set org_id explicitly to a value that differs "
                "from the session's org context — refusing the cross-org "
                "write loudly before it reaches the database wall"
            )


def resolve_org_id(session: Session, alias: str) -> int | None:
    """alias -> org_id, BEFORE any org is bound: the one pre-binding read
    in the system, and why it is safe (decision 3, the l3a0aliaslookup
    migration docstring carries the full argument):

    - The session must be a dedicated, short-lived, UN-instrumented one
      (straight off the base factory) — an instrumented session would
      refuse loudly (MissingOrgContext), which is correct: resolution is
      not a general query path.
    - Under the app role, the ONLY row this can see is the one whose
      kc_org_alias equals the transaction-local variable set here —
      unique column, exact match, at most one row, no enumeration.
    - `alias` must come from the token's VALIDATED membership claim
      (auth._active_org_alias refuses non-members before any SQL runs);
      nothing numeric from the token is ever an org_id — the DB row is
      the sole authority for the id.

    Returns None when no organization answers to the alias; the caller
    refuses exactly as it would a non-membership. The transaction is
    rolled back before returning so the variable dies with it and the
    caller can never accidentally reuse this session's window.
    """
    session.execute(
        text("SELECT set_config(:var, :alias, true)"),
        {"var": RLS_ALIAS_VAR, "alias": alias},
    )
    org_id: int | None = session.execute(
        select(Organization.org_id).where(Organization.kc_org_alias == alias)
    ).scalar_one_or_none()
    session.rollback()
    return org_id


class OrgBoundSessionFactory:
    """A session factory whose every session is already org-bound.

    Wraps (never mutates) the underlying factory: sessions the WRAPPED
    factory hands out elsewhere stay un-instrumented, so test fixtures
    and seeds sharing an engine are unaffected. Since L3 the REQUEST
    path binds per-request through auth.require_active_org; this class
    remains the constant-org binding for the founding-org-only paths
    (CLI, demo seed, kiosk device surface) and the reusable core the
    request path builds on once the org is resolved.
    """

    def __init__(self, factory: SessionFactory, org_id: int) -> None:
        self._factory = factory
        self._org_id = int(org_id)

    def __call__(self) -> Session:
        return bind_org_context(self._factory(), self._org_id)


class ActiveOrgSessionFactory:
    """The per-REQUEST session factory: every session it returns is bound
    (both L2 walls) to the request's validated active org and ONLY it.

    alias -> org_id resolution is deferred to the FIRST session request
    and cached for the request (as an :class:`OrgBoundSessionFactory`,
    which does the actual binding — one binding expression in this
    module, not two): routes that never touch the database never pay
    for (or depend on) the lookup, and the resolved org can never
    change mid-request. The lookup itself runs on a dedicated
    short-lived session from the BASE factory — see
    :func:`resolve_org_id` for why that pre-binding read is safe and
    cannot enumerate orgs.

    ``alias`` must already be VALIDATED against the token's membership
    claim (auth._active_org_alias). An alias no organization row
    answers to raises ``unknown_alias_error()`` — the HTTP layer passes
    a 403 whose words match the non-member refusal exactly (no
    existence oracle); this module stays framework-free.
    """

    def __init__(
        self,
        base_factory: SessionFactory,
        alias: str,
        unknown_alias_error: Callable[[], Exception],
    ) -> None:
        self._base = base_factory
        self._alias = alias
        self._unknown = unknown_alias_error
        self._bound: OrgBoundSessionFactory | None = None

    def __call__(self) -> Session:
        if self._bound is None:
            with self._base() as session:
                org_id = resolve_org_id(session, self._alias)
            if org_id is None:
                raise self._unknown()
            self._bound = OrgBoundSessionFactory(self._base, org_id)
        return self._bound()
