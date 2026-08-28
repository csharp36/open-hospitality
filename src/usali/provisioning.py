"""Tenant provisioning primitive (Pillar L decision 6).

:func:`provision_tenant` chains the four steps that stand up a new tenant —
KC organization -> first (admin) user -> membership -> DB ``organization`` row
plus the org-wide ``org_admin`` grant — and is IDEMPOTENT: find-or-create at
every step, so a re-run after a partial failure adopts what exists rather than
duplicating or erroring (the bootstrap posture, mirroring
``ensure_default_org`` / the persona seed). This is the PRIMITIVE only: no HTTP
endpoint, no self-service signup flow, no sandbox time-box — those are
onboarding build-sequence steps designed on top of this.

OWNER-SESSION CONTRACT (the constraint L6a surfaced). The DB writes here MUST
run on the owner/superuser session — the un-instrumented factory that BYPASSES
RLS — never an app-role/org-bound request session, for two reasons:

- A founding(org 1)-bound app-role session inserting ``Organization(org_id=2)``
  is refused by the L2 RLS ``WITH CHECK`` (org 2 does not exist as a bound
  context yet — the chicken/egg). The owner session bypasses RLS, so the new
  org's own row and its first grant can land before any org≠2 context exists.
- ``role_assignment`` IS ``OrgScoped``, but on the un-instrumented owner
  session the L6a write-stamp (:func:`usali.tenancy._stamp_wall`) does NOT
  fire. So this function sets the grant's ``org_id`` EXPLICITLY (to the new
  org) rather than relying on the stamp, and writes it ORG-WIDE
  (``property_id`` NULL) — the L4/onboarding coherence shape an org_admin's
  ``resolve_scope`` reads as all-properties.

Passing an org-bound session would make the ``role_assignment`` insert trip
:class:`usali.tenancy.OrgContextMismatch` (explicit org_id ≠ the bound org),
which is the wall doing its job — call this with an owner session.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import ORG_ADMIN
from usali.keycloak_admin import KeycloakAdmin, KeycloakAdminConflict
from usali.models import Organization, RoleAssignment
from usali.tenancy import is_org_instrumented


@dataclass(frozen=True)
class ProvisionResult:
    """What provisioning created (or found). ``org_id`` is the DB tenant id
    (the source of truth); ``kc_org_id`` the Keycloak organization id;
    ``admin_subject`` the first admin's realm subject."""

    org_id: int
    kc_org_id: str
    admin_subject: str


def provision_tenant(
    session: Session,
    kc: KeycloakAdmin,
    *,
    org_name: str,
    org_alias: str,
    admin_username: str,
    admin_email: str,
    admin_full_name: str,
    password: str | None = None,
) -> ProvisionResult:
    """Stand up (or reconcile) one tenant. See the module docstring for the
    owner-session contract. The caller commits.

    ``password`` is optional and used ONLY by the self-service signup path
    (Track B/B1): when provided, the chosen admin's Keycloak password is set
    permanently (via `KeycloakAdmin.set_password`), clearing the
    UPDATE_PASSWORD required action and marking the email verified. The
    operator-provisioned path passes password=None (the default) and keeps
    `create_user`'s UPDATE_PASSWORD / reset-on-first-login posture."""
    # Fail EARLY and legibly on the owner-session contract: an org-instrumented
    # session would refuse this function's cross-org Organization/grant writes
    # deep in the walls (OrgContextMismatch / RLS WITH CHECK). Say so up front.
    if is_org_instrumented(session):
        raise ValueError(
            "provision_tenant needs an owner (un-instrumented, RLS-bypassing) "
            "session; got an org-instrumented one. Its cross-org organization "
            "and grant inserts would fail on the write wall / RLS WITH CHECK. "
            "Pass a session straight off the base factory (as seeds do)."
        )

    # 1. KC organization — find-or-create on the alias (the join key L3
    #    resolves against; the fake refuses a duplicate alias exactly as real
    #    Keycloak does).
    kc_org_id = kc.find_organization_by_alias(org_alias)
    if kc_org_id is None:
        kc_org_id = kc.create_organization(name=org_name, alias=org_alias)

    # 2. First admin user — look before creating (create_user is a
    #    non-transactional side effect: reusing an existing account is what
    #    makes a partial failure recoverable).
    #
    #    Look up by EMAIL, not username. The realm sets
    #    duplicateEmailsAllowed=false, so email is the key Keycloak actually
    #    enforces uniqueness on; `admin_username` is the WORKSPACE ALIAS, which
    #    the owner retypes freely on the signup form. Keyed on username, the
    #    recovery above only fires when they happen to reuse the same alias —
    #    and when they do not, `create_user` hits Keycloak's email 409 with
    #    nothing to catch it. That dead-ended a real owner on 2026-08-27: a run
    #    that died after Keycloak but before the DB insert left an account
    #    holding their email, and every retry under a new alias 500'd.
    #
    #    Email is also the stronger claim to identity: it is proven by the
    #    invite OTP, while the alias is free text naming a workspace.
    existing = kc.find_user_by_email(admin_email)
    if existing is not None:
        admin_subject, _existing_username = existing
        # No different-person check is needed on this branch: matching on the
        # invite-proven email IS the identity match. The account keeps whatever
        # username it already has — the org join key is kc_org_alias, never the
        # username, and the realm allows login by email.
        # Re-apply the coarse role mapping on adoption (idempotent): a prior
        # run that created the user but died before create_user's mapping step
        # would otherwise leave this admin without the org_admin operator role.
        kc.assign_realm_roles(admin_subject, [ORG_ADMIN])
    else:
        # Nobody holds this email, so this is a new person. The username can
        # still be taken — by construction it is the workspace alias, and two
        # owners can want the same one. Refuse EXPLICITLY rather than letting
        # create_user return Keycloak's raw 409: this preserves the onboarding
        # rule that two people sharing a username must never collapse into one
        # account (dana@hotel-a.com / dana@hotel-b.com), which the username
        # lookup used to enforce on the branch above.
        clash = kc.find_user_by_username(admin_username)
        if clash is not None:
            _clash_subject, clash_email = clash
            raise KeycloakAdminConflict(
                f"realm username {admin_username!r} already belongs to a "
                f"different person ({clash_email!r}); {admin_email!r} would "
                "silently inherit their identity and roles. Choose a different "
                "workspace name."
            )
        admin_subject = kc.create_user(
            username=admin_username,
            email=admin_email,
            full_name=admin_full_name,
            realm_roles=[ORG_ADMIN],
        )

    # 3. Membership — idempotent (adding an existing member is a no-op).
    kc.add_member(kc_org_id, admin_subject)

    # Self-service signup (Track B/B1): the owner chose a password and proved
    # their email via the invite click. Set it here (idempotent — a re-run just
    # re-sets the same password). Operator provisioning passes password=None and
    # keeps the create_user UPDATE_PASSWORD/reset-on-first-login posture.
    if password is not None:
        kc.set_password(admin_subject, password)

    # 4. DB organization row — find-or-create on kc_org_alias, on the OWNER
    #    session (bypasses RLS; Organization is exempt from the write stamp by
    #    its own PK). Re-running with the same alias adopts the existing row.
    org = session.execute(
        select(Organization).where(Organization.kc_org_alias == org_alias)
    ).scalar_one_or_none()
    if org is None:
        org = Organization(name=org_name, kc_org_alias=org_alias)
        session.add(org)
        session.flush()  # need the autoincrement org_id below
    org_id = org.org_id

    # 5. The first org-wide org_admin grant — org_id set EXPLICITLY (the owner
    #    session's stamp does not fire) and property_id NULL (org-wide, the L4
    #    coherence shape). Find-or-create so a re-run does not duplicate — also
    #    DB-guarded by the NULLS-NOT-DISTINCT unique (l4a0orggrant).
    existing_grant = session.execute(
        select(RoleAssignment).where(
            RoleAssignment.org_id == org_id,
            RoleAssignment.keycloak_subject == admin_subject,
            RoleAssignment.role == ORG_ADMIN,
            RoleAssignment.property_id.is_(None),
        )
    ).scalar_one_or_none()
    if existing_grant is None:
        session.add(
            RoleAssignment(
                org_id=org_id,
                keycloak_subject=admin_subject,
                role=ORG_ADMIN,
                property_id=None,
                department_id=None,
            )
        )
    session.flush()

    return ProvisionResult(
        org_id=org_id, kc_org_id=kc_org_id, admin_subject=admin_subject
    )
