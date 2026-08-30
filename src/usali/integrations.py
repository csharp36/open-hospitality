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
`resolve_qbo` hands the SAME factory to `DbTokenStore`, which writes the
rotated refresh token back in its own short transaction (D-OH17.7). All three
take the same shape so no call site has to remember which is which. Read
`DbTokenStore`'s docstring before relying on the rotation guarantee: it is
durable and per-tenant, and it serializes NOTHING — not across processes and,
since D-OH17.6 removed the shared-client memoizer, not within one either. That
is a decision rather than an omission.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.adp_adapter import AdpAdapter
from usali.config import get_settings
from usali.crm_feed import CrmFeed
from usali.delphi_adapter import DelphiAdapter
from usali.gusto_adapter import GustoAdapter
from usali.models import OrgIntegrationCredential
from usali.payroll_provider import PayrollProvider
from usali.qbo_client import QboClient
from usali.tenancy import SessionFactory
from usali.tripleseat_adapter import TripleseatAdapter

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


# Operator-facing names, for refusal messages only. The row stores "qbo"; a
# hotel controller reads "QuickBooks Online". Never used as a key.
_PRODUCT_NAMES: dict[str, str] = {
    "gusto": "Gusto",
    "adp": "ADP",
    "qbo": "QuickBooks Online",
    "delphi": "Delphi",
    "tripleseat": "Tripleseat",
}

_INTEGRATION_LABELS: dict[str, str] = {
    PAYROLL: "payroll",
    ACCOUNTING: "accounting",
    DEMAND_FEED: "demand feed",
}


def not_connected_detail(integration: str) -> str:
    """The ONE wording for "this tenant has not connected X" (ADR-010).

    Three surfaces refuse this way — the QBO push, the pay run, the demand
    pull — and they used to answer "what next?" three different ways: one
    named the product, one named no provider at all, one named both
    candidates. One function so the convention cannot drift again: the
    integration SLOT, then the products that can fill it, then where to go.

    Naming the candidates discloses nothing. They are `PROVIDERS`, the closed
    product set, identical for every tenant and visible in the docs — what
    must never appear here is which one THIS tenant chose, or any part of a
    credential.

    `/integrations` is a FORWARD REFERENCE: the connect UI lands in OH-17
    tasks 10-11, so until then this path 404s for anyone who follows it. That
    is deliberate — a named blocker an operator can act on soon beats a
    refusal naming an env var they cannot act on at all (ADR-010, and the
    reason `USALI_CRM_PROVIDER` came out of these strings). When the UI ships,
    this is the one string to revisit."""
    label = _INTEGRATION_LABELS.get(integration, integration)
    products = [
        _PRODUCT_NAMES.get(spec.provider, spec.provider)
        for spec in PROVIDERS
        if spec.integration == integration
    ]
    choices = " or ".join(products)
    return (
        f"{label} is not connected for this tenant — "
        f"connect {choices} on /integrations"
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


# ----------------------------------------------- connect-time verification


class CannotVerify(Exception):
    """This credential cannot be PROVEN here, so it must not be stored.

    Distinct from the adapters' own error types on purpose. Those mean "the
    provider said no" — a typo'd key, a company the token cannot reach — and
    the router reports them as such. This one means "nothing was asked",
    which would otherwise be indistinguishable from a pass: a verification
    that silently returns on the path it cannot handle is a verification that
    always succeeds, and D-OH17.8 would be false exactly where it matters.

    Two inhabitants today, both loud rather than silent:
      * a demand feed in a workspace where no property carries a `crm_ref` —
        every real CRM read is property-scoped, so there is nothing to verify
        against (ADR-010: name the blocker the operator can act on);
      * QuickBooks, whose credential is proven by COMPLETING the OAuth grant
        (OH-17 Task 11) and cannot be proven from a paste at all — Intuit
        rotates the refresh token on every grant, so "checking" a pasted one
        would spend it and leave the stored copy dead on first use.
    """


def verify_credentials(
    integration: str, provider: str, values: dict[str, Any], crm_ref: str | None
) -> None:
    """Prove a credential authenticates BEFORE it is stored (D-OH17.8).

    Builds the adapter from the supplied values plus deployment config and
    calls its `verify()`. Raises the adapter's own error type on failure,
    which the router turns into a 422. Nothing is written — not here, and not
    by any `verify()` it calls (each is pinned read-only at the wire by
    `test_provider_contract` / `test_j3_crm_adapters`).

    Dispatches on the PROVIDER passed in, never on which fields happen to be
    present: the router has already validated the pair against `spec_for`,
    and inferring a provider from its field names would silently pick the
    wrong adapter the first time two providers share a field name — sending
    one provider's secret to the other.

    The adapter CLASSES are read through this module's globals rather than
    captured, so a test can substitute a transport-injecting subclass and
    still exercise the real adapter code. Do not "optimize" this into a dict
    built at import time.

    TOTAL BY CONSTRUCTION: the final `raise` is what makes this a
    verification rather than a formality. A provider added to `PROVIDERS`
    (and to the DB CHECK) but forgotten here would otherwise fall off the end
    returning None — which reads as "verified" — and its credentials would be
    stored unchecked while every existing test still passed.

    `integration` is not read today — the provider name alone determines the
    adapter, because `PROVIDERS` keys providers to exactly one slot. It stays
    in the signature because this is a SEAM (`create_app(verify_integration=)`)
    and its shape is the contract a substitute has to satisfy; a caller that
    had to remember which arguments this particular implementation happens to
    consult would be a worse seam.
    """
    del integration
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
            raise CannotVerify(
                "no property in this workspace has a crm_ref, so the demand "
                "feed cannot be verified — declare one in "
                "mapping/properties.yaml and re-seed first"
            )
        feed: CrmFeed = (
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
    if provider == "qbo":
        raise CannotVerify(
            "QuickBooks Online is connected by authorizing Open Hospitality "
            "in Intuit, not by pasting a token — start the authorization "
            "from /integrations"
        )
    # See TOTAL BY CONSTRUCTION above. Not a defensive `else`: this is the
    # branch that makes forgetting a provider fail loudly instead of storing
    # its credentials unverified.
    raise CannotVerify(f"no verification is implemented for provider {provider!r}")


# --------------------------------------------------------------- resolution


class IntegrationNotConfigured(Exception):
    """This tenant has no credential row for the integration.

    Raised only where a caller has ALREADY decided a connection must exist —
    `DbTokenStore`, which is reached from inside a push that resolved a client
    a moment ago. The `resolve_*` functions below return None instead, because
    "not connected" is an ordinary state their callers refuse loudly on their
    own terms (a pay run 409s; the demand pull degrades to the OFF sentinel).
    Raising there would turn the checklist's honest "open" into a 500."""

    def __init__(self, integration: str) -> None:
        super().__init__(f"{integration} is not connected for this tenant")
        self.integration = integration


class DbTokenStore:
    """The QBO refresh token, held on the tenant's credential row (D-OH17.7).

    Intuit rotates the refresh token on EVERY grant, so the holder must write
    the new one back. Before OH-17 the holder was process memory: a restart
    lost the rotation and the next push `invalid_grant`ed against a token
    Intuit had already spent. This store makes the lineage per-tenant and
    DURABLE, which is the bug OH-17 actually fixes.

    Each call opens its OWN short session off the org-bound factory rather
    than joining the caller's request transaction: a push holds its
    transaction for the whole HTTP call, and a rotation must be committed the
    moment it is known — a rotation rolled back with a later failure is a
    token spent at Intuit and lost here, the exact restart bug in a new shape.

    WHAT THIS DOES NOT DO — read this before trusting it (settled 2026-08-30,
    superseding D-OH17.7's original "under a row lock" wording). It does NOT
    serialize concurrent refreshes. The critical section is `load()` ->
    outbound grant -> `store()`; a `SELECT ... FOR UPDATE` taken inside
    `load()` and released when that short session closes covers none of it,
    and a lock that spanned the grant would have NO RELEASE PATH on failure:
    `QboClient._refresh` raises on a bad grant and returns without ever
    calling `store()`. Refresh failure is routine — a revoked or expired token
    fails every grant — so that shape would leak a connection and strand a
    locked row on the common failure while protecting against a rare one.

    That is a scope judgement, not an impossibility: `TokenStore` could grow a
    `rotating()` context manager (or an `abort()`) and get its try/finally.
    The port shape was frozen in the task before this one, and reopening it
    to buy protection against a month-end race did not earn its way in.

    THE STANDING GUARANTEE IS DURABILITY AND PER-TENANT SCOPE — nothing more.
    Do NOT add "and serialized in-process by `QboClient`'s instance lock" back
    to this list, however true it looks: that lock is per-INSTANCE, so it
    serializes only callers sharing ONE client, and D-OH17.6 DELETED the
    `server._shared` memoizer that was the reason they did. Each operator
    action now builds its own client, so two concurrent pushes inside a single
    process fork the lineage exactly as two processes would.

    In every one of those cases the outcome is the same and is ACCEPTED: both
    callers spend the same token, the loser's grant returns `invalid_grant`
    and its push fails visibly, and the winner's rotated token is in the row,
    so a retry succeeds. Nothing is silently lost. These are month-end
    operator actions, not a request path. If that ever stops holding, the fix
    is a lock taken and released around the WHOLE refresh by the caller — the
    `rotating()` port change above, or a Postgres advisory lock — never a
    `FOR UPDATE` smuggled into `load()`."""

    def __init__(self, factory: SessionFactory) -> None:
        self._factory = factory

    def load(self) -> str:
        with self._factory() as session:
            row = credential_for(session, ACCOUNTING)
            if row is None or row.refresh_token is None:
                raise IntegrationNotConfigured(ACCOUNTING)
            return row.refresh_token

    def store(self, refresh_token: str) -> None:
        # Through the MAPPED ATTRIBUTE, not a Core `update()` and never a
        # `text()` UPDATE, for two reasons that both fail silently:
        # (1) ADR-005 — the EncryptedString bind processor runs on the ORM
        #     attribute; a raw UPDATE would write the rotated token to disk in
        #     plaintext while every other test still passed.
        # (2) The unit of work emits `WHERE org_id = ? AND integration = ?`
        #     from the composite PK, so the write is org-scoped by the key
        #     itself. A bare `update(...).where(integration == ACCOUNTING)`
        #     carries no org_id, and tenancy._stamp_wall covers INSERTs only —
        #     UPDATEs "ride the DB wall alone", so such a statement would be
        #     confined by RLS and nothing else (and RLS is bypassed by a
        #     superuser connection, which is what the test suite runs as).
        with self._factory() as session:
            row = credential_for(session, ACCOUNTING)
            if row is None:
                # The row vanished mid-push (a disconnect between resolve and
                # rotation). Refusing loudly beats a 0-row UPDATE that drops
                # the rotated token and reports success.
                raise IntegrationNotConfigured(ACCOUNTING)
            row.refresh_token = refresh_token
            session.commit()


@dataclass(frozen=True)
class ResolvedPayroll:
    """A payroll adapter AND the provider name it was built from.

    The pair is returned together because callers PERSIST the name: it is the
    lookup key of `ProviderEmployeeRef` (`payroll_run.sync_employees`), and
    those refs supply `PayRunEntry.provider_employee_id` on submission. A
    caller that took the adapter from the tenant's row but the name from
    `settings.payroll_provider` would key ADP-side employee ids under "gusto"
    the moment the two disagreed — and a later switch to Gusto would find them
    "fresh" and submit ADP ids to Gusto. That is a mis-PAY, not a mislabel,
    which is why D-OH17.1's "provider and credentials together, inseparable"
    has to hold for the NAME too, not just the secrets.

    Splitting this back into a bare adapter return would restore exactly that
    bug, and would do it silently: every existing test would still pass,
    because a test fake that declares no provider name reads as "".

    Its two siblings do not need this shape, for reasons that are theirs
    alone. `resolve_qbo`'s name is the literal "qbo" — the entire accounting
    half of the closed set, persisted nowhere as a key. `resolve_crm_feed`'s
    name IS persisted (`crm_pull_batch.provider`) but is read by its callers
    from `integrations.connected_provider` on the session they already hold,
    which is the same row in the same transaction."""

    provider_name: str
    adapter: PayrollProvider


def resolve_payroll(factory: SessionFactory) -> ResolvedPayroll | None:
    """The tenant's payroll adapter + its provider name, or None when payroll
    is not connected. See `ResolvedPayroll` for why the name rides along."""
    settings = get_settings()
    with factory() as session:
        row = credential_for(session, PAYROLL)
        if row is None:
            return None
        if row.provider == "gusto":
            return ResolvedPayroll(row.provider, GustoAdapter(
                base_url=settings.gusto_base_url,
                api_token=row.api_token or "",
                company_id=row.company_id or "",
            ))
        if row.provider == "adp":
            return ResolvedPayroll(row.provider, AdpAdapter(
                base_url=settings.adp_base_url,
                client_id=row.client_id or "",
                client_secret=row.client_secret or "",
            ))
        # Unreachable while PROVIDERS and the CHECK agree — which is exactly
        # why it must raise rather than return None. A provider added to the
        # CHECK and forgotten here would otherwise read as "not connected",
        # and the tenant's connected payroll would silently stop running.
        raise RuntimeError(f"unknown payroll provider {row.provider!r}")


def resolve_qbo(factory: SessionFactory) -> QboClient | None:
    """The tenant's QBO client, or None when accounting is not connected.

    Base URL and OUR Intuit application id/secret stay process-wide
    (D-OH17.3) — they identify the app, not the tenant. Only the realm and the
    rotating refresh token are per-tenant, and the token is not read here at
    all: the client pulls it through `DbTokenStore` at grant time, so a
    rotation another worker committed is picked up rather than shadowed by a
    stale snapshot taken when the client was built.

    The SAME factory goes to the store — not a session. The session opened
    here is closed before the client is returned; a store holding it would be
    reading a dead session on the first refresh."""
    settings = get_settings()
    with factory() as session:
        row = credential_for(session, ACCOUNTING)
        if row is None:
            return None
        if row.provider != "qbo":
            # Accounting has exactly one provider today, so unlike its two
            # siblings this guard has no second branch to dispatch to — but it
            # is the SAME rule, and it is here precisely because this is the
            # function where a second one (a Xero, say) would be added. Built
            # unconditionally, a QboClient over a non-QBO row would send our
            # Intuit application credentials to whatever that provider is.
            raise RuntimeError(f"unknown accounting provider {row.provider!r}")
        realm_id = row.realm_id or ""
    return QboClient(
        settings.qbo_base_url,
        settings.qbo_client_id,
        settings.qbo_client_secret,
        realm_id,
        DbTokenStore(factory),
    )


def resolve_crm_feed(factory: SessionFactory) -> CrmFeed | None:
    """The tenant's demand feed, or None when it is not connected — the
    honest successor to `org_settings.crm_provider == ''`."""
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
        # As in resolve_payroll: a provider in CRM_PROVIDERS but not here must
        # fail by name, never degrade to the OFF sentinel.
        raise RuntimeError(f"unknown crm provider {row.provider!r}")
