from datetime import UTC, date, datetime

import pytest

from usali.attendance import business_date_for

_TZ = "America/Los_Angeles"


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_midday_punch_is_same_business_date():
    # 2026-07-07 12:00 local (19:00 UTC, PDT = UTC-7)
    assert business_date_for(_utc(2026, 7, 7, 19), _TZ) == date(2026, 7, 7)


def test_punch_before_cutoff_belongs_to_prior_business_date():
    # 2026-07-08 01:00 local (08:00 UTC) — night shift → prior business day.
    assert business_date_for(_utc(2026, 7, 8, 8), _TZ) == date(2026, 7, 7)


def test_punch_exactly_at_cutoff_is_the_new_business_date():
    # 2026-07-08 04:00 local (11:00 UTC) — at the cutoff, not before it.
    assert business_date_for(_utc(2026, 7, 8, 11), _TZ) == date(2026, 7, 8)


def test_late_evening_punch_is_same_business_date():
    # 2026-07-07 23:30 local (2026-07-08 06:30 UTC).
    assert business_date_for(_utc(2026, 7, 8, 6, 30), _TZ) == date(2026, 7, 7)


def test_cutoff_hour_is_configurable():
    # With a 06:00 cutoff, a 05:00-local punch is still the prior business day.
    assert business_date_for(_utc(2026, 7, 8, 12), _TZ, cutoff_hour=6) == date(2026, 7, 7)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        business_date_for(datetime(2026, 7, 8, 8), _TZ)
