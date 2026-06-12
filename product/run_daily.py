#!/usr/bin/env python3
"""Daily run: alert engine + exit tracker for the stock screener.

Entry point to run every trading day (cron or manual).
Loads the user watchlist, runs the alert engine to detect new BUY signals,
runs the exit tracker for open positions, and prints a daily summary.

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

from product.alerts.alert_engine import AlertEngine, Alert
from product.exit.exit_tracker import ExitTracker, ExitAlert

_WATCHLIST_FILE = Path(__file__).parent.parent / "data" / "user_watchlist.json"
_ALERTS_DIR     = Path(__file__).parent.parent / "data" / "alerts"

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def load_watchlist() -> list[str]:
    if not _WATCHLIST_FILE.exists():
        print(f"[WARN] Watchlist file not found at {_WATCHLIST_FILE}. Using default.")
        return ["NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA", "CRM", "ADBE", "ORCL"]
    with open(_WATCHLIST_FILE) as fh:
        data = json.load(fh)
    return data.get("watchlist", [])


def save_alerts(today: date, alerts: list[Alert], exit_alerts: list[ExitAlert]) -> None:
    _ALERTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = today.isoformat()

    if alerts:
        path = _ALERTS_DIR / f"{date_str}_alerts.json"
        payload = []
        for a in alerts:
            payload.append({
                "ticker":          a.ticker,
                "entry_date":      a.entry_date.isoformat(),
                "entry_price":     a.entry_price,
                "drawdown_pct":    a.drawdown_pct,
                "composite_score": a.composite_score,
                "dip_score":       a.dip_score,
                "momentum_score":  a.momentum_score,
                "volume_score":    a.volume_score,
                "gate_passed":     a.gate_passed,
                "signal_type":     a.signal_type,
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


def main() -> None:
    today     = date.today()
    watchlist = load_watchlist()

    print(f"\nDAILY RUN  --  {today}")
    print(f"Watchlist: {', '.join(watchlist)}")
    print()

    # Step 1: Alert engine
    print("Running alert engine...")
    engine = AlertEngine()
    engine_result = engine.run_daily_alert_check(watchlist, as_of_date=today)

    # Step 2: Exit tracker
    print("Running exit tracker...")
    tracker = ExitTracker()
    # Build current prices from screener result for the exit tracker
    # (open positions may include tickers outside the watchlist)
    from product.screener.daily_screener import run_screener
    screener_result = run_screener(as_of_date=today)
    current_prices = {r.ticker: r.current_price for r in screener_result.full_ranking}
    exit_alerts = tracker.check_exits(today, current_prices=current_prices)

    # Step 3: Print new alerts
    if engine_result.new_alerts:
        for alert in engine_result.new_alerts:
            print_alert(alert)
    else:
        print("No new BUY signals detected today.")

    # Step 4: Print exit alerts
    if exit_alerts:
        for ea in exit_alerts:
            print_exit_alert(ea)
    else:
        print("No exit alerts today.")

    # Step 5: Save to disk
    save_alerts(today, engine_result.new_alerts, exit_alerts)

    # Step 6: Daily summary
    open_count = len(json.loads(
        (Path(__file__).parent.parent / "data" / "positions" / "open_positions.json").read_text()
    ) if (Path(__file__).parent.parent / "data" / "positions" / "open_positions.json").exists() else "[]")

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
    print(f"Positions tracked:   {open_count}")
    print("=" * 72)


if __name__ == "__main__":
    main()
