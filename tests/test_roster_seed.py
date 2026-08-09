"""The roster seeder, and above all what it REFUSES to ingest.

The refusal tests are the important ones. The realistic failure mode is not a
malformed CSV -- it is someone exporting the full user table out of an incumbent
HR system and feeding it straight in, carrying a plaintext SSN column nobody
looked at. Pillar C's whole premise is that an SSN is sealed in the browser and
never server-readable; a seed file would route around it.
"""

import pytest
from sqlalchemy import select

from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import Department, Employee, Organization, Property, RoleAssignment
from usali.roster_seed import RosterError, RosterRow, load_roster, seed_roster
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _seed_property(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.commit()


def _write(tmp_path, text: str):
    path = tmp_path / "roster.csv"
    path.write_text(text, encoding="utf-8")
    return path


# --- what the loader refuses -------------------------------------------------

@pytest.mark.parametrize(
    "column",
    [
        "ssn",
        "employee_ssn",
        "SSN Last4",
        "social_security",
        "bank_account",
        "routing_number",
        "date_of_birth",
        "dob",
        "w4_status",
        "i9_verified",
        "drivers_license",
    ],
)
def test_loader_refuses_sensitive_columns(tmp_path, column):
    """A realm user has no field for any of these. Refuse the file rather than
    ignoring the column -- silent ignore means the plaintext still got written
    to disk by whoever produced the export."""
    path = _write(tmp_path, f"full_name,pay_type,{column}\nA B,hourly,x\n")
    with pytest.raises(RosterError, match="refuses"):
        load_roster(path)


@pytest.mark.parametrize("column", ["pay_rate", "hourly_rate", "salary", "wage", "compensation"])
def test_loader_refuses_compensation_columns(tmp_path, column):
    """Rate is the one number every suppression rule in the system exists to
    protect. It does not travel in a flat file."""
    path = _write(tmp_path, f"full_name,pay_type,{column}\nA B,hourly,25.00\n")
    with pytest.raises(RosterError, match="refuses"):
        load_roster(path)


def test_loader_refuses_unknown_columns(tmp_path):
    path = _write(tmp_path, "full_name,pay_type,favorite_color\nA B,hourly,blue\n")
    with pytest.raises(RosterError, match="unrecognized"):
        load_roster(path)


def test_loader_requires_required_columns(tmp_path):
    path = _write(tmp_path, "full_name,email\nA B,a@x.com\n")
    with pytest.raises(RosterError, match="missing required columns: pay_type"):
        load_roster(path)


def test_operator_role_without_email_is_rejected(tmp_path):
    """Provisioning needs an email; without one onboard_employee silently skips
    Keycloak and you get a 'seeded' operator who cannot log in."""
    path = _write(tmp_path, "full_name,pay_type,role\nGina M,salary,property_gm\n")
    with pytest.raises(RosterError, match="email is required"):
        load_roster(path)


def test_unknown_role_is_rejected(tmp_path):
    """The offending VALUE is withheld even here: a column-shifted export can put
    anything in the role cell, and this message reaches stderr."""
    path = _write(tmp_path, "full_name,pay_type,role,email\nA B,salary,wizard,a@x.com\n")
    with pytest.raises(RosterError, match="unrecognized value in the role column"):
        load_roster(path)


def test_bad_pay_type_is_rejected(tmp_path):
    path = _write(tmp_path, "full_name,pay_type\nA B,contractor\n")
    with pytest.raises(RosterError, match="pay_type must be"):
        load_roster(path)


def test_empty_file_is_rejected(tmp_path):
    with pytest.raises(RosterError, match="empty"):
        load_roster(_write(tmp_path, ""))


def test_header_only_is_rejected(tmp_path):
    with pytest.raises(RosterError, match="no rows"):
        load_roster(_write(tmp_path, "full_name,pay_type\n"))


# --- what it accepts ---------------------------------------------------------

def test_loads_a_wellformed_roster(tmp_path):
    path = _write(
        tmp_path,
        "full_name,email,role,department,pay_type\n"
        "Gina Marsh,gina@x.com,property_gm,,salary\n"
        "\n"  # blank lines tolerated
        "Hank Ortiz,,,Housekeeping,hourly\n",
    )
    rows = load_roster(path)
    assert rows == [
        RosterRow("Gina Marsh", "salary", "gina@x.com", "property_gm", None),
        RosterRow("Hank Ortiz", "hourly", None, None, "Housekeeping"),
    ]


def test_header_casing_and_spaces_are_normalized(tmp_path):
    path = _write(tmp_path, "Full Name,Pay Type\nA B,Hourly\n")
    assert load_roster(path)[0] == RosterRow("A B", "hourly")


# --- seeding -----------------------------------------------------------------

def test_seed_provisions_operators_and_creates_departments(db_session):
    _seed_property(db_session)
    kc = InMemoryKeycloakAdmin()
    result = seed_roster(
        db_session, kc,
        [
            RosterRow("Gina Marsh", "salary", "gina@x.com", "property_gm", None),
            RosterRow("Dana Poe", "salary", "dana@x.com", "department_manager", "Housekeeping"),
            RosterRow("Hank Ortiz", "hourly", None, None, "Housekeeping"),
        ],
        property_id="HISJ",
    )
    db_session.commit()

    assert result.created == 3
    assert result.departments_created == 1  # Housekeeping made once, reused once

    employees = db_session.execute(select(Employee)).scalars().all()
    assert len(employees) == 3
    # The hourly employee gets no Keycloak user -- the kiosk handles their identity.
    assert {e.full_name for e in employees if e.keycloak_subject is None} == {"Hank Ortiz"}
    assert len(kc.users) == 2

    depts = db_session.execute(select(Department)).scalars().all()
    assert [d.name for d in depts] == ["Housekeeping"]

    # The department manager is scoped to their department, the GM is not.
    assignments = {
        ra.role: ra.department_id
        for ra in db_session.execute(select(RoleAssignment)).scalars()
    }
    assert assignments["property_gm"] is None
    assert assignments["department_manager"] == depts[0].department_id


def test_seed_is_idempotent(db_session):
    """Re-running an already-seeded file adds nothing."""
    _seed_property(db_session)
    kc = InMemoryKeycloakAdmin()
    rows = [RosterRow("Hank Ortiz", "hourly", None, None, "Housekeeping")]

    first = seed_roster(db_session, kc, rows, property_id="HISJ")
    second = seed_roster(db_session, kc, rows, property_id="HISJ")

    assert (first.created, first.skipped) == (1, 0)
    assert (second.created, second.skipped) == (0, 1)
    assert len(db_session.execute(select(Employee)).scalars().all()) == 1


def test_partial_failure_leaves_earlier_rows_durable_and_resumable(db_session):
    """The claim the old test did NOT check, and which was false before the fix.

    seed_roster commits per row. A failure partway must leave earlier rows in the
    database, so their Keycloak accounts are tracked by an employee row rather
    than orphaned. Before per-row commit, a rollback discarded the DB rows while
    the realm accounts survived -- and since accountant/payroll_admin derive
    scope from the realm role alone, those orphans were all-properties accounts
    that no DB view could see and terminate_employee could never disable.
    """
    _seed_property(db_session)
    kc = InMemoryKeycloakAdmin()
    good = RosterRow("Gina Marsh", "salary", "gina@x.com", "accountant", None)
    exploding = RosterRow("Boom Person", "salary", "boom@x.com", "accountant", None)

    seed_roster(db_session, kc, [good], property_id="HISJ")

    # Force the second row to fail the way real Keycloak does on a dup username.
    kc.create_user(username="boom", email="someone.else@x.com", full_name="X Y", realm_roles=[])
    with pytest.raises(Exception):
        seed_roster(db_session, kc, [good, exploding], property_id="HISJ")

    survivors = db_session.execute(select(Employee.full_name)).scalars().all()
    assert "Gina Marsh" in survivors, "committed row was rolled back by a later failure"

    # Every provisioned realm account is reachable from an employee row.
    subjects = set(db_session.execute(select(Employee.keycloak_subject)).scalars())
    gina = kc.find_user_by_username("gina")
    assert gina is not None and gina[0] in subjects


def test_two_different_people_sharing_a_name_are_refused_without_a_ref(tmp_path):
    """Silently skipping the second Maria would drop a real employee while the
    CLI reported success."""
    path = _write(
        tmp_path,
        "full_name,pay_type,department\n"
        "Maria Garcia,hourly,Housekeeping\n"
        "Maria Garcia,hourly,Front Office\n",
    )
    with pytest.raises(RosterError, match="repeated names with no employee_ref"):
        load_roster(path)


def test_repeated_names_are_allowed_when_disambiguated_by_ref(tmp_path):
    path = _write(
        tmp_path,
        "employee_ref,full_name,pay_type,department\n"
        "11923,Maria Garcia,hourly,Housekeeping\n"
        "11924,Maria Garcia,hourly,Front Office\n",
    )
    rows = load_roster(path)
    assert [r.employee_ref for r in rows] == ["11923", "11924"]
    assert rows[0].identity_key != rows[1].identity_key


def test_error_messages_never_echo_a_cell_value(tmp_path):
    """A column-shifted export puts an SSN in the pay_type cell, and the CLI
    echoes RosterError to stderr -- into scrollback and CI logs. Report the
    column, never the value. (Same class as the C2 provider-echo leak.)"""
    path = _write(tmp_path, "full_name,pay_type\nMaria Garcia,123-45-6789\n")
    with pytest.raises(RosterError) as exc:
        load_roster(path)
    assert "123-45-6789" not in str(exc.value)
    assert "pay_type" in str(exc.value)


def test_excel_utf8_bom_on_first_header_is_tolerated(tmp_path):
    """Excel's default 'CSV UTF-8' writes a BOM. Without utf-8-sig the loader
    reported 'missing required columns: full_name' about a column plainly
    visible in the file."""
    path = tmp_path / "roster.csv"
    path.write_bytes("﻿full_name,pay_type\nA B,hourly\n".encode("utf-8"))
    assert load_roster(path)[0].full_name == "A B"


def test_non_breaking_space_in_header_is_normalized(tmp_path):
    path = _write(tmp_path, "Full Name,Pay Type\nA B,hourly\n")
    assert load_roster(path)[0] == RosterRow("A B", "hourly")


def test_duplicate_columns_are_refused_not_last_wins(tmp_path):
    path = _write(tmp_path, "full_name,pay_type,full_name\nA B,hourly,ZZZ\n")
    with pytest.raises(RosterError, match="duplicate columns"):
        load_roster(path)


def test_unknown_column_message_warns_against_widening_the_allowlist(tmp_path):
    """The allowlist -- not the denylist -- is the actual control. The message a
    future maintainer sees must not read as an invitation to add the column."""
    path = _write(tmp_path, "full_name,pay_type,iban\nA B,hourly,DE89\n")
    with pytest.raises(RosterError) as exc:
        load_roster(path)
    assert "Do NOT widen this list" in str(exc.value)


def test_seed_reuses_an_existing_department(db_session):
    _seed_property(db_session)
    db_session.add(Department(property_id="HISJ", name="Housekeeping"))
    db_session.commit()

    result = seed_roster(
        db_session, InMemoryKeycloakAdmin(),
        [RosterRow("Hank Ortiz", "hourly", None, None, "Housekeeping")],
        property_id="HISJ",
    )
    db_session.commit()
    assert result.departments_created == 0
    assert len(db_session.execute(select(Department)).scalars().all()) == 1
