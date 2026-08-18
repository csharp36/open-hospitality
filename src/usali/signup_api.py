"""Public, UNGATED signup surface (Track B/B1). Mounted like kiosk_router — no
operator_gates: these endpoints are reached by an unauthenticated owner holding
an invite token. Every refusal is fail-closed and names nothing (no existence
oracle). The one elevated credential — the usali_provisioner session — is opened
ONLY by /complete, which runs exactly provision_tenant. Invite validate/consume
stay on the APP-role session so the provisioner holds NO grant on `invite`."""

import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

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
    pms_source: Literal["opera", "autoclerk", "other"]
    pms_other_name: str | None = Field(default=None, min_length=1, max_length=60)
    wage_jurisdiction: str = Field(min_length=1, max_length=10)
    timezone: str | None = Field(default=None, min_length=1, max_length=50)
    cell: str = Field(min_length=3, max_length=32)
    # An 8-char floor is the D-B5 baseline for the self-service credential. The
    # alias-format 422 is deferred to the handler (after the invite/OTP refusals)
    # so a bad alias can't preempt them, but a too-short password is a pure
    # field-shape refusal and stays here.
    password: str = Field(min_length=8, max_length=200)

    @model_validator(mode="after")
    def _other_requires_name(self) -> "CompleteRequest":
        if self.pms_source == "other" and not self.pms_other_name:
            raise ValueError("pms_other_name is required when pms_source is 'other'")
        return self


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

    # STEP 1 (APP role): validate invite + verify OTP, then ATOMICALLY CLAIM the
    # invite (pending->consumed) so concurrent /complete calls for the same
    # invite serialize and only ONE wins. Capture invite.email before the
    # session closes. A wrong/expired OTP returns 403 WITHOUT claiming (invite
    # stays pending).
    with factory() as session:
        invite = invites.validate(session, payload.token)
        if invite is None:
            raise _refuse()
        invite_email = invite.email
        verified = otp.verify(
            session, purpose=_OTP_PURPOSE, target=payload.cell, code=payload.otp
        )
        if not verified:
            session.commit()  # persist the OTP attempt increment
            raise HTTPException(status_code=403, detail="verification failed")
        # Alias FORMAT check runs AFTER invite+OTP (so it never preempts their
        # 404/403) but BEFORE the claim, so a malformed alias (a client typo)
        # returns 422 without burning the one-time invite. Not yet committed, so
        # the raise rolls the transaction back and leaves the invite pending.
        if not _ALIAS_RE.match(payload.workspace_alias):
            raise HTTPException(status_code=422, detail="invalid workspace alias")
        won = invites.claim(session, payload.token)
        session.commit()
    if not won:
        # A concurrent caller already claimed this invite — one invite, one
        # tenant. No oracle: same 404 as any other invite miss.
        raise _refuse()

    # STEP 2 (PROVISIONER role — the ONLY place this session is opened): run
    # exactly provision_tenant. On failure, revert the claim so the invite is
    # retryable, then re-raise. The provisioner holds NO grant on `invite`, so
    # it never touches it; the revert uses the APP factory.
    try:
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
    except Exception:
        with factory() as session:
            invites.revert_claim(session, payload.token)
            session.commit()
        raise

    # STEP 3 (APP role): record which tenant the invite became (audit).
    with factory() as session:
        invites.mark_consumed_org(session, payload.token, result.org_id)
        session.commit()
    return {"org_alias": payload.workspace_alias}
