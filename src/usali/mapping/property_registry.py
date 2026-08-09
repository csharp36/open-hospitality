"""Seed the property system-of-record + detection registry from YAML into the DB.

`mapping/properties.yaml` is no longer read at detection time (see
`usali.detect.load_registry`); it is a seed source, upserted here into the
`organization` / `property` / `property_detection_alias` tables.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sqlalchemy import update

from usali.config import get_settings
from usali.models import Organization, OrgSettings, Property, PropertyDetectionAlias

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
    must agree on which row is the founding org — two creators with two
    names would split a world across org ids, which is exactly the state
    the L2 walls (ORM criteria + RLS) assume cannot exist. Keyed on the
    unique name, autoincrement id: on any freshly-truncated or A2.1-seeded
    database the org comes out id 1.

    L3: the founding org answers to the dev realm's Keycloak alias, so
    the seed stamps `kc_org_alias` — but only where it is NULL: a bare
    re-seed never blanks or overwrites an alias an operator set by hand
    (the seed_properties find-or-create posture).
    """
    session.execute(
        insert(Organization)
        .values(name=_DEFAULT_ORG, kc_org_alias=DEFAULT_ORG_ALIAS)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    session.execute(
        update(Organization)
        .where(
            Organization.name == _DEFAULT_ORG,
            Organization.kc_org_alias.is_(None),
        )
        .values(kc_org_alias=DEFAULT_ORG_ALIAS)
    )
    org_id = session.execute(
        select(Organization.org_id).where(Organization.name == _DEFAULT_ORG)
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
