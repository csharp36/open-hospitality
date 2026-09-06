"""Chart-of-accounts template loading and per-org seeding (D-OH27.1).

Seeding is insert-only and keyed on absence per (org, account_code): a
re-run inserts template rows the org lacks and NEVER updates a row that
exists — an operator's rename or deactivation survives every later seed
(the OH-17 env-seed lesson, applied from day one).

``seed_chart`` takes an explicit ``org_id`` (default: the founding org)
rather than reading it off ``tenancy.current_org_id``: every caller here
runs on an OWNER (un-instrumented, RLS-bypassing) session —
:func:`usali.provisioning.provision_tenant`'s documented contract, and
the `db_session` test fixture — and ``current_org_id`` only resolves on a
session `tenancy.bind_org_context` has bound, which an owner session
never is (`session.info` carries no org key, so it would raise
``MissingOrgContext`` on every call). Explicit `org_id` matches how the
rest of provisioning already writes OrgScoped rows on the owner session
(e.g. `provisioning.provision_tenant`'s `RoleAssignment` insert, and
`property_registry._seed_integration_credentials`) rather than relying on
a wall that isn't attached here. The `have` lookup below filters on that
same `org_id` explicitly for the same reason: an owner session has no ORM
read wall to scope it automatically, so an unfiltered SELECT would see
every org's codes and wrongly skip seeding a new org whose codes another
org already has.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import GlAccount
from usali.tenancy import FOUNDING_ORG_ID

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


def seed_chart(
    session: Session, org_id: int = FOUNDING_ORG_ID, path: Path = _TEMPLATE
) -> int:
    """Insert template accounts `org_id` lacks; return the count inserted."""
    have = set(
        session.scalars(
            select(GlAccount.account_code).where(GlAccount.org_id == org_id)
        ).all()
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
