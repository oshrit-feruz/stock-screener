#!/usr/bin/env python3
"""V1 vs V1-Score-Plus: upsize-only position sizing funded from idle cash.

V1              : every signal gets 10% of portfolio, max 10 concurrent positions.
V1-Score-Plus   : every signal gets 10% by default; a signal with composite
                  score >= 0.70 gets 12% instead. No category is ever downsized.
                  The extra capital comes from idle cash, never from shrinking
                  another position. If cash can't fund the full 12%, the position
                  opens with whatever is left, as long as at least 5% of the
                  portfolio is still free; otherwise it is skipped.

Conditions identical to the baseline: 2018-2024, same 50 tickers, exit at day
252, no stop-loss, idle cash earns 0%.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from scripts.run_portfolio_sim import (
    _INITIAL_CAP,
    _SIM_END,
    _SIM_START,
    compute_metrics,
    load_all_data,
    spy_metrics,
)
from scripts.run_score_sizing_validation import simulate


def size_bucket(t: dict) -> str:
    """Intended size tier for a trade (comp>=0.70 -> 12%, else 10%)."""
    return "12% (comp>=0.70)" if t["comp"] >= 0.70 else "10% (comp<0.70)"


def bucket_stats(trades: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for label in ("12% (comp>=0.70)", "10% (comp<0.70)"):
        ts   = [t for t in trades if size_bucket(t) == label]
        rets = [t["ret"] for t in ts]
        out[label] = {
            "n":          len(ts),
            "avg_ret":    float(np.mean(rets)) if rets else 0.0,
            "median":     float(np.median(rets)) if rets else 0.0,
            "win_rate":   float(np.mean([1 if r > 0 else 0 for r in rets])) if rets else 0.0,
            "avg_actual": float(np.mean([t["actual_pct"] for t in ts])) if ts else 0.0,
        }
    return out


def main() -> None:
    print("Loading price data and computing signals...")
    prices_obj = PriceData()
    fund       = EdgarFundamentals(fallback=PointInTimeFundamentals())
    crossings_by_ticker, prices_wide, spy_close = load_all_data(prices_obj, fund)

    spy_sim    = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)]
    master_cal = spy_sim.index
    print(f"  Master calendar: {master_cal[0].date()} - {master_cal[-1].date()} "
          f"({len(master_cal)} trading days)")
    print(f"  Tickers with signals: {len(crossings_by_ticker)}")
    print()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        v1   = simulate(crossings_by_ticker, prices_wide, master_cal, "flat",       max_pos=10)
        vsp  = simulate(crossings_by_ticker, prices_wide, master_cal, "score_plus", max_pos=10)

    m1  = compute_metrics(v1["daily_values"],  _INITIAL_CAP)
    ms  = compute_metrics(vsp["daily_values"], _INITIAL_CAP)
    spy = spy_metrics(spy_close, master_cal)

    div = "=" * 78
    print(div)
    print("V1 (flat 10%) vs V1-Score-Plus (upsize >=0.70 to 12%)   2018-01-02 - 2024-12-30")
    print(div)
    print()

    # ── 1. Headline ───────────────────────────────────────────────────────────
    print("SECTION 1 - HEADLINE: V1-Score-Plus vs V1")
    print("-" * 78)
    print(f"  {'Metric':<26}  {'V1 (flat)':>14}  {'V1-Score-Plus':>14}  {'SPY':>12}")
    print(f"  {'-'*70}")
    rows = [
        ("Final value ($100k)", f"${m1['final_value']:>,.0f}", f"${ms['final_value']:>,.0f}", f"${spy['final_value']:>,.0f}"),
        ("Total return",        f"{m1['total_ret']:+.1%}",     f"{ms['total_ret']:+.1%}",     f"{spy['total_ret']:+.1%}"),
        ("CAGR (7y)",           f"{m1['cagr']:+.1%}",          f"{ms['cagr']:+.1%}",          f"{spy['cagr']:+.1%}"),
        ("Sharpe (rf=0%)",      f"{m1['sharpe']:.2f}",         f"{ms['sharpe']:.2f}",         f"{spy['sharpe']:.2f}"),
        ("Max drawdown",        f"{m1['max_dd']:.1%}",         f"{ms['max_dd']:.1%}",         f"{spy['max_dd']:.1%}"),
    ]
    for label, a, b, c in rows:
        print(f"  {label:<26}  {a:>14}  {b:>14}  {c:>12}")
    print()
    print(f"  V1-Score-Plus vs V1:  total return {ms['total_ret']-m1['total_ret']:+.1%},  "
          f"CAGR {ms['cagr']-m1['cagr']:+.1%},  Sharpe {ms['sharpe']-m1['sharpe']:+.2f}")
    print()

    # ── 2. Positions by intended size bucket ──────────────────────────────────
    print("SECTION 2 - V1-Score-Plus POSITIONS BY SIZE BUCKET")
    print("-" * 78)
    bs = bucket_stats(vsp["trades"])
    print(f"  {'Bucket':<20}  {'#Pos':>5}  {'AvgActual%':>11}  {'Avg Ret':>9}  {'Median':>9}  {'Win%':>7}")
    print(f"  {'-'*68}")
    for label in ("12% (comp>=0.70)", "10% (comp<0.70)"):
        s = bs[label]
        print(f"  {label:<20}  {s['n']:>5}  {s['avg_actual']:>10.1%}  "
              f"{s['avg_ret']:>+8.1%}  {s['median']:>+8.1%}  {s['win_rate']:>6.0%}")
    print()
    n_partial = sum(1 for t in vsp["trades"]
                    if t["comp"] >= 0.70 and t["actual_pct"] < 0.119)
    print(f"  Of the 12% bucket, {n_partial} position(s) opened partially "
          f"(cash short of full 12%).")
    skipped_5pct = sum(1 for s in vsp["skipped"] if s["reason"] == "below_5pct_free")
    print(f"  Signals skipped for <5% cash free: {skipped_5pct}")
    print()

    # ── 3. Chart ──────────────────────────────────────────────────────────────
    out_path = Path(__file__).parent.parent / "data" / "score_plus_equity_curve.png"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker

        spy_curve = spy_close.reindex(master_cal, method="ffill")
        spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP

        fig, ax = plt.subplots(figsize=(11, 6))
        ax.plot(v1["daily_values"].index,  v1["daily_values"].values,
                label=f"V1 flat 10%  (${m1['final_value']:,.0f})", lw=1.6)
        ax.plot(vsp["daily_values"].index, vsp["daily_values"].values,
                label=f"V1-Score-Plus  (${ms['final_value']:,.0f})", lw=1.6)
        ax.plot(spy_curve.index, spy_curve.values,
                label=f"SPY buy & hold  (${spy['final_value']:,.0f})",
                lw=1.2, ls="--", color="gray")
        ax.set_title("Portfolio value: V1 vs V1-Score-Plus vs SPY (2018-2024)")
        ax.set_ylabel("Portfolio value ($)")
        ax.set_xlabel("Date")
        ax.legend(loc="upper left")
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x/1000:.0f}k"))
        fig.tight_layout()
        fig.savefig(out_path, dpi=120)
        print(f"  Equity-curve chart saved to: {out_path}")
    except Exception as e:
        print(f"  (chart skipped: {e})")
    print()
    print(div)


if __name__ == "__main__":
    main()
