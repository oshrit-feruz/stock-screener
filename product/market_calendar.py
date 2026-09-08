"""Is the US market open on a given day.

One definition, used by both halves of the system: the daily run, which decides
whether to produce a result, and the web service, which decides how far back a
published result may be. Those two must agree — a producer that skips holidays
and a consumer that counts calendar days will disagree about how stale "stale"
is, which is what took /api/screener down after the 2026 Labor Day weekend.
"""
from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


def is_trading_day(day: date) -> bool:
    """True if `day` is an NYSE trading day (weekday and not a market holiday).

    Uses pandas_market_calendars' NYSE calendar. If the library or its data is
    unavailable, falls back to a plain weekday check so a real trading day is
    never silently skipped (a false 'open' is safer than a false 'closed' — the
    downstream screener is itself date-aware).
    """
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(start_date=day.isoformat(), end_date=day.isoformat())
        return not schedule.empty
    except Exception as exc:
        logger.warning("NYSE calendar unavailable (%s); falling back to weekday check", exc)
        return day.weekday() < 5
