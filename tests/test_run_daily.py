"""Unit tests for product/run_daily.py — the daily scheduling wrapper.

Covers the NYSE trading-day guard, the graceful weekend/holiday skip, and the
fail-non-zero-on-error contract. No network calls (the alert engine is mocked
away on the paths that would otherwise reach it).
"""
from datetime import date
from unittest.mock import MagicMock

import product.run_daily as rd


# ── NYSE trading-day guard ──────────────────────────────────────────────────

def test_is_trading_day_regular_weekday():
    assert rd.is_trading_day(date(2025, 1, 2)) is True     # Thursday, market open


def test_is_trading_day_weekend():
    assert rd.is_trading_day(date(2025, 1, 4)) is False     # Saturday


def test_is_trading_day_new_year_holiday():
    assert rd.is_trading_day(date(2025, 1, 1)) is False     # New Year's Day


def test_is_trading_day_christmas_holiday():
    assert rd.is_trading_day(date(2025, 12, 25)) is False   # Christmas


def test_is_trading_day_calendar_unavailable_falls_back_to_weekday(monkeypatch):
    # Force the pandas_market_calendars import to fail inside is_trading_day.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pandas_market_calendars":
            raise ImportError("simulated missing calendar lib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert rd.is_trading_day(date(2025, 1, 2)) is True      # weekday → open
    assert rd.is_trading_day(date(2025, 1, 4)) is False     # weekend → closed


# ── skip path: no engine work on a closed day ───────────────────────────────

def test_run_skips_on_non_trading_day(monkeypatch):
    monkeypatch.setattr(rd, "is_trading_day", lambda d: False)
    engine_cls = MagicMock(side_effect=AssertionError("engine must not run on a closed day"))
    monkeypatch.setattr(rd, "AlertEngine", engine_cls)
    assert rd.run(date(2025, 1, 1)) == 0
    engine_cls.assert_not_called()


# ── failure contract: non-zero exit + traceback logged ──────────────────────

def test_main_returns_nonzero_on_unhandled_exception(monkeypatch, caplog):
    monkeypatch.setattr(rd, "run", MagicMock(side_effect=RuntimeError("boom")))
    with caplog.at_level("ERROR"):
        rc = rd.main()
    assert rc == 1
    assert any("FAILED" in r.message for r in caplog.records)


def test_main_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr(rd, "run", lambda today: 0)
    assert rd.main() == 0
