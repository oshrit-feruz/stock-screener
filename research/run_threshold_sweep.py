#!/usr/bin/env python3
"""Composite-score entry-threshold sweep on the clean Top-100 PIT universe.

Hypothesis: higher-scoring signals outperform, so raising the entry bar above the
current 0.60 may lift win rate and per-trade return — at the cost of fewer signals.

Four variants, all Top-100 PIT, flat 10%, Fed-funds cash sleeve, 2018-2024,
252d exit, no SL, $100k. Only the entry threshold changes: 0.60 / 0.65 / 0.70 / 0.75.
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
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_pit_market_caps
from research.run_combined_clean_universe import _SIM_END, _SIM_START, _TOP_N, simulate
from scripts.run_combined_validation import load_fedfunds
from scripts.run_portfolio_sim import _INITIAL_CAP, compute_metrics, load_all_data, spy_metrics

_YEARS = list(range(2018, 2025))
_THRESHOLDS = [0.60, 0.65, 0.70, 0.75]


def trade_stats(res):
    trades = res["trades"]
    n = len(trades)
    rets = [t["ret"] for t in trades]
    win = (sum(1 for r in rets if r > 0) / n * 100) if n else 0.0
    avg_ret = (float(np.mean(rets)) * 100) if rets else 0.0
    return n, win, n / len(_YEARS), avg_ret


def main():
    print("Loading data (Top-100 point-in-time universe)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    spy_raw = prices_obj.get_prices("SPY", "2016-01-01", "2024-12-31")
    spy_close = spy_raw["Close"]
    master_cal = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)].index

    fmonths = {}
    for ts in master_cal:
        fmonths.setdefault((ts.year, ts.month), ts)
    union_full = sorted({t for ts in fmonths.values()
                         for t in get_universe(ts.date().isoformat())})
    prefetch_pit_market_caps(union_full, [ts.date().isoformat() for ts in fmonths.values()])
    month_members = {k: set(get_universe_top_n(ts.date().isoformat(), _TOP_N))
                     for k, ts in fmonths.items()}
    union = sorted(set().union(*month_members.values()))
    crossings_by_ticker, prices_wide, _ = load_all_data(prices_obj, fund, union)
    rate_on_cal = load_fedfunds().reindex(master_cal, method="ffill").values.astype(float)
    spy_m = spy_metrics(spy_close, master_cal)

    rows, curves, res_by_thr = [], {}, {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for thr in _THRESHOLDS:
            res = simulate(crossings_by_ticker, prices_wide, master_cal, "flat", "fed_funds",
                           rate_on_cal, month_members, entry_threshold=thr)
            m = compute_metrics(res["daily_values"], _INITIAL_CAP)
            n, win, sy, avg_ret = trade_stats(res)
            rows.append((thr, m, n, win, sy, avg_ret))
            curves[f"thr {thr:.2f}"] = res["daily_values"]
            res_by_thr[thr] = res
            print(f"  thr {thr:.2f}: final ${m['final_value']:>9,.0f}  CAGR {m['cagr']:+.1%}  "
                  f"Sharpe {m['sharpe']:.2f}  trades {n}  win {win:.0f}%  avgret {avg_ret:+.1f}%")

    div = "=" * 100
    print("\n" + div)
    print("ENTRY-THRESHOLD SWEEP — clean Top-100 PIT, flat 10% + Fed funds, 2018-2024, $100k")
    print(div + "\n")
    hdr = (f"  {'Variant':<18}  {'Final $':>10}  {'CAGR':>7}  {'Sharpe':>7}  {'MaxDD':>7}  "
           f"{'Trades':>6}  {'Win%':>5}  {'Sig/yr':>6}  {'AvgRet':>7}")
    print(hdr)
    print("  " + "-" * 96)
    names = {0.60: "A · thr 0.60 (base)", 0.65: "B · thr 0.65",
             0.70: "C · thr 0.70", 0.75: "D · thr 0.75"}
    for thr, m, n, win, sy, avg_ret in rows:
        flag = "" if sy >= 3 else "  <3/yr!"
        print(f"  {names[thr]:<18}  ${m['final_value']:>9,.0f}  {m['cagr']:>+6.1%}  "
              f"{m['sharpe']:>7.2f}  {m['max_dd']:>6.1%}  {n:>6}  {win:>4.0f}%  {sy:>6.1f}  "
              f"{avg_ret:>+6.1f}%{flag}")
    print(f"  {'SPY buy & hold':<18}  ${spy_m['final_value']:>9,.0f}  {spy_m['cagr']:>+6.1%}  "
          f"{spy_m['sharpe']:>7.2f}  {spy_m['max_dd']:>6.1%}  {'—':>6}  {'—':>5}  {'—':>6}  {'—':>7}")
    print()

    # ── Score distribution of the RAW in-universe signal pool ──────────────────
    # Counts every in-universe BUY crossing (no suppression/capacity), with its
    # 252-trading-day forward return, so the buckets are not distorted by which
    # entries a given variant happened to take.
    print("  SCORE DISTRIBUTION — raw in-universe signal pool by score bucket (252d fwd return)")
    print("  " + "-" * 96)
    sim_px = prices_wide.reindex(master_cal, method="ffill")
    px_arr = sim_px.values.astype(float)
    col_map = {c: i for i, c in enumerate(sim_px.columns)}
    cal_list = list(master_cal)

    pool = []  # (comp, fwd_ret)
    for ticker, crossings in crossings_by_ticker.items():
        ci = col_map.get(ticker)
        if ci is None:
            continue
        for ts, comp, price, dd in crossings:
            if not (_SIM_START <= ts <= master_cal[-1]):
                continue
            if ticker not in month_members.get((ts.year, ts.month), ()):
                continue
            idx = master_cal.searchsorted(ts)
            if idx >= len(cal_list):
                continue
            ep = px_arr[idx, ci]
            xp = px_arr[min(idx + 252, len(cal_list) - 1), ci]
            if not (np.isfinite(ep) and ep > 0 and np.isfinite(xp)):
                continue
            pool.append((comp, xp / ep - 1.0))

    buckets = [("0.60–0.64", 0.60, 0.65), ("0.65–0.69", 0.65, 0.70),
               ("0.70–0.74", 0.70, 0.75), ("0.75+", 0.75, 1.01)]
    for label, lo, hi in buckets:
        rets = [r for c, r in pool if lo <= c < hi]
        avg = (np.mean(rets) * 100) if rets else 0.0
        win = (np.mean([1 if r > 0 else 0 for r in rets]) * 100) if rets else 0.0
        per_yr = len(rets) / len(_YEARS)
        print(f"    {label:<10}  {len(rets):>3} signals ({per_yr:>4.1f}/yr)   "
              f"avg 252d ret {avg:>+6.1f}%   win {win:>3.0f}%")
    print(f"    {'TOTAL':<10}  {len(pool):>3} signals ({len(pool)/len(_YEARS):.1f}/yr)")
    print()

    # ── Year-by-year ───────────────────────────────────────────────────────────
    print("  YEAR-BY-YEAR RETURN")
    print("  " + "-" * 96)
    print(f"    {'Year':<6}" + "".join(f"{('thr '+format(t,'.2f')):>12}" for t in _THRESHOLDS)
          + f"{'SPY':>12}")
    metrics_by_thr = {thr: m for thr, m, *_ in rows}
    for y in _YEARS:
        if metrics_by_thr[0.60]["annual_rets"].get(y) is None:
            continue
        cells = "".join(f"{metrics_by_thr[t]['annual_rets'].get(y, 0):>+11.1%} " for t in _THRESHOLDS)
        print(f"    {y:<6}{cells}{spy_m['annual_rets'].get(y, 0):>+11.1%}")
    print()

    # ── Key question ───────────────────────────────────────────────────────────
    base = metrics_by_thr[0.60]
    print(div)
    print("KEY QUESTION — a threshold > 0.60 that lifts BOTH Sharpe & CAGR with ≥3 signals/yr?")
    print(div)
    print(f"  Baseline (0.60): CAGR {base['cagr']:+.1%}  Sharpe {base['sharpe']:.2f}")
    winners = [(thr, m, sy) for thr, m, n, win, sy, ar in rows
               if thr > 0.60 and m["cagr"] > base["cagr"] and m["sharpe"] > base["sharpe"] and sy >= 3]
    if winners:
        for thr, m, sy in winners:
            print(f"  >>> thr {thr:.2f}: CAGR {m['cagr']:+.1%}  Sharpe {m['sharpe']:.2f}  "
                  f"({sy:.1f} sig/yr) — beats baseline on both, ≥3/yr.")
    else:
        print("  >>> NO threshold above 0.60 improves BOTH Sharpe and CAGR while keeping ≥3 signals/yr.")
    print()

    # ── Chart ──────────────────────────────────────────────────────────────────
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    colors = {"thr 0.60": "#888888", "thr 0.65": "#1f77b4",
              "thr 0.70": "#2ca02c", "thr 0.75": "#d62728"}
    spy_curve = spy_close.reindex(master_cal, method="ffill")
    spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP
    fig, ax = plt.subplots(figsize=(12, 7))
    for label, s in curves.items():
        ax.plot(s.index, s.values, label=label, color=colors.get(label), lw=1.9)
    ax.plot(spy_curve.index, spy_curve.values, label="SPY buy & hold",
            color="#9467bd", lw=1.1, ls=":")
    ax.set_title("Entry-threshold sweep — clean Top-100 PIT, flat 10% + Fed funds, 2018-2024",
                 fontsize=12)
    ax.set_ylabel("Portfolio value"); ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(True, alpha=0.3); ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out = Path(__file__).parent.parent / "results" / "research" / "threshold_sweep.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130); plt.close(fig)
    print(f"Chart written: {out}")


if __name__ == "__main__":
    main()
