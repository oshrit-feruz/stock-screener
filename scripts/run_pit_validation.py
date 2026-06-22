#!/usr/bin/env python3
"""
Point-in-time S&P 500 universe validation (Stage 5b-PIT).

Same backtest as run_recovery_validation.py but uses the PIT-filtered S&P 500
universe (constituents filtered by "Date added") instead of the fixed 50-ticker
list. This reduces survivorship bias from future additions but does NOT exclude
historical removals or delistings.

Run locally (~15-30 min for first run — downloads prices + EDGAR for ~370 tickers):
  python scripts/run_pit_validation.py

Results saved to results/recovery_pit_validation.txt.
"""
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from core.data.sp500_universe import get_universe_on_date
from scripts.run_recovery_validation import main

if __name__ == "__main__":
    # Universe as of backtest start date — excludes tickers added after 2018-01-01
    pit_universe = get_universe_on_date(date(2018, 1, 1))
    n = len(pit_universe)
    print(f"PIT universe as of 2018-01-01: {n} tickers")
    print("Note: excludes future S&P 500 additions; historical removals not filtered.\n")

    main(
        out_filename="recovery_pit_validation.txt",
        label=f"Stage 5 PIT (n={n}, constituents as-of 2018-01-01)",
        tickers=pit_universe,
    )
