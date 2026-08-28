"""Public, UNGATED signup surface (Track B/B1). Mounted like kiosk_router — no
operator_gates: these endpoints are reached by an unauthenticated owner holding
an invite token. Every refusal is fail-closed and names nothing (no existence
oracle). The one elevated credential — the usali_provisioner session — is opened
ONLY by /complete, which runs exactly provision_tenant. Invite validate/consume
stay on the APP-role session so the provisioner holds NO grant on `invite`."""

import re
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from usali import invites, pms_interest
from usali.detect import supported_pms_sources
from usali.mapping.property_registry import create_first_property
from usali.models import Invite as _Invite
from usali.otp import OtpService
from usali.provisioning import provision_tenant
from usali.tenancy import bind_org_context

router = APIRouter(prefix="/api/signup")

# The OTP is delivered to the INVITED EMAIL, never to the cell the caller typed.
# Two independent reasons: there is no SMS vendor (SmtpNotifier.send_sms raises),
# and the cell is caller-supplied -- keying delivery on it would let anyone
# holding a leaked invite link redirect the code to a number they control. The
# invited address is the one channel already proven to belong to the invitee:
# they got here by clicking a link sent to it. The cell is still collected, as
# workspace contact data; it is simply not a verification channel.
_OTP_PURPOSE = "signup_email"
# Derived from the detection registry, never hand-listed: signup offers exactly
# what this repo can detect and parse. Everything else routes to pms_interest.
# `test_signup_literal_tracks_the_detection_registry` pins the Literal above to
# this set, so registering an adapter without offering it (or the reverse) fails
# a test rather than shipping silently.
_SUPPORTED_PMS = supported_pms_sources()
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class InviteRequest(BaseModel):
    # A shape check, not an RFC parser: refuse the obvious typo BEFORE an invite
    # row is minted, so a malformed address never leaves an unreachable
    # credential behind. Whether the address is REAL is settled by the send
    # itself, which is the only test that means anything; pulling in
    # pydantic[email] to be stricter here would buy nothing that matters.
    # 254 is the RFC 5321 address ceiling.
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


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
    # SkyTouch was deliberately absent while its Hotel Statistics adapter was
    # un-registered: advertising a source whose night-audit pack would quarantine
    # on ingest is worse than not offering it. Both SkyTouch reports parse now,
    # so it is offered. Keep this set and `_SUPPORTED_PMS` in step -- a member
    # that is not supported silently takes the pms_interest branch instead.
    pms_source: Literal["opera", "autoclerk", "skytouch", "other"]
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


@router.post("/request", status_code=202)
def request_invite(payload: InviteRequest, request: Request) -> dict[str, str]:
    """Self-serve: mint an invite for `email` and send the signup link.

    This is what makes the /try front door honest. Before it, invites existed
    only where an operator ran the CLI, so a stranger who asked to save their
    preview had nowhere to go.

    NO EXISTENCE ORACLE: an address that already has an invite, or already owns
    a workspace, gets the identical 202. Distinguishing them would turn this
    into a "who has signed up?" lookup for anyone on the internet. Abuse is
    bounded by the rate limiter, not by a distinguishable refusal.

    A second ask mints a SECOND invite rather than resending the first: the raw
    token is stored only as a SHA-256, so the original link is unrecoverable by
    construction. Both remain valid until one is claimed -- signup is one invite,
    one tenant (`invites.claim`), so the extra pending row cannot become a
    second workspace.
    """
    email = payload.email.strip()
    limiter = request.app.state.signup_rate_limiter
    if not limiter.allow(f"request:{email.lower()}"):
        raise HTTPException(status_code=429, detail="too many requests")
    factory = request.app.state.db_session_factory
    notifier = request.app.state.notifier
    base = str(request.app.state.public_base_url).rstrip("/")

    with factory() as session:
        invite, raw_token = invites.create_invite(session, email)
        invite_id = invite.invite_id
        session.commit()
    link = f"{base}/signup?token={quote(raw_token, safe='')}"
    try:
        notifier.send_email(
            to=email,
            subject="Set up your Open Hospitality workspace",
            body=(
                "You asked to save your night-audit preview and automate it.\n\n"
                f"Open this link to finish setting up your workspace:\n{link}\n\n"
                "The link works once and expires in 7 days. If this wasn't you, "
                "ignore this email -- nothing was created."
            ),
        )
    except Exception:
        # The token existed ONLY inside the message that failed to send, so a
        # pending row for it is an unreachable credential. Revoke it and tell the
        # caller the send failed -- a 202 here would strand them waiting for an
        # email that is never coming.
        with factory() as session:
            row = session.get(_Invite, invite_id)
            if row is not None:
                invites.revoke(session, row)
            session.commit()
        raise HTTPException(
            status_code=502, detail="could not send the email; please try again"
        ) from None
    return {"status": "sent"}


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
    # Counted against the INVITE, not the typed cell. Keying the ceiling on
    # caller-supplied input meant rotating one digit bought a fresh budget --
    # unlimited codes for one invite, and unlimited mail to one address.
    limiter = request.app.state.signup_rate_limiter
    if not limiter.allow(f"otp:{payload.token}"):
        raise HTTPException(status_code=429, detail="too many requests")
    factory = request.app.state.db_session_factory
    otp: OtpService = request.app.state.otp_service
    notifier = request.app.state.notifier
    with factory() as session:
        invite = invites.validate(session, payload.token)
        if invite is None:
            raise _refuse()
        target = invite.email
        code = otp.issue(session, purpose=_OTP_PURPOSE, target=target)
        session.commit()
    # Send AFTER commit so a delivered code always has a stored challenge.
    notifier.send_email(
        to=target,
        subject="Your Open Hospitality verification code",
        body=f"Your verification code is {code}\n\nIt expires in 10 minutes.",
    )


@router.post("/complete", status_code=201)
def complete(payload: CompleteRequest, request: Request) -> dict[str, str | bool]:
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
        # Verified against the INVITED EMAIL — the same target send_otp issued
        # for. Verifying against payload.cell would let a caller who never
        # received the code choose the target it is checked against.
        verified = otp.verify(
            session, purpose=_OTP_PURPOSE, target=invite_email, code=payload.otp
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

    # STEP 2b (APP role, bound to the NEW org): create the first property. The
    # provisioner role cannot write `property` (D-B7), so this is a fresh
    # app-role session bound to result.org_id. Supported PMS only; "other" is
    # handled separately (Task 6).
    pms_supported = payload.pms_source in _SUPPORTED_PMS
    if pms_supported:
        with factory() as session:
            bind_org_context(session, result.org_id)
            create_first_property(
                session, result.org_id,
                name=payload.property_name,
                pms_source=payload.pms_source,
                wage_jurisdiction=payload.wage_jurisdiction,
                timezone=payload.timezone,
            )
            session.commit()
    else:
        # Unsupported PMS: no property; capture de-duped demand + route to admin.
        # pms_interest_request is not-OrgScoped (keyed by org_alias), so no org
        # binding is needed.
        with factory() as session:
            _, is_new = pms_interest.record_request(
                session, org_alias=payload.workspace_alias, email=invite_email,
                raw_pms=payload.pms_other_name or "",
            )
            session.commit()
        admin_email = request.app.state.admin_notify_email
        if is_new and admin_email:
            request.app.state.notifier.send_email(
                to=admin_email,
                subject="New PMS request from a signup",
                body=(f"Org {payload.workspace_alias} ({invite_email}) requested "
                      f"PMS: {payload.pms_other_name}"),
            )

    # STEP 3 (APP role): record which tenant the invite became (audit).
    with factory() as session:
        invites.mark_consumed_org(session, payload.token, result.org_id)
        session.commit()
    return {"org_alias": payload.workspace_alias, "pms_supported": pms_supported}
