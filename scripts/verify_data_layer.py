#!/usr/bin/env python3
"""Smoke test: fetch prices and point-in-time snapshots for AAPL, MSFT, NVDA."""
import sys
from datetime import date
from pathlib import Path

# Allow running from the project root without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData

TICKERS = ["AAPL", "MSFT", "NVDA"]
AS_OF_DATES = [date(2021, 12, 31), date(2023, 12, 31)]


def fmt(val: float | None, fmt_str: str = ".2f") -> str:
    return f"{val:{fmt_str}}" if val is not None else "N/A"


def main() -> None:
    prices = PriceData()
    fundamentals = PointInTimeFundamentals()

    header = (
        f"{'Ticker':<7} {'As-Of':<12} {'Stmt Date':<12} "
        f"{'Rev Gr':>8} {'D/E':>7} {'ROE':>7} {'Net Mgn':>8}"
    )
    print("\n" + header)
    print("-" * len(header))

    for ticker in TICKERS:
        for as_of in AS_OF_DATES:
            snap = fundamentals.get_snapshot(ticker, as_of)

            if snap is None:
                print(
                    f"{ticker:<7} {str(as_of):<12} {'N/A':<12} "
                    f"{'N/A':>8} {'N/A':>7} {'N/A':>7} {'N/A':>8}"
                )
            else:
                rev = snap.revenue_growth_yoy
                rev_gr = f"{rev:.1%}" if rev is not None else "N/A"
                de = fmt(snap.debt_to_equity)
                roe = f"{snap.roe:.1%}" if snap.roe is not None else "N/A"
                nm = f"{snap.net_margin:.1%}" if snap.net_margin is not None else "N/A"
                print(
                    f"{ticker:<7} {str(as_of):<12} {str(snap.statement_date):<12} "
                    f"{rev_gr:>8} {de:>7} {roe:>7} {nm:>8}"
                )

    print()
    print("Price returns (full-year close-to-close):")
    print(f"{'Ticker':<7} {'Year':<6} {'Return':>8}")
    print("-" * 24)
    for ticker in TICKERS:
        for yr in [2021, 2023]:
            ret = prices.get_return(ticker, f"{yr}-01-02", f"{yr}-12-30")
            ret_str = f"{ret:.1%}" if ret is not None else "N/A"
            print(f"{ticker:<7} {yr:<6} {ret_str:>8}")


if __name__ == "__main__":
    main()
