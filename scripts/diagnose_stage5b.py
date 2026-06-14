#!/usr/bin/env python3
"""Stage 5b diagnostics.

1. Regime-stratified capture rate (HIGH vs RANDOM in bull/bear).
2. BA quality gate at 2020-03-23 (point-in-time EDGAR).
3. Fwd63d case studies from the actual BUY-fire date within ±2 weeks.
"""
import random
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
from core.signals.recovery_score import (
    BUY_THRESHOLD,
    compute_recovery_signals,
    passes_quality_gate,
)
from validation.recovery_backtest import (
    _CASE_WINDOW_DAYS,
    CASE_STUDIES,
    _recovery_captured,
)

_WARMUP_START = "2016-01-01"
_START_DATE   = "2018-01-01"
_END_DATE     = "2024-12-31"
_RANDOM_SEED  = 42
_RANDOM_PROB  = 0.10


# ── helpers ────────────────────────────────────────────────────────────────

def _spy_regime(prices: PriceData) -> dict[date, str]:
    ohlcv = prices.get_prices("SPY", _WARMUP_START, _END_DATE)
    sma200     = ohlcv["Close"].rolling(200).mean()
    sma200_20d = sma200.shift(20)
    bull       = sma200 > sma200_20d
    return {ts.date(): ("bull" if b else "bear") for ts, b in zip(ohlcv.index, bull)}


def _prefetch_quality(ticker: str, fundamentals) -> dict[int, bool | None]:
    result: dict[int, bool | None] = {}
    if fundamentals is None:
        return result
    for year in range(2017, 2026):
        snap = fundamentals.get_snapshot(ticker, date(year, 12, 31))
        result[year] = passes_quality_gate(snap)
    return result


# ── Part 1: regime-stratified capture rate ─────────────────────────────────

def part1_regime_capture(prices: PriceData, fundamentals) -> None:
    print("=" * 60)
    print("PART 1 — REGIME-STRATIFIED CAPTURE RATE")
    print("Capture = stock hit +10% before -10% within 126 days")
    print("=" * 60)

    rng        = random.Random(_RANDOM_SEED)
    regime_map = _spy_regime(prices)
    start_ts   = pd.Timestamp(_START_DATE)
    end_ts     = pd.Timestamp(_END_DATE)

    # {bucket: {regime: [bool, ...]}}
    captured: dict[str, dict[str, list[bool]]] = {
        "HIGH":   {"bull": [], "bear": []},
        "RANDOM": {"bull": [], "bear": []},
    }

    for ticker in VALIDATION_UNIVERSE:
        ohlcv = prices.get_prices(ticker, _WARMUP_START, _END_DATE)
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
            continue

        scored    = compute_recovery_signals(ohlcv)
        close_arr = scored["Close"].to_numpy(dtype=float)
        comp_arr  = scored["composite_score"].to_numpy(dtype=float)
        quality   = _prefetch_quality(ticker, fundamentals)

        for i, (ts, comp) in enumerate(zip(scored.index, comp_arr)):
            if ts < start_ts or ts > end_ts:
                continue

            regime = regime_map.get(ts.date())
            if regime not in ("bull", "bear"):
                continue

            gate  = quality.get(ts.year)
            cap   = _recovery_captured(close_arr, i)

            if not np.isnan(comp) and gate is not False:
                if comp >= BUY_THRESHOLD:
                    captured["HIGH"][regime].append(cap)

            if rng.random() < _RANDOM_PROB and gate is not False:
                captured["RANDOM"][regime].append(cap)

    # Print table
    print(f"\n{'Bucket':<8} {'Regime':<6} {'N':>7}  {'Capture%':>9}  {'Gap vs RANDOM':>14}")
    print("-" * 52)
    for regime in ("bull", "bear", "all"):
        for bucket in ("HIGH", "RANDOM"):
            if regime == "all":
                recs = captured[bucket]["bull"] + captured[bucket]["bear"]
            else:
                recs = captured[bucket][regime]
            n   = len(recs)
            pct = sum(recs) / n if recs else None

            if bucket == "HIGH" and pct is not None:
                if regime == "all":
                    rn_recs = captured["RANDOM"]["bull"] + captured["RANDOM"]["bear"]
                else:
                    rn_recs = captured["RANDOM"][regime]
                rn_pct = sum(rn_recs) / len(rn_recs) if rn_recs else None
                gap = (pct - rn_pct) if rn_pct is not None else None
                gap_str = f"{gap:+.1%}" if gap is not None else "N/A"
            else:
                gap_str = ""

            pct_str = f"{pct:.1%}" if pct is not None else "N/A"
            print(f"{bucket:<8} {regime:<6} {n:>7}  {pct_str:>9}  {gap_str:>14}")
        print()


# ── Part 2: BA quality gate ────────────────────────────────────────────────

def part2_ba_gate(fundamentals) -> bool | None:
    print("=" * 60)
    print("PART 2 — BA QUALITY GATE AT 2020-03-23")
    print(f"EDGAR publication lag: 90 days → cutoff = {date(2020, 3, 23) - pd.Timedelta(days=90)}")
    print("=" * 60)

    snap = fundamentals.get_snapshot("BA", date(2020, 3, 23))
    if snap is None:
        print("get_snapshot returned None — no EDGAR data before cutoff; gate = None")
        return None

    print(f"\nStatement date (10-K period end): {snap.statement_date}")
    print(f"Filed date:                       {snap.filed_date}")
    print(f"Revenue growth YoY:               {snap.revenue_growth_yoy}")
    print(f"Debt-to-equity:                   {snap.debt_to_equity}")
    print(f"Net margin:                       {snap.net_margin}")
    print(f"ROE:                              {snap.roe}")

    gate = passes_quality_gate(snap)
    print("\nGate conditions:")
    rev_check = snap.revenue_growth_yoy is not None and snap.revenue_growth_yoy > 0
    print(f"  revenue_growth_yoy > 0 : {snap.revenue_growth_yoy} → {rev_check}")
    debt_check = snap.debt_to_equity is not None and snap.debt_to_equity < 3
    print(f"  debt_to_equity < 3     : {snap.debt_to_equity}  → {debt_check}")
    margin_check = snap.net_margin is not None and snap.net_margin > 0
    print(f"  net_margin > 0         : {snap.net_margin}  → {margin_check}")
    print(f"\npasses_quality_gate(snap) = {gate}")
    return gate


# ── Part 3: Fwd63d from actual BUY-fire date ──────────────────────────────

def part3_fwd_from_buy(prices: PriceData, fundamentals, ba_gate: bool | None) -> None:
    print("\n" + "=" * 60)
    print("PART 3 — CASE STUDIES: Fwd63d from BUY-fire date")
    print(f"Window: ±{_CASE_WINDOW_DAYS * 2} calendar days around bottom")
    print(f"BUY fires on first date in window with composite >= {BUY_THRESHOLD}")
    print("=" * 60)

    print(
        f"\n{'Ticker':<7} {'Bottom':<12} {'Gate':>5}  {'MaxWin':>7}  "
        f"{'BUY date':<12} {'Offset':>7}  {'Fwd63d':>8}  Note"
    )
    print("-" * 80)

    buy_count = 0
    for ticker, bottom_date, description in CASE_STUDIES:
        # Need 63 trading days past any possible BUY date, which is up to
        # _CASE_WINDOW_DAYS * 2 calendar days after bottom → use 200 cal days total
        end_str    = (bottom_date + pd.Timedelta(days=200)).strftime("%Y-%m-%d")
        ohlcv_full = prices.get_prices(ticker, _WARMUP_START, end_str)
        if ohlcv_full is None or ohlcv_full.empty:
            print(f"{ticker:<7} {str(bottom_date):<12}   N/A     N/A  NO_DATA")
            continue

        scored = compute_recovery_signals(ohlcv_full)

        # Quality gate for this case
        gate = None
        if fundamentals is not None:
            if ticker == "BA":
                gate = ba_gate  # use already-computed result (avoids redundant fetch)
            else:
                snap = fundamentals.get_snapshot(ticker, bottom_date)
                gate = passes_quality_gate(snap)

        gate_str = "?" if gate is None else ("yes" if gate else "NO")

        # Max composite in window
        w_start = pd.Timestamp(bottom_date) - pd.Timedelta(days=_CASE_WINDOW_DAYS * 2)
        w_end   = pd.Timestamp(bottom_date) + pd.Timedelta(days=_CASE_WINDOW_DAYS * 2)
        window  = scored.loc[(scored.index >= w_start) & (scored.index <= w_end)]
        has_scores = window["composite_score"].notna().any()
        max_win = float(window["composite_score"].max()) if has_scores else None
        max_str = f"{max_win:.2f}" if max_win is not None else " N/A"

        # First date in window where composite >= BUY_THRESHOLD
        buy_rows = window[window["composite_score"] >= BUY_THRESHOLD]
        if buy_rows.empty or gate is False:
            note = "gate_fail" if gate is False else "no BUY in window"
            print(
                f"{ticker:<7} {str(bottom_date):<12} {gate_str:>5}  "
                f"{max_str:>7}  {'—':<12} {'':>7}  {'N/A':>8}  {note}"
            )
            continue

        buy_ts  = buy_rows.index[0]
        offset  = int((buy_ts - pd.Timestamp(bottom_date)).days)
        close_s = scored["Close"]

        try:
            idx = close_s.index.get_loc(buy_ts)
        except KeyError:
            idx = (close_s.index <= buy_ts).sum() - 1

        j = idx + 63
        if j < len(close_s):
            ep     = close_s.iloc[idx]
            fwd63d = float(close_s.iloc[j] / ep - 1)
            fwd_str = f"{fwd63d:+.1%}"
            buy_count += 1
        else:
            fwd_str = "N/A"

        note = ""
        if offset < 0:
            note = f"BUY {abs(offset)}d before bottom"
        elif offset > 0:
            note = f"BUY {offset}d after bottom"
        else:
            note = "BUY on bottom day"

        print(
            f"{ticker:<7} {str(bottom_date):<12} {gate_str:>5}  {max_str:>7}  "
            f"{str(buy_ts.date()):<12} {offset:>+7}d  {fwd_str:>8}  {note}"
        )

    print("-" * 80)
    print(f"Cases with BUY in window AND valid Fwd63d: {buy_count}/5")
    print("\nNote: Fwd63d is the 63-trading-day return from the first BUY-signal date")
    print(f"in the ±{_CASE_WINDOW_DAYS * 2}-calendar-day window, not from the bottom date.")


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Universe: {len(VALIDATION_UNIVERSE)} tickers | "
          f"Period: {_START_DATE} – {_END_DATE}\n")

    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    ba_gate = part2_ba_gate(fundamentals)
    part1_regime_capture(prices, fundamentals)
    part3_fwd_from_buy(prices, fundamentals, ba_gate)


if __name__ == "__main__":
    main()
