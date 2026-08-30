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

The `resolve_*` functions (arrive with the resolution seam; they do not exist
yet in this module) take an org-bound SESSION FACTORY, not a session:
`resolve_qbo` hands the same factory to `DbTokenStore` (also arrives with the
resolution seam), which must write the rotated refresh token back in its own
short transaction (D-OH17.7). All three take the same shape so no call site
has to remember which is which.
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
