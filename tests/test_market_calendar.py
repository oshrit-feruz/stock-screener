"""The shared NYSE calendar, and specifically its fallback.

pandas_market_calendars is the source of truth, but it is an optional import:
when it is missing, is_trading_day answers from rules instead. Those rules are
what these tests pin, in two layers.

The nyse_holidays tests call the rule table directly. It does not consult the
library at all, so there is nothing to force — the table either reproduces the
exchange's published calendar or it does not.

Every is_trading_day test forces the fallback path, because that function does
prefer the library: run against it, those tests would pass on its answers and
prove nothing about the code that runs when it is gone.

The fallback used to be a plain weekday check. That was written for the daily
run, where calling a holiday "open" costs nothing: the screener runs and is
date-aware. /api/screener then took the same predicate and counts its lookback
window with it, where calling a holiday "open" spends one of four chances to
find a published result on a day that could never have carried one — the exact
failure the trading-day window exists to prevent.
"""
from __future__ import annotations

import builtins
from datetime import date

import pytest

from product.market_calendar import is_trading_day, nyse_holidays


@pytest.fixture
def _no_calendar_lib(monkeypatch):
    """Force the pandas_market_calendars import inside is_trading_day to fail."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pandas_market_calendars":
            raise ImportError("simulated missing calendar lib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


# ── the rules, against the NYSE's own published calendars ───────────────────

# Every full-day closure the NYSE published for these years. 2022 and 2027 are
# here for their weekend cases, not for coverage: 2022 opens with Jan 1 on a
# Saturday, and 2027 has both Christmas on a Saturday and July 4th on a Sunday.
_PUBLISHED = {
    2022: ["2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
           "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26"],
    2025: ["2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
           "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"],
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
           "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
           "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
}


@pytest.mark.parametrize("year", sorted(_PUBLISHED))
def test_the_rules_reproduce_the_published_holiday_calendar(year):
    """Exact set equality, not containment: an extra day is as wrong as a
    missing one, because each false holiday closes a day the market traded."""
    expected = {date.fromisoformat(d) for d in _PUBLISHED[year]}
    assert nyse_holidays(year) == expected


def test_new_years_day_on_a_saturday_is_not_observed():
    """The one exception to the weekend-shift rule: the NYSE does not close the
    preceding Friday, because that Friday is a full trading day of the year
    before. 2022 opened this way — Dec 31 2021 traded."""
    assert date(2021, 12, 31) not in nyse_holidays(2022)
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert date(2022, 1, 1) not in nyse_holidays(2022)


def test_a_fixed_holiday_on_a_weekend_shifts():
    """Saturday back to Friday, Sunday forward to Monday."""
    assert date(2026, 7, 3) in nyse_holidays(2026)    # Jul 4 2026 is a Saturday
    assert date(2027, 7, 5) in nyse_holidays(2027)    # Jul 4 2027 is a Sunday
    assert date(2027, 12, 24) in nyse_holidays(2027)  # Dec 25 2027 is a Saturday


def test_juneteenth_only_counts_from_2022():
    """It became an NYSE holiday in 2022; before that the market traded."""
    assert date(2021, 6, 18) not in nyse_holidays(2021)
    assert date(2022, 6, 20) in nyse_holidays(2022)


def test_good_friday_tracks_easter():
    """The only holiday with neither a fixed date nor an n-th-weekday rule."""
    assert date(2026, 4, 3) in nyse_holidays(2026)
    assert date(2027, 3, 26) in nyse_holidays(2027)


# ── what the fallback answers ───────────────────────────────────────────────

def test_fallback_closes_on_a_holiday(_no_calendar_lib):
    """The regression this module was rewritten for: Labor Day 2026 is a
    Monday, and a weekday-only fallback called it open. /api/screener would
    then have spent a lookback slot on a day with nothing to find."""
    assert is_trading_day(date(2026, 9, 7)) is False


def test_fallback_opens_on_an_ordinary_weekday(_no_calendar_lib):
    """The control for the holiday case above — without it, a fallback that
    simply answered False to everything would pass that test."""
    assert is_trading_day(date(2026, 9, 8)) is True


def test_fallback_closes_on_a_weekend(_no_calendar_lib):
    """The one thing the old weekday-only fallback did get right; the holiday
    rules are layered on top of it, not in place of it."""
    assert is_trading_day(date(2026, 9, 5)) is False
    assert is_trading_day(date(2026, 9, 6)) is False


def test_fallback_opens_on_a_half_day(_no_calendar_lib):
    """Early closes are not holidays. The market is open on the day after
    Thanksgiving, so a result can be published and the day must count."""
    assert is_trading_day(date(2026, 11, 27)) is True
