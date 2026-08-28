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

from usali.config import get_settings
from usali.models import Organization, OrgSettings, Property, PropertyDetectionAlias
from usali.tenancy import FOUNDING_ORG_ID

# The founding hotel group (multi-org arrives via provisioning, Pillar L).
_DEFAULT_ORG = "Pilot Hotel Group"
# The founding org's Keycloak organization alias (L3, decision 3): must
# match the dev realm's organization (keycloak/realm-usali.json) and
# authkit's default claim — the contract is pinned in
# tests/test_oidc_realm_contract.py.
DEFAULT_ORG_ALIAS = "pilot-hotel-group"


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
    session.execute(
        insert(Organization)
        .values(org_id=FOUNDING_ORG_ID, name=_DEFAULT_ORG, kc_org_alias=DEFAULT_ORG_ALIAS)
        .on_conflict_do_nothing(index_elements=["org_id"])
    )
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
    # L5 decision 5: seed the founding org's per-org settings from the
    # process-wide env default, but ONLY on first insert — a bare re-seed
    # never blanks or overwrites a crm_provider an operator set by hand (the
    # crm_ref / wage_jurisdiction find-or-create posture). `USALI_CRM_PROVIDER`
    # is the SEED default for org 1 ONLY; at runtime the crm router reads the
    # value from this row, never from env (env fallback for any org is the
    # mutant L5 kills).
    session.execute(
        insert(OrgSettings)
        .values(org_id=org_id, crm_provider=get_settings().crm_provider)
        .on_conflict_do_nothing(index_elements=["org_id"])
    )
    return org_id


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
