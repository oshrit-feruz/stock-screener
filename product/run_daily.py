#!/usr/bin/env python3
"""Daily run: alert engine + exit tracker for the stock screener.

Entry point to run every trading day (Render cron or manual). Scans the
point-in-time Top-100 universe for new BUY signals, runs the exit tracker for
open positions, and checks portfolio price alerts.

Scheduling behaviour:
  * Skips gracefully (exit 0) on weekends and US market holidays, using the
    NYSE calendar from pandas_market_calendars.
  * Logs a structured start and end summary (timestamp, tickers scanned,
    signals found, positions opened).
  * On any unhandled error, logs the full traceback and exits non-zero so
    Render marks the run as failed.

Usage:
    python product/run_daily.py
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from product.alerts.alert_engine import Alert, AlertEngine, PortfolioAlert
from product.exit.exit_tracker import ExitAlert, ExitTracker

_ALERTS_DIR      = Path(__file__).parent.parent / "data" / "alerts"
_PORTFOLIO_FILE  = Path(__file__).parent.parent / "data" / "portfolio" / "portfolio.json"
_OPEN_POS_FILE   = Path(__file__).parent.parent / "data" / "positions" / "open_positions.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_daily")


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


def load_portfolio() -> list:
    if not _PORTFOLIO_FILE.exists():
        return []
    try:
        data = json.loads(_PORTFOLIO_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_alerts(today: date, alerts: list[Alert], exit_alerts: list[ExitAlert]) -> None:
    _ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = today.isoformat()

    if alerts:
        path = _ALERTS_DIR / f"{date_str}_alerts.json"
        payload = []
        for a in alerts:
            payload.append({
                "ticker":            a.ticker,
                "entry_date":        a.entry_date.isoformat(),
                "entry_price":       a.entry_price,
                "drawdown_pct":      a.drawdown_pct,
                "composite_score":   a.composite_score,
                "dip_score":         a.dip_score,
                "momentum_score":    a.momentum_score,
                "volume_score":      a.volume_score,
                "gate_passed":       a.gate_passed,
                "signal_type":       a.signal_type,
                "formatted_message": a.formatted_message,
            })
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        logger.info("Saved %d alert(s) -> %s", len(alerts), path)

    if exit_alerts:
        path = _ALERTS_DIR / f"{date_str}_exits.json"
        payload = []
        for e in exit_alerts:
            payload.append({
                "ticker":            e.ticker,
                "entry_date":        e.entry_date.isoformat(),
                "exit_date":         e.exit_date.isoformat(),
                "entry_price":       e.entry_price,
                "current_price":     e.current_price,
                "realized_return":   e.realized_return,
                "days_held":         e.days_held,
                "is_win":            e.is_win,
                "alert_type":        e.alert_type,
                "formatted_message": e.formatted_message,
            })
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        logger.info("Saved %d exit alert(s) -> %s", len(exit_alerts), path)


def _log_alert(a: Alert) -> None:
    logger.info("NEW BUY SIGNAL — %s — %s", a.ticker, a.entry_date)
    logger.info(a.formatted_message)


def _log_exit_alert(e: ExitAlert) -> None:
    logger.info("%s ALERT — %s — %s", e.alert_type, e.ticker, e.exit_date)
    logger.info(e.formatted_message)


def _log_portfolio_alert(a: PortfolioAlert) -> None:
    logger.info("PORTFOLIO %s — %s", a.alert_type, a.ticker)
    logger.info(a.body)


def run(today: date) -> int:
    """Execute one daily run for `today`. Returns a process exit code."""
    started = datetime.now(timezone.utc)
    logger.info("Daily run starting — %s (%s UTC)", today, started.strftime("%Y-%m-%d %H:%M:%S"))

    if not is_trading_day(today):
        logger.info("Market closed on %s (weekend or NYSE holiday) — skipping run.", today)
        return 0

    portfolio = load_portfolio()
    logger.info("Portfolio holdings: %d ticker(s)", len(portfolio))

    # Step 1: Alert engine (scans the point-in-time Top-100 universe).
    logger.info("Running alert engine...")
    engine        = AlertEngine()
    engine_result = engine.run_daily_alert_check(as_of_date=today)

    # Step 2: Exit tracker — reuse the screener result from the alert engine
    # instead of re-running the screener (it already ran inside step 1).
    logger.info("Running exit tracker...")
    tracker = ExitTracker()
    screener_result = engine_result.screener_result
    if screener_result is not None:
        current_prices = {r.ticker: r.current_price for r in screener_result.full_ranking}
    else:
        from product.screener.daily_screener import run_screener
        current_prices = {
            r.ticker: r.current_price
            for r in run_screener(as_of_date=today).full_ranking
        }
    exit_alerts = tracker.check_exits(today, current_prices=current_prices)

    # Step 3: Portfolio alerts
    portfolio_alerts: list[PortfolioAlert] = []
    if portfolio:
        logger.info("Running portfolio alert check...")
        portfolio_alerts = engine.portfolio_alert_check(portfolio, as_of_date=today)

    # Step 4: Per-signal disposition ----------------------------------------
    # The scheduled run SURFACES new BUY signals (it does not itself open
    # positions — that is a human-in-the-loop action via the /positions API,
    # which logs "Position opened: ...", so n_opened stays 0 here). The 8-K veto
    # runs inside the screener: a would-be BUY that carries a recent distress
    # filing is downgraded to "VETO" and logged there ("8-K veto: ..."); those
    # tickers never reach new_alerts. We surface the count in the summary.
    n_signals = len(engine_result.new_alerts)
    for a in engine_result.new_alerts:
        logger.info(f"Signal: {a.ticker} score={a.composite_score:.2f} dip={a.drawdown_pct:.1%}")
        _log_alert(a)
    if not engine_result.new_alerts:
        logger.info("No new BUY signals detected today.")

    opened: list[tuple[str, float, float]] = []   # (ticker, entry_price, alloc)
    for tkr, entry_price, alloc in opened:
        logger.info("Position opened: %s entry=%.2f alloc=%.0f", tkr, entry_price, alloc)
    n_opened = len(opened)

    n_vetoed = len(screener_result.vetoed) if screener_result is not None else 0

    # Step 5: Exit + portfolio alerts
    for ea in exit_alerts:
        _log_exit_alert(ea)
    if not exit_alerts:
        logger.info("No exit alerts today.")
    for pa in portfolio_alerts:
        _log_portfolio_alert(pa)
    if portfolio and not portfolio_alerts:
        logger.info("No portfolio alerts today.")

    # Step 6: Save to disk
    save_alerts(today, engine_result.new_alerts, exit_alerts)

    # Step 7: Structured end summary
    try:
        open_count = len(json.loads(_OPEN_POS_FILE.read_text())) if _OPEN_POS_FILE.exists() else 0
    except Exception:
        open_count = 0

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    # The canonical "Daily screener complete — N signals, ... vetoed" line is
    # emitted inside the screener itself; here we log the richer run-level summary.
    logger.info(
        "Daily run complete — %s | tickers scanned=%d | new signals=%d | "
        "positions opened=%d | vetoed=%d | continuing=%d | dropped=%d | "
        "exit alerts=%d | portfolio alerts=%d | positions tracked=%d | %.1fs",
        today, engine_result.tickers_scanned, n_signals, n_opened, n_vetoed,
        len(engine_result.continuing_signals), len(engine_result.dropped_signals),
        len(exit_alerts), len(portfolio_alerts), open_count, elapsed,
    )
    return 0


def main() -> int:
    """Cron entry point. Catches any unhandled error, logs the full traceback,
    and returns a non-zero exit code so Render marks the run as failed."""
    try:
        return run(date.today())
    except Exception:
        logger.error("Daily run FAILED with an unhandled exception:\n%s", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
