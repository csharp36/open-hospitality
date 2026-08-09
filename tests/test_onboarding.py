from datetime import date

import pytest

from sqlalchemy import select

from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import AuditEvent, Employee, Organization, Property, RoleAssignment
from usali.onboarding import OnboardRequest, onboard_employee, terminate_employee
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.commit()


def test_onboard_operator_provisions_kc_and_writes_assignment(db_session):
    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    emp = onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Gina M", email="gina@x.com", property_id="HISJ",
                       pay_type="salary", role="property_gm"),
        actor_subject="admin-1",
    )
    db_session.commit()

    assert emp.keycloak_subject is not None
    assert kc.users[emp.keycloak_subject]["realm_roles"] == ["property_gm"]
    ra = db_session.execute(select(RoleAssignment)).scalars().all()
    assert len(ra) == 1
    assert ra[0].keycloak_subject == emp.keycloak_subject
    assert ra[0].role == "property_gm" and ra[0].property_id == "HISJ"
    assert ra[0].department_id is None
    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "onboard_employee")
    ).scalars().all()
    assert len(audits) == 1 and audits[0].actor_subject == "admin-1"


def test_onboard_hourly_without_role_creates_record_no_kc(db_session):
    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    emp = onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Hank H", email=None, property_id="HISJ",
                       pay_type="hourly", role=None),
        actor_subject="admin-1",
    )
    db_session.commit()
    assert emp.keycloak_subject is None
    assert kc.users == {}
    assert db_session.execute(select(RoleAssignment)).scalars().all() == []


def test_onboard_org_admin_writes_org_wide_grant_and_resolves_all_properties(db_session):
    """L6b coherence: an org-wide role (org_admin) gets an ORG-WIDE grant
    (property_id NULL) — the SAME shape the seeds/provision_tenant write — so
    its resolve_scope sees ALL properties (the pre-L4 behavior restored), not
    the one property it was onboarded at. The mutant this kills: onboarding
    writing a property-scoped grant for org-wide roles (which narrowed their
    resolve_scope VIEW to a single property)."""
    from usali.auth import Principal
    from usali.workforce import resolve_scope

    _seed(db_session)
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.commit()
    kc = InMemoryKeycloakAdmin()
    emp = onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Ada A", email="ada@x.com", property_id="HISJ",
                       pay_type="salary", role="org_admin"),
        actor_subject="admin-1",
    )
    db_session.commit()

    ra = db_session.execute(select(RoleAssignment)).scalars().one()
    assert ra.role == "org_admin"
    assert ra.property_id is None and ra.department_id is None  # ORG-WIDE
    principal = Principal(
        subject=emp.keycloak_subject, username="ada",
        roles=frozenset({"org_admin"}),
    )
    assert resolve_scope(principal, db_session).all_properties is True


def test_onboard_accountant_also_gets_an_org_wide_grant(db_session):
    """The rule spans every org-wide role: accountant, like org_admin and
    payroll_admin, gets property_id NULL — one grant shape for all three."""
    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Cy C", email="cy@x.com", property_id="HISJ",
                       pay_type="salary", role="accountant"),
        actor_subject="admin-1",
    )
    db_session.commit()
    ra = db_session.execute(select(RoleAssignment)).scalars().one()
    assert ra.role == "accountant" and ra.property_id is None


def test_onboard_property_gm_grant_stays_property_scoped(db_session):
    """The other half of coherence: a PLACE-based role keeps its scoped row.
    property_gm is confined to its property, not widened to org-wide."""
    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Gil G", email="gil@x.com", property_id="HISJ",
                       pay_type="salary", role="property_gm"),
        actor_subject="admin-1",
    )
    db_session.commit()
    ra = db_session.execute(select(RoleAssignment)).scalars().one()
    assert ra.role == "property_gm" and ra.property_id == "HISJ"


def test_onboard_department_manager_scopes_to_department(db_session):
    _seed(db_session)
    from usali.models import Department
    dept = Department(property_id="HISJ", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    kc = InMemoryKeycloakAdmin()
    onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Dana M", email="dana@x.com", property_id="HISJ",
                       department_id=dept.department_id, pay_type="salary",
                       role="department_manager"),
        actor_subject="admin-1",
    )
    db_session.commit()
    from usali.models import RoleAssignment as RA
    ra = db_session.execute(select(RA)).scalars().one()
    assert ra.role == "department_manager" and ra.department_id == dept.department_id


def test_terminate_disables_kc_and_marks_record(db_session):
    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    emp = onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Gina M", email="gina@x.com", property_id="HISJ",
                       pay_type="salary", role="property_gm"),
        actor_subject="admin-1",
    )
    db_session.commit()
    subject = emp.keycloak_subject

    terminate_employee(db_session, kc, emp.employee_id, actor_subject="admin-1",
                       on_date=date(2026, 7, 14))
    db_session.commit()

    assert kc.users[subject]["enabled"] is False
    refreshed = db_session.get(Employee, emp.employee_id)
    assert refreshed.termination_date == date(2026, 7, 14)
    assert db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "terminate_employee")
    ).scalars().all()


def test_existing_realm_user_is_reused_not_duplicated(db_session):
    """Re-onboarding after a partial failure must adopt the existing realm
    account. Creating a second one would strand the first as an untracked
    privileged user that terminate_employee can never disable."""
    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    subject = kc.create_user(
        username="gina", email="gina@x.com", full_name="Gina M", realm_roles=["accountant"]
    )
    emp = onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Gina M", email="gina@x.com", property_id="HISJ",
                       pay_type="salary", role="accountant"),
        actor_subject="admin-1",
    )
    db_session.commit()
    assert emp.keycloak_subject == subject
    assert len(kc.users) == 1


def test_reonboarding_remaps_the_role_on_an_adopted_user(db_session):
    """Issue 2: a create-then-map-failed run left the operator's realm account
    with no role. Re-onboarding adopts the account AND re-applies the mapping,
    so the operator is not left below the coarse gate."""
    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    sub = kc.create_user(
        username="gina", email="gina@x.com", full_name="Gina M", realm_roles=[],
    )
    emp = onboard_employee(
        db_session, kc,
        OnboardRequest(full_name="Gina M", email="gina@x.com", property_id="HISJ",
                       pay_type="salary", role="property_gm"),
        actor_subject="admin-1",
    )
    db_session.commit()
    assert emp.keycloak_subject == sub
    assert "property_gm" in kc.users[sub]["realm_roles"]


def test_username_collision_between_two_people_is_refused(db_session):
    """dana@hotel-a.com and dana@hotel-b.com both derive username 'dana'.
    Reusing the account would let the second person inherit the first one's
    identity and roles; real Keycloak 409s, so the fake must too."""
    from usali.keycloak_admin import KeycloakAdminConflict

    _seed(db_session)
    kc = InMemoryKeycloakAdmin()
    kc.create_user(
        username="dana", email="dana@hotel-a.com", full_name="Dana A", realm_roles=["accountant"]
    )
    with pytest.raises(KeycloakAdminConflict, match="different person"):
        onboard_employee(
            db_session, kc,
            OnboardRequest(full_name="Dana B", email="dana@hotel-b.com", property_id="HISJ",
                           pay_type="salary", role="accountant"),
            actor_subject="admin-1",
        )
