"""F7: the demo seed's face-matching integration.

Two load-bearing pieces get pinned here (the seed itself is exercised by
running scripts/demo.sh, but these decide WHAT it does):

- `_face_stars`: which demo people get a synthetic face — one hourly worker
  per property, so every kiosk has someone who matches.
- `_stamp_demo_match_states`: giving the seeded OPEN punches a coherent
  match story (enrolled people verified, one red, everyone else grey)
  without ever touching history or a verdict a live kiosk already wrote.
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from tests.employees import make_employee
from usali.kiosk import mint_device_token
from usali.models import KioskDevice, Organization, Property, Punch
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS
from usali.tenancy import FOUNDING_ORG_ID, bind_org_context

_SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed", _SCRIPT)
assert _spec is not None and _spec.loader is not None
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)


def _worker(ref, pay_type="hourly", placements=(("HISJ", "Front Desk"),)):
    return demo_seed.DemoWorker(
        ref=ref, full_name=f"Person {ref}", pay_type=pay_type,
        placements=tuple(placements),
    )


def test_face_stars_picks_one_hourly_person_per_property():
    workers = [
        _worker(9, pay_type="salary"),                      # never a star
        _worker(4),                                         # HISJ star (lowest ref)
        _worker(6),                                         # same property: skipped
        _worker(7, placements=(("SSSJ", "Housekeeping"),)),  # SSSJ star
        _worker(8, placements=(("SSSJ", "Laundry"),)),       # already covered
    ]
    stars = demo_seed._face_stars(workers)
    assert [w.ref for w in stars] == [4, 7]


def test_face_stars_caps_at_the_fixture_count():
    # More properties than committed synthetic faces: no star without a face.
    workers = [
        _worker(1, placements=(("AAAA", "X"),)),
        _worker(2, placements=(("BBBB", "X"),)),
        _worker(3, placements=(("CCCC", "X"),)),
    ]
    assert len(demo_seed._face_stars(workers)) == len(demo_seed._FACE_FIXTURES)


def _punch_world(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    _, token_hash = mint_device_token()
    device = KioskDevice(property_id="HISJ", name="K", token_hash=token_hash,
                         enrolled_by="t")
    db_session.add(device)
    enrolled = make_employee(db_session, property_id="HISJ",
                             full_name="Enrolled Star", pay_type="hourly")
    other = make_employee(db_session, property_id="HISJ",
                          full_name="Cold Start", pay_type="hourly")
    db_session.flush()

    horizon = demo_seed.PERIOD_STARTS[-1] \
        + timedelta(days=demo_seed.PERIOD_DAYS)

    def punch(emp, day, hour, **kw):
        p = Punch(
            employee_id=emp.employee_id, kiosk_device_id=device.device_id,
            punch_type="clock_in",
            punched_at=datetime(day.year, day.month, day.day, hour,
                                tzinfo=UTC),
            business_date=day, **kw,
        )
        db_session.add(p)
        return p

    history = punch(enrolled, demo_seed.PERIOD_STARTS[0], 9)
    cold_history = punch(other, demo_seed.PERIOD_STARTS[0], 9)
    # The LATEST punch already carries a live kiosk verdict: the red-punch
    # pick must skip it and flip only a verdict the seed itself stamped.
    live = punch(enrolled, horizon, 18, match_state="unverified",
                 match_score=0.2)
    open_in = punch(enrolled, horizon, 10)
    open_out = punch(enrolled, horizon, 17)
    cold = punch(other, horizon, 9)
    db_session.commit()
    return enrolled, history, cold_history, live, open_in, open_out, cold


def _state(db_session, p):
    return db_session.execute(
        select(Punch.match_state, Punch.match_score)
        .where(Punch.punch_id == p.punch_id)).one()


def test_stamp_demo_match_states_tells_a_coherent_story(db_session):
    (enrolled, history, cold_history, live,
     open_in, open_out, cold) = _punch_world(db_session)
    # The stamping population is the id list the world seed returns —
    # history and the live punch are NOT in it.
    demo_seed._stamp_demo_match_states(
        db_session, [enrolled.employee_id],
        [open_in.punch_id, open_out.punch_id, cold.punch_id],
    )

    state = lambda p: _state(db_session, p)  # noqa: E731

    # History predates matching: no verdict is the honest record — for the
    # enrolled star AND the cold-start person alike.
    assert state(history) == (None, None)
    assert state(cold_history) == (None, None)
    # A verdict a live kiosk already wrote is never overwritten.
    assert state(live) == ("unverified", 0.2)
    # The enrolled star's open punches verify — except the last one, which
    # goes red so the approval-gate demo has something to gate.
    assert state(open_in)[0] == "verified"
    assert state(open_in)[1] is not None
    red_state, red_score = state(open_out)
    assert red_state == "unverified"
    assert red_score is not None and red_score < 0.60
    # Everyone unenrolled is a grey cold start.
    assert state(cold) == ("no_template", None)


def test_live_null_punches_are_not_the_seeds_to_rewrite(db_session):
    """A live punch recorded while the engine was down carries NULL — the
    honest 'recorded without matching'. Being NULL and recent must not make
    it stampable (the F8 money-lens HIGH): only ids the world seed created
    this run are. Here the seed list is EMPTY — the topped-up re-run — and
    nothing may move, no matter who enrolled."""
    (enrolled, history, cold_history, live,
     open_in, open_out, cold) = _punch_world(db_session)
    demo_seed._stamp_demo_match_states(db_session, [enrolled.employee_id], [])

    # open_in/open_out/cold model LIVE outage punches this time (they are
    # not in the seed-id list): every verdict stays exactly as recorded.
    for p in (history, cold_history, open_in, open_out, cold):
        assert _state(db_session, p) == (None, None)
    assert _state(db_session, live) == ("unverified", 0.2)


def test_stamp_with_nobody_enrolled_is_a_noop(db_session):
    """Nobody newly enrolled but the world DID seed punches: stamping must
    be a no-op — without the guard, notin_([]) matches every seed punch
    (all stamped no_template) and enrolled_ids[0] then raises. Both belt
    and suspenders live in _stamp itself, not only in its caller."""
    (enrolled, history, cold_history, live,
     open_in, open_out, cold) = _punch_world(db_session)
    demo_seed._stamp_demo_match_states(
        db_session, [], [open_in.punch_id, open_out.punch_id, cold.punch_id]
    )
    for p in (open_in, open_out, cold):
        assert _state(db_session, p) == (None, None)


def test_red_flip_never_touches_a_live_verified_punch(db_session):
    """The star enrolled via the API, punched live (genuine green 0.97),
    template later removed, seed re-runs and re-enrolls. The red-flip must
    pick only among the seed's OWN stamped punches — flipping the live
    green to a fabricated approval-gating red is the F8 refutation."""
    (enrolled, history, cold_history, live,
     open_in, open_out, cold) = _punch_world(db_session)
    db_session.execute(
        Punch.__table__.update()
        .where(Punch.punch_id == live.punch_id)
        .values(match_state="verified", match_score=0.97)
    )
    db_session.commit()

    demo_seed._stamp_demo_match_states(
        db_session, [enrolled.employee_id],
        [open_in.punch_id, open_out.punch_id],
    )

    # The live green — the star's LATEST verified punch — is untouched;
    # the red went to the last punch the seed itself stamped.
    assert _state(db_session, live) == ("verified", 0.97)
    assert _state(db_session, open_out)[0] == "unverified"


class _FakeSeedEngine:
    model_version = "fake-seed-engine-v1"

    def embed_largest_face(self, image_bytes: bytes) -> list[float] | None:
        return [1.0, 0.0, 0.0, 0.0]


def test_seed_faces_enrolls_stars_and_stamps_only_seed_punches(
    db_session, tmp_path, monkeypatch
):
    """_seed_faces end to end with a fake engine: the star enrolls through
    write_face_template, and stamping reaches exactly the seed-created ids."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    monkeypatch.setenv("USALI_PHOTO_STORE_DIR", str(tmp_path / "photos"))
    import usali.face_match
    monkeypatch.setattr(usali.face_match, "OnnxFaceEngine",
                        lambda model_dir: _FakeSeedEngine())
    (enrolled, history, cold_history, live,
     open_in, open_out, cold) = _punch_world(db_session)
    # The real seed runs on a FOUNDING-org-bound session (demo_seed.main);
    # write_face_template reads that org (L5) to prefix the reference key.
    bind_org_context(db_session, FOUNDING_ORG_ID)
    workers = [demo_seed.DemoWorker(
        ref=1, full_name=enrolled.full_name, pay_type="hourly",
        placements=(("HISJ", "Front Desk"),),
    )]

    demo_seed._seed_faces(db_session, workers,
                          [open_in.punch_id, open_out.punch_id])

    from usali.models import EmployeeFaceTemplate
    assert db_session.execute(
        select(EmployeeFaceTemplate)
        .where(EmployeeFaceTemplate.employee_id == enrolled.employee_id)
    ).scalar_one() is not None
    assert _state(db_session, open_in)[0] == "verified"
    assert _state(db_session, open_out)[0] == "unverified"  # the red
    assert _state(db_session, live) == ("unverified", 0.2)
    assert _state(db_session, cold) == (None, None)  # not a seed punch


def test_seed_faces_rerun_with_everyone_enrolled_stamps_nothing(
    db_session, tmp_path, monkeypatch
):
    """The all-already-enrolled re-run: no crash, no stamping — even with a
    non-empty seed list, nobody NEWLY enrolled means no story to tell (the
    F8 migration-lens S4 mutant: `if enrolled_ids:` -> `if True:` called
    the stamper with an empty enrolled list and died on [0])."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    monkeypatch.setenv("USALI_PHOTO_STORE_DIR", str(tmp_path / "photos"))
    import usali.face_match
    monkeypatch.setattr(usali.face_match, "OnnxFaceEngine",
                        lambda model_dir: _FakeSeedEngine())
    (enrolled, history, cold_history, live,
     open_in, open_out, cold) = _punch_world(db_session)
    from usali.models import EmployeeFaceTemplate
    db_session.add(EmployeeFaceTemplate(
        employee_id=enrolled.employee_id,
        embedding=b"\x00\x00\x80?" + b"\x00" * 12,
        model_version=_FakeSeedEngine.model_version, created_by="t"))
    db_session.commit()
    workers = [demo_seed.DemoWorker(
        ref=1, full_name=enrolled.full_name, pay_type="hourly",
        placements=(("HISJ", "Front Desk"),),
    )]

    demo_seed._seed_faces(db_session, workers,
                          [open_in.punch_id, open_out.punch_id])

    for p in (open_in, open_out, cold):
        assert _state(db_session, p) == (None, None)
