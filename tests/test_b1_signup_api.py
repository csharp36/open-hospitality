"""The ungated public signup router: two-org happy path through the real app
role + provisioner role, fail-closed refusals, and the confinement pin — the
only elevated session the endpoint opens is the provisioner one, only in
completion."""

import re

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


def _signup_client(db_url, tmp_path, *, notifier, kc, spy=None, admin_email: str = "",
                   public_base_url: str = "http://testserver"):
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
        public_base_url=public_base_url,
    )
    return TestClient(app)


@pytest.fixture
def _founding_committed(db_session):
    from usali.mapping.property_registry import ensure_default_org
    ensure_default_org(db_session)
    db_session.commit()


def _last_code(notifier) -> str:
    """The 6-digit code from the most recent OTP email. The body is prose now
    (an owner reads it), so the tests extract rather than assume it IS the code."""
    body = notifier.emails[-1]["body"]
    match = re.search(r"\b(\d{6})\b", body)
    assert match is not None, f"no 6-digit code in OTP email: {body!r}"
    return match.group(1)


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


def test_otp_goes_to_the_invited_email_not_the_typed_cell(
    db_url, tmp_path, _founding_committed
):
    """The code is delivered to the address the invite was issued to, and NEVER
    to the cell the caller typed. Two reasons, and both matter:

    there is no SMS vendor (SmtpNotifier.send_sms raises), so an SMS code would
    reach nobody; and the cell is caller-supplied, so keying delivery on it
    would let anyone holding a leaked invite link redirect the code to a number
    they control. The invited email is the one channel already proven to belong
    to the invitee — they clicked a link sent to it."""
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    ok = client.post("/api/signup/otp", json={"token": raw, "cell": "+15550000000"})
    assert ok.status_code == 204
    assert notifier.smses == []
    assert len(notifier.emails) == 1 and notifier.emails[0]["to"] == "owner@example.test"
    assert _last_code(notifier)
    bad = client.post("/api/signup/otp", json={"token": "nope", "cell": "+15550000000"})
    assert bad.status_code == 404


def test_otp_ceiling_is_keyed_on_the_invite_not_the_caller_typed_cell(
    db_url, tmp_path, _founding_committed
):
    """Rotating the cell must not buy a fresh budget. The old key was the typed
    cell, so a caller could mint unlimited codes for one invite (and one
    mailbox) just by changing a digit. The invite token is the thing being
    spent, so it is the thing that is counted."""
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    client = _signup_client(db_url, tmp_path, notifier=notifier,
                            kc=InMemoryKeycloakAdmin())
    # settings.signup_otp_max_per_window defaults to 5.
    for i in range(5):
        assert client.post("/api/signup/otp",
                           json={"token": raw, "cell": f"+1555000000{i}"}).status_code == 204
    blocked = client.post("/api/signup/otp", json={"token": raw, "cell": "+15559999999"})
    assert blocked.status_code == 429
    assert len(notifier.emails) == 5


def test_happy_path_provisions_a_second_tenant_and_consumes_the_invite(
    db_url, tmp_path, _founding_committed
):
    raw = _make_invite(db_url, "owner@example.test")
    notifier = CapturingNotifier()
    kc = InMemoryKeycloakAdmin()
    client = _signup_client(db_url, tmp_path, notifier=notifier, kc=kc)

    assert client.post("/api/signup/otp",
                       json={"token": raw, "cell": "+15550000000"}).status_code == 204
    code = _last_code(notifier)

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
    code = _last_code(notifier)

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

    code = _last_code(notifier)
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
    code = _last_code(notifier)
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
    code = _last_code(notifier)
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
    code = _last_code(notifier)
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
    code = _last_code(notifier)

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
    code = _last_code(notifier)

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
    code = _last_code(notifier)

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
    # Nothing was routed to the admin. (The OTP email to the OWNER is expected —
    # that is the verification channel now, so the assertion names the admin
    # address rather than "no email at all".)
    assert [e for e in notifier.emails if e["to"] == "ops@example.test"] == []


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


# --- POST /api/signup/request: the self-serve front door --------------------
# Until now an invite could only be minted by an operator running the CLI. That
# is why "Save & automate" on /try had nowhere to go. This endpoint lets a
# stranger ask for one and be emailed the link, with the same no-oracle posture
# as every other public refusal here.


def _requests_client(db_url, tmp_path, *, notifier, base_url="https://demo.example.test"):
    return _signup_client(
        db_url, tmp_path, notifier=notifier, kc=InMemoryKeycloakAdmin(),
        public_base_url=base_url,
    )


def _link_from(notifier) -> str:
    body = notifier.emails[-1]["body"]
    match = re.search(r"https?://\S+", body)
    assert match is not None, f"no signup link in invite email: {body!r}"
    return match.group(0)


def test_request_emails_a_working_signup_link(db_url, tmp_path, _founding_committed):
    notifier = CapturingNotifier()
    client = _requests_client(db_url, tmp_path, notifier=notifier)

    r = client.post("/api/signup/request", json={"email": "owner@example.test"})
    assert r.status_code == 202

    assert len(notifier.emails) == 1 and notifier.emails[0]["to"] == "owner@example.test"
    link = _link_from(notifier)
    assert link.startswith("https://demo.example.test/signup?token=")
    # The end-to-end pin: the emailed token is one the signup surface accepts.
    token = link.split("token=", 1)[1]
    invite = client.get(f"/api/signup/invite/{token}")
    assert invite.status_code == 200 and invite.json()["email"] == "owner@example.test"


def test_request_is_accepted_identically_for_an_address_that_already_has_an_invite(
    db_url, tmp_path, _founding_committed
):
    """No existence oracle. A second ask for the same address answers exactly as
    the first did, so the endpoint cannot be used to enumerate who has already
    signed up. The rate limiter, not a distinguishable refusal, is what stops
    abuse."""
    notifier = CapturingNotifier()
    client = _requests_client(db_url, tmp_path, notifier=notifier)
    first = client.post("/api/signup/request", json={"email": "owner@example.test"})
    second = client.post("/api/signup/request", json={"email": "owner@example.test"})
    assert (first.status_code, first.json()) == (second.status_code, second.json())


def test_request_rate_limits_per_address(db_url, tmp_path, _founding_committed):
    notifier = CapturingNotifier()
    client = _requests_client(db_url, tmp_path, notifier=notifier)
    for _ in range(5):  # signup_otp_max_per_window default
        assert client.post("/api/signup/request",
                           json={"email": "owner@example.test"}).status_code == 202
    blocked = client.post("/api/signup/request", json={"email": "owner@example.test"})
    assert blocked.status_code == 429
    assert len(notifier.emails) == 5


def test_request_rejects_a_malformed_address_before_minting_anything(
    db_url, tmp_path, _founding_committed
):
    notifier = CapturingNotifier()
    client = _requests_client(db_url, tmp_path, notifier=notifier)
    r = client.post("/api/signup/request", json={"email": "not-an-address"})
    assert r.status_code == 422
    assert notifier.emails == []


def test_request_does_not_leave_a_usable_invite_when_the_email_cannot_be_sent(
    db_url, tmp_path, _founding_committed
):
    """A relay outage must not mint a live invite nobody can reach. The token
    only ever existed in the undelivered message, so a pending row for it is an
    unreachable credential sitting in the database — revoke it and tell the
    caller it failed, rather than answering 202 and stranding them."""
    class _BrokenNotifier(CapturingNotifier):
        def send_email(self, *, to: str, subject: str, body: str) -> None:
            raise RuntimeError("relay refused")

    notifier = _BrokenNotifier()
    client = _requests_client(db_url, tmp_path, notifier=notifier)
    r = client.post("/api/signup/request", json={"email": "owner@example.test"})
    assert r.status_code == 502

    from usali.models import Invite
    factory = make_session_factory(make_engine(app_role_url(db_url)))
    with factory() as s:
        statuses = list(s.execute(select(Invite.status)).scalars())
    assert "pending" not in statuses
