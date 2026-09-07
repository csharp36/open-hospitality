"""Chart-of-accounts template loading and per-org seeding (D-OH27.1).

Seeding is insert-only and keyed on absence per (org, account_code): a
re-run inserts template rows the org lacks and NEVER updates a row that
exists — an operator's rename or deactivation survives every later seed
(the OH-17 env-seed lesson, applied from day one).

``seed_chart`` takes an explicit ``org_id`` rather than reading it off
``tenancy.current_org_id``: every caller here runs on an un-instrumented
session (owner or provisioner) — neither binds an org context. That
covers both callers by name: the `db_session` test fixture's owner
session, and :func:`usali.provisioning.provision_tenant`'s `usali_provisioner`
session (RLS-bound but never org-bound, per migration `g2a0provgl`).
``current_org_id`` only resolves on a session `tenancy.bind_org_context`
has bound, which neither of these is (`session.info` carries no org key,
so it would raise ``MissingOrgContext`` on every call). Explicit `org_id` matches how the
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

_TEMPLATE = Path(__file__).resolve().parents[2] / "mapping" / "gl_accounts_usali.yaml"

# The columns fill_usali may write, and the only ones it may write.
# test_usali_fields_tuple_matches_the_models_usali_columns pins this to
# GlAccount's usali_* column set, so a new usali_ column cannot be
# silently skipped here.
_USALI_FIELDS = (
    "usali_schedule_id",
    "usali_major_category",
    "usali_sub_category",
    "usali_line_item",
)


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


# org_id is keyword-only with no default: a default org in a multi-tenant
# seeder is a silent-wrong-tenant footgun, so callers must say which org.
def seed_chart(session: Session, *, org_id: int, path: Path = _TEMPLATE) -> int:
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


@dataclass(frozen=True)
class UsaliBackfill:
    filled: int  # accounts where at least one NULL usali_* column was filled
    unchanged: int  # template accounts present in the chart, left as they were


def fill_usali(session: Session, *, org_id: int, path: Path = _TEMPLATE) -> UsaliBackfill:
    """Fill NULL ``usali_*`` columns on `org_id`'s existing accounts from the
    template, matched by ``account_code``.

    The complement of :func:`seed_chart`: seeding inserts and never updates,
    so a chart seeded before the template carried a classification never
    receives it — this fills exactly that gap and nothing else. Per column,
    a value is written only where the row holds NULL and the template holds
    one; a non-NULL value is never replaced, columns outside
    ``_USALI_FIELDS`` are never written, chart rows the template lacks are
    never touched, and template rows the chart lacks are never inserted
    (inserting is `seed_chart`'s job). Explicit `org_id` for the same
    unbound-session reason `seed_chart` documents above.
    """
    rows = {
        row.account_code: row
        for row in session.scalars(
            select(GlAccount).where(GlAccount.org_id == org_id)
        )
    }
    filled = 0
    unchanged = 0
    for acct in load_template(path):
        row = rows.get(acct.account_code)
        if row is None:
            continue
        touched = False
        for field in _USALI_FIELDS:
            template_value = getattr(acct, field)
            if template_value is not None and getattr(row, field) is None:
                setattr(row, field, template_value)
                touched = True
        if touched:
            filled += 1
        else:
            unchanged += 1
    session.flush()
    return UsaliBackfill(filled=filled, unchanged=unchanged)
