"""L6b (Pillar L decision 6): the provisioning primitive.

`provision_tenant` chains KC org -> first admin user -> membership -> DB
`organization` row + org-wide `org_admin` grant, IDEMPOTENTLY, on the owner
(un-instrumented, RLS-bypassing) session. The pins here kill the mutants the
task named: provision not idempotent (a second run duplicates/errors), the
grant written property-scoped or non-org-wide, and — tying L3+L4 together — a
provisioned admin whose authority is visible ONLY inside their own org.
"""

import pytest
from sqlalchemy import func, select

from tests.orgwall import app_role_url
from usali.auth import effective_roles
from usali.db import make_engine, make_session_factory
from usali.keycloak_admin import (
    InMemoryKeycloakAdmin,
    KeycloakAdminConflict,
    KeycloakAdminError,
)
from usali.models import Organization, RoleAssignment
from usali.provisioning import provision_tenant
from usali.tenancy import bind_org_context

RIVAL_ALIAS = "rival-hotel-group"


def _provision(session, kc, *, alias=RIVAL_ALIAS, name="Rival Hotel Group",
               username="rival-admin", email="admin@rival.com"):
    return provision_tenant(
        session, kc,
        org_name=name, org_alias=alias,
        admin_username=username, admin_email=email, admin_full_name="Rival Admin",
    )


def test_provision_creates_org_user_membership_row_and_grant(db_session, founding_org):
    """The founding org is org 1; provisioning stands up org 2 with its first
    admin — a KC org, a realm user in it, a DB organization row keyed on the
    alias, and the org-wide org_admin grant."""
    kc = InMemoryKeycloakAdmin()
    result = _provision(db_session, kc)
    db_session.commit()

    # KC side: an org exists under the alias, the admin user exists and is a member.
    assert kc.find_organization_by_alias(RIVAL_ALIAS) == result.kc_org_id
    assert kc.users[result.admin_subject]["realm_roles"] == ["org_admin"]
    assert result.admin_subject in kc.organizations[result.kc_org_id]["members"]

    # DB side: the organization row carries the alias (the L3 join key).
    org = db_session.get(Organization, result.org_id)
    assert org.kc_org_alias == RIVAL_ALIAS and org.name == "Rival Hotel Group"
    assert result.org_id == 2  # founding org is 1


def test_provision_writes_the_grant_org_wide_in_the_new_org(db_session, founding_org):
    """The mutant: a grant written property-scoped, or under the wrong org.
    The first grant is org_admin, org_id = the NEW org, property_id NULL."""
    kc = InMemoryKeycloakAdmin()
    result = _provision(db_session, kc)
    db_session.commit()

    grant = db_session.execute(
        select(RoleAssignment).where(
            RoleAssignment.keycloak_subject == result.admin_subject
        )
    ).scalar_one()
    assert grant.role == "org_admin"
    assert grant.org_id == result.org_id
    assert grant.property_id is None  # ORG-WIDE, not scoped to a property
    assert grant.department_id is None


def test_provision_is_idempotent(db_session, founding_org):
    """The marquee L6b pin: a second run changes NOTHING — one KC org, one
    admin user, one DB org row, one grant. Find-or-create at every step."""
    kc = InMemoryKeycloakAdmin()
    first = _provision(db_session, kc)
    db_session.commit()
    second = _provision(db_session, kc)
    db_session.commit()

    assert second == first  # same ids everywhere
    assert len(kc.organizations) == 1
    assert len(kc.users) == 1
    assert db_session.execute(
        select(func.count()).select_from(Organization).where(
            Organization.kc_org_alias == RIVAL_ALIAS
        )
    ).scalar_one() == 1
    assert db_session.execute(
        select(func.count()).select_from(RoleAssignment).where(
            RoleAssignment.keycloak_subject == first.admin_subject
        )
    ).scalar_one() == 1


def test_provision_reuses_an_existing_realm_user(db_session, founding_org):
    """Partial-failure recovery: a realm account left by a half-finished run
    is adopted, not duplicated (the create_user look-before-create posture)."""
    kc = InMemoryKeycloakAdmin()
    sub = kc.create_user(
        username="rival-admin", email="admin@rival.com",
        full_name="Rival Admin", realm_roles=["org_admin"],
    )
    result = _provision(db_session, kc)
    db_session.commit()
    assert result.admin_subject == sub
    assert len(kc.users) == 1


def test_provision_refuses_a_username_owned_by_a_different_person(db_session, founding_org):
    """The onboarding conflict rule carried in: a username already held by a
    different email must not be silently reused."""
    kc = InMemoryKeycloakAdmin()
    kc.create_user(
        username="rival-admin", email="someone-else@rival.com",
        full_name="Someone Else", realm_roles=["org_admin"],
    )
    with pytest.raises(KeycloakAdminConflict, match="different person"):
        _provision(db_session, kc)


def test_provision_remaps_the_role_when_adopting_a_user_missing_it(db_session, founding_org):
    """Issue 2: a prior create-succeeded-but-map-failed run left the admin
    WITHOUT org_admin. Adoption re-applies the mapping (idempotent), so the
    admin is not stranded below even the coarse operator gate."""
    kc = InMemoryKeycloakAdmin()
    sub = kc.create_user(
        username="rival-admin", email="admin@rival.com",
        full_name="Rival Admin", realm_roles=[],  # role never got mapped
    )
    result = _provision(db_session, kc)
    db_session.commit()
    assert result.admin_subject == sub
    assert "org_admin" in kc.users[sub]["realm_roles"]


def test_provision_refuses_an_org_instrumented_session(db_session, founding_org):
    """Issue 3: an org-bound session gets a diagnosable message up front, not a
    deep OrgContextMismatch/RLS failure. No KC side effects on the refused path."""
    bind_org_context(db_session, 1)
    kc = InMemoryKeycloakAdmin()
    with pytest.raises(ValueError, match="owner .* session"):
        _provision(db_session, kc)
    assert kc.organizations == {} and kc.users == {}


@pytest.fixture(scope="module")
def app_role_engine(db_url):
    engine = make_engine(app_role_url(db_url))
    yield engine
    engine.dispose()


def test_provisioned_admin_acts_only_in_their_org(
    db_session, founding_org, app_role_engine
):
    """Ties L3+L4 together end to end: the provisioned admin's org_admin
    authority is visible under the NEW org's walls and NOWHERE else. On the
    RLS-bound app role, a session bound to the new org sees the grant; the same
    subject bound to the founding org sees none (the grant lives in org 2, and
    org 1's walls show none of it)."""
    kc = InMemoryKeycloakAdmin()
    result = _provision(db_session, kc)
    db_session.commit()

    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, result.org_id)
        assert effective_roles(s, result.admin_subject) == frozenset({"org_admin"})
    with factory() as s:
        bind_org_context(s, 1)  # the founding org
        assert effective_roles(s, result.admin_subject) == frozenset()


def test_fake_create_user_refuses_a_duplicate_email():
    """Parity with the realm: `duplicateEmailsAllowed=false` on usali, so real
    Keycloak 409s on a second account with the same email. The fake refused a
    duplicate USERNAME but happily took a duplicate email — a double more
    permissive than production, which is exactly how the live 409 below reached
    a real owner through a fully green suite."""
    kc = InMemoryKeycloakAdmin()
    kc.create_user(
        username="test02", email="owner@hotel.test",
        full_name="Owner", realm_roles=["org_admin"],
    )
    with pytest.raises(KeycloakAdminError, match="duplicate email"):
        kc.create_user(
            username="test36", email="owner@hotel.test",
            full_name="Owner", realm_roles=["org_admin"],
        )


def test_provision_adopts_the_account_holding_the_email_under_another_username(
    db_session, founding_org
):
    """The live dead end, 2026-08-27.

    A signup died AFTER Keycloak provisioning but BEFORE the DB org insert (an
    organization_pkey collision), leaving a realm user `test02` holding the
    invited email. The owner retried and — told to pick a fresh workspace name —
    chose `test36`. `admin_username` is the workspace alias, so the
    username lookup missed, `create_user` ran, and Keycloak 409'd on the email:
    `{"errorMessage":"User exists with same email"}`, surfaced as a 500.

    The look-before-create posture is meant to make a partial failure
    recoverable, but keyed on username it only recovers when the owner happens
    to retype the same alias. Nothing tells them to, and the form invites a new
    one. The EMAIL is the person here — it is proven by the invite OTP, while
    the alias merely names the workspace — so adoption keys on it.
    """
    kc = InMemoryKeycloakAdmin()
    orphan = kc.create_user(
        username="test02", email="admin@rival.com",
        full_name="Rival Admin", realm_roles=["org_admin"],
    )
    result = _provision(db_session, kc, alias="test36", username="test36")
    db_session.commit()
    assert result.admin_subject == orphan, "must adopt the account holding the email"
    assert len(kc.users) == 1, "no second account for the same person"
