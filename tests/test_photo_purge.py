import hashlib
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from typer.testing import CliRunner

from tests.employees import make_employee
from usali.cli import app
from usali.models import KioskDevice, Organization, Property, Punch, Timecard
from usali.photo_store import InMemoryPhotoStore
from usali.timecards import assemble_timecard, purge_approved_photos
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)


def _seed_org(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()


def _seed_employee(db_session, property_id: str, name: str, photo_key: str, *, hour: int = 9):
    device = KioskDevice(property_id=property_id, name="iPad",
                         token_hash=hashlib.sha256(name.encode()).hexdigest(),
                         enrolled_by="adm")
    emp = make_employee(db_session, property_id=property_id, full_name=name, pay_type="hourly")
    db_session.add_all([device, emp])
    db_session.flush()
    db_session.add(Punch(
        employee_id=emp.employee_id, kiosk_device_id=device.device_id,
        punch_type="clock_in", punched_at=datetime(2026, 7, 7, hour, tzinfo=UTC),
        business_date=date(2026, 7, 7), photo_key=photo_key,
    ))
    db_session.commit()
    return emp.employee_id


def _seed_card(db_session, *, approved_days_ago: int | None):
    _seed_org(db_session)
    emp_id = _seed_employee(db_session, "HISJ", "Hank H", "HISJ/2026-07-07/abc.bin")
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    if approved_days_ago is not None:
        card.status = "approved"
        card.approved_at = datetime.now(UTC) - timedelta(days=approved_days_ago)
    db_session.commit()
    return card


def test_purges_photos_for_long_approved_cards(db_session):
    _seed_card(db_session, approved_days_ago=100)
    store = InMemoryPhotoStore()
    store.put("HISJ/2026-07-07/abc.bin", b"\xff\xd8\xff x")

    purged = purge_approved_photos(db_session, store, retention_days=90)
    db_session.commit()

    assert purged == 1
    assert store.keys() == set()                       # image really deleted
    punch = db_session.execute(select(Punch)).scalars().one()
    assert punch.photo_key is None                     # pointer cleared
    card = db_session.execute(select(Timecard)).scalars().one()
    assert card.photos_purged_at is not None


def test_does_not_purge_recently_approved(db_session):
    _seed_card(db_session, approved_days_ago=10)
    store = InMemoryPhotoStore()
    store.put("HISJ/2026-07-07/abc.bin", b"\xff\xd8\xff x")
    assert purge_approved_photos(db_session, store, retention_days=90) == 0
    assert store.keys() == {"HISJ/2026-07-07/abc.bin"}


def test_does_not_purge_unapproved(db_session):
    _seed_card(db_session, approved_days_ago=None)  # still open
    store = InMemoryPhotoStore()
    store.put("HISJ/2026-07-07/abc.bin", b"\xff\xd8\xff x")
    assert purge_approved_photos(db_session, store, retention_days=90) == 0
    assert store.keys() == {"HISJ/2026-07-07/abc.bin"}


def test_purge_is_idempotent(db_session):
    """A second run purges nothing — `photos_purged_at` is the stop flag, and a
    cleared `photo_key` is never re-deleted."""
    _seed_card(db_session, approved_days_ago=100)
    store = InMemoryPhotoStore()
    store.put("HISJ/2026-07-07/abc.bin", b"\xff\xd8\xff x")

    assert purge_approved_photos(db_session, store, retention_days=90) == 1
    db_session.commit()
    assert purge_approved_photos(db_session, store, retention_days=90) == 0
    db_session.commit()

    card = db_session.execute(select(Timecard)).scalars().one()
    first_stamp = card.photos_purged_at
    assert purge_approved_photos(db_session, store, retention_days=90) == 0
    db_session.commit()
    assert card.photos_purged_at == first_stamp  # stamp not rewritten


def test_leaves_other_properties_photos_alone(db_session):
    """The purge is confined to the punches of the card being purged — an open
    card at another property keeps its evidence."""
    _seed_org(db_session)
    hisj_emp = _seed_employee(db_session, "HISJ", "Hank H", "HISJ/2026-07-07/abc.bin")
    sssj_emp = _seed_employee(db_session, "SSSJ", "Sam S", "SSSJ/2026-07-07/xyz.bin", hour=10)

    approved = assemble_timecard(db_session, hisj_emp, date(2026, 7, 7), anchor=_ANCHOR)
    approved.status = "approved"
    approved.approved_at = datetime.now(UTC) - timedelta(days=100)
    assemble_timecard(db_session, sssj_emp, date(2026, 7, 7), anchor=_ANCHOR)  # open
    db_session.commit()

    store = InMemoryPhotoStore()
    store.put("HISJ/2026-07-07/abc.bin", b"\xff\xd8\xff a")
    store.put("SSSJ/2026-07-07/xyz.bin", b"\xff\xd8\xff b")

    assert purge_approved_photos(db_session, store, retention_days=90) == 1
    db_session.commit()

    assert store.keys() == {"SSSJ/2026-07-07/xyz.bin"}
    other = db_session.execute(
        select(Punch).where(Punch.employee_id == sssj_emp)
    ).scalars().one()
    assert other.photo_key == "SSSJ/2026-07-07/xyz.bin"


class _FlakyStore(InMemoryPhotoStore):
    """A store whose backend fails for one key (an S3 5xx, a permissions blip)."""

    def __init__(self, bad_key: str) -> None:
        super().__init__()
        self._bad_key = bad_key

    def delete(self, key: str) -> None:
        if key == self._bad_key:
            raise RuntimeError("backend unavailable")
        super().delete(key)


def test_one_bad_key_does_not_abort_the_run(db_session):
    """A backend failure on one key must not strand the rest of the batch, must
    NOT clear that punch's pointer (the image still exists), and must leave the
    card unstamped so the next run retries it."""
    _seed_org(db_session)
    emp_a = _seed_employee(db_session, "HISJ", "Hank H", "HISJ/2026-07-07/bad.bin")
    emp_b = _seed_employee(db_session, "HISJ", "Bea B", "HISJ/2026-07-07/good.bin", hour=10)
    for emp in (emp_a, emp_b):
        card = assemble_timecard(db_session, emp, date(2026, 7, 7), anchor=_ANCHOR)
        card.status = "approved"
        card.approved_at = datetime.now(UTC) - timedelta(days=100)
    db_session.commit()

    store = _FlakyStore("HISJ/2026-07-07/bad.bin")
    store.put("HISJ/2026-07-07/bad.bin", b"\xff\xd8\xff a")
    store.put("HISJ/2026-07-07/good.bin", b"\xff\xd8\xff b")

    purged = purge_approved_photos(db_session, store, retention_days=90)
    db_session.commit()

    assert purged == 1  # only the good one counts
    assert store.keys() == {"HISJ/2026-07-07/bad.bin"}

    bad = db_session.execute(
        select(Punch).where(Punch.employee_id == emp_a)
    ).scalars().one()
    assert bad.photo_key == "HISJ/2026-07-07/bad.bin"  # pointer kept: image is still there
    good = db_session.execute(
        select(Punch).where(Punch.employee_id == emp_b)
    ).scalars().one()
    assert good.photo_key is None

    cards = {c.employee_id: c for c in db_session.execute(select(Timecard)).scalars()}
    assert cards[emp_a].photos_purged_at is None      # unfinished -> retried next run
    assert cards[emp_b].photos_purged_at is not None

    # The retry (with a healthy store) finishes the job.
    healthy = InMemoryPhotoStore()
    healthy.put("HISJ/2026-07-07/bad.bin", b"\xff\xd8\xff a")
    assert purge_approved_photos(db_session, healthy, retention_days=90) == 1
    db_session.commit()
    assert healthy.keys() == set()
    assert cards[emp_a].photos_purged_at is not None


def test_an_unlinked_punchs_photo_purges_by_its_own_age(db_session):
    """H3 (decision 3): an unlinked punch belongs to no card, so the
    approved-card clock never reaches its photo — before the orphan
    clause, being unlinked made a face photo immortal. Its own age
    (punched_at) is the only clock it has: past retention it purges,
    inside retention it STAYS (it is evidence for the reopen review), and
    a second run is a no-op because the cleared pointer is the stop flag."""
    _seed_org(db_session)
    device = KioskDevice(property_id="HISJ", name="iPad",
                         token_hash="o" * 64, enrolled_by="adm")
    emp = make_employee(db_session, property_id="HISJ", full_name="Orla O",
                        pay_type="hourly")
    db_session.add_all([device, emp])
    db_session.flush()
    old = Punch(
        employee_id=emp.employee_id, kiosk_device_id=device.device_id,
        punch_type="clock_in",
        punched_at=datetime.now(UTC) - timedelta(days=100),
        business_date=date(2026, 4, 20), photo_key="HISJ/old.bin",
    )
    young = Punch(
        employee_id=emp.employee_id, kiosk_device_id=device.device_id,
        punch_type="clock_in",
        punched_at=datetime.now(UTC) - timedelta(days=10),
        business_date=date(2026, 7, 19), photo_key="HISJ/young.bin",
    )
    db_session.add_all([old, young])
    db_session.commit()
    store = InMemoryPhotoStore()
    store.put("HISJ/old.bin", b"\xff\xd8\xff a")
    store.put("HISJ/young.bin", b"\xff\xd8\xff b")

    assert purge_approved_photos(db_session, store, retention_days=90) == 1
    db_session.commit()

    assert store.keys() == {"HISJ/young.bin"}
    db_session.refresh(old)
    db_session.refresh(young)
    assert old.photo_key is None
    assert young.photo_key == "HISJ/young.bin"
    assert purge_approved_photos(db_session, store, retention_days=90) == 0


def test_purge_cli_command_runs(db_url):
    """Wiring smoke test: the command resolves settings, the store, and a session.
    Nothing in the shared schema is approved beyond the retention window."""
    result = CliRunner().invoke(app, ["purge-punch-photos"])
    assert result.exit_code == 0, result.output
    assert "Purged 0 punch photos" in result.output

