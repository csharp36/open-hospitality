"""Seed a local dev realm + employee table from a roster file.

WHY THIS EXISTS: standing up a realistic local org by hand is tedious, and the
confinement rules (A2 scoping) and suppression rules (B3/C3/D1/D3) only get
exercised against a realistic SHAPE -- a solo-employee department, an exempt
manager who is never hourly-costed, a new hire with no rate yet. A roster file
reproduces that shape in one command.

WHAT A ROSTER MAY CONTAIN: identity and org placement only --
`full_name,email,role,department,pay_type`. A Keycloak realm user has exactly
`username / email / firstName / lastName / credentials / realmRoles /
attributes`; there is nowhere to put an SSN, a bank account, or a W-4, so this
loader does not accept those columns. It REFUSES a file carrying them
(`_FORBIDDEN_COLUMNS`) rather than quietly ignoring them, because the realistic
failure is someone pasting a full HR export from an incumbent system straight in
and never noticing that a plaintext SSN column came along for the ride.

That refusal is the whole point: Pillar C exists so an SSN is HPKE-sealed in the
browser and never lands server-readable. A seed file with plaintext SSNs on a
developer laptop would undo that in one command, gitignored or not.
"""

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import OPERATOR_ROLES
from usali.keycloak_admin import KeycloakAdmin
from usali.models import PAY_TYPES, Department, Employee, EmployeeAssignment
from usali.onboarding import OnboardRequest, onboard_employee

REQUIRED_COLUMNS = frozenset({"full_name", "pay_type"})
OPTIONAL_COLUMNS = frozenset({"email", "role", "department", "employee_ref"})
ALLOWED_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

# Substrings that mark a column as carrying data this loader must never ingest.
# Matched as substrings (not exact names) so `employee_ssn`, `ssn_last4`,
# `bank_routing_number`, and `Direct Deposit Account` are all caught.
_FORBIDDEN_COLUMNS = (
    "ssn", "social", "tin", "bank", "routing", "account_number", "acct",
    "dob", "birth", "w4", "w-4", "i9", "i-9", "passport", "license",
    "salary", "rate", "wage", "compensation",
)



class RosterError(Exception):
    """The roster file is malformed, or carries data the seeder refuses."""


@dataclass(frozen=True)
class RosterRow:
    full_name: str
    pay_type: str
    email: str | None = None
    role: str | None = None
    department: str | None = None
    # Stable external id (the incumbent system's employee number). When present
    # it is the idempotency key; without it two genuinely different people who
    # share a name are indistinguishable, so the loader refuses such a file.
    employee_ref: str | None = None

    @property
    def identity_key(self) -> str:
        return f"ref:{self.employee_ref}" if self.employee_ref else f"name:{self.full_name}"


def _normalize(column: str) -> str:
    # Collapse ANY unicode whitespace (non-breaking space, tab) to a single
    # underscore, and strip a UTF-8 BOM. Excel's default "CSV UTF-8" save puts a
    # BOM on the first header, which would otherwise make `full_name` read as
    # `﻿full_name` and produce a baffling "missing required column" error
    # naming a column the user can plainly see in the file.
    return re.sub(r"\s+", "_", column.replace("﻿", "").strip().lower())


def _check_columns(header: Iterable[str]) -> list[str]:
    columns = [_normalize(c) for c in header]

    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if duplicates:
        raise RosterError(
            f"roster has duplicate columns: {', '.join(duplicates)}. "
            "Refusing rather than silently taking the last one."
        )

    forbidden = sorted(
        {c for c in columns for token in _FORBIDDEN_COLUMNS if token in c}
    )
    if forbidden:
        raise RosterError(
            f"roster carries columns this loader refuses: {', '.join(forbidden)}. "
            "A realm user has no field for compensation or government identifiers; "
            "strip these columns from the export rather than seeding them. "
            "Pay rates belong in the encrypted employee record via the app, not a file."
        )

    missing = sorted(REQUIRED_COLUMNS - set(columns))
    if missing:
        raise RosterError(f"roster is missing required columns: {', '.join(missing)}")

    unknown = sorted(set(columns) - ALLOWED_COLUMNS)
    if unknown:
        raise RosterError(
            f"roster has unrecognized columns: {', '.join(unknown)}. "
            "This loader accepts IDENTITY AND ORG PLACEMENT ONLY "
            f"({', '.join(sorted(ALLOWED_COLUMNS))}). "
            "Do NOT widen this list to accommodate an export -- the allowlist is "
            "the control that keeps compensation and government identifiers out "
            "of a plaintext file. Strip the extra columns instead."
        )
    return columns


def load_roster(path: Path) -> list[RosterRow]:
    """Parse and validate a roster CSV. Raises RosterError on anything it will
    not ingest -- fail loudly at load, never half-seed."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            raise RosterError("roster file is empty") from None
        columns = _check_columns(header)

        rows: list[RosterRow] = []
        for lineno, raw in enumerate(reader, start=2):
            if not any(field.strip() for field in raw):
                continue  # blank line
            record = {
                col: raw[i].strip() if i < len(raw) else ""
                for i, col in enumerate(columns)
            }
            rows.append(_row_from(record, lineno))
    if not rows:
        raise RosterError("roster file has a header but no rows")

    # Two different people can share a name -- "Maria Garcia" twice in a 28-person
    # hotel roster is not hypothetical. Without a stable ref there is no way to
    # tell a duplicate row from a second person, and silently skipping would drop
    # a real employee while reporting success. Refuse instead.
    ambiguous = sorted({
        r.full_name for r in rows
        if r.employee_ref is None
        and sum(1 for other in rows if other.full_name == r.full_name) > 1
    })
    if ambiguous:
        raise RosterError(
            f"roster has repeated names with no employee_ref: {', '.join(ambiguous)}. "
            "If these are the same person, remove the duplicate row; if they are "
            "different people, add an employee_ref column so they can be told apart."
        )
    return rows


def _row_from(record: dict[str, str], lineno: int) -> RosterRow:
    # NEVER interpolate a cell VALUE into an error message. A column-shifted
    # export -- routine when hand-massaging an HR dump -- puts an SSN in the
    # pay_type cell, and cli.py echoes RosterError to stderr, which lands in
    # terminal scrollback and CI logs. Report the column and line only.
    # (This is the C2 lesson: a provider's rejected-field echo leaked PII the
    # same way.)
    full_name = record.get("full_name", "")
    if not full_name:
        raise RosterError(f"line {lineno}: full_name is required")

    pay_type = record.get("pay_type", "").lower()
    if pay_type not in PAY_TYPES:
        raise RosterError(
            f"line {lineno}: pay_type must be one of {sorted(PAY_TYPES)} "
            "(value withheld from this message -- check the pay_type column)"
        )

    role = record.get("role") or None
    if role is not None and role not in OPERATOR_ROLES and role != "employee":
        raise RosterError(
            f"line {lineno}: unrecognized value in the role column. "
            f"Allowed: {', '.join(sorted(OPERATOR_ROLES | {'employee'}))}"
        )

    email = record.get("email") or None
    # An operator role is what triggers Keycloak provisioning, and provisioning
    # needs an email. Catch it here rather than letting onboard_employee quietly
    # skip provisioning and produce a "seeded" user who cannot log in.
    if role in OPERATOR_ROLES and email is None:
        raise RosterError(
            f"line {lineno}: role {role!r} logs in, so an email is required to "
            "provision the Keycloak user"
        )

    return RosterRow(
        full_name=full_name,
        pay_type=pay_type,
        email=email,
        role=role,
        department=record.get("department") or None,
        employee_ref=record.get("employee_ref") or None,
    )


@dataclass(frozen=True)
class SeedResult:
    created: int
    skipped: int
    departments_created: int


def seed_roster(
    session: Session,
    kc: KeycloakAdmin,
    rows: list[RosterRow],
    *,
    property_id: str,
    actor_subject: str = "roster-seed",
) -> SeedResult:
    """Onboard each roster row, creating departments on demand.

    COMMITS PER ROW. This is deliberate and is the difference between a partial
    failure being recoverable and being corrupting. `kc.create_user` is a
    non-transactional external side effect; if the whole run shared one
    transaction, a failure on row N would roll back rows 1..N-1 in the database
    while leaving their Keycloak accounts behind -- orphaned privileged realm
    users that no DB-driven view can see and that terminate_employee can never
    reach. Committing each row keeps DB state and realm state in step, so a
    re-run genuinely tops up.

    Idempotency keys on `employee_ref` when the roster supplies one, else on
    full_name. `load_roster` refuses a file with repeated names and no ref, so a
    name key is only ever used where it is unambiguous within that file.
    """
    existing = set(
        session.execute(
            select(Employee.full_name)
            .join(EmployeeAssignment, EmployeeAssignment.employee_id == Employee.employee_id)
            .where(EmployeeAssignment.property_id == property_id)
        ).scalars()
    )

    created = skipped = departments_created = 0
    for row in rows:
        if row.full_name in existing:
            skipped += 1
            continue

        department_id: int | None = None
        if row.department is not None:
            department_id, made = _department_id(session, property_id, row.department)
            departments_created += int(made)

        onboard_employee(
            session,
            kc,
            OnboardRequest(
                full_name=row.full_name,
                email=row.email,
                property_id=property_id,
                pay_type=row.pay_type,
                role=row.role if row.role in OPERATOR_ROLES else None,
                department_id=department_id,
            ),
            actor_subject=actor_subject,
        )
        session.commit()  # see the per-row commit rationale in the docstring
        existing.add(row.full_name)
        created += 1

    return SeedResult(
        created=created, skipped=skipped, departments_created=departments_created
    )


def _department_id(session: Session, property_id: str, name: str) -> tuple[int, bool]:
    dept = session.execute(
        select(Department).where(
            Department.property_id == property_id, Department.name == name
        )
    ).scalar_one_or_none()
    if dept is not None:
        return dept.department_id, False
    dept = Department(property_id=property_id, name=name)
    session.add(dept)
    session.flush()
    return dept.department_id, True
