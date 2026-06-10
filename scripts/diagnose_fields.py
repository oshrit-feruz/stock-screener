#!/usr/bin/env python3
"""Diagnose: print actual yfinance row labels and available date ranges."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import yfinance as yf

TICKERS = ["AAPL", "MSFT", "NVDA"]


def diagnose(ticker: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {ticker}")
    print(f"{'='*60}")
    stock = yf.Ticker(ticker)

    for attr, label in [("income_stmt", "INCOME STMT"), ("balance_sheet", "BALANCE SHEET")]:
        df = getattr(stock, attr)
        if df is None or df.empty:
            print(f"  {label}: EMPTY/NONE")
            continue
        cols = [c.strftime("%Y-%m-%d") for c in df.columns]
        print(f"\n  {label} — {len(cols)} years: {cols}")
        print(f"  Row labels ({len(df.index)}):")
        for row in df.index:
            vals = [f"{df.loc[row, c]:.0f}" if pd.notna(df.loc[row, c]) else "NaN"
                    for c in df.columns]
            print(f"    {row:<50} {vals}")


def main() -> None:
    print(f"yfinance version: {yf.__version__}")
    for ticker in TICKERS:
        try:
            diagnose(ticker)
        except Exception as e:
            print(f"  ERROR for {ticker}: {e}")


if __name__ == "__main__":
    main()
