from usali.auth import Principal
from usali.models import Department, Organization, Property, RoleAssignment
from usali.workforce import resolve_scope
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _principal(roles, sub="u1", scopes=None):
    return Principal(subject=sub, username=sub, roles=frozenset(roles), scopes=scopes)


def test_org_wide_grant_gets_all_properties(db_session):
    """L4: all-properties comes from an ORG-WIDE grant row (property_id
    NULL), never from the token role — the same principal WITHOUT the
    row resolves to nothing, whatever the token says."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.commit()
    for role in ("org_admin", "accountant", "payroll_admin"):
        sub = f"wide-{role}"
        db_session.add(RoleAssignment(
            keycloak_subject=sub, role=role, property_id=None
        ))
        db_session.commit()
        scope = resolve_scope(_principal([role], sub=sub), db_session)
        assert scope.all_properties is True
        assert scope.allows_property("HISJ") and scope.allows_property("SSSJ")


def test_a_token_role_alone_grants_nothing(db_session):
    """The decision-4 unit pin: realm roles are realm-global, so with NO
    grant row visible, even an org_admin token resolves to zero scope."""
    for role in ("org_admin", "accountant", "payroll_admin"):
        scope = resolve_scope(_principal([role]), db_session)
        assert scope.all_properties is False
        assert not scope.allows_property("HISJ")


def test_scope_from_claims_when_present(db_session):
    # L8-F5: the claim is intersected with the properties visible under the
    # active org, so the claimed property must actually exist in-org (it always
    # does on a real request — a GM's claimed hotel is one of their org's).
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.commit()
    p = _principal(["property_gm"], scopes=frozenset({("HISJ", None)}))
    scope = resolve_scope(p, db_session)
    assert scope.all_properties is False
    assert scope.allows_property("HISJ")
    assert not scope.allows_property("SSSJ")


def test_empty_claim_denies_all(db_session):
    # Claim present but empty → authoritative deny (NOT a DB fallback).
    p = _principal(["property_gm"], scopes=frozenset())
    scope = resolve_scope(p, db_session)
    assert not scope.allows_property("HISJ")


def _seed_org_property_dept(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    return dept.department_id


def test_scope_from_db_when_no_claim(db_session):
    dept_id = _seed_org_property_dept(db_session)
    db_session.add(RoleAssignment(keycloak_subject="mgr", role="department_manager",
                                  property_id="HISJ", department_id=dept_id))
    db_session.commit()

    p = _principal(["department_manager"], sub="mgr", scopes=None)  # no claim → DB
    scope = resolve_scope(p, db_session)
    assert scope.allows_department("HISJ", dept_id)
    assert not scope.allows_property("HISJ")  # dept-scoped, not property-wide


def test_claims_and_db_agree(db_session):
    # Same assignment expressed as a claim vs a role_assignment row → same Scope.
    dept_id = _seed_org_property_dept(db_session)
    db_session.add(RoleAssignment(keycloak_subject="mgr", role="department_manager",
                                  property_id="HISJ", department_id=dept_id))
    db_session.commit()

    from_db = resolve_scope(_principal(["department_manager"], sub="mgr"), db_session)
    from_claims = resolve_scope(
        _principal(["department_manager"], sub="mgr", scopes=frozenset({("HISJ", dept_id)})),
        db_session,
    )
    assert from_db == from_claims
