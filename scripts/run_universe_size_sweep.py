#!/usr/bin/env python3
"""Universe-size sweep: does restricting the point-in-time S&P 500 to the largest
names restore the signal's edge, or was the edge survivorship bias?

Same signal and sizing as V1 (10% / max 10, exit day 252, no stop-loss, $100k,
2018-2024). Only the candidate universe changes:

    full      — full point-in-time S&P 500 (≈500/month)
    top100    — 100 largest members by market cap, per month
    top150    — 150 largest
    top200    — 200 largest
    survivors — the original 50 hardcoded VALIDATION_UNIVERSE (reference)
    SPY       — buy & hold (reference)

All variants share one data load (the union of full monthly membership); each is
just a different monthly membership mask passed to the same simulate().

NOTE on the size filter: top-N ranks by *current* market cap (see
data/sp500_universe.py) — a static proxy that reintroduces survivorship/look-
ahead bias and drops delisted names. Read the variants accordingly.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_market_caps
from scripts.run_portfolio_sim import (
    _INITIAL_CAP,
    _SIM_END,
    _SIM_START,
    build_monthly_universe,
    compute_metrics,
    load_all_data,
    simulate,
    spy_metrics,
)

_N_YEARS = 7  # 2018-2024 inclusive


def first_trading_days(cal: pd.DatetimeIndex) -> dict[tuple[int, int], pd.Timestamp]:
    out: dict[tuple[int, int], pd.Timestamp] = {}
    for ts in cal:
        out.setdefault((ts.year, ts.month), ts)
    return out


def topn_monthly(fmonths: dict, n: int) -> dict[tuple[int, int], set[str]]:
    return {key: set(get_universe_top_n(ts.date().isoformat(), n))
            for key, ts in fmonths.items()}


def trade_stats(res: dict) -> tuple[int, float, float]:
    """(#trades, win_rate %, signals/yr) from a simulate() result."""
    trades = res["trades"]
    n = len(trades)
    wins = sum(1 for t in trades if t["ret"] > 0)
    win_rate = (wins / n * 100) if n else 0.0
    return n, win_rate, n / _N_YEARS


def main() -> None:
    print("Loading data (point-in-time S&P 500 union)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    spy_raw = prices_obj.get_prices("SPY", "2016-01-01", "2024-12-31")
    spy_close = spy_raw["Close"]
    master_cal = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)].index

    full_members = build_monthly_universe(master_cal)
    fmonths = first_trading_days(master_cal)
    union = sorted(set().union(*full_members.values()))

    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} ({len(master_cal)} days)")
    print(f"  Union tickers: {len(union)}; warming market-cap cache...")
    prefetch_market_caps(union)

    crossings_by_ticker, prices_wide, spy_close2 = load_all_data(prices_obj, fund, union)
    print(f"  Tickers with price data: {len(crossings_by_ticker)}")

    # Membership masks per variant.
    survivors_set = set(VALIDATION_UNIVERSE)
    variants = [
        ("Full S&P 500 (PIT)", full_members),
        ("Top 100 by mcap",    topn_monthly(fmonths, 100)),
        ("Top 150 by mcap",    topn_monthly(fmonths, 150)),
        ("Top 200 by mcap",    topn_monthly(fmonths, 200)),
        ("Original 50 survivors", {k: survivors_set for k in full_members}),
    ]

    spy_m = spy_metrics(spy_close, master_cal)

    rows, curves = [], {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for name, members in variants:
            res = simulate(crossings_by_ticker, prices_wide, master_cal,
                           0.10, 10, month_members=members)
            m = compute_metrics(res["daily_values"], _INITIAL_CAP)
            n_tr, win, sig_yr = trade_stats(res)
            rows.append((name, m, n_tr, win, sig_yr))
            curves[name] = res["daily_values"]
            print(f"  {name:<24} final ${m['final_value']:>10,.0f}  CAGR {m['cagr']:+.1%}  "
                  f"Sharpe {m['sharpe']:.2f}  trades {n_tr}")

    # ── Report ─────────────────────────────────────────────────────────────────
    div = "=" * 104
    print()
    print(div)
    print("UNIVERSE-SIZE SWEEP — V1 signal/sizing (10%/max10, 252d), 2018-2024, $100k start")
    print(div)
    print()
    hdr = (f"  {'Variant':<24}  {'Final $':>11}  {'TotRet':>8}  {'CAGR':>7}  {'Sharpe':>7}  "
           f"{'MaxDD':>7}  {'Trades':>6}  {'Win%':>6}  {'Sig/yr':>6}")
    print(hdr)
    print("  " + "-" * 100)
    for name, m, n_tr, win, sig_yr in rows:
        beat = "*" if m["sharpe"] > spy_m["sharpe"] else " "
        print(f"  {name:<24}  ${m['final_value']:>10,.0f}  {m['total_ret']:>+7.1%}  "
              f"{m['cagr']:>+6.1%}  {m['sharpe']:>6.2f}{beat}  {m['max_dd']:>6.1%}  "
              f"{n_tr:>6}  {win:>5.0f}%  {sig_yr:>6.1f}")
    print(f"  {'SPY buy & hold':<24}  ${spy_m['final_value']:>10,.0f}  {spy_m['total_ret']:>+7.1%}  "
          f"{spy_m['cagr']:>+6.1%}  {spy_m['sharpe']:>6.2f}   {spy_m['max_dd']:>6.1%}  "
          f"{'—':>6}  {'—':>6}  {'—':>6}")
    print()
    print("  (* = Sharpe above SPY)")
    print()

    # ── Signals-per-year note ──────────────────────────────────────────────────
    print("  SIGNAL DENSITY — completed trades per year (are small universes too thin?)")
    print("  " + "-" * 100)
    for name, m, n_tr, win, sig_yr in rows:
        print(f"    {name:<24} {sig_yr:>5.1f} trades/yr   ({n_tr} total over {_N_YEARS}y)")
    print()

    # ── Answer the key question ────────────────────────────────────────────────
    beats = [(name, m) for name, m, *_ in rows
             if m["sharpe"] > spy_m["sharpe"] and "survivors" not in name.lower()]
    print(div)
    print("KEY QUESTION — a top-N (100-200) that beats SPY on Sharpe WITHOUT survivorship bias?")
    print(div)
    print(f"  SPY Sharpe = {spy_m['sharpe']:.2f}")
    if beats:
        names = ", ".join(n for n, _ in beats)
        print(f"  Variants beating SPY Sharpe (excl. the 50 survivors): {names}")
    else:
        print("  No point-in-time variant (full or top-100/150/200) beats SPY on Sharpe.")
    print("  Caveat: top-N ranks by CURRENT market cap, which itself favours past winners")
    print("  and drops delisted names — so any top-N edge is partly re-introduced")
    print("  survivorship bias, not a clean result. See data/sp500_universe.py header.")
    print()

    # ── Chart ──────────────────────────────────────────────────────────────────
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter

        styles = {
            "Full S&P 500 (PIT)":    dict(color="#d62728", lw=2.0),
            "Top 100 by mcap":       dict(color="#1f77b4", lw=1.8),
            "Top 150 by mcap":       dict(color="#2ca02c", lw=1.8),
            "Top 200 by mcap":       dict(color="#ff7f0e", lw=1.8),
            "Original 50 survivors": dict(color="#9467bd", lw=1.8, ls="--"),
        }
        spy_curve = spy_close.reindex(master_cal, method="ffill")
        spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP

        fig, ax = plt.subplots(figsize=(12, 7))
        for name, s in curves.items():
            ax.plot(s.index, s.values, label=name, **styles.get(name, {}))
        ax.plot(spy_curve.index, spy_curve.values, label="SPY buy & hold",
                color="#7f7f7f", lw=1.2, ls=":")
        ax.set_title("Universe-size sweep — V1 signal (10%/max10, 252d), 2018-2024, $100k start\n"
                     "point-in-time S&P 500 restricted to top-N by market cap", fontsize=12)
        ax.set_ylabel("Portfolio value")
        ax.set_xlabel("Date")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper left", fontsize=9)
        fig.tight_layout()
        out = Path(__file__).parent.parent / "results" / "universe_size_sweep.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print(f"Chart written: {out}")
    except Exception as e:
        print(f"(chart skipped: {e})")


if __name__ == "__main__":
    main()
