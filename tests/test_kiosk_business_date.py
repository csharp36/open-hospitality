"""The kiosk asks its date-scoped questions in the PROPERTY's frame.

A punch is stamped `business_date_for(punched_at, prop.timezone, cutoff)`, but
the assignment-window checks around it used to pass `date.today()` — the
container's local date, which is UTC because nothing sets TZ. `Property.timezone`
defaults to America/Los_Angeles, so from 17:00 local the two disagree.

That is not academic. `terminate_employee` sets `effective_to = last_day + 1`
and `covers` is half-open, so on an employee's FINAL day the evening shift was
refused — 403 "employee out of kiosk scope", the last shift unrecorded, and no
error naming the employee. `in_effect_on`'s docstring records that exact outcome
having already been fixed once by a different route; the predicate was right,
the date handed to it was not.
"""

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from tests.authkit import make_authkit
from tests.employees import make_employee
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.kiosk import _property_business_date, mint_device_token
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS
from usali.models import KioskDevice, Organization, Property
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app

# 18:00 on 2026-08-23 at a Pacific property is already 2026-08-24 in UTC. This
# is the whole bug in one instant: the punch belongs to the 23rd, the server
# thinks it is the 24th.
EVENING_UTC = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
PROPERTY_DAY = date(2026, 8, 23)
SERVER_DAY = date(2026, 8, 24)


@pytest.fixture
def frozen_evening(monkeypatch):
    """Pin wall-clock to 18:00 Pacific / 01:00 UTC the next day."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return EVENING_UTC.astimezone(tz) if tz else EVENING_UTC

    monkeypatch.setattr("usali.kiosk.datetime", _Frozen)


def _seed(db_session, *, effective_to: date | None):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    # Pacific, i.e. the server_default — this is the pilot's own configuration,
    # not a contrived edge case.
    db_session.add(
        Property(
            property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA",
            wage_jurisdiction="US-CA", timezone="America/Los_Angeles",
        )
    )
    db_session.flush()
    token, token_hash = mint_device_token()
    db_session.add(
        KioskDevice(property_id="HISJ", name="iPad", token_hash=token_hash, enrolled_by="adm")
    )
    leaver = make_employee(
        db_session, property_id="HISJ", full_name="Elena Leaving",
        pay_type="hourly", effective_to=effective_to,
    )
    db_session.flush()
    return token, leaver


def _app(db_engine, tmp_path):
    return create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )


def test_business_date_is_the_propertys_day_not_the_servers(db_session, frozen_evening):
    """The deterministic guard: at 18:00 Pacific the two dates differ, and the
    kiosk must use the property's. If this ever returns SERVER_DAY, every
    date-scoped kiosk query has silently moved a day."""
    _seed(db_session, effective_to=None)
    db_session.flush()

    assert _property_business_date(db_session, "HISJ") == PROPERTY_DAY
    assert _property_business_date(db_session, "HISJ") != SERVER_DAY


def test_final_day_evening_punch_state_is_allowed(db_engine, db_session, tmp_path, frozen_evening):
    """An employee whose LAST WORKED DAY is today can still clock out at 18:00.

    `effective_to` is exclusive (`terminate_employee` writes `last_day + 1`), so
    the window closes at SERVER_DAY. Checked against the server's date the
    assignment reads as already over and the kiosk answers 403; checked against
    the property's business date it is still in force, which is the truth.
    """
    token, leaver = _seed(db_session, effective_to=SERVER_DAY)
    db_session.commit()

    assert _property_business_date(db_session, "HISJ") == PROPERTY_DAY
    assert EVENING_UTC.date() == SERVER_DAY, "fixture must straddle midnight UTC"

    client = TestClient(_app(db_engine, tmp_path))
    resp = client.get(
        "/api/kiosk/punch-state",
        params={"employee_id": leaver.employee_id},
        headers={"X-Kiosk-Token": token},
    )
    assert resp.status_code == 200, (
        "an employee on their final day was refused at 18:00 local — the "
        "assignment window was evaluated against the server's date, not the "
        "property's business date"
    )


def test_final_day_evening_roster_still_lists_the_leaver(
    db_engine, db_session, tmp_path, frozen_evening
):
    """Same boundary, via the roster: they must still have a tile to tap."""
    token, leaver = _seed(db_session, effective_to=SERVER_DAY)
    db_session.commit()

    client = TestClient(_app(db_engine, tmp_path))
    resp = client.get("/api/kiosk/employees", headers={"X-Kiosk-Token": token})
    assert resp.status_code == 200
    assert leaver.employee_id in [e["employee_id"] for e in resp.json()]


def test_the_day_after_the_window_closes_is_still_refused(
    db_engine, db_session, tmp_path, frozen_evening
):
    """The fix must not become a blanket bypass: once the window has genuinely
    closed IN THE PROPERTY'S FRAME, the kiosk still refuses."""
    token, leaver = _seed(db_session, effective_to=PROPERTY_DAY)  # exclusive: last day was the 22nd
    db_session.commit()

    client = TestClient(_app(db_engine, tmp_path))
    resp = client.get(
        "/api/kiosk/punch-state",
        params={"employee_id": leaver.employee_id},
        headers={"X-Kiosk-Token": token},
    )
    assert resp.status_code == 403
