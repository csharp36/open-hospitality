"""Schedule projection engine (Pillar D1) — PURE, no database.

Converts a week's draft shifts into per-employee day-hours (shift span minus an
assumed unpaid meal on long shifts, mirroring how such a day actually punches)
and feeds the EXISTING California overtime engine. Schedule weeks sit on the
payroll workweek's Monday grid, so weekly-40 and 7th-consecutive-day projection
are exact within one week.

Warnings FLAG, never block — a 9-hour scheduled day may be deliberate; the GM
decides, the system makes the cost visible. Warnings speak in HOURS, never
money: per-employee cost is never computed here (a GM must not learn
rate = cost / hours; cost aggregation happens API-side at department level).

Clopening rule: rest under the floor between an employee's consecutive shifts
warns ONLY when the pair spans a night — the shifts sit on different business
dates, or the rest interval itself crosses a calendar midnight. A same-day
split shift (breakfast + dinner service with a short afternoon gap) is normal
hotel practice and does NOT warn: the rest floor protects overnight rest, not
intra-day gaps. Overlapping shifts (negative rest) are the API's 422 to raise;
the engine simply skips them.

Cross-week context (D3): `context_shifts` — adjacent weeks' shifts — join each
employee's rest-pairing sequence (sorted globally by span start) but contribute
NOTHING to hours, OT, department_hours, or employee rows. A warning is emitted
only when the SECOND shift of a sub-floor night-spanning pair is in-week; the
mirror-image warning belongs to the other week's projection.

Actual-hours merge (D3): `actual_hours` (per employee, per date) REPLACES the
schedule-derived day hours in the OT math — including days with no shifts at
all (worked-unscheduled counts toward weekly OT). `scheduled_overtime` warning
hours follow the merged math; clopening and the seventh_day warning stay
schedule-derived (rest planning is about the plan). department_hours also stay
schedule-derived — they describe the plan.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from usali.overtime import DayOvertime, compute_overtime
from usali.overtime_rules import OvertimeRules

_HOUR_Q = Decimal("0.01")


@dataclass(frozen=True)
class ScheduledShift:
    business_date: date
    department_id: int
    start_time: time
    end_time: time
    crosses_midnight: bool
    employee_id: int | None  # None = open shift


@dataclass(frozen=True)
class ProjectionWarning:
    code: str  # "scheduled_overtime" | "clopening" | "seventh_day"
    employee_id: int
    business_date: date
    hours: Decimal  # OT hours for scheduled_overtime; 0 otherwise


@dataclass(frozen=True)
class EmployeeProjection:
    employee_id: int
    total_hours: Decimal
    regular_hours: Decimal
    ot_hours: Decimal  # ot + dt combined for display; warnings carry the split
    # The per-day reg/OT/DT rows the totals were derived from, chronological.
    # Carried so the API can PRICE the split (reg 1x, OT 1.5x, DT 2x) without
    # re-deriving overtime — the engine stays the single source of the split.
    day_rows: tuple[DayOvertime, ...]


@dataclass(frozen=True)
class WeekProjection:
    employees: list[EmployeeProjection]
    warnings: list[ProjectionWarning]
    department_hours: dict[int, Decimal] = field(default_factory=dict)


def _span(shift: ScheduledShift) -> tuple[datetime, datetime]:
    start = datetime.combine(shift.business_date, shift.start_time)
    end_date = (
        shift.business_date + timedelta(days=1)
        if shift.crosses_midnight
        else shift.business_date
    )
    return start, datetime.combine(end_date, shift.end_time)


def shift_hours(
    shift: ScheduledShift, *, meal_threshold_hours: int, meal_deduction_minutes: int
) -> Decimal:
    """Worked hours a shift projects to: span minus the assumed unpaid meal.

    A span of EXACTLY the threshold does not deduct — only strictly longer
    shifts assume a meal (a 6h shift punches 6h; a 6h01m shift punches with
    a 30-minute meal out).
    """
    start, end = _span(shift)
    minutes = Decimal(int((end - start).total_seconds() // 60))
    if minutes > meal_threshold_hours * 60:
        minutes -= meal_deduction_minutes
    return (minutes / 60).quantize(_HOUR_Q)


def shifts_overlap(a: ScheduledShift, b: ScheduledShift) -> bool:
    """True when the spans intersect; back-to-back (end == start) is NOT overlap."""
    (a0, a1), (b0, b1) = _span(a), _span(b)
    return a0 < b1 and b0 < a1


def _spans_a_night(prev: ScheduledShift, nxt: ScheduledShift) -> bool:
    """The rest gap between prev and nxt spans a night (see module docstring)."""
    if prev.business_date != nxt.business_date:
        return True
    return _span(prev)[1].date() != _span(nxt)[0].date()


def project_week(
    shifts: list[ScheduledShift],
    *,
    week_start: date,
    exempt_employee_ids: set[int],
    rules: OvertimeRules,
    meal_threshold_hours: int,
    meal_deduction_minutes: int,
    min_rest_hours: int,
    context_shifts: list[ScheduledShift] | None = None,
    actual_hours: Mapping[int, Mapping[date, Decimal]] | None = None,
) -> WeekProjection:
    department_hours: dict[int, Decimal] = {}
    by_employee: dict[int, list[ScheduledShift]] = {}
    for s in shifts:
        h = shift_hours(
            s,
            meal_threshold_hours=meal_threshold_hours,
            meal_deduction_minutes=meal_deduction_minutes,
        )
        department_hours[s.department_id] = (
            department_hours.get(s.department_id, Decimal("0")) + h
        )
        if s.employee_id is not None:
            by_employee.setdefault(s.employee_id, []).append(s)

    context_by_employee: dict[int, list[ScheduledShift]] = {}
    for s in context_shifts or []:
        if s.employee_id is not None:
            context_by_employee.setdefault(s.employee_id, []).append(s)

    employees: list[EmployeeProjection] = []
    warnings: list[ProjectionWarning] = []
    # An employee present only in actual_hours worked entirely unscheduled —
    # the merged week must still surface those hours. Context alone never
    # creates a row.
    for employee_id in sorted(set(by_employee) | set(actual_hours or {})):
        emp_shifts = sorted(by_employee.get(employee_id, []), key=lambda s: _span(s)[0])
        day_hours: dict[date, Decimal] = {}
        for s in emp_shifts:
            h = shift_hours(
                s,
                meal_threshold_hours=meal_threshold_hours,
                meal_deduction_minutes=meal_deduction_minutes,
            )
            day_hours[s.business_date] = day_hours.get(s.business_date, Decimal("0")) + h
        if actual_hours is not None:
            # REPLACE (never add to) the derived hours where an actual exists;
            # a date absent from the schedule is inserted — worked-unscheduled
            # counts toward the weekly OT math.
            for d, h in actual_hours.get(employee_id, {}).items():
                day_hours[d] = h
        if not day_hours:
            continue

        rows = compute_overtime(
            day_hours, anchor=week_start,
            exempt=employee_id in exempt_employee_ids, rules=rules,
        )
        reg = sum((r.regular_hours for r in rows), Decimal("0"))
        ot = sum((r.ot_hours + r.dt_hours for r in rows), Decimal("0"))
        employees.append(
            EmployeeProjection(
                employee_id=employee_id,
                total_hours=(reg + ot).quantize(_HOUR_Q),
                regular_hours=reg.quantize(_HOUR_Q),
                ot_hours=ot.quantize(_HOUR_Q),
                day_rows=tuple(rows),
            )
        )
        for r in rows:
            if r.ot_hours + r.dt_hours > 0:
                warnings.append(
                    ProjectionWarning(
                        code="scheduled_overtime",
                        employee_id=employee_id,
                        business_date=r.business_date,
                        hours=(r.ot_hours + r.dt_hours).quantize(_HOUR_Q),
                    )
                )
        if len({s.business_date for s in emp_shifts}) == 7:
            warnings.append(
                ProjectionWarning(
                    code="seventh_day",
                    employee_id=employee_id,
                    business_date=week_start + timedelta(days=6),
                    hours=Decimal("0"),
                )
            )
        # Rest pairing merges context shifts, sorted globally by span start.
        # (shift, is_context) tuples keep identity even when a context shift
        # and an in-week shift share a business date.
        sequence = sorted(
            [(s, False) for s in emp_shifts]
            + [(s, True) for s in context_by_employee.get(employee_id, [])],
            key=lambda pair: _span(pair[0])[0],
        )
        for (prev, _), (nxt, nxt_is_context) in zip(sequence, sequence[1:], strict=False):
            if nxt_is_context:
                # The pair's second shift lives in another week — the warning
                # belongs to THAT week's projection, not this one.
                continue
            rest = _span(nxt)[0] - _span(prev)[1]
            if (
                timedelta(0) <= rest < timedelta(hours=min_rest_hours)
                and _spans_a_night(prev, nxt)
            ):
                warnings.append(
                    ProjectionWarning(
                        code="clopening",
                        employee_id=employee_id,
                        business_date=nxt.business_date,
                        hours=Decimal("0"),
                    )
                )

    return WeekProjection(
        employees=employees, warnings=warnings, department_hours=department_hours
    )
