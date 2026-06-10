#!/usr/bin/env python3
"""
Smoke test for EdgarFundamentals.

Fetches EDGAR snapshots for AAPL, MSFT, NVDA at three historical dates,
verifies point-in-time correctness (filed_date ≤ as_of - 90d), and
cross-checks 2022-12-31 values against yfinance for AAPL.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import FundamentalSnapshot, PointInTimeFundamentals

_TICKERS   = ["AAPL", "MSFT", "NVDA"]
_SNAP_DATES = [date(2018, 12, 31), date(2020, 12, 31), date(2022, 12, 31)]
_LAG        = 90


def _fmt(val: float | None, pct: bool = False) -> str:
    if val is None:
        return "   N/A"
    if pct:
        return f"{val:+.1%}"
    return f"{val:.2f}"


def main() -> None:
    edgar    = EdgarFundamentals()
    yfinance = PointInTimeFundamentals()

    print("EDGAR Snapshot Table")
    print("=" * 90)
    header = f"{'Ticker':<6} {'As-Of':>12} {'Filed':>12} {'RevGrowth':>10} {'D/E':>7} {'ROE':>7} {'NMgn':>7}  PT-OK?"
    print(header)
    print("-" * 90)

    lag_failures = 0
    for ticker in _TICKERS:
        for as_of in _SNAP_DATES:
            snap = edgar.get_snapshot(ticker, as_of)
            if snap is None:
                print(f"{ticker:<6} {str(as_of):>12}   {'[no data]':<60}")
                continue

            cutoff = as_of - timedelta(days=_LAG)
            pt_ok = snap.filed_date is not None and snap.filed_date <= cutoff
            if not pt_ok and snap.filed_date is not None:
                lag_failures += 1

            print(
                f"{ticker:<6} {str(as_of):>12} {str(snap.filed_date or 'N/A'):>12}"
                f" {_fmt(snap.revenue_growth_yoy, pct=True):>10}"
                f" {_fmt(snap.debt_to_equity):>7}"
                f" {_fmt(snap.roe):>7}"
                f" {_fmt(snap.net_margin):>7}"
                f"  {'OK' if pt_ok else 'FAIL'}"
            )

    print("-" * 90)
    if lag_failures:
        print(f"WARNING: {lag_failures} point-in-time violations detected!")
    else:
        print("Point-in-time check: all filed_dates are within the 90-day lag. OK")

    # ── Cross-check all 3 tickers at 2022-12-31 ──────────────────────────
    print()
    print("Cross-check at 2022-12-31: EDGAR vs yfinance")
    print("=" * 80)
    fields = ["revenue_growth_yoy", "debt_to_equity", "roe", "net_margin"]

    for ticker in _TICKERS:
        snap_e = edgar.get_snapshot(ticker, date(2022, 12, 31))
        snap_y = yfinance.get_snapshot(ticker, date(2022, 12, 31))
        e_stmt = str(snap_e.statement_date) if snap_e else "N/A"
        y_stmt = str(snap_y.statement_date) if snap_y else "N/A"
        e_filed = str(snap_e.filed_date) if snap_e and snap_e.filed_date else "N/A"
        print(f"\n{ticker}  EDGAR stmt={e_stmt} filed={e_filed}  |  yfinance stmt={y_stmt}")
        print(f"{'Field':<22} {'EDGAR':>10} {'yfinance':>10} {'Δ%':>8}  Match?")
        print("-" * 60)
        for field in fields:
            e_val = getattr(snap_e, field, None) if snap_e else None
            y_val = getattr(snap_y, field, None) if snap_y else None
            if e_val is None or y_val is None:
                delta_str, match = "  N/A", "N/A"
            else:
                delta_pct = abs(e_val - y_val) / (abs(y_val) or 1) * 100
                delta_str = f"{delta_pct:.1f}%"
                match = "OK" if delta_pct <= 5.0 else "DIFF"
            print(
                f"{field:<22} {_fmt(e_val, pct=True):>10} {_fmt(y_val, pct=True):>10}"
                f" {delta_str:>8}  {match}"
            )


if __name__ == "__main__":
    main()
