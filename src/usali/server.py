import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TypeVar

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from usali.adaptors import autoclerk_transaction_summary, opera_trial_balance
from usali.adaptors.pdf import extract_words_from_bytes
from usali.adp_adapter import AdpAdapter
from usali.auth import (
    TokenVerifier,
    request_session_factory,
    require_active_org,
    require_operator,
)
from usali.checklist_api import router as checklist_router
from usali.config import Settings, get_settings
from usali.crm_api import router as crm_router
from usali.crm_feed import CRM_PROVIDERS, CrmFeed
from usali.db import make_engine, make_session_factory
from usali.delphi_adapter import DelphiAdapter
from usali.detect import detect_report_signature
from usali.gusto_adapter import GustoAdapter
from usali.ingestion import ProcessingError, process_file
from usali.keycloak_admin import KeycloakAdmin, KeycloakAdminClient
from usali.face_enrollment import router as face_enrollment_router
from usali.face_match import FaceEmbedder
from usali.kiosk import admin_router as kiosk_admin_router
from usali.kiosk import kiosk_router
from usali.notifications import Notifier, notifier_from_settings
from usali.opener import Opener, SoftwareOpener
from usali.otp import OtpService
from usali.payroll_provider import PayrollProvider
from usali.payroll_run_api import router as payroll_run_router
from usali.photo_store import PhotoStore, photo_store_from_settings
from usali.pii_api import router as pii_router
from usali.preview import PreviewPayload, build_financial_preview
from usali.ratelimit import RateLimiter
from usali.recognition import recognize_vendor
from usali.redaction import redact
from usali.sick_leave_api import router as sick_leave_router
from usali.portal_api import router as portal_router
from usali.property_config_api import router as property_config_router
from usali.qbo_client import QboClient, StaticTokenStore
from usali.schedule_api import router as schedule_router
from usali.signup_api import router as signup_router
from usali.tenancy import FOUNDING_ORG_ID, OrgBoundSessionFactory, SessionFactory
from usali.timecard_api import router as timecard_router
from usali.tripleseat_adapter import TripleseatAdapter
from usali.workforce import router as workforce_router


_DEFAULT_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"

# PMS reports are normally well under 1 MiB. 25 MiB leaves ample room for
# unusually image-heavy exports while keeping one authenticated request from
# consuming an unbounded amount of worker memory.
_MAX_PDF_BYTES = 25 * 1024 * 1024

_T = TypeVar("_T")

_MAX_PREVIEW_BYTES = 10 * 1024 * 1024
_MAX_PREVIEW_PAGES = 80
_PREVIEW_ADAPTERS = {
    ("OPERA", "trial_balance"): (
        opera_trial_balance.parse_trial_balance,
        opera_trial_balance.extract_business_date,
    ),
    ("AUTOCLERK", "transaction_summary"): (
        autoclerk_transaction_summary.parse_transaction_summary,
        autoclerk_transaction_summary.extract_business_date,
    ),
}
_UNREADABLE_HINTS = [
    "Is it a night-audit / trial-balance report (not a single folio or a photo)?",
    "Is it the original PDF your PMS emailed, not a scan?",
]


def _payload_json(p: PreviewPayload) -> dict[str, object]:
    d = asdict(p)
    d["business_date"] = p.business_date.isoformat()
    # net_total is documented as never-a-balance-signal (D8) — it stays on the
    # server-side PreviewPayload dataclass but is deliberately NOT serialized to
    # the client, so nothing downstream can rebuild the dishonest signal from it.
    d.pop("net_total", None)
    for line in d["pnl_lines"]:
        line["amount"] = str(line["amount"])
    for kpi in d["kpis"]:
        kpi["value"] = str(kpi["value"])
    return d


def _parse_preview_sync(data: bytes) -> dict[str, object]:
    """Runs in a worker thread (anyio.to_thread) — all blocking CPU parse work
    lives here so the async event loop is never held during a preview."""
    try:
        words = extract_words_from_bytes(data, max_pages=_MAX_PREVIEW_PAGES)
    except Exception:
        return {"status": "unreadable", "hints": _UNREADABLE_HINTS}
    sig = detect_report_signature(words)
    if sig in _PREVIEW_ADAPTERS:
        parse_fn, date_fn = _PREVIEW_ADAPTERS[sig]
        try:
            business_date = date_fn(words)
            records = parse_fn(words, property_id="PREVIEW", business_date=business_date)
        except Exception:
            return {"status": "unreadable", "hints": _UNREADABLE_HINTS}
        payload = redact(
            build_financial_preview(
                source=sig[0], report_type=sig[1], business_date=business_date, records=records
            )
        )
        return {"status": "ok", "payload": _payload_json(payload)}
    if sig is not None:
        return {"status": "unsupported", "vendor": sig[0].title(), "reason": "no_preview_for_report"}
    vendor = recognize_vendor(words)
    if vendor is not None:
        return {"status": "unsupported", "vendor": vendor, "reason": "vendor_not_supported"}
    return {"status": "unreadable", "hints": _UNREADABLE_HINTS}


def _qbo_client_from_settings() -> QboClient:
    """The default QBO client: settings read lazily, on the first push only."""
    settings = get_settings()
    return QboClient(
        settings.qbo_base_url,
        settings.qbo_client_id,
        settings.qbo_client_secret,
        settings.qbo_realm_id,
        StaticTokenStore(settings.qbo_refresh_token),
    )


def _shared(build: Callable[[], _T]) -> Callable[[], _T]:
    """Memoize a client factory: build lazily ONCE, return that instance forever.

    For QboClient: one client instance = ONE refresh-token rotation lineage.
    QboClient keeps its rotated refresh token in memory only (see its
    docstring), so per-request clients would each start from the
    already-consumed bootstrap token and invalid_grant on the second push.
    Payroll adapters share the same shape: AdpAdapter caches its OAuth bearer,
    and one shared instance = one connection pool + one token lineage.
    Request-level concurrency is NOT this memoizer's concern — clients
    serialize their own requests where needed. The lock here only guards
    construction: two concurrent first-calls from the threadpool must not each
    build a client and fork the lineage.
    """
    lock = threading.Lock()
    holder: list[_T] = []

    def get() -> _T:
        with lock:
            if not holder:
                holder.append(build())
            return holder[0]

    return get


def _shared_by_key(build: Callable[[str], _T]) -> Callable[[str], _T]:
    """Memoize a factory that is a FUNCTION of a key: build each distinct
    key's value lazily ONCE and cache it. The CRM feed uses this — the
    provider is per-org now (L5), so one shared adapter is no longer right;
    but there are only a handful of provider names, and one adapter per
    provider = one client + one pool + (for OAuth adapters) one token
    lineage, the `_shared` posture applied per key rather than globally."""
    lock = threading.Lock()
    cache: dict[str, _T] = {}

    def get(key: str) -> _T:
        with lock:
            if key not in cache:
                cache[key] = build(key)
            return cache[key]

    return get


def _payroll_provider_from_settings() -> PayrollProvider:
    """The default payroll provider (Pillar C2): selected by configuration
    alone — USALI_PAYROLL_PROVIDER=gusto|adp is the ONLY switch."""
    settings = get_settings()
    if settings.payroll_provider == "gusto":
        return GustoAdapter.from_settings(settings)
    if settings.payroll_provider == "adp":
        return AdpAdapter.from_settings(settings)
    raise RuntimeError(
        f"unknown payroll provider {settings.payroll_provider!r} (expected gusto|adp)"
    )


def _crm_feed_for_provider(provider: str) -> CrmFeed | None:
    """The demand feed (Pillar J) for ONE provider name. L5: the provider
    is now per-ORG (read from `org_settings` under the active org), so the
    factory is keyed on the resolved provider rather than reading the
    process-wide env. EMPTY means the feature is OFF for that org: None,
    which the pull surface (J4) refuses loudly — never a silent no-op
    adapter. Base URLs / credentials remain process-wide deployment config
    (`Settings`)."""
    if provider == "":
        return None
    settings = get_settings()
    if provider == "delphi":
        return DelphiAdapter.from_settings(settings)
    if provider == "tripleseat":
        return TripleseatAdapter.from_settings(settings)
    raise RuntimeError(
        f"unknown crm provider {provider!r} "
        f"(expected {'|'.join(CRM_PROVIDERS)}, or empty for off)"
    )


def _face_engine_from_settings() -> FaceEmbedder:
    # Imported lazily: OnnxFaceEngine's dependencies are the optional `face`
    # extra, and an install without it must still serve everything else —
    # construction raises FaceModelsMissing, which the enrollment/kiosk
    # routes surface as 503, only when a face route is actually hit.
    from usali.face_match import OnnxFaceEngine

    return OnnxFaceEngine(get_settings().face_model_dir)


def _opener_from_settings(settings: Settings) -> Opener:
    # Production requires an HSM-backed Opener; C1 does not build one (a deploy-
    # time drop-in against the Protocol, like S3PhotoStore). Refuse to fall back
    # to an in-process private key in prod.
    if settings.is_production:
        raise RuntimeError(
            "production requires an HSM-backed Opener; none is configured "
            "(C1 ships SoftwareOpener for dev/test only)"
        )
    return SoftwareOpener.from_settings(settings)


def _provisioner_session_factory_from_settings(settings: Settings) -> SessionFactory:
    """The least-privilege provisioner session factory (D-B7): an UNBOUND base
    factory connected as usali_provisioner. Unbound on purpose — provision_tenant
    refuses an org-instrumented session; the role's permissive RLS policy lets its
    cross-org writes land without BYPASSRLS."""
    from sqlalchemy.engine import make_url

    prov_url = make_url(settings.db_url).set(
        username=settings.provisioner_db_role,
        password=settings.provisioner_db_password,
    ).render_as_string(hide_password=False)
    return make_session_factory(make_engine(prov_url))


class _SpaStaticFiles(StaticFiles):
    """StaticFiles with an SPA history fallback.

    Client-side routes (`/coverage`, `/upload`, ...) have no file on disk, so a
    browser refresh or bookmark would 404 under plain `StaticFiles(html=True)`.
    A 404 for a non-API path serves `index.html` instead and lets the router in
    the SPA take over. API paths (`api/...`, `ingest`) keep their real 404s —
    matched API routes never reach this mount anyway (it is registered last),
    but *unknown* API paths must not be shadowed by the fallback.

    A path that NAMES A FILE is also excluded, and gets its real 404. Answering
    `/wp-config.php` with a 200 and a page of HTML is not a vulnerability, but
    it is a lie: every automated scanner records the probe as a hit, so a scan
    report reads as a list of successful PHP exploits against a service that
    runs no PHP. Seen live on 2026-08-26, when a scanner got 200s for
    `/wp-content/plugins/hellopress/wp_filemanager.php` and
    `/this_is_a_new_hello_world.php`.
    """

    @staticmethod
    def _is_api_path(path: str) -> bool:
        # `path` is relative to the mount root (no leading slash).
        return path in ("api", "ingest") or path.startswith("api/")

    @staticmethod
    def _names_a_file(path: str) -> bool:
        """True when the path looks like it is asking for a file on disk.

        Two shapes, because scanners use both:

        * a dot in the LAST segment -- `vpn.php`, `backup.sql`, `.env`;
        * a dot-prefixed SEGMENT anywhere -- `.git/HEAD`, `.ssh/id_rsa`,
          `.aws/credentials`, where the filename itself is perfectly ordinary.
          The first version of this checked only the last segment and let
          `/.git/HEAD` through, which is one of the most probed paths there is.

        CONSTRAINT THIS PLACES ON ROUTING: no client-side route may contain a
        dot in its final segment or a dot-prefixed segment, or a refresh of it
        will 404 instead of reaching the router. Every route today is dot-free
        (`/coverage`, `/night-audit`, `/kiosk-devices`), and search params live
        in the query string, which never reaches here. Real assets
        (`/assets/index-abc.js`) DO have a dot, but they exist on disk and are
        served before the fallback is consulted -- only a MISSING file-shaped
        path lands here.
        """
        segments = path.split("/")
        return "." in segments[-1] or any(seg.startswith(".") for seg in segments)

    def _should_fall_back(self, path: str) -> bool:
        return not self._is_api_path(path) and not self._names_a_file(path)

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self._should_fall_back(path):
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404 and self._should_fall_back(path):
            return await super().get_response("index.html", scope)
        return response


def create_app(
    *,
    inbox_dir: Path | None = None,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
    session_factory: SessionFactory | None = None,
    dist_dir: Path | None = None,
    qbo_client_factory: Callable[[], QboClient] | None = None,
    token_verifier: TokenVerifier | None = None,
    keycloak_admin: KeycloakAdmin | None = None,
    photo_store: PhotoStore | None = None,
    opener: Opener | None = None,
    payroll_provider_factory: Callable[[], PayrollProvider] | None = None,
    face_engine_factory: Callable[[], FaceEmbedder] | None = None,
    crm_feed_factory: Callable[[str], CrmFeed | None] | None = None,
    notifier: Notifier | None = None,
    provisioner_session_factory: SessionFactory | None = None,
    admin_notify_email: str | None = None,
    public_base_url: str | None = None,
) -> FastAPI:
    settings = get_settings()
    # Fail fast on a misconfigured provider NAME (cheap string check — the
    # adapter itself stays lazily built via _payroll_provider_from_settings,
    # which re-raises defensively). An injected factory bypasses settings.
    if payroll_provider_factory is None and settings.payroll_provider not in ("gusto", "adp"):
        raise RuntimeError(
            f"unknown payroll provider {settings.payroll_provider!r} (expected gusto|adp)"
        )
    # Same fail-fast for the CRM feed NAME — but empty is legal (feature
    # off; the pull endpoint refuses loudly per request).
    if crm_feed_factory is None and settings.crm_provider not in ("", *CRM_PROVIDERS):
        raise RuntimeError(
            f"unknown crm provider {settings.crm_provider!r} "
            f"(expected {'|'.join(CRM_PROVIDERS)}, or empty for off)"
        )
    inbox = inbox_dir or settings.inbox_dir
    processed = processed_dir or settings.processed_dir
    failed = failed_dir or settings.failed_dir
    app = FastAPI(
        title="Open Hospitality", docs_url=None, redoc_url=None, openapi_url=None
    )
    # Per-request sessions (tests inject a factory bound to their engine;
    # the default reads settings once here — the engine connects lazily,
    # so an unused default costs nothing). L3 (decision 3): the OPERATOR
    # surfaces no longer share one app-level factory — require_active_org
    # resolves the token's validated active org per request and stashes
    # an org-bound factory as `request.state.session_factory`, which is
    # what the routers read. Only two app-level factories remain:
    #  - db_session_factory: the UNBOUND base — require_active_org builds
    #    the per-request binding from it, and resolve_org_id's narrow
    #    alias lookup runs on it. Never handed to a handler directly.
    #  - device_session_factory: the kiosk DEVICE surface (X-Kiosk-Token,
    #    no OIDC claim to resolve an org from) stays founding-org-bound —
    #    kiosk multi-org is deferred alongside provisioning (L6+).
    base_factory: SessionFactory = (
        session_factory or make_session_factory(make_engine(settings.db_url))
    )
    app.state.db_session_factory = base_factory
    app.state.device_session_factory = OrgBoundSessionFactory(
        base_factory, FOUNDING_ORG_ID
    )
    # The QBO client is built lazily on the first push and SHARED for the app's
    # lifetime (refresh-token rotation is in client memory — see _shared). Tests
    # inject a factory wired to the in-process mock; the default reads settings.
    app.state.get_qbo_client = _shared(qbo_client_factory or _qbo_client_from_settings)
    # The OIDC resource-server verifier. Tests inject one wired to a local
    # keypair (tests/authkit); the default fetches the realm JWKS lazily.
    app.state.token_verifier = token_verifier or TokenVerifier.from_settings(settings)
    # The Keycloak admin client for operator provisioning (A2.3). Tests inject
    # InMemoryKeycloakAdmin; the default builds the real REST client lazily.
    app.state.keycloak_admin = keycloak_admin or KeycloakAdminClient.from_settings(settings)
    # Punch-photo store (B1). Tests inject InMemoryPhotoStore; the default is
    # config-selected (K1): a GCS bucket name picks the cloud store (lazy
    # client, app-side ciphertext), else AES-GCM-encrypted local files under
    # settings.photo_store_dir. Selection lives in photo_store_from_settings —
    # the demo seed uses the same function (one predicate, one function).
    app.state.photo_store = photo_store or photo_store_from_settings(settings)
    # Notification seam (B1/D-B6). Tests inject a capturing fake; the default is
    # config-selected (console-only in B1). One instance for the app's lifetime.
    app.state.notifier = notifier or notifier_from_settings(settings)
    # Admin routing address for unsupported-PMS signup demand (B1/Task 6). Tests
    # inject a specific address via _signup_client; the default reads settings.
    app.state.admin_notify_email = (
        admin_notify_email if admin_notify_email is not None
        else settings.admin_notify_email
    )
    # Where the self-serve signup link points (POST /api/signup/request). The
    # request has no trustworthy origin behind a proxy, so the public host is
    # configured, never derived from the request -- an attacker-controlled Host
    # header would otherwise put its own domain in an email we sent.
    app.state.public_base_url = (
        public_base_url if public_base_url is not None else settings.public_base_url
    )
    # Provisioner seam (D-B7): the confined signup-completion path's ONLY
    # elevated credential. Tests inject a factory on the provisioner role; the
    # default builds one from settings. Unbound — provision_tenant refuses an
    # instrumented session.
    app.state.provisioner_session_factory = (
        provisioner_session_factory
        or _provisioner_session_factory_from_settings(settings)
    )
    # OTP + rate-limit singletons for the public signup surface.
    app.state.otp_service = OtpService()
    app.state.signup_rate_limiter = RateLimiter(
        max_events=settings.signup_otp_max_per_window,
        window_seconds=settings.signup_rate_window_seconds,
    )
    # Sealed-PII Opener seam (C1). Tests inject a SoftwareOpener; the default
    # builds one from settings, but env=prod refuses the in-process key entirely
    # (an HSM-backed Opener is a deploy-time drop-in, not shipped in C1).
    app.state.opener = opener or _opener_from_settings(settings)
    # Payroll provider seam (C2). Config-selected (gusto|adp) and SHARED for the
    # app's lifetime (AdpAdapter caches its OAuth bearer; one instance = one
    # token lineage + one pool). Tests inject a factory returning a fake.
    app.state.get_payroll_provider = _shared(
        payroll_provider_factory or _payroll_provider_from_settings
    )
    # Face engine seam (F3). Tests inject a fake; the default loads the ONNX
    # models lazily on the first face route and is SHARED for the app's
    # lifetime (two onnxruntime sessions per process, not per request).
    app.state.get_face_engine = _shared(
        face_engine_factory or _face_engine_from_settings
    )
    # CRM demand feed seam (J4/L5). The provider is PER-ORG now: the crm
    # router reads the active org's `org_settings.crm_provider` and asks
    # this factory for that provider's feed (empty = OFF, None, the pull
    # endpoint 503s naming the switch). Keyed-and-shared: one adapter per
    # provider name (one client + pool), built lazily. Tests inject a
    # factory returning a fake for any non-empty provider.
    app.state.get_crm_feed = _shared_by_key(
        crm_feed_factory or _crm_feed_for_provider
    )
    # Every operator router: authentication+role gate first, then the
    # active-org resolution (L3) — its 400/403 refusals fire before any
    # handler, and it stashes the request's org-bound session factory.
    operator_gates = [Depends(require_operator), Depends(require_active_org)]
    app.include_router(portal_router, dependencies=operator_gates)
    app.include_router(workforce_router, dependencies=operator_gates)
    app.include_router(property_config_router, dependencies=operator_gates)
    app.include_router(checklist_router, dependencies=operator_gates)
    # Face-template enrollment (F3). Route-level require_onboarder narrows to
    # org_admin/property_gm — require_operator is only the outer gate.
    app.include_router(face_enrollment_router, dependencies=operator_gates)
    app.include_router(kiosk_admin_router, dependencies=operator_gates)
    # Timecard review/approval. The router's own require_approver narrows this
    # further to org_admin/property_gm — require_operator is only the outer gate.
    app.include_router(timecard_router, dependencies=operator_gates)
    # Schedule builder (D1). The router's own require_scheduler narrows every
    # route to org_admin/property_gm — require_operator is only the outer gate.
    app.include_router(schedule_router, dependencies=operator_gates)
    # CRM pull (J4). The router's require_crm_scheduler narrows to
    # org_admin/property_gm (demand is a manager surface) — the outer
    # gate is only authentication, like the schedule router.
    app.include_router(crm_router, dependencies=operator_gates)
    # Sealed-PII vault (C1). The public-key route needs only an authenticated
    # operator; the profile write/status routes (Task 6) add require_payroll_admin
    # on top of this outer gate.
    app.include_router(pii_router, dependencies=operator_gates)
    # E4: sick-leave balance/usage/adjustments -- payroll-admin-gated inside
    # the router, operator-authenticated at the door like the vault.
    app.include_router(sick_leave_router, dependencies=operator_gates)
    # Pay-run execution/results (C2). Route-level require_payroll_admin composes
    # on top of this outer operator gate (same pattern as the vault routes).
    app.include_router(payroll_run_router, dependencies=operator_gates)
    # Device-authenticated (X-Kiosk-Token), NOT an operator session — so this
    # router is deliberately included without require_operator.
    app.include_router(kiosk_router)
    # Public, UNGATED signup surface (Track B/B1) — like kiosk_router, mounted
    # without operator_gates. Its own invite + OTP checks are the gate.
    app.include_router(signup_router)

    @app.post("/ingest", dependencies=operator_gates)
    async def ingest(request: Request, file: UploadFile) -> dict[str, object]:
        # The request's org-bound factory (L3): the upload lands inside
        # the caller's validated active org — require_active_org stashed
        # the factory, and both walls confine every row process_file
        # writes. The session opens BEFORE anything touches the inbox:
        # opening it is what fires the deferred alias -> org_id
        # resolution, and a token whose org has no DB row must refuse
        # (403) leaving NOTHING behind — bytes written first would
        # dangle un-filed in the inbox for a later `usali watch` (which
        # drains pre-existing files under the founding org) to ingest
        # into org 1's data.
        factory = request_session_factory(request)
        with factory() as session:
            upload_name = file.filename or "upload.pdf"
            # Multipart filenames are attacker-controlled. Keep them as a
            # display name only: path components (including Windows separators
            # on a Linux server) must never influence where the API writes.
            if (
                upload_name in {".", ".."}
                or "/" in upload_name
                or "\\" in upload_name
                or "\x00" in upload_name
            ):
                raise HTTPException(status_code=422, detail="unsafe upload filename")

            payload = await file.read(_MAX_PDF_BYTES + 1)
            if len(payload) > _MAX_PDF_BYTES:
                raise HTTPException(status_code=413, detail="PDF too large")
            if not payload.startswith(b"%PDF-"):
                raise HTTPException(status_code=422, detail="upload must be a PDF")

            inbox.mkdir(parents=True, exist_ok=True)
            dest = inbox / upload_name
            try:
                # Exclusive creation prevents a concurrent or repeated upload
                # from overwriting a report already waiting in the inbox.
                with dest.open("xb") as staged:
                    staged.write(payload)
            except FileExistsError as exc:
                raise HTTPException(
                    status_code=409, detail="an upload with that filename is pending"
                ) from exc
            try:
                r = process_file(
                    session, dest, processed_dir=processed, failed_dir=failed
                )
            except ProcessingError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "pms_source": r.pms_source,
            "report_type": r.report_type,
            "property_id": r.property_id,
            "business_date": r.business_date.isoformat(),
            "staged": r.staged,
            "mapped": r.mapped,
            "unmapped": r.unmapped,
            "skipped": r.skipped,
        }

    app.state.preview_rate_limiter = RateLimiter(max_events=20, window_seconds=60.0)
    # Hard ceiling across ALL callers, independent of the per-IP key: the global
    # limiter is the cap that holds even if the per-IP host is spoofable.
    app.state.preview_global_limiter = RateLimiter(max_events=300, window_seconds=60.0)

    @app.post("/api/preview")  # PUBLIC: no operator_gates, no session, persists nothing
    async def preview(request: Request) -> dict[str, object]:
        # Per-IP limiter FIRST, then the global ceiling — so a single abusive IP
        # is rejected on its own budget BEFORE it can consume a token from the
        # shared 300/min global budget and 429 every other caller. Per-IP trusts
        # request.client.host, which behind a proxy is only trustworthy if
        # uvicorn's forwarded_allow_ips is restricted to the proxy CIDR (deploy-
        # side follow-up, tracked); the global ceiling below is the hard cap that
        # holds regardless of any per-IP spoofing.
        limiter: RateLimiter = request.app.state.preview_rate_limiter
        client_ip = request.client.host if request.client else "unknown"
        if not limiter.allow(client_ip):
            raise HTTPException(
                status_code=429,
                detail="too many previews; try again shortly",
                headers={"Retry-After": "60"},
            )
        # Hard ceiling across ALL callers — a single constant key caps total
        # preview volume regardless of how many distinct (possibly spoofed)
        # client IPs appear.
        global_limiter: RateLimiter = request.app.state.preview_global_limiter
        if not global_limiter.allow("global"):
            raise HTTPException(
                status_code=429,
                detail="the preview is busy; try again shortly",
                headers={"Retry-After": "60"},
            )
        if "application/pdf" not in request.headers.get("content-type", ""):
            raise HTTPException(status_code=415, detail="please upload a PDF")
        # Early Content-Length reject: refuse an over-large upload before we
        # stream a single byte (the streamed cap below still enforces the true
        # size, since Content-Length can lie or be absent under chunked).
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _MAX_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail="file too large")
        # Stream the raw PDF body with a hard cap — never spool guest PII / card
        # PANs to an on-disk SpooledTemporaryFile (which multipart would do).
        # NOTE: this read is bounded in SIZE (10 MB) but not in TIME — a slow-
        # trickle upload can hold the stream open. The pilot relies on the Cloud
        # Run request timeout as the wall-clock bound; if we outgrow that, the
        # alternative is an in-process concurrency semaphore around the
        # stream+parse (deploy-side, no behavior change here).
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > _MAX_PREVIEW_BYTES:
                raise HTTPException(status_code=413, detail="file too large")
        data = bytes(buf)
        if data[:5] != b"%PDF-":
            raise HTTPException(status_code=415, detail="please upload a PDF")
        # The whole detect -> parse -> build -> redact phase is synchronous CPU
        # work that would otherwise block the entire async event loop (stalling
        # ALL routes) for the parse duration, anonymously triggerable. Run it in
        # a worker thread so the loop stays free. Fail-closed to "unreadable" on
        # an over-long PDF (page ceiling) lives inside the helper; a wall-clock/
        # CPU timeout for a small-but-pathological (decompression-bomb) PDF is a
        # documented residual for the pilot — bounded for now by the 10MB size
        # cap + the page ceiling; tracked as a follow-up.
        return await anyio.to_thread.run_sync(_parse_preview_sync, data)

    # Serve the built portal SPA when a frontend build exists. Mounted LAST so
    # the /api/* and /ingest routes above always win; without a build the app
    # is API-only (backend tests and CLI users never need node).
    if (dist := dist_dir or _DEFAULT_DIST).is_dir():
        app.mount("/", _SpaStaticFiles(directory=dist, html=True), name="spa")

    return app
