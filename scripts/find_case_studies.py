#!/usr/bin/env python3
"""
Find case study candidates for the 40-60% tier structure.
Scans all tickers for dates where:
  - drawdown from 52w high is in 40-60% (sweet spot) or 30-40% (approach)
  - composite_score >= 0.60
  - quality gate passes
  - fwd_63d > 0 (verified recovery)
  - date is at or near a local price minimum (actual bottom)

Outputs ranked candidates for manual selection of 5 case studies.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from core.signals.recovery_score import compute_recovery_signals, passes_quality_gate

_WARMUP_START = "2016-01-01"
_START_DATE   = pd.Timestamp("2018-01-01")
_END_DATE     = pd.Timestamp("2024-12-31")
BUY_THRESHOLD = 0.60


def prefetch_quality(ticker, fundamentals):
    result = {}
    for year in range(2017, 2026):
        snap = fundamentals.get_snapshot(ticker, date(year, 12, 31))
        result[year] = passes_quality_gate(snap)
    return result


def main():
    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    candidates = []

    for ticker in VALIDATION_UNIVERSE:
        ohlcv = prices.get_prices(ticker, _WARMUP_START, _END_DATE.strftime("%Y-%m-%d"))
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
            continue

        scored  = compute_recovery_signals(ohlcv)
        quality = prefetch_quality(ticker, fundamentals)
        close   = scored["Close"]

        for i, ts in enumerate(scored.index):
            if ts < _START_DATE or ts > _END_DATE:
                continue

            comp = scored["composite_score"].iloc[i]
            dip  = scored["dip_score"].iloc[i]
            dd   = scored["drawdown_52w"].iloc[i]

            if pd.isna(comp) or comp < BUY_THRESHOLD:
                continue
            if pd.isna(dip) or dip < 0.99:  # only sweet-spot (1.0) entries
                continue

            gate = quality.get(ts.year)
            if gate is not True:
                continue

            # Forward 63d return
            j = i + 63
            if j >= len(close):
                continue
            ep  = close.iloc[i]
            fwd = close.iloc[j] / ep - 1

            if fwd <= 0:   # must be a real recovery
                continue

            # Local minimum check: is this close to the lowest price
            # in a ±30-day window? (find bottom of the dip cluster)
            lo  = max(0, i - 30)
            hi  = min(len(close), i + 31)
            win = close.iloc[lo:hi].to_numpy()
            is_near_min = ep <= np.percentile(win, 15)  # within 15th pct of the window

            candidates.append(dict(
                ticker = ticker,
                date   = ts.date(),
                dd_pct = float(dd),
                comp   = float(comp),
                dip    = float(dip),
                gate   = gate,
                fwd63d = float(fwd),
                near_min = is_near_min,
            ))

    # Sort: prefer near-bottom dates, then by fwd63d, then by comp
    candidates.sort(key=lambda c: (-int(c["near_min"]), -c["fwd63d"]))

    # Deduplicate: one per ticker (pick best per ticker)
    seen = {}
    deduped = []
    for c in candidates:
        t = c["ticker"]
        if t not in seen:
            seen[t] = c
            deduped.append(c)

    print(f"\nBEST CANDIDATE PER TICKER (composite>=0.60, dip=1.0, gate=True, fwd63d>0)")
    print(f"{'Ticker':<7} {'Date':<12} {'DD%':>6}  {'Comp':>5}  {'Fwd63d':>7}  {'NearMin':>8}")
    print("-" * 55)
    for c in sorted(deduped, key=lambda x: -x["fwd63d"])[:20]:
        nm = "yes" if c["near_min"] else "no"
        print(f"{c['ticker']:<7} {str(c['date']):<12} {c['dd_pct']:>6.1%}  {c['comp']:>5.2f}  {c['fwd63d']:>7.1%}  {nm:>8}")

    # All candidates for top tickers (show year/event clusters)
    print(f"\nALL CANDIDATES BY TICKER (top 30 by fwd63d):")
    print(f"{'Ticker':<7} {'Date':<12} {'DD%':>6}  {'Comp':>5}  {'Fwd63d':>7}  {'NearMin':>8}")
    print("-" * 55)
    for c in candidates[:30]:
        nm = "yes" if c["near_min"] else "no"
        print(f"{c['ticker']:<7} {str(c['date']):<12} {c['dd_pct']:>6.1%}  {c['comp']:>5.2f}  {c['fwd63d']:>7.1%}  {nm:>8}")


if __name__ == "__main__":
    main()
