"""Was the US market open on a given day.

One definition, used by both halves of the system: the daily run, which decides
whether to produce a result, and the web service, which decides how far back a
published result may be. Those two must agree — a producer that skips holidays
and a consumer that counts calendar days will disagree about how stale "stale"
is, which is what took /api/screener down after the 2026 Labor Day weekend.

pandas_market_calendars is the source of truth. It is in requirements.txt, so a
healthy deploy always has it; the rule-based fallback below covers a broken
install, and it has to get holidays right rather than merely weekends. The two
callers fail in opposite directions, so "close enough" is not available:
  * the producer treats a false 'open' as harmless — it runs the screener on a
    closed day and the screener is itself date-aware;
  * the consumer treats a false 'open' as a lost lookback slot — it spends one
    of its four chances on a day that could never have published, which is the
    exact failure this module exists to prevent.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from functools import lru_cache

logger = logging.getLogger(__name__)

MONDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = 0, 3, 4, 5, 6


def is_trading_day(day: date) -> bool:
    """True if `day` is an NYSE trading day (a weekday that is not a holiday)."""
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(start_date=day.isoformat(), end_date=day.isoformat())
        return not schedule.empty
    except Exception as exc:
        logger.warning("NYSE calendar unavailable (%s); using the rule-based fallback", exc)
        return day.weekday() < SATURDAY and day not in nyse_holidays(day.year)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` of a month (n is 1-based; weekday is Mon=0)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last `weekday` of a month."""
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Easter Sunday, by the anonymous Gregorian algorithm.

    Needed only for Good Friday, the one NYSE holiday with no fixed-date or
    n-th-weekday rule.
    """
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month = (h + lam - 7 * m + 114) // 31
    dayval = ((h + lam - 7 * m + 114) % 31) + 1
    return date(year, month, dayval)


def _observed(day: date) -> date | None:
    """Where a fixed-date holiday actually falls when it lands on a weekend.

    Saturday moves back to Friday, Sunday forward to Monday — the NYSE's own
    rule, and the reason July 4th can close the market on the 3rd or the 5th.
    """
    if day.weekday() == SATURDAY:
        return day - timedelta(days=1)
    if day.weekday() == SUNDAY:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=32)
def nyse_holidays(year: int) -> frozenset[date]:
    """The NYSE's full-day holidays for `year`, by rule.

    Half-days (the early closes around Thanksgiving and Christmas) are absent
    on purpose: the market IS open, so a result can be published, and both
    callers only ask whether it was open at all.

    Unscheduled closures — a national day of mourning, a hurricane — cannot be
    derived from a rule and are not here. Those stay a false 'open', which is
    the harmless direction for the producer and costs the consumer at most one
    lookback slot out of four.
    """
    days = {
        _nth_weekday(year, 1, MONDAY, 3),        # Martin Luther King Jr. Day
        _nth_weekday(year, 2, MONDAY, 3),        # Washington's Birthday
        _easter(year) - timedelta(days=2),       # Good Friday
        _last_weekday(year, 5, MONDAY),          # Memorial Day
        _nth_weekday(year, 9, MONDAY, 1),        # Labor Day
        _nth_weekday(year, 11, THURSDAY, 4),     # Thanksgiving
    }

    # New Year's Day is the one exception to the observance rule: when Jan 1 is
    # a Saturday the NYSE does NOT close the preceding Friday, because that
    # Friday belongs to the previous year and was a full trading day.
    new_year = date(year, 1, 1)
    if new_year.weekday() != SATURDAY:
        days.add(_observed(new_year))

    for fixed in (date(year, 7, 4), date(year, 12, 25)):
        days.add(_observed(fixed))

    # Juneteenth became an NYSE holiday in 2022.
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))

    return frozenset(days)
