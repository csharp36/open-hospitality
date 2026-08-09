"""Playwright e2e backend: throwaway Postgres + seeded data + portal API.

Starts a Testcontainers Postgres (same conventions as tests/conftest.py),
migrates the schema, seeds schedules + both mapping YAMLs, processes the six
sample PDFs from docs/reference/samples/, then serves `create_app()` on
127.0.0.1:8100 until killed. A mock QuickBooks Online server (P8) also runs on
127.0.0.1:9200 in a daemon thread of this process, with a refresh token
bootstrapped from it into USALI_QBO_* env before the app starts, so the /qbo
portal flow can push for real. Run with the repo venv:

    .venv/bin/python scripts/e2e_backend.py

frontend/playwright.config.ts launches this as a webServer and waits on
/api/properties, which only responds once seeding is done.
"""

import os
import sys

# Playwright launches this script standalone (from frontend/), so the repo root
# is NOT on sys.path — `from tests.authkit import make_authkit` (used below to
# gate the app with the offline verifier) would fail. Put the repo root first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Disable the Testcontainers "Ryuk" reaper sidecar (see tests/conftest.py) —
# must be set BEFORE importing testcontainers.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from types import FrameType  # noqa: E402
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from usali.opener import SoftwareOpener

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 8100
QBO_PORT = 9200  # mock QBO, same port as `usali qbo-mock`'s default
GUSTO_PORT = 9300  # mock Gusto, same port as `usali gusto-mock`'s default

# Fixed name so a previous run that was killed hard (bypassing the graceful
# SIGTERM path, with Ryuk disabled) can be swept before we start fresh.
CONTAINER_NAME = "usali-e2e-postgres"

# All sample PDFs, discovered by glob (tests/conftest.py keeps its explicit
# list; here every sample in the directory belongs in the seeded dataset).
SAMPLES_DIR = REPO_ROOT / "docs" / "reference" / "samples"


def _remove_stale_container() -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", CONTAINER_NAME],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        print("Docker is required to run the e2e backend")
        raise SystemExit(1) from None


def _seed(db_url: str, work_dir: Path, opener: "SoftwareOpener") -> None:
    """Migrate, seed schedules + mappings, and process the six sample PDFs."""
    from datetime import UTC, date, datetime, time, timedelta

    from usali.db import make_engine, make_session_factory
    from usali.ingestion import process_file
    from usali.kiosk import mint_device_token
    from usali.mapping.loader import load_mappings
    from usali.mapping.property_registry import seed_properties
    from usali.mapping.schedules import seed_schedules
    from usali.deposit_accounts import account_slot, routing_slot
    from usali.models import (
        AssignmentRate,
        Department,
        DepositAccount,
        Employee,
        EmployeeAssignment,
        EmployeePayrollProfile,
        KioskDevice,
        PaySchedule,
        Punch,
        RoleAssignment,
        Schedule,
        Shift,
    )
    from usali.opener import seal_for_test
    from usali.timecards import assemble_timecard

    # L2: the l2a0rlswall migration refuses without the app role (CREATE
    # ROLE is cluster-level) — create it first, as every environment does.
    from tests.orgwall import ensure_app_role

    ensure_app_role(db_url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    factory = make_session_factory(make_engine(db_url))
    with factory() as session:
        seed_schedules(session, str(REPO_ROOT / "mapping" / "usali_schedules.yaml"))
        load_mappings(session, str(REPO_ROOT / "mapping" / "opera.yaml"))
        load_mappings(session, str(REPO_ROOT / "mapping" / "autoclerk.yaml"))
        seed_properties(session, str(REPO_ROOT / "mapping" / "properties.yaml"))
        # L4 (Pillar L decision 4): org authority is the org-scoped
        # role_assignment grants, not realm token roles — plant the
        # org-wide grant row for each minted e2e token's subject, exactly
        # as the demo seed does for the dev personas.
        for sub, role in (
            ("user-1", "accountant"),        # .e2e-token (authkit default sub)
            ("e2e-admin", "org_admin"),      # .e2e-admin-token
            ("e2e-payroll", "payroll_admin"),  # .e2e-payroll-token
        ):
            session.add(RoleAssignment(
                keycloak_subject=sub, role=role, property_id=None,
            ))
        session.commit()

        # A kiosk + one employee for the passwordless clock-in e2e
        # (frontend/e2e/kiosk.spec.ts) — no operator session involved, so the
        # device token is minted here and handed to Playwright via a file,
        # mirroring how .e2e-token carries the operator bearer below.
        token, token_hash = mint_device_token()
        device = KioskDevice(
            property_id="HISJ", name="E2E iPad",
            token_hash=token_hash, enrolled_by="e2e",
        )
        session.add(device)
        clocker = Employee(full_name="E2E Clocker", pay_type="hourly")
        session.add(clocker)
        session.flush()
        # WHERE someone works is a PLACEMENT since E1 Task 9, not a column on
        # the person. The kiosk roster reads assignments, so without this the
        # clocker cannot tap their name.
        session.add(EmployeeAssignment(
            employee_id=clocker.employee_id, property_id="HISJ",
            is_primary=True, status="active", effective_from=date(2026, 1, 5),
        ))
        session.commit()
        (REPO_ROOT / ".e2e-kiosk-token").write_text(token)

        # Pay-run e2e (payrun.spec.ts): one employee with everything a run
        # needs — an approved timecard in the 2026-06-08..2026-06-21 period
        # (verified: period_for(2026-06-09, anchor 2026-01-05) — a period of
        # the DEFAULT payroll_period_anchor, safely in the PAST so today's
        # kiosk-e2e punches can never land a colliding open timecard in it),
        # a pay rate, a sealed payroll profile (sealed to the app opener's
        # key — SYNTHETIC values only), and a biweekly PaySchedule.
        anchor = date(2026, 1, 5)
        shift_day = date(2026, 6, 9)
        dept = Department(property_id="HISJ", name="Housekeeping")
        session.add(dept)
        session.flush()
        # payroll_data_complete: the model default is False (a real hire is
        # blocked until a human states the paperwork is in), which since E3
        # is a preflight BLOCKER -- the payrun spec needs a payable employee.
        worker = Employee(full_name="E2E Payrollee", pay_type="hourly",
                          payroll_data_complete=True)
        session.add(worker)
        session.flush()
        # Placement first, then the rate ON that placement: since E2 a rate
        # belongs to an assignment and a date range, so a rate with no placement
        # to hang off resolves to nothing and the pay run blocks on "no pay rate
        # on file".
        worker_assignment = EmployeeAssignment(
            employee_id=worker.employee_id, property_id="HISJ",
            department_id=dept.department_id, is_primary=True, status="active",
            effective_from=anchor,
        )
        session.add(worker_assignment)
        session.flush()
        session.add(AssignmentRate(
            assignment_id=worker_assignment.assignment_id, rate_type="regular",
            amount="20.00", effective_from=anchor,
        ))
        session.flush()

        def _sealed(field_name: str, plaintext: bytes) -> str:
            return seal_for_test(
                opener.public_key(), plaintext,
                aad=f"{worker.employee_id}:{field_name}".encode(),
            ).to_json()

        # The deposit destination is a CHAIN since E5 -- one remainder row
        # sealed under the per-ordinal slot aad. The profile keeps identity
        # PII only.
        session.add(EmployeePayrollProfile(
            employee_id=worker.employee_id,
            ssn_sealed=_sealed("ssn", b"123-45-6789"),
        ))
        session.add(DepositAccount(
            employee_id=worker.employee_id, ordinal=1,
            allocation_type="remainder", allocation_value=None,
            account_type="checking",
            sealed_account=_sealed(account_slot(1, False), b"000123456"),
            sealed_routing=_sealed(routing_slot(1, False), b"021000021"),
            legacy_sealed=False,
        ))
        session.add(PaySchedule(
            property_id="HISJ", frequency="biweekly", anchor=anchor,
            check_date_offset_days=5,
        ))
        # One 8h shift (09:00-17:00, no lunch) -> 8 regular hours at $20/h.
        for punch_type, hour in (("clock_in", 9), ("clock_out", 17)):
            session.add(Punch(
                employee_id=worker.employee_id, kiosk_device_id=device.device_id,
                punch_type=punch_type,
                punched_at=datetime(2026, 6, 9, hour, tzinfo=UTC),
                business_date=shift_day,
            ))
        card = assemble_timecard(session, worker.employee_id, shift_day, anchor=anchor)
        card.status = "approved"
        card.approved_by = "e2e"
        card.approved_at = datetime.now(UTC)

        # Kiosk my-week e2e (kiosk.spec.ts): a PUBLISHED schedule for the
        # UPCOMING week with one shift for the kiosk employee. The Monday is
        # computed with the SAME rule the kiosk UI applies client-side
        # (frontend/src/lib/week.ts upcomingWeekMonday): this week's Monday +
        # 7 days — relative to today rather than pinned, because the UI always
        # asks for the week ahead of the test run. Every Monday is on the
        # payroll anchor grid (anchor 2026-01-05 is itself a Monday).
        # Published-only is the point — a draft would be invisible here.
        kiosk_week = date.today() - timedelta(days=date.today().weekday()) + timedelta(days=7)
        sched = Schedule(
            property_id="HISJ", week_start=kiosk_week, status="published",
            version=1, published_by="e2e", published_at=datetime.now(UTC),
        )
        session.add(sched)
        session.flush()
        session.add(Shift(
            schedule_id=sched.schedule_id, business_date=kiosk_week,
            department_id=dept.department_id, start_time=time(9, 0),
            end_time=time(17, 0), crosses_midnight=False,
            employee_id=clocker.employee_id,
        ))
        session.commit()

        inbox = work_dir / "inbox"
        inbox.mkdir()
        for sample in sorted(SAMPLES_DIR.glob("*.pdf")):
            shutil.copy(sample, inbox / sample.name)
        for pdf in sorted(inbox.glob("*.pdf")):
            process_file(
                session,
                pdf,
                processed_dir=work_dir / "processed",
                failed_dir=work_dir / "failed",
            )


def _start_mock_qbo() -> None:
    """Run the mock QBO on 127.0.0.1:9200 in a daemon thread of this process.

    A daemon thread (not a subprocess) so the SIGTERM → SystemExit shutdown
    path stays exactly as it is: nothing here can block the context managers
    below from unwinding, and the thread dies with the process. uvicorn only
    installs its own signal handlers on the main thread, so the mock server
    never touches the SIGTERM handler this script relies on.
    """
    from usali.qbo_mock import create_mock_qbo

    config = uvicorn.Config(
        create_mock_qbo(), host=HOST, port=QBO_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mock-qbo", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive():
            raise SystemExit(
                f"mock QBO failed to start on {HOST}:{QBO_PORT} — most likely the "
                f"port is already bound (a dev `usali qbo-mock`?); stop it and retry"
            )
        if time.monotonic() > deadline:
            raise SystemExit(f"mock QBO did not come up on {HOST}:{QBO_PORT} within 30s")
        time.sleep(0.05)


def _start_mock_gusto() -> None:
    """Run the mock Gusto on 127.0.0.1:9300 in a daemon thread of this process.

    Same daemon-thread pattern as _start_mock_qbo (and for the same reasons:
    the SIGTERM -> SystemExit shutdown path stays untouched, uvicorn installs
    no signal handlers off the main thread, the thread dies with the process).
    No token bootstrap is needed — the app's settings default (Bearer "mock")
    is what the mock accepts out of the box.
    """
    from usali.gusto_mock import create_mock_gusto

    config = uvicorn.Config(
        create_mock_gusto(), host=HOST, port=GUSTO_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mock-gusto", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive():
            raise SystemExit(
                f"mock Gusto failed to start on {HOST}:{GUSTO_PORT} — most likely the "
                f"port is already bound (a dev `usali gusto-mock`?); stop it and retry"
            )
        if time.monotonic() > deadline:
            raise SystemExit(
                f"mock Gusto did not come up on {HOST}:{GUSTO_PORT} within 30s"
            )
        time.sleep(0.05)


def _bootstrap_refresh_token(base_url: str) -> str:
    """authorization_code grant against the running mock (the e2e stand-in for
    Intuit's consent flow — mirrors tests/test_qbo_push.py).

    The mock also pre-seeds the settings-default refresh token ("mock"), so
    the e2e could rely on that instead; bootstrapping deliberately stays — it
    exercises the authorization_code grant over real HTTP and keeps the e2e
    independent of the pre-seeded default.
    """
    try:
        resp = httpx.post(
            f"{base_url}/oauth2/v1/tokens/bearer",
            data={"grant_type": "authorization_code", "code": "e2e-bootstrap"},
            # The mock validates the header shape only, like tests do: e2e:e2e.
            headers={"Authorization": "Basic ZTJlOmUyZQ=="},
        )
        resp.raise_for_status()
        token: str = resp.json()["refresh_token"]
        return token
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise SystemExit(f"mock QBO token bootstrap failed: {exc}") from exc


def _sigterm_to_system_exit(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(0)


def main() -> None:
    # Playwright stops this webServer with SIGTERM (gracefulShutdown in
    # frontend/playwright.config.ts). uvicorn captures the signal, shuts down,
    # restores the pre-serve handler, and RE-RAISES the signal — with the OS
    # default handler that would kill the process before the `with` block
    # below unwinds, leaking the postgres container. Raising SystemExit
    # instead lets the context managers run and remove the container.
    signal.signal(signal.SIGTERM, _sigterm_to_system_exit)
    # The pipeline resolves paths like mapping/properties.yaml relative to the
    # cwd; Playwright launches this script from frontend/, so anchor at the
    # repo root regardless of the caller.
    os.chdir(REPO_ROOT)
    _remove_stale_container()
    with (
        PostgresContainer("postgres:16", driver="psycopg").with_name(CONTAINER_NAME) as pg,
        tempfile.TemporaryDirectory(prefix="usali-e2e-") as tmp,
    ):
        db_url = pg.get_connection_url()
        # create_app()'s default session factory reads USALI_DB_URL via settings.
        os.environ["USALI_DB_URL"] = db_url
        work_dir = Path(tmp)
        # One SoftwareOpener instance shared by the seed (which seals the
        # pay-run employee's profile to its public key) and the app (whose
        # sync_employees opens those envelopes at submit time) — the same
        # keypair on both sides is what makes the vault round-trip real.
        from usali.opener import SoftwareOpener as _SoftwareOpener

        opener = _SoftwareOpener.generate(key_id="e2e")
        _seed(db_url, work_dir, opener)

        # Mock QBO + a bootstrapped refresh token, both in env BEFORE the app
        # starts. The app's shared QboClient reads these lazily via
        # get_settings() on the first push, so setting them here is early
        # enough — and both the mock's token state and the app's client are
        # fresh per run, keeping the pair consistent.
        qbo_base_url = f"http://{HOST}:{QBO_PORT}"
        _start_mock_qbo()
        os.environ["USALI_QBO_BASE_URL"] = qbo_base_url
        os.environ["USALI_QBO_REFRESH_TOKEN"] = _bootstrap_refresh_token(qbo_base_url)

        # Mock Gusto for the pay-run e2e (payrun.spec.ts). No env needed: the
        # settings defaults already point the GustoAdapter at 127.0.0.1:9300
        # with the static "mock" token, and payroll_provider defaults to gusto.
        _start_mock_gusto()

        # Gate the app with the offline RSA verifier (no Keycloak in the e2e),
        # and write the matching operator token where the Playwright globalSetup
        # can seed it into the browser's oidc store. A generous exp_in covers
        # container pull + seeding + the whole suite. Written BEFORE uvicorn
        # answers /api/properties, so the file exists by the time Playwright's
        # webServer readiness check passes and globalSetup runs.
        from tests.authkit import make_authkit

        verifier, mint = make_authkit()
        (REPO_ROOT / ".e2e-token").write_text(mint(roles=["accountant"], exp_in=3600))
        # A second, org_admin token for the onboarding e2e (employees.spec.ts) —
        # the default operator token above is `accountant`, which is correctly
        # NOT an onboarder (see require_onboarder in workforce.py).
        (REPO_ROOT / ".e2e-admin-token").write_text(
            mint(roles=["org_admin"], sub="e2e-admin", exp_in=3600)
        )
        # A third, payroll_admin token for the sealed-PII vault e2e
        # (payroll.spec.ts): the blind-overwrite write/status routes are gated by
        # require_payroll_admin (Pillar C1), which neither the accountant nor the
        # org_admin token above satisfies.
        (REPO_ROOT / ".e2e-payroll-token").write_text(
            mint(roles=["payroll_admin"], sub="e2e-payroll", exp_in=3600)
        )

        from usali.keycloak_admin import InMemoryKeycloakAdmin
        from usali.photo_store import InMemoryPhotoStore
        from usali.server import create_app

        app = create_app(
            inbox_dir=work_dir / "upload-inbox",
            processed_dir=work_dir / "processed",
            failed_dir=work_dir / "failed",
            token_verifier=verifier,
            # Onboarding (A2.3) calls request.app.state.keycloak_admin; without
            # this the default KeycloakAdminClient would try to reach a real
            # Keycloak, which isn't running in the e2e.
            keycloak_admin=InMemoryKeycloakAdmin(),
            # Kiosk punch photos (B1): without this the default LocalPhotoStore
            # would write AES-GCM-encrypted files to disk during the e2e run.
            photo_store=InMemoryPhotoStore(),
            # The SAME opener the seed sealed the payrun employee's profile to
            # (the default would generate a fresh keypair and fail to open it).
            # payroll.spec.ts also seals against this instance's public key.
            opener=opener,
        )
        uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
