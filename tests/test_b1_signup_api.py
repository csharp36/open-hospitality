"""The ungated public signup router: two-org happy path through the real app
role + provisioner role, fail-closed refusals, and the confinement pin — the
only elevated session the endpoint opens is the provisioner one, only in
completion."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.authkit import make_authkit
from tests.notifiers import CapturingNotifier
from tests.orgwall import app_role_url, provisioner_role_url
from usali import invites
from usali.db import make_engine, make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import Employee, Organization
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app


def _signup_client(db_url, tmp_path, *, notifier, kc, spy=None, admin_email: str = ""):
    """A serving app whose PUBLIC surfaces run as usali_app and whose
    provisioner seam runs as usali_provisioner. `spy` wraps the provisioner
    factory so a test can assert it is (or is not) opened."""
    verifier, _ = make_authkit()
    base = make_session_factory(make_engine(app_role_url(db_url)))
    prov = make_session_factory(make_engine(provisioner_role_url(db_url)))
    if spy is not None:
        prov = spy(prov)
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=base,
        provisioner_session_factory=prov,
        token_verifier=verifier,
        keycloak_admin=kc,
        photo_store=InMemoryPhotoStore(),
        notifier=notifier,
        admin_notify_email=admin_email,
    )
    return TestClient(app)


@pytest.fixture
def _founding_committed(db_session):
    from usali.mapping.property_registry import ensure_default_org
    ensure_default_org(db_session)
    db_session.commit()


def _make_invite(db_url, email):
    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        _, raw = invites.create_invite(s, email)
        s.commit()
    return raw


def test_get_invite_returns_email_for_a_valid_token(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    client = _signup_client(db_url, tmp_path, notifier=CapturingNotifier(),
                            kc=InMemoryKeycloakAdmin())
    r = client.get(f"/api/signup/invite/{raw}")
    assert r.status_code == 200 and r.json()["email"] == "owner@example.test"


def test_get_invite_refuses_unknown_token_without_oracle(db_url, tmp_path, _founding_committed):
    client = _signup_client(db_url, tmp_path, notifier=CapturingNotifier(),
                            kc=InMemoryKeycloakAdmin())
    r = client.get("/api/signup/invite/not-a-real-token")
    assert r.status_code == 404


def test_otp_requires_a_valid_invite_and_sends_the_sms(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    ok = client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    assert ok.status_code == 204
    assert len(notifier.smses) == 1 and notifier.smses[0]["to"] == "+15550000000"
    bad = client.post("/api/signup/otp", json={"token": "nope", "cell": "+15550000000"})
    assert bad.status_code == 404


def test_happy_path_provisions_a_second_tenant_and_consumes_the_invite(
    db_url, tmp_path, _founding_committed
):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    kc = InMemoryKeycloakAdmin()
    client = _signup_client(db_url, tmp_path, notifier=notifier, kc=kc)

    assert client.post("/api/signup/otp",
                       json={"token": raw, "cell": "+15550000000"}).status_code == 204
    code = notifier.smses[-1]["body"]

    done = client.post("/api/signup/complete", json={
        "token": raw, "otp": code,
        "workspace_name": "New Owner Group", "workspace_alias": "new-owner-group",
        "property_name": "Owner Hotel", "pms_source": "opera",
        "wage_jurisdiction": "US-CA", "cell": "+15550000000",
        "password": "chosen-password",
    })
    assert done.status_code == 201, done.text
    alias = done.json()["org_alias"]
    assert alias == "new-owner-group"

    from usali.db import make_session_factory as msf
    from usali.db import make_engine as me
    su = msf(me(db_url))
    with su() as s:
        org = s.execute(select(Organization).where(
            Organization.kc_org_alias == "new-owner-group")).scalar_one()
        assert org.org_id != 1
        assert s.execute(select(func.count()).select_from(Employee)
                         .where(Employee.org_id == org.org_id)).scalar_one() == 0
    assert any(u["password"] == "chosen-password" and u["email_verified"]
               for u in kc.users.values())

    again = client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "x", "workspace_alias": "y",
        "property_name": "z", "pms_source": "opera", "wage_jurisdiction": "US-CA",
        "cell": "+15550000000", "password": "passw0rd",
    })
    assert again.status_code in (404, 409)


def test_complete_fails_closed_on_wrong_otp(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    client = _signup_client(db_url, tmp_path, notifier=CapturingNotifier(),
                            kc=InMemoryKeycloakAdmin())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    r = client.post("/api/signup/complete", json={
        "token": raw, "otp": "000000", "workspace_name": "x",
        "workspace_alias": "y", "property_name": "z", "pms_source": "opera",
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "passw0rd",
    })
    assert r.status_code == 403
    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        assert invites.validate(s, raw) is not None


def test_complete_reverts_claim_when_provisioning_fails(
    db_url, tmp_path, _founding_committed
):
    """Provisioning boom AFTER the atomic claim must revert the claim so the
    invite returns to pending and stays retryable — the documented retry
    posture. TestClient re-raises the server exception, so it surfaces as a
    raised KeycloakAdminError rather than a 500; the load-bearing assertion is
    that the invite is pending afterward."""
    from usali.keycloak_admin import InMemoryKeycloakAdmin, KeycloakAdminError

    class _BoomKc(InMemoryKeycloakAdmin):
        def create_user(self, *a, **k):
            raise KeycloakAdminError("boom")

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier, kc=_BoomKc())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]

    with pytest.raises(KeycloakAdminError):
        client.post("/api/signup/complete", json={
            "token": raw, "otp": code,
            "workspace_name": "Boom Group", "workspace_alias": "boom-group",
            "property_name": "H", "pms_source": "opera",
            "wage_jurisdiction": "US-CA", "cell": "+15550000000",
            "password": "passw0rd",
        })

    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        assert invites.validate(s, raw) is not None  # reverted to pending, retryable


def test_the_endpoint_opens_the_provisioner_session_only_in_completion(
    db_url, tmp_path, _founding_committed
):
    """The confinement pin: GET invite and POST otp NEVER open the provisioner
    session; complete opens it exactly once."""
    opened: list[str] = []

    def spy(factory):
        def wrapped():
            opened.append("prov")
            return factory()
        return wrapped

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin(), spy=spy)

    client.get(f"/api/signup/invite/{raw}")
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    assert opened == []  # provisioner untouched by the read + otp paths

    code = notifier.smses[-1]["body"]
    client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "Only Once Group",
        "workspace_alias": "only-once-group", "property_name": "H",
        "pms_source": "opera", "wage_jurisdiction": "US-CA",
        "cell": "+15550000000", "password": "passw0rd",
    })
    assert opened == ["prov"]  # exactly one confined provisioning session


def test_malformed_alias_is_422_and_does_not_burn_the_invite(
    db_url, tmp_path, _founding_committed
):
    """A valid invite + verified OTP but a malformed workspace alias returns 422
    WITHOUT claiming/consuming the one-time invite — the alias check runs before
    the claim, so a client typo leaves the invite pending and retryable."""
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]
    r = client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "Typo Group",
        "workspace_alias": "Bad Alias!!",  # spaces + caps + punctuation
        "property_name": "H", "pms_source": "opera",
        "wage_jurisdiction": "US-CA", "cell": "+15550000000",
        "password": "passw0rd",
    })
    assert r.status_code == 422
    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        assert invites.validate(s, raw) is not None  # still pending, not burned


def test_complete_rejects_other_pms_without_a_name(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]
    r = client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "W", "workspace_alias": "w-x",
        "property_name": "P", "pms_source": "other",  # no pms_other_name
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "passw0rd",
    })
    assert r.status_code == 422


def test_complete_rejects_unknown_pms_source(db_url, tmp_path, _founding_committed):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]
    r = client.post("/api/signup/complete", json={
        "token": raw, "otp": code, "workspace_name": "W", "workspace_alias": "w-y",
        "property_name": "P", "pms_source": "sabre-x",  # not a member of the set
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "passw0rd",
    })
    assert r.status_code == 422


def test_complete_creates_the_first_property(db_url, tmp_path, _founding_committed):
    from usali.db import make_engine as me
    from usali.db import make_session_factory as msf
    from usali.models import Property, Organization
    from sqlalchemy import select

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    kc = InMemoryKeycloakAdmin()
    client = _signup_client(db_url, tmp_path, notifier=notifier, kc=kc)
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]

    done = client.post("/api/signup/complete", json={
        "token": raw, "otp": code,
        "workspace_name": "New Owner Group", "workspace_alias": "new-owner-group",
        "property_name": "Owner Hotel", "pms_source": "opera",
        "wage_jurisdiction": "US-CA", "timezone": "America/New_York",
        "cell": "+15550000000", "password": "chosen-password",
    })
    assert done.status_code == 201, done.text
    assert done.json()["pms_supported"] is True

    su = msf(me(db_url))  # superuser session sees across orgs
    with su() as s:
        org = s.execute(select(Organization).where(
            Organization.kc_org_alias == "new-owner-group")).scalar_one()
        prop = s.execute(select(Property).where(
            Property.org_id == org.org_id)).scalar_one()
        assert prop.name == "Owner Hotel" and prop.pms_source == "opera"
        assert prop.wage_jurisdiction == "US-CA"
        assert prop.timezone == "America/New_York"


def test_complete_other_pms_records_interest_and_notifies_admin(
    db_url, tmp_path, _founding_committed
):
    from usali.db import make_engine as me
    from usali.db import make_session_factory as msf
    from usali.models import Organization, PmsInterestRequest, Property
    from sqlalchemy import select

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin(), admin_email="ops@example.test")
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]

    # HotelKey, not SkyTouch: SkyTouch is a SUPPORTED source now, so using it as
    # the example of an unsupported one would assert nothing.
    done = client.post("/api/signup/complete", json={
        "token": raw, "otp": code,
        "workspace_name": "Sky Group", "workspace_alias": "sky-group",
        "property_name": "Sky Hotel", "pms_source": "other",
        "pms_other_name": "HotelKey",
        "wage_jurisdiction": "US-CA", "cell": "+15550000000", "password": "passw0rd",
    })
    assert done.status_code == 201, done.text
    assert done.json()["pms_supported"] is False

    su = msf(me(db_url))
    with su() as s:
        org = s.execute(select(Organization).where(
            Organization.kc_org_alias == "sky-group")).scalar_one()
        # No property was created for an unsupported PMS.
        assert s.execute(select(Property).where(
            Property.org_id == org.org_id)).scalar_one_or_none() is None
        # A de-duped interest row was recorded (keyed by org_alias).
        req = s.execute(select(PmsInterestRequest).where(
            PmsInterestRequest.org_alias == "sky-group")).scalar_one()
        assert req.raw_pms == "HotelKey" and req.normalized_pms == "hotelkey"
    # The admin was emailed (new request).
    assert any(e["to"] == "ops@example.test" and "HotelKey" in e["body"]
               for e in notifier.emails)


def test_complete_skytouch_pms_creates_a_property(db_url, tmp_path, _founding_committed):
    """SkyTouch is a supported source: it must take the property-creating branch,
    not the pms_interest one.

    It was held out of the Literal on purpose while the Hotel Statistics adapter
    was un-registered -- advertising a source whose pack would quarantine on
    ingest is worse than not offering it. Both SkyTouch reports parse now
    (mapping/skytouch.yaml maps them, and detection no longer misroutes sections
    of a real pack), so the source is offered.
    """
    from usali.db import make_engine as me
    from usali.db import make_session_factory as msf
    from usali.models import Organization, PmsInterestRequest, Property
    from sqlalchemy import select

    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin(), admin_email="ops@example.test")
    client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    code = notifier.smses[-1]["body"]

    done = client.post("/api/signup/complete", json={
        "token": raw, "otp": code,
        "workspace_name": "Redstone Group", "workspace_alias": "redstone-group",
        "property_name": "Redstone Test Inn", "pms_source": "skytouch",
        "wage_jurisdiction": "US-CA", "timezone": "America/Denver",
        "cell": "+15550000000", "password": "chosen-password",
    })
    assert done.status_code == 201, done.text
    assert done.json()["pms_supported"] is True

    su = msf(me(db_url))
    with su() as s:
        org = s.execute(select(Organization).where(
            Organization.kc_org_alias == "redstone-group")).scalar_one()
        prop = s.execute(select(Property).where(
            Property.org_id == org.org_id)).scalar_one()
        assert prop.name == "Redstone Test Inn" and prop.pms_source == "skytouch"
        assert prop.timezone == "America/Denver"
        # A supported source records NO interest row and emails no admin.
        assert s.execute(select(PmsInterestRequest).where(
            PmsInterestRequest.org_alias == "redstone-group")).scalar_one_or_none() is None
    assert not notifier.emails


def test_signup_literal_tracks_the_detection_registry():
    """The `pms_source` Literal must offer exactly the detectable sources, plus
    'other'.

    This is the guard the SkyTouch gap needed. SkyTouch had a registered adapter
    and a mapping/skytouch.yaml, but signup's hand-maintained Literal never
    learned about it, so the front door quietly offered two sources while the
    pipeline supported three -- and nothing failed. Registering (or
    un-registering) an adapter now breaks this test instead.
    """
    from typing import get_args

    from usali.detect import supported_pms_sources
    from usali.signup_api import CompleteRequest

    offered = set(get_args(CompleteRequest.model_fields["pms_source"].annotation))
    assert offered == supported_pms_sources() | {"other"}
