"""GM demo seed: real roster identities, synthetic money.

Reads the Inn-Flow roster export (JSON, on the encrypted volume) and seeds the
dev database with the REAL people — names, properties, departments, pay types —
so the GM can picture how the product works. Everything with a dollar sign or a
government identifier is SYNTHETIC, generated here:

  - The importer takes an ALLOWLIST of identity/org fields and drops everything
    else. A dropped field whose name matches the compensation/identifier
    patterns (`roster_seed._FORBIDDEN_COLUMNS`) is reported BY NAME — its value
    is never read into a variable, never printed, never stored. This is the
    E2-recorded rule: real compensation data stays on the encrypted volume and
    never enters this repository or its database.
  - Pay rates, SSNs, and deposit accounts are synthetic, derived
    deterministically from the incumbent employee number so re-runs agree.

Run via scripts/demo.sh, which starts the stack and exports the shared dev
HPKE key (USALI_PII_HPKE_PRIVATE_KEY) so the sealed synthetic PII this script
plants can be opened by the running API:

    scripts/demo.sh /Volumes/Employees/inn-flow-roster-2026-07-18.json

The demo world (once, guarded by a sentinel audit event):
  - Two closed biweekly periods of punches (2026-06-22..07-05, 07-06..07-19)
    with varied day lengths, assembled, approved, and promoted — so timecards,
    labor facts, Schedule 14/15, and CA sick-leave accrual all have history.
    The sample PDFs (business date 2026-07-07) land revenue facts in the same
    window, so an SOS over 07-06..07-19 prices labor against real revenue.
  - One mid-period raise (a dated AssignmentRate change), one employee with no
    rate on file, one with payroll_data_complete=False — the pay-run preflight
    blocker showcase. All three live at ONE property (G6), so the OTHER
    property (the sick department's) preflights clean and its runs EXECUTE:
    sick hours ride its period-2 submission (G3), and editing the chain
    star's deposit split between runs demos the silent re-sync (G2/G4)
    instead of a stale-payload blocker.
  - The settle story (I5): a period-0 run at the clean property is filed by
    the seed itself, then a corrected evening shift lands AFTER payment —
    unlinked (H1), named by preflight (H4), and resolved live through the
    full loop: reopen → re-approve → settle the drift (I3) → green.
  - Sealed payroll profiles + deposit chains (one 2-account chain), sick
    opening balances and one usage, a published schedule for the coming week,
    a few open punches from yesterday, and kiosk devices with printed tokens.
  - The demand story (J6): one audited CRM pull through the config-selected
    Delphi adapter against the local mock — the fixed world's fat Thursday
    (2026-08-06: 132 on the books + a 50-room block spread over two groups)
    renders as chips and labels on the Schedule page, week of 2026-08-03.
    Contact and rate salt in the mock payloads is dropped unread, counted
    by field name in the talking points.
  - Face matching (Pillar F, when enabled and models are fetched): one hourly
    worker per property enrolled with the committed SYNTHETIC faces
    (tests/fixtures/faces/ — these people do not exist), and the open punches
    stamped green/red/grey so the timecard approval gate demos immediately.
    Real faces are NEVER seeded as reference photos.
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from usali.config import get_settings  # noqa: E402
from usali.db import make_engine, make_session_factory  # noqa: E402
from usali.deposit_accounts import account_slot, routing_slot  # noqa: E402
from usali.ingestion import process_file, record_coverage  # noqa: E402
from usali.keycloak_admin import KeycloakAdminClient, KeycloakAdminError  # noqa: E402
from usali.kiosk import mint_device_token  # noqa: E402
from usali.labor import promote_timecard  # noqa: E402
from usali.mapping.loader import load_mappings  # noqa: E402
from usali.mapping.property_registry import seed_properties  # noqa: E402
from usali.mapping.schedules import seed_schedules  # noqa: E402
from usali.crm_pull import store_pull  # noqa: E402
from usali.models import (  # noqa: E402
    AssignmentRate,
    AuditEvent,
    DepositAccount,
    Employee,
    EmployeeAssignment,
    EmployeePayrollProfile,
    FiscalCalendar,
    IngestBatch,
    KioskDevice,
    OutOfOrderRoom,
    PaySchedule,
    Property,
    PropertyStatConfig,
    Punch,
    RoleAssignment,
    RoomInventory,
    Schedule,
    Shift,
    SickLeaveLedger,
)
from usali.opener import SoftwareOpener, seal_for_test  # noqa: E402
from usali.payroll_run import execute_pay_run  # noqa: E402
from usali.roster_seed import (  # noqa: E402
    _FORBIDDEN_COLUMNS,
    RosterRow,
    _department_id,
    seed_roster,
)
from usali.sick_leave import cap_hours_on  # noqa: E402
from usali.tenancy import FOUNDING_ORG_ID, OrgBoundSessionFactory  # noqa: E402
from usali.timecards import assemble_timecard  # noqa: E402

# ---------------------------------------------------------------- import rules

# The Inn-Flow EHIDs, mapped onto the property registry (mapping/properties.yaml).
EHID_TO_PROPERTY = {"SJCES": "HISJ", "58033": "SSSJ"}

PAY_TYPE_MAP = {
    "Hourly Wage": "hourly",
    "Salary Exempt from OT": "salary",
    "Exclude from Payroll": "exclude_from_payroll",
}

# Inn-Flow job titles -> demo departments. Anything unmapped becomes its own
# title-cased department rather than failing: a new job title in a re-export
# should not block a demo.
POSITION_TO_DEPARTMENT = {
    "BREAKFAST ATTENDANT": "Breakfast",
    "FACILITIES ENG": "Maintenance",
    "FRONT DESK ASSOCIATE": "Front Desk",
    "FRONT OFFICE": "Front Desk",
    "NIGHT AUDITOR": "Front Desk",
    "GENERAL MANAGER": "Administration",
    "HOUSEKEEPER": "Housekeeping",
    "LAUNDRY ATTENDANT": "Laundry",
}

# The only per-employee fields this importer will read. Everything else is
# dropped unread — see _filter_record.
ALLOWED_KEYS = frozenset({
    "employee_number", "username", "first_name", "last_name",
    "status", "pay_type", "ehids", "role", "positions",
})

ANCHOR = date(2026, 1, 5)  # settings.payroll_period_anchor (a Monday)
# The two most recent CLOSED biweekly periods on the anchor grid; the sample
# PDFs' revenue facts (2026-07-07) fall inside the second.
PERIOD_STARTS = (date(2026, 6, 22), date(2026, 7, 6))
# The settle story's period (I5): filed BEFORE the demo's two, its run
# submitted by the SEED itself — so the late corrected shift has paid
# history to drift against and the reopen -> re-approve -> settle loop
# can be walked live to a green preflight.
PERIOD0_START = PERIOD_STARTS[0] - timedelta(days=14)
PERIOD_DAYS = 14


class DemoRosterError(Exception):
    pass


@dataclass(frozen=True)
class DemoWorker:
    ref: int  # incumbent employee number: the idempotency + determinism key
    full_name: str
    pay_type: str  # already mapped to PAY_TYPES
    # (property_id, department_name) per EHID assignment; index 0 is primary.
    placements: tuple[tuple[str, str], ...]


def _filter_record(raw: dict, dropped: dict[str, int]) -> dict:
    """Return only the allowlisted fields; count dropped keys BY NAME.

    Never reads a dropped value — `raw[key]` is simply not touched for keys
    outside the allowlist, so compensation data in a future re-export cannot
    leak into logs, errors, or the database through this path.
    """
    for key in raw.keys() - ALLOWED_KEYS:
        dropped[key] += 1
    return {k: raw[k] for k in raw.keys() & ALLOWED_KEYS}


def load_demo_roster(path: Path) -> tuple[list[DemoWorker], list[str]]:
    """Parse the export; return active workers + human-readable filter report."""
    with path.open() as fh:
        data = json.load(fh)

    actives = data.get("employees")
    if not isinstance(actives, list) or not actives:
        raise DemoRosterError("export has no employees[] list")

    dropped: dict[str, int] = defaultdict(int)
    workers: list[DemoWorker] = []
    for raw in actives:
        rec = _filter_record(raw, dropped)
        ref = rec.get("employee_number")
        first, last = rec.get("first_name", ""), rec.get("last_name", "")
        full_name = f"{first} {last}".strip()
        if not isinstance(ref, int) or not full_name:
            raise DemoRosterError(
                "a roster record is missing employee_number or a name "
                "(values withheld; inspect the export on the volume)"
            )
        pay_type = PAY_TYPE_MAP.get(rec.get("pay_type", ""))
        if pay_type is None:
            raise DemoRosterError(
                f"employee_number {ref}: unrecognized pay_type "
                "(value withheld; expected one of "
                f"{sorted(PAY_TYPE_MAP)})"
            )
        ehids = rec.get("ehids") or []
        positions = rec.get("positions") or []
        if not ehids:
            raise DemoRosterError(f"employee_number {ref}: no EHIDs")
        placements = []
        for i, ehid in enumerate(ehids):
            prop = EHID_TO_PROPERTY.get(ehid)
            if prop is None:
                raise DemoRosterError(
                    f"employee_number {ref}: unknown EHID {ehid!r} "
                    f"(known: {sorted(EHID_TO_PROPERTY)})"
                )
            # positions[] is "one per EHID assignment" (export field notes).
            title = positions[i] if i < len(positions) else None
            dept = (
                POSITION_TO_DEPARTMENT.get(title, title.title())
                if title else "Unassigned"
            )
            placements.append((prop, dept))
        workers.append(DemoWorker(
            ref=ref, full_name=full_name, pay_type=pay_type,
            placements=tuple(placements),
        ))

    report = []
    inactive = data.get("inactive_employees") or []
    if inactive:
        report.append(
            f"skipped {len(inactive)} inactive employees (the export marks its "
            "inactive set as partial; the demo seeds active staff only)"
        )
    for key, n in sorted(dropped.items()):
        sensitive = any(tok in key.lower() for tok in _FORBIDDEN_COLUMNS)
        tag = "COMPENSATION/IDENTIFIER — value never read" if sensitive else "unused"
        report.append(f"dropped field {key!r} on {n} record(s) [{tag}]")
    return workers, report


# ------------------------------------------------------------ synthetic money

_BASE_RATE = {
    "Housekeeping": Decimal("21.00"), "Laundry": Decimal("20.00"),
    "Front Desk": Decimal("22.00"), "Breakfast": Decimal("19.00"),
    "Maintenance": Decimal("28.00"), "Administration": Decimal("30.00"),
}


def _rate_for(worker: DemoWorker, department: str) -> Decimal:
    """SYNTHETIC hourly rate: department base + a deterministic per-person
    wiggle from the incumbent employee number. Bears no relationship to what
    anyone is actually paid."""
    base = _BASE_RATE.get(department, Decimal("20.00"))
    return base + Decimal(worker.ref % 7) * Decimal("0.25")


def _ssn_for(worker: DemoWorker) -> bytes:
    # 900-xx range is never issued as an SSN; unmistakably synthetic.
    return f"900-55-{worker.ref % 10000:04d}".encode()


def _account_for(worker: DemoWorker) -> bytes:
    return f"0001{worker.ref % 100000:05d}".encode()


_DEMO_ROUTING = b"021000021"  # a well-known test routing number


# --------------------------------------------------------------- world seeding

def _seed_base(session: Session) -> None:
    seed_schedules(session, str(REPO_ROOT / "mapping" / "usali_schedules.yaml"))
    load_mappings(session, str(REPO_ROOT / "mapping" / "opera.yaml"))
    load_mappings(session, str(REPO_ROOT / "mapping" / "autoclerk.yaml"))
    seed_properties(session, str(REPO_ROOT / "mapping" / "properties.yaml"))
    session.commit()


def _seed_people(session: Session, workers: list[DemoWorker]) -> None:
    """Roster people via the guarded seed path, then the demo extras the CSV
    path deliberately does not do: secondary placements and backdating."""
    from usali.keycloak_admin import InMemoryKeycloakAdmin

    # No roster row carries an operator role, so Keycloak is never called —
    # the in-memory admin keeps this seed independent of the realm being up.
    kc = InMemoryKeycloakAdmin()
    by_primary: dict[str, list[RosterRow]] = defaultdict(list)
    for w in workers:
        prop, dept = w.placements[0]
        by_primary[prop].append(RosterRow(
            full_name=w.full_name, pay_type=w.pay_type, department=dept,
            employee_ref=str(w.ref),
        ))
    for prop, rows in sorted(by_primary.items()):
        result = seed_roster(session, kc, rows, property_id=prop,
                             actor_subject="demo-seed")
        print(f"  {prop}: {result.created} created, {result.skipped} existing, "
              f"{result.departments_created} new department(s)")

    ids = _employee_ids(session, workers)

    # Secondary placements: the export pairs positions[i] with ehids[i], so a
    # person can hold a different job at the other property.
    for w in workers:
        for prop, dept in w.placements[1:]:
            emp_id = ids[w.full_name]
            exists = session.execute(
                select(EmployeeAssignment).where(
                    EmployeeAssignment.employee_id == emp_id,
                    EmployeeAssignment.property_id == prop,
                )
            ).scalar_one_or_none()
            if exists is None:
                dept_id, _ = _department_id(session, prop, dept)
                session.add(EmployeeAssignment(
                    employee_id=emp_id, property_id=prop,
                    department_id=dept_id, is_primary=False, status="active",
                    effective_from=ANCHOR,
                ))

    # Onboarding stamps hire_date/effective_from with TODAY; the demo's worked
    # periods are in June/July, so backdate everyone to the payroll anchor —
    # otherwise rate resolution finds no placement on the worked days.
    emp_ids = list(ids.values())
    session.execute(update(Employee).where(Employee.employee_id.in_(emp_ids))
                    .values(hire_date=ANCHOR))
    session.execute(
        update(EmployeeAssignment)
        .where(EmployeeAssignment.employee_id.in_(emp_ids))
        .values(effective_from=ANCHOR)
    )
    session.commit()


def _employee_ids(session: Session, workers: list[DemoWorker]) -> dict[str, int]:
    names = [w.full_name for w in workers]
    rows = session.execute(
        select(Employee.full_name, Employee.employee_id)
        .where(Employee.full_name.in_(names))
    ).all()
    ids: dict[str, int] = {}
    for name, emp_id in rows:
        if name in ids:
            raise DemoRosterError(
                f"two employee rows share the name {name!r}; refusing to guess "
                "which is the roster person. Wipe and re-seed (demo.sh --fresh)."
            )
        ids[name] = emp_id
    missing = set(names) - set(ids)
    if missing:
        raise DemoRosterError(f"{len(missing)} roster people never seeded")
    return ids


def _workdays(worker: DemoWorker, period_start: date) -> list[tuple[date, int]]:
    """(business_date, shift_hours) for one period: 5 days a week, start-day
    and shift length varied by employee number so day-length data (and the CA
    sick caps derived from it) differ across people. ref%5==0 people pull one
    10h day a week — daily-OT material."""
    days = []
    first = worker.ref % 2  # 0: Mon-Fri, 1: Tue-Sat
    length = 7 + worker.ref % 3
    for week in (0, 1):
        for i in range(5):
            day = period_start + timedelta(days=week * 7 + first + i)
            hours = 10 if worker.ref % 5 == 0 and i == 3 else length
            days.append((day, hours))
    return days


def _punch_period(session: Session, emp_id: int, worker: DemoWorker,
                  kiosks: dict[str, int], period_start: date,
                  skip: frozenset[date] = frozenset()) -> None:
    """Punches at the PRIMARY property's kiosk — except dual-property people,
    who spend the last two workdays of each week at their second hotel.
    Promotion splits each day's hours by where they were worked (combined-first
    overtime, then apportioned), so this is the cross-property payroll story —
    and it also puts enough distinct priced people at the smaller property to
    clear the ≥2-employee disclosure floor on its SOS. `skip` drops whole
    days: the sick stars were OUT on their usage day, so they must not also
    punch a full shift on it — worked + full-day sick on one date is the
    double-pay shape the G7 stacking guard refuses by name."""
    for i, (day, hours) in enumerate(_workdays(worker, period_start)):
        if day in skip:
            continue
        prop = (worker.placements[1][0]
                if len(worker.placements) > 1 and i % 5 >= 3
                else worker.placements[0][0])
        for punch_type, hour in (("clock_in", 9), ("clock_out", 9 + hours)):
            session.add(Punch(
                employee_id=emp_id, kiosk_device_id=kiosks[prop],
                punch_type=punch_type,
                punched_at=datetime(day.year, day.month, day.day, hour,
                                    tzinfo=UTC),
                business_date=day,
            ))


def _payroll_provider():
    """The same config-selected adapter the API process uses (the C2 seam)
    — a module-level function so the G6 tests can point it at the
    in-process mock instead of the :9300 dev service."""
    from usali.server import _payroll_provider_from_settings

    return _payroll_provider_from_settings()


def _crm_feed():
    """The same provider-selected demand feed the API uses (the J4/L5
    seam) — None when USALI_CRM_PROVIDER is unset (demo.sh sets delphi).
    The demo seeds the FOUNDING org, whose org_settings.crm_provider is
    seeded from this same env (ensure_default_org), so reading env here is
    reading org 1's provider. Tests point this at the in-process mock."""
    from usali.server import _crm_feed_for_provider

    return _crm_feed_for_provider(get_settings().crm_provider)


# The Delphi mock's world is FIXED: the week of 2026-08-03, a fat Thursday
# group block landing 2026-08-06. The pull windows THAT week (plus the
# endpoint's 90-day span) rather than a today-relative horizon — this is a
# talking-point world with anchored dates, like the pay periods above.
DEMAND_WEEK = date(2026, 8, 3)


def _seed_property_config(session: Session) -> None:
    """Per-property config (room inventory, fiscal calendars, OOO, stat
    config — issues #8/#9), IDEMPOTENT and run on EVERY seed.

    Deliberately OUTSIDE the `_seed_world` sentinel: these are additive
    config rows a re-deploy must be able to BACKFILL onto an already-seeded
    world. `_seed_world` is sentinel-sealed and cannot re-run, so config
    that once lived there never reached a world seeded before it was added —
    that is how the live demo ended up with no room inventory, failing every
    performance/availability query. Find-or-create keeps a re-run a no-op.

    HISJ: calendar-month fiscal year with a mid-history inventory change (the
    effective-dated path); nets comp/house-use out of ADR's denominator.
    SSSJ: 4-4-5 fiscal year; ROOMS_OCCUPIED as reported.
    """
    def _room(property_id: str, effective_date: date, total_rooms: int) -> None:
        exists = session.execute(
            select(RoomInventory).where(
                RoomInventory.property_id == property_id,
                RoomInventory.effective_date == effective_date,
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(RoomInventory(property_id=property_id,
                                      effective_date=effective_date,
                                      total_rooms=total_rooms))

    def _fiscal(property_id: str, **kwargs: object) -> None:
        if session.get(FiscalCalendar, property_id) is None:
            session.add(FiscalCalendar(property_id=property_id, **kwargs))

    def _stat(property_id: str, adr_room_basis: str) -> None:
        if session.get(PropertyStatConfig, property_id) is None:
            session.add(PropertyStatConfig(property_id=property_id,
                                           adr_room_basis=adr_room_basis))

    _room("HISJ", date(2025, 1, 1), 140)
    _room("HISJ", date(2026, 3, 1), 138)
    _room("SSSJ", date(2025, 1, 1), 90)
    _fiscal("HISJ", calendar_type="calendar_month",
            fiscal_year_start_month=1, week_start_weekday=None)
    _fiscal("SSSJ", calendar_type="445",
            fiscal_year_start_month=1, week_start_weekday=6)
    _stat("HISJ", "exclude_comp_house")
    _stat("SSSJ", "as_reported")

    # One OOO block (HISJ renovation). OOO has no natural unique key, so a
    # re-run is guarded on the block's own shape to avoid stacking duplicates.
    ooo_exists = session.execute(
        select(OutOfOrderRoom).where(
            OutOfOrderRoom.property_id == "HISJ",
            OutOfOrderRoom.start_date == date(2026, 2, 2),
            OutOfOrderRoom.end_date == date(2026, 2, 8),
            OutOfOrderRoom.reason_code == "renovation",
        )
    ).scalar_one_or_none()
    if ooo_exists is None:
        session.add(OutOfOrderRoom(
            property_id="HISJ", start_date=date(2026, 2, 2),
            end_date=date(2026, 2, 8), room_count=3, reason_code="renovation",
            note="Wing 2 soft-goods refresh"))
    session.commit()


def _seed_world(
    session: Session, workers: list[DemoWorker]
) -> tuple[list[str], list[int]]:
    """The run-once demo enrichment. Returns talking-point lines to print
    and the ids of the open punches it created (the stamping population)."""
    ids = _employee_ids(session, workers)
    notes: list[str] = []

    # Pay schedules + one kiosk per property.
    kiosks: dict[str, int] = {}
    kiosk_tokens: dict[str, str] = {}
    for prop in sorted(EHID_TO_PROPERTY.values()):
        session.add(PaySchedule(property_id=prop, frequency="biweekly",
                                anchor=ANCHOR, check_date_offset_days=5))
        token, token_hash = mint_device_token()
        device = KioskDevice(property_id=prop, name=f"Demo Kiosk {prop}",
                             token_hash=token_hash, enrolled_by="demo-seed")
        session.add(device)
        session.flush()
        kiosks[prop] = device.device_id
        kiosk_tokens[prop] = token
    (REPO_ROOT / ".demo-kiosk-token").write_text(
        "".join(f"{p} {t}\n" for p, t in sorted(kiosk_tokens.items()))
    )

    # Property config (room inventory, fiscal calendars, OOO, stat config —
    # issues #8/#9) is NOT seeded here: it lives in `_seed_property_config`,
    # which `main` runs on EVERY seed, outside this run-once sentinel. It was
    # here originally, but `_seed_world` is sentinel-sealed (it inserts
    # punches/pay-runs/etc. unconditionally and cannot re-run), so config
    # added after a world was first seeded never reached an already-seeded
    # demo — the live demo lost room inventory exactly this way. Additive,
    # idempotent config belongs on the always-run path.

    hourly = sorted((w for w in workers if w.pay_type == "hourly"),
                    key=lambda w: w.ref)
    if len(hourly) < 4:
        raise DemoRosterError("fewer than 4 hourly people; demo needs variety")
    # Sick stars come from ONE primary department: the SOS sick-pay block
    # discloses per department per date only with >= 2 distinct priced
    # people, so takers spread across departments would all suppress.
    # Picked FIRST (G6): their property becomes the CLEAN property, whose
    # runs must EXECUTE end to end — sick hours riding the submission (G3)
    # and the between-runs chain-edit re-sync (G2/G4). The intended
    # preflight blockers live at the OTHER property, so the blocker story
    # can never block the executable one.
    by_dept: dict[tuple[str, str], list[DemoWorker]] = defaultdict(list)
    for w in hourly:
        by_dept[w.placements[0]].append(w)
    sick_stars = max(by_dept.values(), key=len)[:3]  # opening sick balances
    clean_prop = sick_stars[0].placements[0][0]
    home = [w for w in hourly if w.placements[0][0] == clean_prop]
    away = [w for w in hourly if w.placements[0][0] != clean_prop]
    if len(away) >= 3:
        raise_star = away[0]        # mid-period raise story
        incomplete_star = away[-2]  # pay-run blocker: paperwork incomplete
        norate_star = away[-1]      # pay-run blocker: no pay rate on file
    else:
        # Single-property (or lopsided) roster: the pre-G6 blind picks.
        # The blocker stories then gum up the executable ones — a tiny
        # demo can live with preflight-only pay-run stories.
        raise_star, norate_star, incomplete_star = (
            hourly[0], hourly[-1], hourly[-2])
    blockers = {raise_star, norate_star, incomplete_star}
    sick_stars = [w for w in sick_stars if w not in blockers]
    if len(sick_stars) < 2:  # tiny roster fallback; sick pay may suppress
        sick_stars = [w for w in hourly if w not in blockers][:3]
    # The chain star hosts the between-runs deposit edit, so they live at
    # the clean property too — and outside the sick cast when possible, so
    # each talking point has its own face.
    chain_pool = (
        [w for w in home if w not in blockers and w not in sick_stars]
        or [w for w in home if w not in blockers]
        or [w for w in hourly if w not in blockers]
    )
    chain_star = chain_pool[0]  # 2-account deposit chain

    # Everyone's paperwork is "in" except the named blocker.
    session.execute(
        update(Employee)
        .where(Employee.employee_id.in_(list(ids.values())),
               Employee.employee_id != ids[incomplete_star.full_name])
        .values(payroll_data_complete=True)
    )

    # SYNTHETIC rates on every placement of every hourly person (except the
    # no-rate star). The raise star's primary rate steps up mid-period.
    assignments = {
        (a.employee_id, a.property_id): a
        for a in session.execute(
            select(EmployeeAssignment).where(
                EmployeeAssignment.employee_id.in_(list(ids.values()))
            )
        ).scalars()
    }
    raise_day = PERIOD_STARTS[1] + timedelta(days=7)  # Monday of week 2
    for w in hourly:
        if w is norate_star:
            continue
        # ONE rate across all of a person's placements: the pay run submits a
        # single rate per employee and refuses when several are in force, so
        # per-placement differentials would blocker-flood every dual-property
        # worker. (Differing placement rates ARE supported — surfacing them
        # through the provider port is the recorded E2/E5 deferral.) The
        # primary placement's department sets the rate.
        for i, (prop, _dept) in enumerate(w.placements):
            a = assignments[(ids[w.full_name], prop)]
            rate = _rate_for(w, w.placements[0][1])
            if w is raise_star and i == 0:
                session.add(AssignmentRate(
                    assignment_id=a.assignment_id, rate_type="regular",
                    amount=str(rate), effective_from=ANCHOR,
                    effective_to=raise_day,
                ))
                session.add(AssignmentRate(
                    assignment_id=a.assignment_id, rate_type="regular",
                    amount=str(rate + Decimal("1.50")),
                    effective_from=raise_day,
                ))
            else:
                session.add(AssignmentRate(
                    assignment_id=a.assignment_id, rate_type="regular",
                    amount=str(rate), effective_from=ANCHOR,
                ))
    notes.append(f"raise: {raise_star.full_name} "
                 f"({raise_star.placements[0][0]}) steps up $1.50 on "
                 f"{raise_day.isoformat()} — mid-period, so the pay run "
                 "REFUSES to average and asks for a period split (intended)")
    notes.append(f"pay-run blockers: {norate_star.full_name} (no rate on "
                 f"file), {incomplete_star.full_name} (paperwork incomplete) "
                 f"— {clean_prop}'s runs stay clean and EXECUTABLE")

    # Sealed SYNTHETIC payroll PII for every payable hourly person, sealed to
    # the shared dev HPKE key demo.sh exports for the API process too.
    opener = SoftwareOpener.from_settings(get_settings())

    def _sealed(emp_id: int, slot: str, plaintext: bytes) -> str:
        return seal_for_test(opener.public_key(), plaintext,
                             aad=f"{emp_id}:{slot}".encode()).to_json()

    for w in hourly:
        if w in (norate_star, incomplete_star):
            continue  # their blockers should be the ONLY thing preflight names
        emp_id = ids[w.full_name]
        session.add(EmployeePayrollProfile(
            employee_id=emp_id, ssn_sealed=_sealed(emp_id, "ssn", _ssn_for(w)),
        ))
        chain = (
            [("amount", Decimal("100.00"), "checking"),
             ("remainder", None, "savings")]
            if w is chain_star else [("remainder", None, "checking")]
        )
        for ordinal, (alloc, value, acct_type) in enumerate(chain, start=1):
            session.add(DepositAccount(
                employee_id=emp_id, ordinal=ordinal, allocation_type=alloc,
                allocation_value=value, account_type=acct_type,
                sealed_account=_sealed(emp_id, account_slot(ordinal, False),
                                       _account_for(w)),
                sealed_routing=_sealed(emp_id, routing_slot(ordinal, False),
                                       _DEMO_ROUTING),
                legacy_sealed=False,
            ))
    notes.append(f"deposit chains: everyone payable has one; "
                 f"{chain_star.full_name} splits $100 to checking + remainder "
                 "to savings")
    notes.append(f"re-sync: run {clean_prop} period 1, edit "
                 f"{chain_star.full_name}'s deposit split on the Employees "
                 "page, then run period 2 — no stale-payload blocker; the "
                 "provider receives ONE full-replace update (G2/G4)")
    session.commit()

    # Two closed periods of punches at the PRIMARY property's kiosk, then
    # assemble -> approve -> promote (labor facts + CA sick accrual land here).
    # The two sick takers do NOT punch on their usage day — they were out
    # sick; a full shift AND a full sick day on one date is the double-pay
    # shape preflight now refuses (G7 money C), and it would block the very
    # run the sick story rides.
    usage_day = PERIOD_STARTS[1] + timedelta(days=2)
    takers = {ids[w.full_name] for w in sick_stars[:2]}
    promoted = 0
    for w in hourly:
        if w is norate_star:
            continue  # unpriced hours would gum up every SOS demo
        emp_id = ids[w.full_name]
        for start in PERIOD_STARTS:
            _punch_period(session, emp_id, w, kiosks, start,
                          skip=(frozenset({usage_day}) if emp_id in takers
                                else frozenset()))
            card = assemble_timecard(session, emp_id, start, anchor=ANCHOR)
            card.status = "approved"
            card.approved_by = "demo-seed"
            card.approved_at = datetime.now(UTC)
            session.flush()
            promoted += promote_timecard(session, card, anchor=ANCHOR)
    session.commit()
    notes.append(f"history: {len(PERIOD_STARTS)} closed periods, "
                 f"{promoted} labor facts promoted, sick leave accrued")

    # H8 -> I5: the settle story, END TO END, at the CLEAN property. Period
    # 0 is filed history: one worker's card was approved and PAID by a
    # run the seed submits itself (through the same config-selected
    # adapter the API uses), and THEN a corrected evening shift keys in.
    # H1 keeps it OFF the locked card (recorded, unlinked), the pay-run
    # preflight names it by employee and date (H4) — and since I3 the
    # loop CLOSES: reopen relinks it, re-approval turns the marker into
    # the worked-hours drift blocker (what the run paid vs what the card
    # now shows), and the settlement records the 4h as paid outside the
    # integration. The loop gums the clean property's period-1 run UNTIL
    # it is walked — that is the talking point, not a defect: the old
    # half-loop story ended at a blocker with no resolution.
    late_pool = (
        [w for w in home if w not in blockers and w is not chain_star
         and ids[w.full_name] not in takers]
        or [w for w in home if w not in blockers]
        or home[:1]
    )
    if late_pool:
        late = late_pool[0]
        late_emp = ids[late.full_name]
        _punch_period(session, late_emp, late, kiosks, PERIOD0_START)
        card0 = assemble_timecard(session, late_emp, PERIOD0_START,
                                  anchor=ANCHOR)
        card0.status = "approved"
        card0.approved_by = "demo-seed"
        card0.approved_at = datetime.now(UTC)
        session.flush()
        promote_timecard(session, card0, anchor=ANCHOR)
        session.commit()
        run0 = execute_pay_run(
            session, clean_prop, PERIOD0_START, anchor=ANCHOR,
            provider=_payroll_provider(),
            provider_name=get_settings().payroll_provider,
            opener=opener, actor="demo-seed",
        )
        session.commit()
        if run0.status != "submitted":
            notes.append(
                f"WARNING: the settle story's {PERIOD0_START.isoformat()} "
                f"run did not submit ({run0.failure_reason}) — is the "
                "payroll mock running? The reopen/settle loop is off this "
                "demo."
            )
        else:
            late_day = PERIOD0_START + timedelta(days=2)
            for punch_type, hour in (("clock_in", 18), ("clock_out", 22)):
                session.add(Punch(
                    employee_id=late_emp,
                    kiosk_device_id=kiosks[clean_prop],
                    punch_type=punch_type,
                    punched_at=datetime(late_day.year, late_day.month,
                                        late_day.day, hour, tzinfo=UTC),
                    business_date=late_day,
                ))
            card0 = assemble_timecard(session, late_emp, PERIOD0_START,
                                      anchor=ANCHOR)
            assert card0.status == "approved", "the story needs a locked card"
            session.commit()
            notes.append(
                f"settle: {late.full_name}'s corrected evening shift on "
                f"{late_day.isoformat()} keyed in AFTER {clean_prop}'s "
                f"{PERIOD0_START.isoformat()} run had already PAID that "
                "card — it stays UNLINKED (H1) and the preflight names it. "
                "Walk the full loop: Timecards → Reopen → re-approve (the "
                "marker becomes the worked-hours drift blocker: what the "
                "run paid vs what the card now shows) → settle the 4h "
                "delta via POST /api/payroll/runs/"
                f"{run0.pay_run_id}/settlements (org or payroll admin; "
                "the server computes the delta, the note is audit-only) → "
                "preflight goes GREEN and period 1 executes. Bonus: try "
                "approving an OPEN card — the current period refuses "
                "until it closes, naming the earliest approvable date"
            )

    # Sick-leave stories: opening balances (audited adjustments, cap recorded)
    # and one usage that will price onto the SOS.
    opening_day = PERIOD_STARTS[1]
    for w in sick_stars:
        emp_id = ids[w.full_name]
        session.add(SickLeaveLedger(
            employee_id=emp_id, entry_type="adjustment",
            hours=Decimal("16.00"), effective_on=opening_day,
            note="demo opening balance (synthetic)",
            cap_hours=cap_hours_on(session, emp_id, opening_day),
        ))
    # TWO people out sick the same day (usage_day, their punches skipped
    # above): the SOS sick-pay block discloses per date only with >= 2
    # distinct priced employees, so a lone usage would demo as a
    # suppression marker instead of a dollar line.
    for w in sick_stars[:2]:
        session.add(SickLeaveLedger(
            employee_id=ids[w.full_name], entry_type="usage",
            hours=Decimal("-8.00"), effective_on=usage_day,
        ))
    session.commit()
    taker_names = " and ".join(w.full_name for w in sick_stars[:2])
    notes.append(f"sick leave: {taker_names} each took 8h on "
                 f"{usage_day.isoformat()} — priced on the SOS sick-pay "
                 f"block AND riding {clean_prop}'s period-2 pay run (G3: "
                 "the submitted entries carry sick_hours)")

    # A published schedule for the coming week, so the schedule and kiosk
    # my-week views have something to show.
    week_start = date.today() - timedelta(days=date.today().weekday()) \
        + timedelta(days=7)
    scheduled = 0
    for prop in sorted(EHID_TO_PROPERTY.values()):
        sched = Schedule(property_id=prop, week_start=week_start,
                         status="published", version=1,
                         published_by="demo-seed",
                         published_at=datetime.now(UTC))
        session.add(sched)
        session.flush()
        staff = [w for w in hourly
                 if w.placements[0][0] == prop and w is not norate_star][:6]
        for w in staff:
            a = assignments[(ids[w.full_name], prop)]
            for offset in range(5):
                day = week_start + timedelta(days=offset + w.ref % 2)
                session.add(Shift(
                    schedule_id=sched.schedule_id, business_date=day,
                    department_id=a.department_id, start_time=time(9, 0),
                    end_time=time(17, 0), crosses_midnight=False,
                    employee_id=ids[w.full_name],
                ))
                scheduled += 1
    notes.append(f"published schedule for week of {week_start.isoformat()}: "
                 f"{scheduled} shifts")

    # The demand story (J6): ONE audited pull through the config-selected
    # adapter — the exact path POST /api/crm/refresh takes — against the
    # Delphi mock's fixed world. HISJ is the crm_ref property (the
    # registry file declares it); labels land in the snapshots for the
    # scheduler page, and deliberately NOT in these notes (stdout is
    # terminal scrollback and CI logs — the roster-import lesson).
    feed = _crm_feed()
    crm_prop = session.get(Property, "HISJ")
    if feed is None:
        notes.append("CRM demand: skipped — set USALI_CRM_PROVIDER=delphi "
                     "(demo.sh does) and re-seed to pull the demand world")
    elif crm_prop is None or crm_prop.crm_ref is None:
        notes.append("CRM demand: skipped — HISJ has no crm_ref in "
                     "mapping/properties.yaml")
    else:
        horizon_end = DEMAND_WEEK + timedelta(days=90)
        pull = feed.fetch_demand(crm_prop.crm_ref, DEMAND_WEEK, horizon_end)
        batch = store_pull(
            session, property_id="HISJ",
            provider=get_settings().crm_provider,
            horizon_start=DEMAND_WEEK, horizon_end=horizon_end, pull=pull,
        )
        session.add(AuditEvent(
            actor_subject="demo-seed", action="crm_refresh",
            resource_type="crm_pull_batch", resource_id=str(batch.batch_id),
        ))
        session.commit()
        fat = max(pull.days, key=lambda d: d.rooms_on_books or 0)
        notes.append(
            f"CRM demand pulled for HISJ ({len(pull.days)} days; dropped "
            f"unread by name: {', '.join(sorted(pull.dropped_fields))}). "
            f"Fat Thursday {fat.stay_date.isoformat()}: "
            f"{fat.rooms_on_books} on books + {fat.group_rooms} group "
            f"rooms — open the Schedule page, week {DEMAND_WEEK.isoformat()},"
            " to see the chips and the block names beside the forecast"
        )

    # A few open punches from yesterday: live, unapproved timecards to review.
    # Their ids are RETURNED: they are the only punches the face stamping may
    # ever touch (a live punch is never the seed's to rewrite — F8).
    yesterday = date.today() - timedelta(days=1)
    open_punches: list[Punch] = []
    for w in hourly[:5]:
        if w is norate_star:
            continue
        emp_id = ids[w.full_name]
        device_id = kiosks[w.placements[0][0]]
        for punch_type, hour in (("clock_in", 9), ("clock_out", 17)):
            p = Punch(
                employee_id=emp_id, kiosk_device_id=device_id,
                punch_type=punch_type,
                punched_at=datetime(yesterday.year, yesterday.month,
                                    yesterday.day, hour, tzinfo=UTC),
                business_date=yesterday,
            )
            session.add(p)
            open_punches.append(p)
        assemble_timecard(session, emp_id, yesterday, anchor=ANCHOR)
    session.commit()
    notes.append(f"open timecards: punches from {yesterday.isoformat()} "
                 "awaiting review/approval")
    return notes, [p.punch_id for p in open_punches]


# ----------------------------------------------------------------- face demo

# Committed SYNTHETIC faces (SFHQ samples; these people DO NOT EXIST — see
# tests/fixtures/faces/README.md). One per property, so each hotel's kiosk
# has someone the matcher can find: hold the photo up to the camera, or
# enroll yourself from the Employees page and punch as that person.
_FACE_FIXTURES = (
    REPO_ROOT / "tests" / "fixtures" / "faces" / "person_a.jpg",
    REPO_ROOT / "tests" / "fixtures" / "faces" / "person_b.jpg",
)


def _face_stars(workers: list[DemoWorker]) -> list[DemoWorker]:
    """One hourly worker per primary property, lowest employee number first,
    capped at the number of committed synthetic faces."""
    stars: list[DemoWorker] = []
    seen: set[str] = set()
    for w in sorted(workers, key=lambda w: w.ref):
        if w.pay_type != "hourly" or w.placements[0][0] in seen:
            continue
        seen.add(w.placements[0][0])
        stars.append(w)
        if len(stars) == len(_FACE_FIXTURES):
            break
    return stars


def _stamp_demo_match_states(session: Session, enrolled_ids: list[int],
                             seed_punch_ids: list[int]) -> None:
    """Stamp the punches THIS run's world seed created — identified by id,
    nothing else — with the story enrollment implies: enrolled people
    verified, their last stamped punch red so the approval gate has
    something to gate, everyone else a grey cold start.

    The id list IS the guard (F8 money lens): 'NULL and recent' described
    live punches too — a punch recorded during an engine outage carries
    NULL as its honest verdict, and a re-run must not fabricate over it.
    Only what the seed wrote is the seed's to rewrite."""
    if not seed_punch_ids or not enrolled_ids:
        return
    session.execute(
        update(Punch)
        .where(Punch.punch_id.in_(seed_punch_ids),
               Punch.employee_id.notin_(enrolled_ids))
        .values(match_state="no_template")
    )
    session.execute(
        update(Punch)
        .where(Punch.punch_id.in_(seed_punch_ids),
               Punch.employee_id.in_(enrolled_ids))
        .values(match_state="verified", match_score=0.93)
    )
    red = session.execute(
        select(Punch.punch_id)
        .where(Punch.employee_id == enrolled_ids[0],
               Punch.punch_id.in_(seed_punch_ids),
               Punch.match_state == "verified")
        .order_by(Punch.punched_at.desc())
    ).scalars().first()
    if red is not None:
        session.execute(
            update(Punch).where(Punch.punch_id == red)
            .values(match_state="unverified", match_score=0.41)
        )
    session.commit()


def _seed_faces(session: Session, workers: list[DemoWorker],
                seed_punch_ids: list[int]) -> None:
    """Enroll the demo stars with the committed synthetic faces via the same
    writer the API uses, then stamp the match story onto the open punches
    the world seed created THIS run (seed_punch_ids — empty on a topped-up
    re-run, so stamping is structurally impossible then). Idempotent: an
    enrolled star is left alone; live punches are identified by NOT being
    in the id list, never by their verdict shape."""
    from usali.face_enrollment import write_face_template
    from usali.face_match import FaceModelsMissing, OnnxFaceEngine
    from usali.models import EmployeeFaceTemplate
    from usali.photo_store import photo_store_from_settings

    settings = get_settings()
    if not settings.biometric_matching_enabled:
        print("  matching disabled (USALI_BIOMETRIC_MATCHING_ENABLED) — "
              "kiosk demos search-only")
        return
    try:
        engine = OnnxFaceEngine(settings.face_model_dir)
    except FaceModelsMissing as exc:
        print(f"  WARNING: {exc}")
        print("  no templates enrolled — kiosk demos search-only")
        return

    store = photo_store_from_settings(settings)
    ids = _employee_ids(session, workers)
    notice = settings.biometric_notice_version.strip() or None
    enrolled_ids: list[int] = []
    for w, photo in zip(_face_stars(workers), _FACE_FIXTURES):
        emp_id = ids[w.full_name]
        already = session.execute(
            select(EmployeeFaceTemplate).where(
                EmployeeFaceTemplate.employee_id == emp_id)
        ).first()
        if already:
            print(f"  {w.full_name}: already enrolled")
            continue
        action = write_face_template(
            session, engine, store, employee_id=emp_id,
            photo_bytes=photo.read_bytes(), actor_subject="demo-seed",
            notice_version=notice,
        )
        if action is None:  # unreachable with the committed fixtures
            print(f"  WARNING: no face found in {photo.name}; skipped")
            continue
        enrolled_ids.append(emp_id)
        print(f"  {w.full_name} enrolled with synthetic face {photo.name} "
              f"(hold tests/fixtures/faces/{photo.name} up to the kiosk "
              "camera to match as them)")
    session.commit()
    if enrolled_ids and seed_punch_ids:
        _stamp_demo_match_states(session, enrolled_ids, seed_punch_ids)
        print("  seeded open punches stamped: green for the enrolled stars, "
              "one red for the approval-gate story, grey for everyone else")
    elif enrolled_ids:
        print("  enrolled on an already-seeded world — open punches left "
              "exactly as recorded (live verdicts are not the seed's to "
              "rewrite)")


def _seed_documents(session: Session) -> None:
    """Ingest the sample PDFs (revenue facts for 2026-07-07, both properties).

    Skips PER FILE by content hash: a pre-existing dev database may hold a
    PARTIAL ingestion (it did — Opera only), and a batch-level guard would
    leave the other property's revenue missing and every SOS for it refusing.
    """
    import hashlib
    import shutil
    import tempfile

    done = set(session.execute(
        select(IngestBatch.file_hash)
        .where(IngestBatch.status == "transformed")
    ).scalars())
    samples = REPO_ROOT / "docs" / "reference" / "samples"
    ingested = skipped = 0
    with tempfile.TemporaryDirectory(prefix="usali-demo-") as tmp:
        work = Path(tmp)
        inbox = work / "inbox"
        inbox.mkdir()
        for sample in sorted(samples.glob("*.pdf")):
            if hashlib.sha256(sample.read_bytes()).hexdigest() in done:
                skipped += 1
                continue
            target = inbox / sample.name
            shutil.copy(sample, target)
            process_file(session, target, processed_dir=work / "processed",
                         failed_dir=work / "failed")
            ingested += 1
    print(f"  ingested {ingested} sample PDFs, {skipped} already present "
          "(revenue facts 2026-07-07)")


def _seed_synthetic_year(session: Session) -> None:
    """The cloud world's financials (K6b): 365 INVENTED days per property
    through the real stage -> transform/promote pipeline. Replaces
    `_seed_documents` under --synthetic-year — the sample PDFs carry real
    production figures, and the public instance must be fictitious by
    construction. Idempotent per (property, day): every stream of a day
    shares one file_hash marker, statuses flip to `transformed` together
    in the day's single transaction, and a re-run skips the day whole —
    a partially seeded day can never be stranded half-done."""
    import hashlib

    from usali.ledger_promote import promote_ledgers
    from usali.ledger_stage import stage_ledgers
    from usali.segment_promote import promote_segments
    from usali.segment_stage import stage_segments
    from usali.stage import stage_records
    from usali.stats_promote import promote_statistics
    from usali.stats_stage import stage_statistics
    from usali.synthetic_year import PROPERTY_SOURCE, synthetic_dates, synthetic_day
    from usali.transform import transform

    done = set(session.execute(
        select(IngestBatch.file_hash)
        .where(IngestBatch.status == "transformed")
    ).scalars())
    ledgers_yaml = str(REPO_ROOT / "mapping" / "ledgers.yaml")
    stats_yaml = str(REPO_ROOT / "mapping" / "statistics.yaml")
    segments_yaml = str(REPO_ROOT / "mapping" / "segments.yaml")
    for pid, source in sorted(PROPERTY_SOURCE.items()):
        seeded = skipped = 0
        for day_date in synthetic_dates():
            marker = hashlib.sha256(
                f"synthetic-v1:{pid}:{day_date}".encode()).hexdigest()
            if marker in done:
                skipped += 1
                continue
            day = synthetic_day(pid, day_date)
            name = f"synthetic:{pid}:{day_date}"
            batches = [stage_records(session, day.financial,
                                     source_file=name, file_hash=marker)]
            result = transform(session, source=source,
                               business_date=day_date, edition=12)
            if result.unmapped:
                raise SystemExit(
                    f"synthetic day {name} staged {result.unmapped} unmapped "
                    "rows — the generator drifted from the mapping YAML")
            if day.ledgers:
                stage_ledgers(session, day.ledgers, batch=batches[0],
                              source_file=name, file_hash=marker)
                promote_ledgers(session, ledgers_yaml, source=source,
                                business_date=day_date)
            batches.append(stage_statistics(session, day.statistics,
                                            source_file=name, file_hash=marker))
            promote_statistics(session, stats_yaml, source=source,
                               business_date=day_date)
            # Coverage row for the day's statistics report (issue #9). The
            # synthetic path stages/promotes directly, so unlike process_file
            # it must record coverage itself — without it complete_days (and
            # therefore every trend/comparison) would ignore the seeded days.
            record_coverage(session, pid, day_date,
                            "manager_flash" if source == "OPERA" else "manager_report")
            batches.append(stage_segments(session, day.segments,
                                          source_file=name, file_hash=marker))
            promote_segments(session, segments_yaml, source=source,
                             business_date=day_date)
            for batch in batches:
                batch.status = "transformed"
            session.commit()
            seeded += 1
        print(f"  {pid}: {seeded} synthetic days seeded, {skipped} already "
              "present")


# The dev personas' ORG-WIDE grants (Pillar L decision 4): role authority
# is the org-scoped role_assignment rows, not realm token roles — no SQL
# backfill can derive these from tokens, so the seed writes them. A row
# with property_id None is an org-wide grant; the seed session is bound
# to the FOUNDING org (L2 wiring), so org_id lands as org 1's.
_PERSONA_ORG_GRANTS = (
    ("dev-admin", "org_admin"),
    ("dev-payroll", "payroll_admin"),
    ("dev-accountant", "accountant"),
)


def _ensure_grant(session: Session, subject: str, role: str,
                  property_id: str | None) -> None:
    """Find-or-create ONE grant row — a re-seed never duplicates (also
    DB-enforced since l4a0orggrant: the unique is NULLS NOT DISTINCT)."""
    exists = session.execute(
        select(RoleAssignment).where(
            RoleAssignment.keycloak_subject == subject,
            RoleAssignment.role == role,
            # IS NOT DISTINCT FROM: matches NULL = NULL (the org-wide row).
            RoleAssignment.property_id.is_not_distinct_from(property_id),
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(RoleAssignment(keycloak_subject=subject, role=role,
                                   property_id=property_id))


def _grant_persona_roles(session: Session) -> None:
    """The dev personas' role authority, planted as role_assignment rows;
    the realm import cannot write those, so the seed does: dev-gm gets
    property_gm scoped to both properties, dev-admin/dev-payroll/
    dev-accountant get their roles ORG-WIDE (property_id None)."""
    try:
        kc = KeycloakAdminClient.from_settings(get_settings())
        lookups = {
            username: kc.find_user_by_username(username)
            for username, _ in _PERSONA_ORG_GRANTS
        }
        lookups["dev-gm"] = kc.find_user_by_username("dev-gm")
    except (KeycloakAdminError, httpx.HTTPError) as exc:
        print(f"  WARNING: cannot reach Keycloak to grant persona roles "
              f"({exc}); operator logins will hold NO org authority")
        return
    for username, role in _PERSONA_ORG_GRANTS:
        found = lookups[username]
        if found is None:
            print(f"  WARNING: realm has no {username} user (old Keycloak "
                  "container?); run demo.sh again with --fresh")
            continue
        _ensure_grant(session, found[0], role, None)
        print(f"  {username} granted {role} org-wide")
    gm = lookups["dev-gm"]
    if gm is None:
        print("  WARNING: realm has no dev-gm user (old Keycloak container?); "
              "run demo.sh again with --fresh, or log in as dev-payroll")
    else:
        for prop in sorted(EHID_TO_PROPERTY.values()):
            _ensure_grant(session, gm[0], "property_gm", prop)
        print("  dev-gm scoped as property_gm over HISJ + SSSJ")
    session.commit()


_SENTINEL_ACTION = "demo_seed"
# The enrichment marker's two states. `_seed_world` commits many times
# and inserts UNCONDITIONALLY (payroll profiles, punches, pay runs, sick
# ledgers, schedules) — it is not safe to re-run over a partial world.
# A STARTED marker written before it and a DONE marker after turn a
# crash mid-enrichment into a loud refusal, not a silent double-seed on
# the next run (K7 ops lens). DONE stays "v1" so a world seeded before
# this fence still reads as complete.
_SENTINEL_DONE = "v1"
_SENTINEL_STARTED = "v1-started"


def _enrichment_state(session: Session) -> str:
    """'done' | 'interrupted' | 'fresh' for the demo-world enrichment."""
    markers = set(session.execute(
        select(AuditEvent.resource_id).where(
            AuditEvent.action == _SENTINEL_ACTION,
            AuditEvent.resource_type == "demo",
        )
    ).scalars())
    if _SENTINEL_DONE in markers:
        return "done"
    if _SENTINEL_STARTED in markers:
        return "interrupted"
    return "fresh"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", required=True, type=Path,
                        help="Inn-Flow roster export (JSON, on the volume)")
    parser.add_argument("--synthetic-year", action="store_true",
                        help="seed 365 INVENTED financial days instead of "
                             "ingesting the sample PDFs (cloud: real report "
                             "figures must never reach the public instance)")
    args = parser.parse_args()
    if not args.roster.exists():
        raise SystemExit(f"roster not found: {args.roster} — is the encrypted "
                         "volume mounted?")

    workers, report = load_demo_roster(args.roster)
    print(f"Roster: {len(workers)} active people")
    for line in report:
        print(f"  {line}")

    # L2: the seed runs as the OWNER role (job.sh keeps it), which FORCE
    # RLS still filters at query time — so the session must be org-bound
    # like every serving session. FOUNDING_ORG_ID is the L3 seam; the
    # demo world is the founding org's by construction.
    factory = OrgBoundSessionFactory(
        make_session_factory(make_engine(get_settings().db_url)),
        FOUNDING_ORG_ID,
    )
    with factory() as session:
        print("Base seeds (schedules, mappings, properties)")
        _seed_base(session)

        print("People")
        _seed_people(session, workers)

        if args.synthetic_year:
            print("Synthetic financial year (365 invented days per property)")
            _seed_synthetic_year(session)
        else:
            print("Documents (per-file idempotent)")
            _seed_documents(session)

        # Additive, idempotent property config — OUTSIDE the sentinel so a
        # re-deploy backfills it onto an already-seeded world (the room
        # inventory / stat-config the sealed enrichment could never add).
        print("Property config (room inventory, fiscal, OOO, stat — idempotent)")
        _seed_property_config(session)

        state = _enrichment_state(session)
        seed_punch_ids: list[int] = []
        if state == "done":
            print("Demo world already seeded — people topped up, enrichment "
                  "skipped (scripts/demo.sh --fresh rebuilds from scratch)")
        elif state == "interrupted":
            raise SystemExit(
                "Demo world enrichment was INTERRUPTED on a previous run "
                "(started marker present, no completion). The world is "
                "half-seeded and _seed_world cannot be re-run safely — it "
                "inserts unconditionally and would double-seed. Drop the "
                "usali database (or run scripts/cloud/teardown.sh) and "
                "reseed from scratch.")
        else:
            print("Demo world (synthetic rates, history, sick leave, PII)")
            # STARTED before the multi-commit enrichment, DONE after —
            # so a crash in between is caught as 'interrupted' next run.
            session.add(AuditEvent(actor_subject="demo-seed",
                                   action=_SENTINEL_ACTION,
                                   resource_type="demo",
                                   resource_id=_SENTINEL_STARTED))
            session.commit()
            notes, seed_punch_ids = _seed_world(session, workers)
            session.add(AuditEvent(actor_subject="demo-seed",
                                   action=_SENTINEL_ACTION,
                                   resource_type="demo",
                                   resource_id=_SENTINEL_DONE))
            session.commit()
            print("Talking points:")
            for note in notes:
                print(f"  - {note}")

        print("Faces (synthetic enrollment)")
        _seed_faces(session, workers, seed_punch_ids)

        _grant_persona_roles(session)
    print("Seed complete.")


if __name__ == "__main__":
    main()
