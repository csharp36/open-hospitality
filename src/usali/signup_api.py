"""Public, UNGATED signup surface (Track B/B1). Mounted like kiosk_router — no
operator_gates: these endpoints are reached by an unauthenticated owner holding
an invite token. Every refusal is fail-closed and names nothing (no existence
oracle). The one elevated credential — the usali_provisioner session — is opened
ONLY by /complete, which runs exactly provision_tenant. Invite validate/consume
stay on the APP-role session so the provisioner holds NO grant on `invite`."""

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from usali import invites
from usali.otp import OtpService
from usali.provisioning import provision_tenant

router = APIRouter(prefix="/api/signup")

_OTP_PURPOSE = "signup_cell"
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class OtpRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    cell: str = Field(min_length=3, max_length=32)


class CompleteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    otp: str = Field(min_length=1, max_length=12)
    workspace_name: str = Field(min_length=1, max_length=200)
    # Alias FORMAT is enforced by _ALIAS_RE in the handler AFTER the invite +
    # OTP checks so a bad alias never preempts their 404/403 (the tests pin
    # that precedence). The Field bound is only a coarse length ceiling.
    workspace_alias: str = Field(min_length=1, max_length=63)
    property_name: str = Field(min_length=1, max_length=200)
    pms_source: str = Field(min_length=1, max_length=20)
    wage_jurisdiction: str = Field(min_length=1, max_length=10)
    cell: str = Field(min_length=3, max_length=32)
    # An 8-char floor is the D-B5 baseline for the self-service credential. The
    # alias-format 422 is deferred to the handler (after the invite/OTP refusals)
    # so a bad alias can't preempt them, but a too-short password is a pure
    # field-shape refusal and stays here.
    password: str = Field(min_length=8, max_length=200)


def _refuse() -> HTTPException:
    return HTTPException(status_code=404, detail="not found")


@router.get("/invite/{token}")
def get_invite(token: str, request: Request) -> dict[str, str]:
    factory = request.app.state.db_session_factory
    with factory() as session:
        invite = invites.validate(session, token)
        if invite is None:
            raise _refuse()
        return {"email": invite.email}


@router.post("/otp", status_code=204)
def send_otp(payload: OtpRequest, request: Request) -> None:
    limiter = request.app.state.signup_rate_limiter
    if not limiter.allow(f"otp:{payload.cell}"):
        raise HTTPException(status_code=429, detail="too many requests")
    factory = request.app.state.db_session_factory
    otp: OtpService = request.app.state.otp_service
    notifier = request.app.state.notifier
    with factory() as session:
        invite = invites.validate(session, payload.token)
        if invite is None:
            raise _refuse()
        code = otp.issue(session, purpose=_OTP_PURPOSE, target=payload.cell)
        session.commit()
    # Send AFTER commit so a delivered code always has a stored challenge.
    notifier.send_sms(to=payload.cell, body=code)


@router.post("/complete", status_code=201)
def complete(payload: CompleteRequest, request: Request) -> dict[str, str]:
    limiter = request.app.state.signup_rate_limiter
    if not limiter.allow(f"complete:{payload.token}"):
        raise HTTPException(status_code=429, detail="too many requests")

    factory = request.app.state.db_session_factory
    otp: OtpService = request.app.state.otp_service
    kc = request.app.state.keycloak_admin
    prov_factory = request.app.state.provisioner_session_factory

    # STEP 1 (APP role): validate invite + verify OTP. Capture invite.email
    # before leaving the session. The OTP attempt increment/consume commits
    # regardless of verify outcome. A wrong/expired OTP must NOT consume the
    # invite (pinned by test_complete_fails_closed_on_wrong_otp).
    with factory() as session:
        invite = invites.validate(session, payload.token)
        if invite is None:
            raise _refuse()
        invite_email = invite.email
        verified = otp.verify(
            session, purpose=_OTP_PURPOSE, target=payload.cell, code=payload.otp
        )
        session.commit()
    if not verified:
        raise HTTPException(status_code=403, detail="verification failed")

    # Alias FORMAT check runs only AFTER a valid invite + verified OTP, so a
    # malformed alias never preempts the 404/403 those checks owe the caller.
    if not _ALIAS_RE.match(payload.workspace_alias):
        raise HTTPException(status_code=422, detail="invalid workspace alias")

    # STEP 2 (PROVISIONER role — the ONLY place this session is opened): run
    # exactly provision_tenant. The provisioner holds NO grant on `invite`, so
    # it never touches it. provision_tenant is find-or-create idempotent.
    with prov_factory() as session:
        result = provision_tenant(
            session, kc,
            org_name=payload.workspace_name,
            org_alias=payload.workspace_alias,
            admin_username=payload.workspace_alias,
            admin_email=invite_email,
            admin_full_name=invite_email,
            password=payload.password,
        )
        session.commit()

    # STEP 3 (APP role): consume the invite, recording the tenant it became.
    # Separate transaction from provision (acceptable: provision_tenant is
    # idempotent, so a re-tried completion after a step-3 failure adopts the
    # existing org and re-consumes).
    with factory() as session:
        fresh = invites.validate(session, payload.token)
        if fresh is not None:
            invites.consume(session, fresh, org_id=result.org_id)
            session.commit()
    return {"org_alias": payload.workspace_alias}
