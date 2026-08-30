"""The onboarding open-items checklist (Track B/B4, D8.2).

Status is DERIVED, never stored: each item owns a `probe(session) -> bool`
that answers "is this configured for the active org?" against the caller's
already-org-bound session, so both tenancy walls apply and a probe cannot
observe another tenant's rows. The only persisted state is a dismissal
(`OrgChecklistOverride`), and a dismissal LOSES to a probe that says done
(D-B4.4) — an operator who dismissed payroll in August and connected it in
March must see `done`, not a stale "dismissed".

`ITEMS` is the closed set, the `CRM_PROVIDERS` idiom: one place to read the
whole checklist. Its keys are mirrored by a literal CHECK on
`org_checklist_override.item_key` (models.py) so the DB refuses an unknown
key independently of this import.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from usali.models import (
    FiscalCalendar,
    IngestBatch,
    OrgChecklistOverride,
    OrgIntegrationCredential,
    Property,
    RoleAssignment,
    RoomInventory,
)

logger = logging.getLogger("usali.checklist")

Probe = Callable[[Session], bool]

DONE = "done"
OPEN = "open"
DISMISSED = "dismissed"
ERROR = "error"


@dataclass(frozen=True)
class ChecklistItem:
    """D-B4.8 pairs `where` with `unavailable_reason`: `where is None` ⟺
    `unavailable_reason is not None`. An item either routes somewhere that can
    actually close it, or it says why it cannot be closed yet — never both
    (a link the reason contradicts) and never neither (a dead end).
    (`probe` sits between them only because a dataclass cannot put a defaulted
    field before a non-defaulted one.)"""

    key: str
    title: str
    description: str
    required: bool
    where: str | None  # the SPA route that closes this item
    probe: Probe
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ItemStatus:
    key: str
    title: str
    description: str
    required: bool
    where: str | None
    status: str
    detail: str | None = None
    unavailable_reason: str | None = None


def evaluate(
    session: Session, items: Sequence[ChecklistItem] | None = None
) -> list[ItemStatus]:
    """One ItemStatus per registered item, computed under `session`'s org."""
    registry = ITEMS if items is None else items
    dismissed = {
        key for (key,) in session.execute(select(OrgChecklistOverride.item_key))
    }
    out: list[ItemStatus] = []
    for item in registry:
        try:
            done = item.probe(session)
        except Exception as exc:  # design §8: loud, but contained
            logger.exception("checklist probe failed for %s", item.key)
            # A DBAPI-level failure leaves the Session in "pending rollback":
            # without this, every LATER probe would raise PendingRollbackError
            # and degrade too, making the containment illusory. The house
            # pattern (ingestion.py:273, crm_api.py:124).
            session.rollback()
            out.append(_status(item, ERROR, detail=type(exc).__name__))
            continue
        if done:
            status = DONE
        elif item.key in dismissed:
            status = DISMISSED
        else:
            status = OPEN
        out.append(_status(item, status))
    return out


@dataclass(frozen=True)
class ChecklistSummary:
    open_count: int
    error_count: int
    all_clear: bool


def summarize(rows: list[ItemStatus]) -> ChecklistSummary:
    """Aggregate derived statuses. `all_clear` requires that nothing is open
    AND that nothing failed to evaluate: an item we could not check is not a
    finished item, and reporting otherwise would tell an operator to stop
    when the truth is unknown (ADR-010, design §8)."""
    open_count = sum(1 for r in rows if r.status == OPEN)
    error_count = sum(1 for r in rows if r.status == ERROR)
    return ChecklistSummary(
        open_count=open_count,
        error_count=error_count,
        all_clear=open_count == 0 and error_count == 0,
    )


def _status(item: ChecklistItem, status: str, *, detail: str | None = None) -> ItemStatus:
    return ItemStatus(
        key=item.key, title=item.title, description=item.description,
        required=item.required, where=item.where, status=status, detail=detail,
        # Unconditional: the reason is a static property of the item, not of
        # this evaluation. `demand_feed` can be `done` while still carrying
        # one, so the string must read correctly against any status.
        unavailable_reason=item.unavailable_reason,
    )


def _every_property_has(session: Session, column: InstrumentedAttribute[str]) -> bool:
    """True when the org has at least one property AND every one of them has a
    row carrying `column`. The at-least-one guard matters: `all()` over an
    empty property list is vacuously true, which would report a
    partially-provisioned tenant as configured."""
    properties = {pid for (pid,) in session.execute(select(Property.property_id))}
    if not properties:
        return False
    # distinct() is NOT redundant with the set comprehension below, though a
    # test cannot tell them apart: room_inventory is effective-dated, so one
    # property carries a row per change. DISTINCT bounds what Postgres sends
    # to one row per property; without it the wire carries every historical
    # row and Python throws them away. Behaviour is identical either way —
    # which is why no cheap test guards this. Do not "simplify" it out.
    covered = {pid for (pid,) in session.execute(select(distinct(column)))}
    return properties <= covered


def _probe_first_report(session: Session) -> bool:
    return session.execute(select(IngestBatch.batch_id).limit(1)).first() is not None


def _probe_room_inventory(session: Session) -> bool:
    return _every_property_has(session, RoomInventory.property_id)


def _probe_fiscal_calendar(session: Session) -> bool:
    return _every_property_has(session, FiscalCalendar.property_id)


def _probe_payroll(session: Session) -> bool:
    """D-B4.3: deliberately ignores `settings.payroll_provider`. A
    process-wide credential is not this tenant's connection, so the honest
    answer for a real tenant is "not connected". OH-17 replaces this body."""
    return False


def _probe_accounting(session: Session) -> bool:
    """D-B4.3, as `_probe_payroll`. OH-17 replaces this body."""
    return False


def _probe_demand_feed(session: Session) -> bool:
    """OH-17 Task 7 replaces this body. `integration == "demand_feed"` picks
    out the one row (if any) this org holds for the demand-feed slot — the
    same filter `crm_api._active_org_crm_provider` uses, so the checklist and
    the pull endpoint agree on what "connected" means.

    "OFF" used to be `org_settings.crm_provider == ''`, an explicit sentinel
    value on an always-present row. Under `OrgIntegrationCredential` there is
    no sentinel: OFF is the ABSENCE of a row entirely (D-OH17.1 — a tenant
    cannot hold a provider without its credentials, so an unconnected org
    simply has no row for this integration). `bool(row)` is therefore still
    the right test, but now for a different reason: it used to reject the
    empty string, and now it rejects `None` from `scalar_one_or_none()`."""
    row = session.execute(
        select(OrgIntegrationCredential.provider).where(
            OrgIntegrationCredential.integration == "demand_feed"
        )
    ).scalar_one_or_none()
    return bool(row)


def _probe_team(session: Session) -> bool:
    count = session.execute(
        select(func.count(distinct(RoleAssignment.keycloak_subject)))
    ).scalar_one()
    return count > 1


# One constant, not three near-identical strings: the three integration items
# share a single cause, and D-B4.8's point is that this is one class rather
# than three special cases. OH-17 deletes it along with the `where=None`s.
# Deliberately says nothing about dismissing: `demand_feed` can probe `done`
# today, and "you can dismiss this" is false next to a Done badge (D-B4.4).
_OH17_REASON = (
    "No connect surface yet — per-tenant integration setup arrives with OH-17."
)

ITEMS: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        key="first_report", title="Upload your first PMS report",
        description="Drop a night-audit export to see your USALI statement.",
        required=True, where="/upload", probe=_probe_first_report,
    ),
    ChecklistItem(
        key="room_inventory", title="Set sellable room inventory",
        description="Occupancy, ADR and RevPAR divide by this — they cannot be "
                    "computed without it.",
        required=True, where="/property-config", probe=_probe_room_inventory,
    ),
    ChecklistItem(
        key="fiscal_calendar", title="Define the fiscal calendar",
        description="Calendar-month or 4-4-5, per property.",
        required=True, where="/property-config", probe=_probe_fiscal_calendar,
    ),
    ChecklistItem(
        key="payroll", title="Connect payroll",
        description="Optional. Compare estimated labor cost against the actual "
                    "gross-to-net from your provider.",
        required=False, where=None, probe=_probe_payroll,
        unavailable_reason=_OH17_REASON,
    ),
    ChecklistItem(
        key="accounting", title="Connect QuickBooks Online",
        description="Optional. Push the journal entry behind your statement.",
        required=False, where=None, probe=_probe_accounting,
        unavailable_reason=_OH17_REASON,
    ),
    ChecklistItem(
        key="demand_feed", title="Connect a demand feed",
        description="Optional. Pull group and event demand from Delphi or Tripleseat.",
        required=False, where=None, probe=_probe_demand_feed,
        unavailable_reason=_OH17_REASON,
    ),
    ChecklistItem(
        key="team", title="Invite your team",
        description="Optional. Add a second operator so you are not the only "
                    "person who can log in.",
        required=False, where="/employees", probe=_probe_team,
    ),
)
