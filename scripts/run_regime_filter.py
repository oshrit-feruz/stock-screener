#!/usr/bin/env python3
"""Market-regime filter on the clean Top-100 PIT universe.

Hypothesis: a dip while the broad market is in a downtrend is more likely a
falling knife than a recovery. So block NEW entries on days when SPY closes below
its 200-day simple moving average (existing positions are held; idle cash still
earns Fed funds).

  A  Current best baseline : Top-100 PIT, flat 10%, Fed-funds cash, no filter
  B  Regime filter         : identical to A, but no new entries while SPY < 200d MA

Same params: 2018-2024, 252d exit, no SL, $100k, max 10.
"""
from __future__ import annotations

import sys
import warnings
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_pit_market_caps
from scripts.run_combined_clean_universe import _SIM_END, _SIM_START, _TOP_N, simulate
from scripts.run_combined_validation import load_fedfunds
from scripts.run_portfolio_sim import _INITIAL_CAP, compute_metrics, load_all_data, spy_metrics

_YEARS = list(range(2018, 2025))


def trade_stats(res):
    trades = res["trades"]
    n = len(trades)
    win = (sum(1 for t in trades if t["ret"] > 0) / n * 100) if n else 0.0
    return n, win, n / len(_YEARS)


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

    # SPY 200-day SMA regime (computed on the full series so the lookback is valid
    # at the 2018 start), then aligned to the trading calendar.
    sma200 = spy_close.rolling(200).mean()
    regime_above = (spy_close >= sma200).reindex(master_cal).fillna(False)
    regime_ok = regime_above.values.astype(bool)

    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} ({len(master_cal)} days)")
    print(f"  Top-{_TOP_N} union: {len(union)} tickers; SPY above 200d MA on "
          f"{regime_ok.mean():.0%} of days")

    blocked_log = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resA = simulate(crossings_by_ticker, prices_wide, master_cal, "flat", "fed_funds",
                        rate_on_cal, month_members)
        resB = simulate(crossings_by_ticker, prices_wide, master_cal, "flat", "fed_funds",
                        rate_on_cal, month_members, regime_ok=regime_ok, blocked_log=blocked_log)

    mA = compute_metrics(resA["daily_values"], _INITIAL_CAP)
    mB = compute_metrics(resB["daily_values"], _INITIAL_CAP)
    spy_m = spy_metrics(spy_close, master_cal)
    nA, winA, syA = trade_stats(resA)
    nB, winB, syB = trade_stats(resB)

    div = "=" * 92
    print("\n" + div)
    print("REGIME FILTER (SPY 200d MA) on CLEAN Top-100 PIT — 2018-2024, flat 10% + Fed funds")
    print(div + "\n")
    hdr = (f"  {'Variant':<28}  {'Final $':>10}  {'CAGR':>7}  {'Sharpe':>7}  {'MaxDD':>7}  "
           f"{'Trades':>6}  {'Win%':>5}  {'Sig/yr':>6}")
    print(hdr)
    print("  " + "-" * 86)
    for name, m, n, win, sy in [("A · No filter (best baseline)", mA, nA, winA, syA),
                                ("B · Regime filter", mB, nB, winB, syB)]:
        print(f"  {name:<28}  ${m['final_value']:>9,.0f}  {m['cagr']:>+6.1%}  "
              f"{m['sharpe']:>7.2f}  {m['max_dd']:>6.1%}  {n:>6}  {win:>4.0f}%  {sy:>6.1f}")
    print(f"  {'SPY buy & hold':<28}  ${spy_m['final_value']:>9,.0f}  {spy_m['cagr']:>+6.1%}  "
          f"{spy_m['sharpe']:>7.2f}  {spy_m['max_dd']:>6.1%}  {'—':>6}  {'—':>5}  {'—':>6}")
    print()

    # ── Regime breakdown ───────────────────────────────────────────────────────
    print("  REGIME BREAKDOWN — SPY vs its 200-day MA, and signals blocked per year")
    print("  " + "-" * 86)
    above = int(regime_ok.sum()); below = len(regime_ok) - above
    print(f"    Days SPY ABOVE 200d MA: {above:>4} ({above/len(regime_ok):>4.0%})   "
          f"BELOW: {below:>4} ({below/len(regime_ok):>4.0%})")
    blocked_by_year = Counter(d.year for d, _ in blocked_log)
    years_idx = pd.DatetimeIndex(master_cal)
    below_by_year = {y: int((~regime_above[years_idx.year == y]).sum()) for y in _YEARS}
    print(f"    {'Year':<6}{'DaysBelowMA':>13}{'SignalsBlocked':>16}")
    for y in _YEARS:
        print(f"    {y:<6}{below_by_year[y]:>13}{blocked_by_year.get(y, 0):>16}")
    print(f"    {'TOTAL':<6}{sum(below_by_year.values()):>13}{len(blocked_log):>16}")
    print()

    # ── Year-by-year returns ───────────────────────────────────────────────────
    print("  YEAR-BY-YEAR RETURN — did the filter help in bear years at a cost in others?")
    print("  " + "-" * 86)
    print(f"    {'Year':<6}{'A no-filter':>14}{'B regime':>12}{'B − A':>10}{'SPY':>10}")
    for y in _YEARS:
        a = mA["annual_rets"].get(y); b = mB["annual_rets"].get(y); s = spy_m["annual_rets"].get(y)
        if a is None:
            continue
        print(f"    {y:<6}{a:>+13.1%}{b:>+12.1%}{(b - a):>+10.1%}{s:>+10.1%}")
    print()

    # ── Key question ───────────────────────────────────────────────────────────
    print(div)
    print("KEY QUESTION — does the filter raise Sharpe & cut Max DD without hurting CAGR much?")
    print(div)
    d_sharpe = mB["sharpe"] - mA["sharpe"]
    d_dd = mB["max_dd"] - mA["max_dd"]            # positive = shallower (better)
    d_cagr = mB["cagr"] - mA["cagr"]
    print(f"  Sharpe:  {mA['sharpe']:.2f} → {mB['sharpe']:.2f}  ({d_sharpe:+.2f})")
    print(f"  Max DD:  {mA['max_dd']:+.1%} → {mB['max_dd']:+.1%}  ({d_dd:+.1%} pts, "
          f"{'shallower' if d_dd > 0 else 'deeper'})")
    print(f"  CAGR:    {mA['cagr']:+.1%} → {mB['cagr']:+.1%}  ({d_cagr:+.1%} pts)")
    helps = d_sharpe > 0 and d_dd > 0
    print(f"  >>> {'YES' if helps else 'NO'} — the filter "
          f"{'improves' if helps else 'does NOT improve'} risk-adjusted return "
          f"{'(at' if helps else '('}{d_cagr:+.1%} CAGR).")
    print()

    # ── Chart ──────────────────────────────────────────────────────────────────
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    spy_curve = spy_close.reindex(master_cal, method="ffill")
    spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP
    fig, ax = plt.subplots(figsize=(12, 7))
    ax.plot(resA["daily_values"].index, resA["daily_values"].values,
            label=f"A · No filter (${mA['final_value']:,.0f})", color="#1f77b4", lw=1.8)
    ax.plot(resB["daily_values"].index, resB["daily_values"].values,
            label=f"B · Regime filter (${mB['final_value']:,.0f})", color="#d62728", lw=2.2)
    ax.plot(spy_curve.index, spy_curve.values,
            label=f"SPY buy & hold (${spy_m['final_value']:,.0f})", color="#9467bd", lw=1.1, ls=":")
    ax.set_title("Regime filter (SPY 200d MA) — clean Top-100 PIT, flat 10% + Fed funds, 2018-2024",
                 fontsize=12)
    ax.set_ylabel("Portfolio value"); ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(True, alpha=0.3); ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out = Path(__file__).parent.parent / "results" / "regime_filter.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130); plt.close(fig)
    print(f"Chart written: {out}")


if __name__ == "__main__":
    main()
