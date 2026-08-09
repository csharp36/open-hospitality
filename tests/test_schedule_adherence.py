"""Endpoint tests for D3 adherence: scheduled vs punched per department/day
with no_show / unscheduled_punch / deviation exceptions.

Punched hours come from B2's MERGED timeline (punches + audited adjustments),
so THE test here is the manager-corrected day: a punch missing its clock-out,
fixed via a `TimecardAdjustment`, is CLEAN — never a deviation. Everything is
hours-only (the money rule), elapsed-days-only (strictly before `as_of`), and
`as_of` is ALWAYS passed explicitly — the server-today default would make
these fixed 2026-07 weeks date-dependent.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from tests.employees import make_employee, set_department
from usali.models import Employee, Punch, Timecard, TimecardAdjustment
from tests.authkit import make_authkit
from tests.test_schedule_api import (
    _add,
    _admin,
    _assemble,
    _client,
    _gm,
    _kiosk,
    _make_week,
    _punch_day,
    _seed,
    _seed_costing,
    _shift,
)

_MON = date(2026, 7, 6)  # the schedule week (payroll-grid Monday)
_TUE = date(2026, 7, 7)


def _punch_simple(db_session, device_id, emp_id, day, start_h, end_h):
    """A lunchless punched day: clock in/out only (worked = the raw span)."""
    for ptype, hh in (("clock_in", start_h), ("clock_out", end_h)):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(day.year, day.month, day.day, hh, tzinfo=UTC),
            business_date=day,
        ))


def _adherence(c, headers, week_id, as_of):
    r = c.get(f"/api/schedule/weeks/{week_id}/adherence",
              params={"as_of": as_of}, headers=headers)
    assert r.status_code == 200, r.text
    return r


def test_clean_day_matches_scheduled_and_punched(db_engine, db_session, tmp_path):
    """Scheduled 09:00-17:00 (meal-adjusted 7.50); punched a real 9-17 day
    with a 30-minute lunch (7.50 worked) -> 7.50/7.50, no exception."""
    ids = _seed(db_session)
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))  # Monday 09:00-17:00
    _punch_day(db_session, device_id, ids["emp"], _MON)
    _assemble(db_session, ids["emp"])

    body = _adherence(c, admin, week_id, "2026-07-07").json()
    assert body["schedule_id"] == week_id
    assert body["week_start"] == "2026-07-06"
    assert body["as_of"] == "2026-07-07"
    assert body["exceptions"] == []
    [dept] = body["departments"]
    assert dept["department"] == "Front Desk"
    assert dept["days"] == [{"business_date": "2026-07-06",
                             "scheduled_hours": "7.50", "punched_hours": "7.50"}]
    assert dept["scheduled_total"] == "7.50"
    assert dept["punched_total"] == "7.50"


def test_no_show(db_engine, db_session, tmp_path):
    """Scheduled Monday, zero punches -> no_show 7.50/0.00; the department
    day shows punched 0."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))

    body = _adherence(c, admin, week_id, "2026-07-07").json()
    assert body["exceptions"] == [{
        "code": "no_show", "employee_id": ids["emp"], "full_name": "Hank H",
        "business_date": "2026-07-06",
        "scheduled_hours": "7.50", "punched_hours": "0.00",
    }]
    [dept] = body["departments"]
    assert dept["days"][0]["punched_hours"] == "0.00"


def test_unscheduled_punch_attributes_to_the_home_department(
    db_engine, db_session, tmp_path
):
    """Punches on a day with no shift -> unscheduled_punch; the hours land in
    the employee's HOME department — and "Unassigned" when they have none."""
    ids = _seed(db_session)
    device_id = _kiosk(db_session)
    # Hank's home department is Front Desk; Drifter has no home department.
    hank = db_session.get(Employee, ids["emp"])
    set_department(db_session, hank, ids["dept"])
    drifter = make_employee(db_session, property_id="HISJ", full_name="Drifter D", pay_type="hourly")
    db_session.add(drifter)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    # NO shifts at all this week — every punch below is unscheduled.
    _punch_day(db_session, device_id, ids["emp"], _MON)  # 7.50 worked
    _punch_simple(db_session, device_id, drifter.employee_id, _MON, 10, 14)  # 4.00
    _assemble(db_session, ids["emp"])
    _assemble(db_session, drifter.employee_id)

    body = _adherence(c, admin, week_id, "2026-07-07").json()
    assert body["exceptions"] == [
        {"code": "unscheduled_punch", "employee_id": drifter.employee_id,
         "full_name": "Drifter D", "business_date": "2026-07-06",
         "scheduled_hours": "0.00", "punched_hours": "4.00"},
        {"code": "unscheduled_punch", "employee_id": ids["emp"],
         "full_name": "Hank H", "business_date": "2026-07-06",
         "scheduled_hours": "0.00", "punched_hours": "7.50"},
    ]
    depts = {d["department"]: d for d in body["departments"]}
    assert depts["Front Desk"]["punched_total"] == "7.50"  # Hank's home dept
    assert depts["Front Desk"]["scheduled_total"] == "0.00"
    assert depts["Unassigned"]["punched_total"] == "4.00"  # no home dept
    assert depts["Unassigned"]["scheduled_total"] == "0.00"


def test_deviation_at_the_60_minute_threshold(db_engine, db_session, tmp_path):
    """Scheduled 7.50 both days. Monday punched 4.00 (210 min short) ->
    deviation; Tuesday punched 7.00 (30 min short) -> NO exception at the
    60-minute default."""
    ids = _seed(db_session)
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))  # Monday 09:00-17:00 -> 7.50
    _add(c, admin, week_id, _shift(ids, business_date="2026-07-07"))
    _punch_simple(db_session, device_id, ids["emp"], _MON, 9, 13)  # 4.00
    _punch_simple(db_session, device_id, ids["emp"], _TUE, 9, 16)  # 7.00
    _assemble(db_session, ids["emp"])

    body = _adherence(c, admin, week_id, "2026-07-08").json()
    assert body["exceptions"] == [{
        "code": "deviation", "employee_id": ids["emp"], "full_name": "Hank H",
        "business_date": "2026-07-06",
        "scheduled_hours": "7.50", "punched_hours": "4.00",
    }]


def test_manager_corrected_day_is_clean(db_engine, db_session, tmp_path):
    """THE test for deliberate call 1: a punch missing its clock-out,
    corrected via a TimecardAdjustment (B2's additive rule) -> punched hours
    reflect the correction; no deviation, no no_show."""
    ids = _seed(db_session)
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))  # Monday 09:00-17:00 -> 7.50
    # Clock in + lunch, but the clock-out never happened.
    for ptype, hh, mm in (("clock_in", 9, 0), ("lunch_start", 12, 0),
                          ("lunch_end", 12, 30)):
        db_session.add(Punch(
            employee_id=ids["emp"], kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(2026, 7, 6, hh, mm, tzinfo=UTC),
            business_date=_MON,
        ))
    _assemble(db_session, ids["emp"])
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == ids["emp"])
    ).scalar_one()
    db_session.add(TimecardAdjustment(
        timecard_id=card.timecard_id, punch_type="clock_out",
        adjusted_at=datetime(2026, 7, 6, 17, tzinfo=UTC), business_date=_MON,
        reason="forgot to clock out", actor_subject="gm",
    ))
    db_session.commit()

    body = _adherence(c, admin, week_id, "2026-07-07").json()
    assert body["exceptions"] == []  # corrected, therefore CLEAN
    [dept] = body["departments"]
    assert dept["days"][0]["punched_hours"] == "7.50"  # the merged timeline


def test_elapsed_days_only(db_engine, db_session, tmp_path):
    """Days >= as_of are absent and raise no exceptions: Mon-Fri scheduled,
    as_of Wednesday -> only Mon/Tue appear (both no_shows); a fully-future
    week (as_of == week_start) -> empty days, empty exceptions, 200."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    for i in range(5):
        d = (_MON + timedelta(days=i)).isoformat()
        _add(c, admin, week_id, _shift(ids, business_date=d))

    body = _adherence(c, admin, week_id, "2026-07-08").json()
    [dept] = body["departments"]
    assert [d["business_date"] for d in dept["days"]] == [
        "2026-07-06", "2026-07-07"
    ]
    assert [(x["code"], x["business_date"]) for x in body["exceptions"]] == [
        ("no_show", "2026-07-06"), ("no_show", "2026-07-07")
    ]
    # as_of past the week end: the window caps at week_end + 1 (7 days).
    late = _adherence(c, admin, week_id, "2026-07-20").json()
    [late_dept] = late["departments"]
    assert len(late_dept["days"]) == 7

    future = _adherence(c, admin, week_id, "2026-07-06").json()
    assert future["departments"] == [] and future["exceptions"] == []


def test_open_shift_counts_hours_but_never_no_shows(
    db_engine, db_session, tmp_path
):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids, employee_id=None))  # open, 7.50

    body = _adherence(c, admin, week_id, "2026-07-07").json()
    assert body["exceptions"] == []  # nobody was assigned — nobody no-shows
    [dept] = body["departments"]
    assert dept["scheduled_total"] == "7.50"
    assert dept["punched_total"] == "0.00"


def test_multi_department_day_splits_punched_proportionally(
    db_engine, db_session, tmp_path
):
    """The attribution choice, pinned: an employee scheduled in TWO
    departments one day has that day's punched hours split proportionally to
    the day's SCHEDULED department split (4.00/4.00 -> 50/50 of 7.50)."""
    ids = _seed_costing(db_session, _seed(db_session))
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids, start_time="09:00", end_time="13:00"))
    _add(c, admin, week_id, _shift(ids, department_id=ids["dept2"],
                                   start_time="14:00", end_time="18:00"))
    _punch_day(db_session, device_id, ids["emp"], _MON)  # 7.50 worked
    _assemble(db_session, ids["emp"])

    body = _adherence(c, admin, week_id, "2026-07-07").json()
    # 8.00 scheduled vs 7.50 punched: 30 min short -> no deviation.
    assert body["exceptions"] == []
    depts = {d["department"]: d for d in body["departments"]}
    assert depts["Front Desk"]["days"][0] == {
        "business_date": "2026-07-06",
        "scheduled_hours": "4.00", "punched_hours": "3.75",
    }
    assert depts["Housekeeping"]["days"][0] == {
        "business_date": "2026-07-06",
        "scheduled_hours": "4.00", "punched_hours": "3.75",
    }


def test_response_carries_no_rate_or_cost(db_engine, db_session, tmp_path):
    """THE MONEY RULE: a rated employee's adherence is hours only — no rate,
    no cost, no money-shaped key anywhere in the response text."""
    ids = _seed_costing(db_session, _seed(db_session))  # Hank's rate: 23.17
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))
    _punch_day(db_session, device_id, ids["emp"], _MON)
    _assemble(db_session, ids["emp"])

    r = _adherence(c, admin, week_id, "2026-07-07")
    assert "23.17" not in r.text
    assert "cost" not in r.text and "rate" not in r.text
    # Hank's day cost (7.50 x 23.17) must not be derivable from the text.
    assert "173.78" not in r.text and "173.77" not in r.text


def test_adherence_confinement(db_engine, db_session, tmp_path):
    """SSSJ's GM 403 (leaking nothing), accountant 403, unauth 401, unknown
    week 404."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))

    r = c.get(f"/api/schedule/weeks/{week_id}/adherence",
              params={"as_of": "2026-07-07"}, headers=_gm(mint, prop="SSSJ"))
    assert r.status_code == 403
    assert "Hank" not in r.text and "HISJ" not in r.text

    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}
    assert c.get(f"/api/schedule/weeks/{week_id}/adherence",
                 params={"as_of": "2026-07-07"}, headers=acct).status_code == 403
    assert c.get(f"/api/schedule/weeks/{week_id}/adherence",
                 params={"as_of": "2026-07-07"}).status_code == 401
    assert c.get("/api/schedule/weeks/999999/adherence",
                 params={"as_of": "2026-07-07"}, headers=admin).status_code == 404

    # The property's own GM sees it, of course.
    assert c.get(f"/api/schedule/weeks/{week_id}/adherence",
                 params={"as_of": "2026-07-07"},
                 headers=_gm(mint, prop="HISJ")).status_code == 200
