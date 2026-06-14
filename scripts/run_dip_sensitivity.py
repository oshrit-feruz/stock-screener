#!/usr/bin/env python3
"""
Dip Range Sensitivity Analysis — pre-Stage 6 validation.

For each of 10 drawdown ranges, dip_score is redefined as binary:
  1.0 if drawdown_52w in [lo, hi], else 0.0.
All other signal components (momentum, volume, gate, threshold) are frozen.
This isolates which drawdown band drives the signal's edge.

Saves to results/dip_sensitivity.txt.
"""
from __future__ import annotations

import sys
from datetime import date
from io import StringIO
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
    WEIGHTS,
    compute_recovery_signals,
    passes_quality_gate,
)

_RANDOM_MEAN     = 0.223    # Stage 5b constant — do not re-derive
_BASELINE_SPREAD = 0.122    # Stage 5b HIGH spread — UX normalisation denominator
_WARMUP_START    = "2016-01-01"
_START_DATE      = "2018-01-01"
_END_DATE        = "2024-12-31"

DIP_RANGES = [
    ("10-20%", 0.10, 0.20),
    ("20-30%", 0.20, 0.30),
    ("30-40%", 0.30, 0.40),
    ("40-50%", 0.40, 0.50),
    ("30-50%", 0.30, 0.50),   # CURRENT — binary equivalent
    ("20-50%", 0.20, 0.50),
    ("20-40%", 0.20, 0.40),
    ("30-60%", 0.30, 0.60),
    ("40-60%", 0.40, 0.60),
    ("50-70%", 0.50, 0.70),
]

_RESULTS_DIR = Path(__file__).parent.parent / "results"


def _spy_regime(prices: PriceData) -> dict[date, str]:
    ohlcv = prices.get_prices("SPY", _WARMUP_START, _END_DATE)
    if ohlcv is None or ohlcv.empty:
        return {}
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


def _fwd252_vectorized(close_arr: np.ndarray) -> np.ndarray:
    n      = len(close_arr)
    fwd252 = np.full(n, np.nan)
    if n > 252:
        base  = close_arr[:n - 252]
        valid = base > 0
        fwd252[:n - 252][valid] = close_arr[252:][valid] / base[valid] - 1
    return fwd252


def _mae_vectorized(close_arr: np.ndarray) -> np.ndarray:
    """Max adverse excursion: min(close[i+1:i+253]) / close[i] - 1."""
    n   = len(close_arr)
    mae = np.full(n, np.nan)
    if n < 3:
        return mae
    # sliding_window_view(close_arr, 252)[i] = close_arr[i:i+252]
    # We want min(close_arr[i+1:i+253]) = min(windows[i+1])
    windows = np.lib.stride_tricks.sliding_window_view(close_arr, 252)
    n_win   = len(windows)           # n - 251
    if n_win > 1:
        fwd_min          = np.min(windows[1:], axis=1)   # shape: (n_win-1,)
        valid            = close_arr[:n_win - 1] > 0
        mae[:n_win - 1]  = np.where(valid, fwd_min / close_arr[:n_win - 1] - 1, np.nan)
    return mae


def run_sensitivity(prices: PriceData, fundamentals) -> dict[str, dict]:
    spy_regime = _spy_regime(prices)
    start_ts   = pd.Timestamp(_START_DATE)
    end_ts     = pd.Timestamp(_END_DATE)

    # Accumulate records per range
    records: dict[str, list[dict]] = {label: [] for label, _, _ in DIP_RANGES}

    for ticker in VALIDATION_UNIVERSE:
        ohlcv = prices.get_prices(ticker, _WARMUP_START, _END_DATE)
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
            continue

        scored       = compute_recovery_signals(ohlcv)
        close_arr    = scored["Close"].to_numpy(dtype=float)
        drawdown_arr = scored["drawdown_52w"].to_numpy(dtype=float)
        mom_arr      = scored["momentum_score"].to_numpy(dtype=float)
        vol_arr      = scored["volume_score"].to_numpy(dtype=float)
        dates        = scored.index
        quality      = _prefetch_quality(ticker, fundamentals)

        # Forward returns and MAE — computed once, shared across all ranges
        fwd252 = _fwd252_vectorized(close_arr)
        mae    = _mae_vectorized(close_arr)

        for label, lo, hi in DIP_RANGES:
            # Binary dip_score for this range
            dip_arr = np.where(
                np.isnan(drawdown_arr), np.nan,
                np.where((drawdown_arr >= lo) & (drawdown_arr <= hi), 1.0, 0.0),
            )
            # Composite score
            valid    = ~np.isnan(dip_arr) & ~np.isnan(mom_arr) & ~np.isnan(vol_arr)
            comp_arr = np.where(
                valid,
                WEIGHTS["dip"] * dip_arr + WEIGHTS["momentum"] * mom_arr + WEIGHTS["volume"] * vol_arr,
                np.nan,
            )

            for i, ts in enumerate(dates):
                if ts < start_ts or ts > end_ts:
                    continue
                gate = quality.get(ts.year)
                if gate is not True:
                    continue
                comp = comp_arr[i]
                if np.isnan(comp) or comp < BUY_THRESHOLD:
                    continue
                regime = spy_regime.get(ts.date(), "unknown")
                records[label].append(dict(fwd252=fwd252[i], mae=mae[i], regime=regime))

    # Aggregate
    out: dict[str, dict] = {}
    for label, _, _ in DIP_RANGES:
        recs = records[label]
        n    = len(recs)
        if n < 200:
            out[label] = dict(n=n, too_few=True)
            continue

        fwds = np.array([r["fwd252"] for r in recs if not np.isnan(r["fwd252"] if r["fwd252"] == r["fwd252"] else float("nan"))])
        # handle nan properly
        fwds = np.array([r["fwd252"] for r in recs], dtype=float)
        maes = np.array([r["mae"]    for r in recs], dtype=float)

        fwds_valid = fwds[~np.isnan(fwds)]
        maes_valid = maes[~np.isnan(maes)]

        if len(fwds_valid) == 0:
            out[label] = dict(n=n, too_few=True)
            continue

        mean_r    = float(np.mean(fwds_valid))
        spread    = mean_r - _RANDOM_MEAN
        pct_pos   = float(np.mean(fwds_valid > 0))
        pct_el    = float(np.mean(fwds_valid < 0))
        p10       = float(np.percentile(fwds_valid, 10))
        p90       = float(np.percentile(fwds_valid, 90))
        pct_hit20 = float(np.mean(maes_valid <= -0.20)) if len(maes_valid) > 0 else float("nan")
        ux        = (spread / _BASELINE_SPREAD) * (1.0 - pct_hit20) * (1.0 - pct_el)

        bull_fwds = np.array(
            [r["fwd252"] for r in recs if r["regime"] == "bull"], dtype=float
        )
        bear_fwds = np.array(
            [r["fwd252"] for r in recs if r["regime"] == "bear"], dtype=float
        )
        bull_fwds = bull_fwds[~np.isnan(bull_fwds)]
        bear_fwds = bear_fwds[~np.isnan(bear_fwds)]

        out[label] = dict(
            n         = n,
            too_few   = False,
            mean      = mean_r,
            median    = float(np.median(fwds_valid)),
            pct_pos   = pct_pos,
            spread    = spread,
            p10       = p10,
            p90       = p90,
            pct_hit20 = pct_hit20,
            pct_el    = pct_el,
            ux        = ux,
            bull      = float(np.mean(bull_fwds)) if len(bull_fwds) > 0 else None,
            bear      = float(np.mean(bear_fwds)) if len(bear_fwds) > 0 else None,
        )

    return out


def _pct(v: float | None, w: int = 7) -> str:
    if v is None:
        return " " * (w - 4) + " N/A"
    return f"{v:+{w}.1%}"


def format_table(results: dict[str, dict]) -> str:
    hdr = (
        f"{'Range':<8} {'n_HIGH':>7}  {'Mean':>7} {'Median':>7} {'%Pos':>5}  "
        f"{'Spread':>7} {'P10':>7} {'P90':>7}  "
        f"{'%Hit-20':>8} {'%ExLoss':>8}  {'UX':>6}  "
        f"{'Bull':>7} {'Bear':>7}"
    )
    sep = "-" * len(hdr)
    lines = [hdr, sep]
    for label, _, _ in DIP_RANGES:
        m = results.get(label, {})
        n = m.get("n", 0)
        if m.get("too_few", True):
            lines.append(f"{label:<8} {n:>7}  (too few entries — unreliable)")
            continue
        cur = " *" if label == "30-50%" else "  "
        lines.append(
            f"{label:<8} {n:>7}  "
            f"{_pct(m['mean'])} {_pct(m['median'])} {m['pct_pos']:>5.1%}  "
            f"{_pct(m['spread'])} {_pct(m['p10'])} {_pct(m['p90'])}  "
            f"{m['pct_hit20']:>8.1%} {m['pct_el']:>8.1%}  "
            f"{m['ux']:>6.3f}  "
            f"{_pct(m['bull'])} {_pct(m['bear'])}"
            f"{cur}"
        )
    lines.append(sep)
    lines.append("* = current production range")
    return "\n".join(lines)


def format_findings(results: dict[str, dict]) -> str:
    buf = StringIO()

    eligible = {
        label: m for label, _, _ in DIP_RANGES
        if not results.get(label, {}).get("too_few", True)
        and results[label]["n"] >= 500
        for m in [results[label]]
    }

    current = results.get("30-50%", {})
    current_spread = current.get("spread", None)

    # FINDING 1 — sweet spot
    buf.write("FINDING 1 -- SWEET SPOT\n")
    if eligible:
        best_label = max(eligible, key=lambda k: eligible[k]["spread"])
        best_m     = eligible[best_label]
        buf.write(f"  Highest spread: {best_label}  spread={_pct(best_m['spread'])}  n={best_m['n']}\n")
        if current_spread is not None:
            margin = best_m["spread"] - current_spread
            materially_better = margin > 0.02 and best_label != "30-50%"
            buf.write(f"  Current (30-50%) spread: {_pct(current_spread)}\n")
            buf.write(f"  Margin of best over current: {_pct(margin)}\n")
            if materially_better:
                buf.write(f"  -> Best range {best_label} outperforms by {_pct(margin)} (> 2pp) with n={best_m['n']} (>500).\n")
                buf.write(f"  -> REVISION CANDIDATE.\n")
            else:
                buf.write(f"  -> 30-50% is within 2pp of the best range. Empirically supported.\n")
    else:
        buf.write("  No eligible ranges (n >= 500).\n")

    # FINDING 2 — pain vs return
    buf.write("\nFINDING 2 -- PAIN VS RETURN TRADEOFF\n")
    floor_eligible = {
        label: m for label, m in eligible.items()
        if m["spread"] >= 0.08
    }
    if floor_eligible:
        safest_label = min(floor_eligible, key=lambda k: floor_eligible[k]["pct_hit20"])
        safest_m     = floor_eligible[safest_label]
        cur_hit20    = current.get("pct_hit20", None)
        buf.write(f"  Lowest %Hit-20% (spread >= +8pp): {safest_label}  pct_hit20={safest_m['pct_hit20']:.1%}  spread={_pct(safest_m['spread'])}\n")
        if cur_hit20 is not None:
            pain_reduction = cur_hit20 - safest_m["pct_hit20"]
            meaningful     = pain_reduction > 0.03
            buf.write(f"  Current (30-50%) pct_hit20: {cur_hit20:.1%}\n")
            buf.write(f"  Pain reduction vs current: {pain_reduction:.1%}pp\n")
            if meaningful and safest_label != "30-50%":
                buf.write(f"  -> {safest_label} is meaningfully safer ({pain_reduction:.1%}pp less pain) while staying eligible.\n")
            else:
                buf.write(f"  -> No range is meaningfully safer (>3pp) while preserving spread >= +8pp.\n")
    else:
        buf.write("  No ranges meet spread >= +8pp floor.\n")

    # FINDING 3 — regime sensitivity
    buf.write("\nFINDING 3 -- REGIME SENSITIVITY\n")
    bull_best = max(eligible, key=lambda k: eligible[k].get("bull") or -99, default=None)
    bear_best = max(eligible, key=lambda k: eligible[k].get("bear") or -99, default=None)
    if bull_best and bear_best:
        buf.write(f"  Best bull regime range: {bull_best}  bull mean={_pct(eligible[bull_best].get('bull'))}\n")
        buf.write(f"  Best bear regime range: {bear_best}  bear mean={_pct(eligible[bear_best].get('bear'))}\n")
        if bull_best == bear_best:
            buf.write(f"  -> Same range optimal in both regimes. No regime-specific adjustment needed.\n")
        else:
            bull_m = eligible[bull_best]
            bear_m = eligible[bear_best]
            buf.write(f"  -> Optimal range differs by regime ({bull_best} bull, {bear_best} bear).\n")
            buf.write(f"     Note for future personalization — not needed for MVP.\n")
    else:
        buf.write("  Insufficient data for regime comparison.\n")

    # FINDING 4 — range stability
    buf.write("\nFINDING 4 -- RANGE STABILITY\n")
    above_8pp  = sum(1 for m in eligible.values() if m["spread"] >= 0.08)
    above_0pp  = sum(1 for m in eligible.values() if m["spread"] >= 0.00)
    total_elig = len(eligible)
    buf.write(f"  Ranges with n >= 500: {total_elig}\n")
    buf.write(f"  Of those, spread >= +8pp (eligible): {above_8pp}\n")
    buf.write(f"  Of those, spread >= 0pp (positive):  {above_0pp}\n")
    if above_8pp >= 4:
        buf.write(f"  -> ROBUST: {above_8pp}/{total_elig} tested ranges pass the +8pp floor.\n")
        buf.write(f"     Signal works across a wide drawdown band.\n")
    elif above_8pp >= 2:
        buf.write(f"  -> MODERATELY ROBUST: {above_8pp}/{total_elig} ranges pass the +8pp floor.\n")
    else:
        buf.write(f"  -> FRAGILE: Only {above_8pp}/{total_elig} ranges pass the +8pp floor.\n")
        if above_8pp > 0:
            narrow_ranges = [label for label, m in eligible.items() if m["spread"] >= 0.08]
            buf.write(f"     Signal only works in: {', '.join(narrow_ranges)}. Document as a key risk.\n")

    # DECISION RULE
    buf.write("\nDECISION RULE\n")
    if eligible and current_spread is not None:
        best_label   = max(eligible, key=lambda k: eligible[k]["spread"])
        best_spread  = eligible[best_label]["spread"]
        best_n       = eligible[best_label]["n"]
        margin       = best_spread - current_spread
        if margin <= 0.02 or best_label == "30-50%":
            buf.write("  CONFIRMED: 30-50% is empirically supported. No change needed.\n")
            buf.write(f"  (Best range {best_label} beats current by only {_pct(margin)}, within the 2pp tolerance.)\n")
        elif best_n < 500:
            buf.write(f"  Best range {best_label} has n={best_n} < 500 -- too few entries for a reliable conclusion.\n")
            buf.write("  CONFIRMED: 30-50% is empirically supported. No change needed.\n")
        else:
            buf.write(f"  REVISION NEEDED: {best_label} outperforms by {_pct(margin)}.\n")
            buf.write(f"  Re-run Stage 5b final validation with new range before Stage 6.\n")

        # Fragile check
        if above_8pp <= 1:
            narrow_ranges = [label for label, m in eligible.items() if m["spread"] >= 0.08]
            buf.write(f"  FRAGILE: Signal only works in {narrow_ranges}. Document as a key risk.\n")
    else:
        buf.write("  INDETERMINATE: Insufficient eligible ranges.\n")

    return buf.getvalue()


def main() -> None:
    print("Loading prices and EDGAR fundamentals...")
    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    print(f"Running sensitivity across {len(DIP_RANGES)} ranges, {len(VALIDATION_UNIVERSE)} tickers, 2018-2024...")
    results = run_sensitivity(prices, fundamentals)

    buf = StringIO()
    buf.write("=" * 80 + "\n")
    buf.write("DIP RANGE SENSITIVITY ANALYSIS (pre-Stage 6)\n")
    buf.write("=" * 80 + "\n")
    buf.write(f"Universe: {len(VALIDATION_UNIVERSE)} tickers  |  2018-2024  |  BUY >= {BUY_THRESHOLD}\n")
    buf.write(f"dip_score: binary (1.0 in range, 0.0 outside) for each test\n")
    buf.write(f"Weights frozen: dip={int(WEIGHTS['dip']*100)}%  momentum={int(WEIGHTS['momentum']*100)}%  volume={int(WEIGHTS['volume']*100)}%\n")
    buf.write(f"RANDOM baseline: {_RANDOM_MEAN:.1%}  |  UX normalised to current spread ({_BASELINE_SPREAD:.1%})\n")
    buf.write(f"Eligibility floor for findings/decision: n >= 500\n\n")
    buf.write(format_table(results))
    buf.write("\n\n")
    buf.write(format_findings(results))

    output = buf.getvalue()
    print(output)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / "dip_sensitivity.txt"
    out_path.write_text(output)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
