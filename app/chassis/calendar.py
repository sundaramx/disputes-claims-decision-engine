"""US federal-holiday business-day calendar, 2025-2027 (observed dates).

Reg E investigation clocks are business-day based; getting this exact is a compliance
property, not a convenience (paper §2). Dates are the *observed* federal holidays.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

_HOLIDAYS: set[date] = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 5, 26),
    date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 10, 13),
    date(2025, 11, 11), date(2025, 11, 27), date(2025, 12, 25),
    # 2026 (Jul 4 falls Saturday -> observed Fri Jul 3)
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12),
    date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25),
    # 2027 (Jun 19 Sat -> Fri Jun 18; Jul 4 Sun -> Mon Jul 5; Dec 25 Sat -> Fri Dec 24;
    #       Jan 1 2028 Sat -> observed Fri Dec 31 2027)
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 10, 11),
    date(2027, 11, 11), date(2027, 11, 25), date(2027, 12, 24), date(2027, 12, 31),
}

_EOD = time(23, 59, 59)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _HOLIDAYS


def add_business_days(start: datetime, n: int) -> datetime:
    """Deadline at end of the nth business day AFTER the start date.

    Reg E convention: a notice received on day 0 gives the institution through the close
    of the nth business day following receipt.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    d = start.date()
    remaining = n
    while remaining > 0:
        d += timedelta(days=1)
        if is_business_day(d):
            remaining -= 1
    return datetime.combine(d, _EOD, tzinfo=start.tzinfo or timezone.utc)


def business_days_between(start: datetime, end: datetime) -> int:
    """Count business days strictly after start.date() up to and including end.date()."""
    if end < start:
        return -business_days_between(end, start)
    d, count = start.date(), 0
    while d < end.date():
        d += timedelta(days=1)
        if is_business_day(d):
            count += 1
    return count


def add_calendar_days(start: datetime, n: int) -> datetime:
    return datetime.combine((start + timedelta(days=n)).date(), _EOD,
                            tzinfo=start.tzinfo or timezone.utc)
