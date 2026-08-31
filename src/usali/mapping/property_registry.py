"""Seed the property system-of-record + detection registry from YAML into the DB.

`mapping/properties.yaml` is no longer read at detection time (see
`usali.detect.load_registry`); it is a seed source, upserted here into the
`organization` / `property` / `property_detection_alias` tables.
"""

import re
import secrets
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sqlalchemy import update

from usali.config import Settings, get_settings
from usali.models import (
    Organization,
    OrgIntegrationCredential,
    Property,
    PropertyDetectionAlias,
)
from usali.tenancy import FOUNDING_ORG_ID

# The founding hotel group (multi-org arrives via provisioning, Pillar L).
_DEFAULT_ORG = "Pilot Hotel Group"
# The founding org's Keycloak organization alias (L3, decision 3): must
# match the dev realm's organization (keycloak/realm-usali.json) and
# authkit's default claim — the contract is pinned in
# tests/test_oidc_realm_contract.py.
DEFAULT_ORG_ALIAS = "pilot-hotel-group"

# The `connected_by` recorded for env-seeded rows: no human connected these,
# and attributing them to one would be a lie in the audit trail.
_SEED_SUBJECT = "seed:env"


def ensure_default_org(session: Session) -> int:
    """Find-or-create THE default org, returning its id.

    One implementation on purpose (L1): the seed path and every test world
    must agree on which row is the founding org — two creators would split a
    world across org ids, exactly the state the L2 walls (ORM criteria + RLS)
    assume cannot exist. The founding org is org 1 BY CONSTRUCTION
    (FOUNDING_ORG_ID), so create it with an EXPLICIT id, keyed on the org_id
    PK — NOT an autoincrement keyed on name.

    Why explicit: the seed runs on a founding-BOUND session (job.sh keeps the
    owner role, which FORCE ROW LEVEL SECURITY still filters at query time), so
    the DB wall's WITH CHECK demands the new row's org_id EQUAL the bound org
    (== FOUNDING_ORG_ID). A bare autoincrement can hand out a different id — a
    prior failed insert already burned the sequence's 1, and nextval does not
    roll back — which RLS then refuses (the cloud non-superuser owner does not
    bypass FORCE RLS; only the test/dev superuser did, which is why this only
    ever surfaced in Cloud SQL). An explicit id passes the check
    deterministically and is idempotent on the PK. `Organization` is exempt
    from the write-stamp (org-scoped by its own PK), so the explicit id is not
    a cross-org write.

    L3: the founding org answers to the dev realm's Keycloak alias, so
    the seed stamps `kc_org_alias` — but only where it is NULL: a bare
    re-seed never blanks or overwrites an alias an operator set by hand
    (the seed_properties find-or-create posture).
    """
    # RETURNING tells us whether this call CREATED the founding org or found
    # it. `on_conflict_do_nothing` returns no row when it conflicts, so an
    # empty result means "already existed" — which is what gates the
    # credential seed at the end of this function. See there for why.
    org_was_created = session.execute(
        insert(Organization)
        .values(org_id=FOUNDING_ORG_ID, name=_DEFAULT_ORG, kc_org_alias=DEFAULT_ORG_ALIAS)
        .on_conflict_do_nothing(index_elements=["org_id"])
        .returning(Organization.org_id)
    ).first() is not None
    # An EXPLICIT id does not advance the identity sequence (no nextval was
    # called), so a later provisioned org's autoincrement would collide on the
    # founding id. Advance it past the current max.
    #
    # `max(org_id) FROM organization` CANNOT be that max. `organization` carries
    # FORCE RLS (l2a0rlswall) and the cloud seed runs as a non-superuser owner,
    # which does not bypass it; the seed session is founding-bound, so the
    # visible max is FOUNDING_ORG_ID no matter how many tenants exist. Reading
    # it made every re-seed RESET the sequence to 1 — and job.sh re-seeds on
    # EVERY deploy, so the next stranger to sign up got an id a tenant already
    # held and `POST /api/signup/complete` 500'd on organization_pkey. The
    # test/dev superuser bypasses RLS and sees the true max, which is why this
    # only ever surfaced in Cloud SQL.
    #
    # The sequence's OWN last_value is not policy-filtered, so it cannot be
    # hidden by the session's binding: taking it as a floor makes this
    # monotonic by construction — a re-seed can only ever advance. The filtered
    # max stays in the GREATEST because it can only raise the floor, never
    # lower it, and it still covers a sequence that somehow trails its table.
    # NULL (a sequence never yet called) is ignored by GREATEST.
    session.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('organization', 'org_id'), "
            "GREATEST("
            "pg_sequence_last_value("
            "pg_get_serial_sequence('organization', 'org_id')::regclass), "
            "(SELECT max(org_id) FROM organization), "
            ":founding))"
        ),
        {"founding": FOUNDING_ORG_ID},
    )
    session.execute(
        update(Organization)
        .where(
            Organization.org_id == FOUNDING_ORG_ID,
            Organization.kc_org_alias.is_(None),
        )
        .values(kc_org_alias=DEFAULT_ORG_ALIAS)
    )
    org_id = session.execute(
        select(Organization.org_id).where(Organization.org_id == FOUNDING_ORG_ID)
    ).scalar_one()
    if org_was_created:
        # FIRST PROVISIONING ONLY (2026-08-31). Seeding used to run on every
        # call, idempotent by `on_conflict_do_nothing` — which keys on the row
        # being PRESENT. Disconnecting is a row DELETE
        # (`integrations_api.disconnect`), so absence read as "never seeded"
        # rather than "deliberately removed", and `scripts/cloud/job.sh` runs
        # the seed on EVERY deploy: an operator who revoked a compromised
        # credential got it silently reinstated from env at the next deploy.
        # Found in review.
        #
        # Gating on org creation rather than on a tombstone keeps this
        # schema-free — a tombstone table would drag in all four
        # hand-maintained RLS lists for one boolean.
        #
        # Consequence, deliberate: an org that already exists never gains
        # credential rows from env again. A fresh database still seeds exactly
        # as before (the org is created in the same call), so
        # `scripts/e2e_backend.py`'s no-env Gusto world and payrun.spec.ts are
        # untouched.
        _seed_integration_credentials(session, org_id)
    return org_id


# The closed provider set per integration — the same partition
# `ck_org_integration_credential_provider_fields` enumerates. Held here so an
# unknown provider is refused BY NAME (below) instead of reaching the DB as a
# CHECK violation that names no provider at all.
_SEED_PROVIDERS: dict[str, tuple[str, ...]] = {
    "payroll": ("gusto", "adp"),
    "accounting": ("qbo",),
    "demand_feed": ("delphi", "tripleseat"),
}


def _seed_credential_fields(
    integration: str, provider: str, settings: Settings
) -> dict[str, str]:
    """The env-sourced credential columns ONE (integration, provider) pair
    needs — keyed exactly as the CHECK demands, and enumerated rather than
    inferred.

    It REFUSES an unknown pair, matching `integrations.resolve_payroll`, which
    raises by name on a provider it cannot build rather than degrading to "not
    connected". Since OH-17 deleted `server._payroll_provider_from_settings`
    and create_app's boot-time check, THIS function is the only place a
    misspelled `USALI_PAYROLL_PROVIDER` is caught before the DB CHECK sees it —
    which is the right place, because seeding is the only thing that still
    reads that variable. A branch whose `else` meant "adp" would
    instead build a row carrying ADP's columns under the misspelled provider
    name, and the typo would surface as a CHECK violation naming no provider —
    the same failure, several layers further from its cause.
    """
    match (integration, provider):
        case ("payroll", "gusto"):
            return {"api_token": settings.gusto_api_token,
                    "company_id": settings.gusto_company_id}
        case ("payroll", "adp"):
            return {"client_id": settings.adp_client_id,
                    "client_secret": settings.adp_client_secret}
        case ("accounting", "qbo"):
            return {"realm_id": settings.qbo_realm_id,
                    "refresh_token": settings.qbo_refresh_token}
        case ("demand_feed", "delphi"):
            return {"subscription_key": settings.delphi_subscription_key}
        case ("demand_feed", "tripleseat"):
            return {"api_key": settings.tripleseat_api_key}
        case _:
            legal = "|".join(_SEED_PROVIDERS.get(integration, ()))
            raise RuntimeError(
                f"unknown {integration} provider {provider!r}"
                + (f" (expected {legal})" if legal else "")
            )


def _seed_integration_credentials(session: Session, org_id: int) -> None:
    """Seed org 1's integration credentials from the process-wide env.

    Called ONLY when `ensure_default_org` just created the org (2026-08-31) —
    see the call site. The `on_conflict_do_nothing` below is still here as the
    inner guard, but it is no longer what makes a re-seed safe: it keys on the
    row being present, and a disconnect deletes the row. At runtime every
    adapter reads THIS row, never env; an env fallback for org != 1 is the
    mutant L5 killed.

    This is a BRIDGE, not a connect action: it reproduces exactly what each
    default means today and does NOT run the connect endpoint's verification.
    Do NOT "improve" it into seeding only when env differs from the committed
    mock defaults — scripts/e2e_backend.py:399 states that the Gusto defaults
    ARE the working local config with no env set, and that rule would break
    payrun.spec.ts silently.

    Payroll and accounting are UNCONDITIONAL because neither has an off state
    to represent: `payroll_provider` defaults to gusto and every Gusto/QBO
    setting defaults to a working local mock, so "no env set" already means a
    live connection here — that is precisely what the pay-run e2e leans on.
    The demand feed is the one integration with a genuine OFF state, and it
    keeps its sentinel: `crm_provider == ''` means NO ROW, so an unset
    `USALI_CRM_PROVIDER` still produces demo_seed.py's honest "skipped" note
    rather than a connection to nothing. Note the test is truthiness, not
    `== 'delphi' or == 'tripleseat'`: a typo'd provider must be REFUSED by
    `_seed_credential_fields`, the way the old `org_settings` CHECK refused it
    at seed time, never silently fall through to "off".

    Accounting's provider is the literal "qbo" and not a setting because qbo
    is the ENTIRE accounting half of the closed provider set — there is no
    `USALI_ACCOUNTING_PROVIDER` to read, and inventing one to make the three
    integrations look symmetrical would be a config knob with one legal value.

    In tension with D-B4.3, deliberately: `checklist.py:167` holds that a
    process-wide credential is NOT this tenant's connection, and this function
    turns exactly that env into org 1's rows — which Task 7's probe will then
    read as connected. That is honest for org 1 and only org 1: it is the
    pilot/demo org, whose rows say precisely what the process-wide config they
    replace already said. The property D-B4.3 protects is that a NEWLY
    PROVISIONED tenant inherits nothing — `provision_tenant` writes no
    credential rows, so its checklist reads "open" no matter what env the
    server happens to hold.
    """
    settings = get_settings()
    seeds: list[tuple[str, str]] = [
        ("payroll", settings.payroll_provider),
        ("accounting", "qbo"),
    ]
    if settings.crm_provider:
        seeds.append(("demand_feed", settings.crm_provider))
    for integration, provider in seeds:
        session.execute(
            insert(OrgIntegrationCredential)
            .values(
                org_id=org_id,
                integration=integration,
                provider=provider,
                connected_by=_SEED_SUBJECT,
                **_seed_credential_fields(integration, provider, settings),
            )
            .on_conflict_do_nothing(index_elements=["org_id", "integration"])
        )


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


class PropertyEntry(BaseModel):
    match: str
    property_id: str
    pms_source: str
    # The registry is the HUMAN-authored place a property is declared, which
    # makes it the right place to state wage jurisdiction. PMS ingestion also
    # creates properties, and a nightly revenue file knows nothing about wage
    # law -- those come out NULL and `rules_for` refuses to cost them until
    # someone says. Optional here for the same reason: silence is answered with
    # a blocked pay run, never with a guess.
    wage_jurisdiction: str | None = None
    # The CRM-side identity of the property (Pillar J): a Delphi hotel
    # ref or Tripleseat location id. Same posture as wage_jurisdiction:
    # optional here, and a property without one refuses to pull, by name.
    crm_ref: str | None = None


def seed_properties(session: Session, yaml_path: str | Path) -> int:
    """Upsert property + detection-alias rows from a YAML registry file.

    Idempotent: keyed on `property_id` (property) and the
    (property_id, pms_source, match_phrase) triple (alias). Returns the number
    of registry rows processed.
    """
    raw: list[dict[str, Any]] = yaml.safe_load(Path(yaml_path).read_text())
    entries = [PropertyEntry(**row) for row in raw]

    org_id = ensure_default_org(session)

    for e in entries:
        session.execute(
            insert(Property)
            .values(
                property_id=e.property_id,
                org_id=org_id,
                name=e.match,
                pms_source=e.pms_source,
                wage_jurisdiction=e.wage_jurisdiction,
                crm_ref=e.crm_ref,
            )
            .on_conflict_do_update(
                index_elements=["property_id"],
                set_={
                    "org_id": org_id,
                    "name": e.match,
                    "pms_source": e.pms_source,
                    # Only OVERWRITE when the registry states one. A re-seed must
                    # not blank a jurisdiction (or a CRM ref) an operator set by
                    # hand.
                    **({"wage_jurisdiction": e.wage_jurisdiction}
                       if e.wage_jurisdiction is not None else {}),
                    **({"crm_ref": e.crm_ref}
                       if e.crm_ref is not None else {}),
                },
            )
        )
        session.execute(
            insert(PropertyDetectionAlias)
            .values(
                property_id=e.property_id,
                pms_source=e.pms_source,
                match_phrase=e.match,
            )
            .on_conflict_do_nothing(
                index_elements=["property_id", "pms_source", "match_phrase"]
            )
        )
    return len(entries)
