#!/usr/bin/env python3
"""Daily run: alert engine + exit tracker for the stock screener.

Entry point to run every trading day (cron or manual).
Runs the alert engine across all 50 tickers to detect new BUY signals,
runs the exit tracker for open positions, and checks portfolio price alerts.

Usage:
    python product/run_daily.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from product.alerts.alert_engine import Alert, AlertEngine, PortfolioAlert
from product.exit.exit_tracker import ExitAlert, ExitTracker

_ALERTS_DIR      = Path(__file__).parent.parent / "data" / "alerts"
_PORTFOLIO_FILE  = Path(__file__).parent.parent / "data" / "portfolio" / "portfolio.json"
_OPEN_POS_FILE   = Path(__file__).parent.parent / "data" / "positions" / "open_positions.json"

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


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
        print(f"[SAVED] {len(alerts)} alert(s) -> {path}")

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
        print(f"[SAVED] {len(exit_alerts)} exit alert(s) -> {path}")


def print_alert(a: Alert) -> None:
    sep = "=" * 72
    print()
    print(sep)
    print(f"NEW BUY SIGNAL  --  {a.ticker}  --  {a.entry_date}")
    print(sep)
    print(a.formatted_message)
    print(sep)


def print_exit_alert(e: ExitAlert) -> None:
    sep = "-" * 72
    print()
    print(sep)
    print(f"{e.alert_type} ALERT  --  {e.ticker}  --  {e.exit_date}")
    print(sep)
    print(e.formatted_message)
    print(sep)


def print_portfolio_alert(a: PortfolioAlert) -> None:
    sep = "~" * 72
    print()
    print(sep)
    print(f"PORTFOLIO {a.alert_type}  --  {a.ticker}")
    print(sep)
    print(a.body)
    print(sep)


def main() -> None:
    today     = date.today()
    portfolio = load_portfolio()

    print(f"\nDAILY RUN  --  {today}")
    print(f"Portfolio holdings: {len(portfolio)} ticker(s)")
    print()

    # Step 1: Alert engine (full 50-ticker universe)
    print("Running alert engine...")
    engine        = AlertEngine()
    engine_result = engine.run_daily_alert_check(as_of_date=today)

    # Step 2: Exit tracker
    print("Running exit tracker...")
    tracker = ExitTracker()
    from product.screener.daily_screener import run_screener
    screener_result = run_screener(as_of_date=today)
    current_prices  = {r.ticker: r.current_price for r in screener_result.full_ranking}
    exit_alerts     = tracker.check_exits(today, current_prices=current_prices)

    # Step 3: Portfolio alerts
    portfolio_alerts: list[PortfolioAlert] = []
    if portfolio:
        print("Running portfolio alert check...")
        portfolio_alerts = engine.portfolio_alert_check(portfolio, as_of_date=today)

    # Step 4: Print new BUY alerts
    if engine_result.new_alerts:
        for alert in engine_result.new_alerts:
            print_alert(alert)
    else:
        print("No new BUY signals detected today.")

    # Step 5: Print exit alerts
    if exit_alerts:
        for ea in exit_alerts:
            print_exit_alert(ea)
    else:
        print("No exit alerts today.")

    # Step 6: Print portfolio alerts
    if portfolio_alerts:
        for pa in portfolio_alerts:
            print_portfolio_alert(pa)
    elif portfolio:
        print("No portfolio alerts today.")

    # Step 7: Save to disk
    save_alerts(today, engine_result.new_alerts, exit_alerts)

    # Step 8: Daily summary
    try:
        open_count = len(json.loads(_OPEN_POS_FILE.read_text())) if _OPEN_POS_FILE.exists() else 0
    except Exception:
        open_count = 0

    print()
    print("=" * 72)
    print(f"Daily run complete -- {today}")
    print(f"Tickers scanned:     {engine_result.tickers_scanned}")
    print(f"New BUY signals:     {len(engine_result.new_alerts)}")
    print(f"Continuing signals:  {len(engine_result.continuing_signals)}"
          + (f"  ({', '.join(engine_result.continuing_signals)})" if engine_result.continuing_signals else ""))
    print(f"Dropped signals:     {len(engine_result.dropped_signals)}"
          + (f"  ({', '.join(engine_result.dropped_signals)})" if engine_result.dropped_signals else ""))
    print(f"Exit alerts:         {len(exit_alerts)}")
    print(f"Portfolio alerts:    {len(portfolio_alerts)}")
    print(f"Positions tracked:   {open_count}")
    print("=" * 72)


if __name__ == "__main__":
    main()
