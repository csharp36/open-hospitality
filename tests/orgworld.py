"""The shared two-org world — one clean tenant pair the tenancy tests build on.

L7's walk (`test_l7_two_org_walk.py`) proved this world isolated end to end;
L8's three adversarial lenses attach their cross-org probes (ORM
update/delete, raw `text()`, bulk inserts, the money/settlement surfaces,
`audit_event` disclosure, kiosk-device ROW isolation) to the SAME world and
the SAME RLS-bound app-role client rather than re-deriving either. Keeping the
build in one place means a lens cannot accidentally probe a DIFFERENTLY-shaped
world and mistake a fixture divergence for a leak.

The two fixtures (`app_role_engine`, `two_tenant_world`) live in
`tests/conftest.py` so any test file gets them without an import; the
constants and the `rls_client` helper are imported from here.
"""

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session

from tests.employees import make_employee
from tests.grants import grant_role
from tests.orgwall import app_role_url
from usali.auth import TokenVerifier
from usali.db import make_engine, make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import OrgIntegrationCredential, Property
from usali.photo_store import InMemoryPhotoStore
from usali.provisioning import provision_tenant
from usali.tenancy import bind_org_context

# The founding org (org 1) answers to tests.authkit.DEFAULT_ORG_ALIAS
# ("pilot-hotel-group"); org 2 is the second, provisioned tenant.
ORG2_ALIAS = "second-hotel-group"
ORG1_ADMIN = "org1-admin"
ORG2_ADMIN_USERNAME = "org2-admin"


def set_demand_feed(session: Session, provider: str, *, org_id: int = 1) -> None:
    """Point one org's demand feed at `provider`, or disconnect it when
    `provider` is ''. The OH-17 replacement for `update(OrgSettings)
    .values(crm_provider=...)`: the credential row IS the connection, so
    'off' is the ABSENCE of a row rather than an empty string."""
    session.execute(
        delete(OrgIntegrationCredential).where(
            OrgIntegrationCredential.org_id == org_id,
            OrgIntegrationCredential.integration == "demand_feed",
        )
    )
    if provider:
        secret = ("subscription_key" if provider == "delphi" else "api_key")
        session.add(OrgIntegrationCredential(
            org_id=org_id, integration="demand_feed", provider=provider,
            connected_by="test", **{secret: "mock"},
        ))
    session.flush()


def build_two_tenant_world(
    db_session: Session, app_role_engine: Engine
) -> SimpleNamespace:
    """Stand up the full two-org world on the OWNER session (`db_session`) with
    org 2 seeded through an org-2-bound APP-ROLE session; return the
    ids/subjects the tests assert against. The caller supplies a `founding_org`
    (org 1 already exists with the pilot alias and the integration credential
    rows `ensure_default_org` seeds from env)."""
    # ---- ORG 1 (the founding demo world) ----------------------------------
    # Its demand feed is delphi (the OH-17 per-org credential row); one
    # property, one employee, and an org-wide org_admin grant — the shape a
    # real org 1 holds.
    set_demand_feed(db_session, "delphi")
    db_session.add(Property(
        property_id="ONE1", org_id=1, name="Org One Grand", pms_source="opera"
    ))
    db_session.flush()
    make_employee(db_session, property_id="ONE1", full_name="Ada One",
                  pay_type="hourly")
    db_session.commit()
    grant_role(db_session, "org_admin", sub=ORG1_ADMIN, org_id=1)

    # ---- ORG 2 (provisioned beside org 1) ---------------------------------
    # provision_tenant runs on the OWNER (un-instrumented, RLS-bypassing)
    # session — its cross-org organization + grant writes would trip the walls
    # on any org-bound session (the L6b owner-session contract).
    kc = InMemoryKeycloakAdmin()
    result = provision_tenant(
        db_session, kc,
        org_name="Second Hotel Group", org_alias=ORG2_ALIAS,
        admin_username=ORG2_ADMIN_USERNAME, admin_email="admin@second.example",
        admin_full_name="Bo Two",
    )
    db_session.commit()

    # Seed org 2 a MINIMAL world through an ORG-2-BOUND APP-ROLE session: the
    # write wall stamps org_id=2 from the bound context and the DB wall's
    # WITH CHECK (on the non-owner role) accepts it — the L6a prerequisite,
    # proven live. A pre-L6a session would fail here (server default '1' vs an
    # org-2 WITH CHECK), so this leg is itself an assertion.
    app_factory = make_session_factory(app_role_engine)
    with app_factory() as s:
        bind_org_context(s, result.org_id)
        s.add(Property(property_id="TWO1", name="Org Two Plaza",
                       pms_source="opera"))  # org_id stamped -> 2
        s.flush()
        org2_emp = make_employee(s, property_id="TWO1", full_name="Cy Two",
                                 pay_type="hourly")
        s.commit()
        org2_emp_id = org2_emp.employee_id

    return SimpleNamespace(
        org2_id=result.org_id,
        org2_admin=result.admin_subject,
        org2_emp_id=org2_emp_id,
    )


def rls_client(db_url: str, tmp_path: Path, verifier: TokenVerifier) -> TestClient:
    """A serving app connected as the RLS-bound app role — the full stack
    (org resolution -> grant authority -> both walls), not the superuser
    bypass. The client L8's HTTP-level cross-org probes hang off."""
    from usali.server import create_app

    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(make_engine(app_role_url(db_url))),
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    return TestClient(app)
