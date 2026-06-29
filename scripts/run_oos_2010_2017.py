#!/usr/bin/env python3
"""Out-of-sample validation of the 0.65 threshold on 2010-2017.

The 0.65 > 0.60 finding came from 2018-2024. This re-tests it on a window we
never optimised on. Two variants on the clean Top-100 PIT universe, flat 10% +
Fed-funds cash, 252d exit, no SL, $100k:

  A  threshold 0.60 (original)
  B  threshold 0.65 (candidate)

Warmup starts 2008 so the 200-day history and EDGAR fundamentals are populated
before the first signal date (2010-01-01).

SURVIVORSHIP CAVEAT: delisted/renamed tickers with no usable price history are
skipped, so results (especially in the early years) are slightly optimistic.
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
from scripts.run_combined_clean_universe import _TOP_N, simulate
from scripts.run_combined_validation import load_fedfunds
from scripts.run_portfolio_sim import load_all_data

_SIM_START = pd.Timestamp("2010-01-01")
_SIM_END = pd.Timestamp("2017-12-31")
_WARMUP = "2008-01-01"
_YEARS = list(range(2010, 2018))
_INITIAL = 100_000.0


def series_metrics(dv: pd.Series, years: float) -> dict:
    final = float(dv.iloc[-1])
    cagr = (final / _INITIAL) ** (1.0 / years) - 1
    rmax = dv.cummax()
    max_dd = float(((dv - rmax) / rmax).min())
    rets = dv.pct_change().dropna()
    std = float(rets.std())
    sharpe = float(rets.mean() * np.sqrt(252) / std) if std > 0 else 0.0
    return {"final_value": final, "total_ret": final / _INITIAL - 1, "cagr": cagr,
            "max_dd": max_dd, "sharpe": sharpe, "annual": _annual(dv)}


def _annual(dv: pd.Series) -> dict:
    out = {}
    for y in _YEARS:
        days = dv.index[dv.index.year == y]
        if len(days) == 0:
            continue
        if y == _YEARS[0]:
            start = _INITIAL
        else:
            prev = dv.index[dv.index.year == y - 1]
            start = float(dv[prev[-1]]) if len(prev) else _INITIAL
        out[y] = float(dv[days[-1]]) / start - 1
    return out


def trade_stats(res):
    trades = res["trades"]
    n = len(trades)
    rets = [t["ret"] for t in trades]
    win = (sum(1 for r in rets if r > 0) / n * 100) if n else 0.0
    avg = (float(np.mean(rets)) * 100) if rets else 0.0
    return n, win, n / len(_YEARS), avg


def main():
    print("OOS 2010-2017 — loading (Top-100 PIT, 2008 warmup)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    spy_raw = prices_obj.get_prices("SPY", _WARMUP, "2024-12-31")
    spy_close = spy_raw["Close"]
    master_cal = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)].index
    years = (master_cal[-1] - master_cal[0]).days / 365.25

    fmonths = {}
    for ts in master_cal:
        fmonths.setdefault((ts.year, ts.month), ts)
    month_dates = [ts.date().isoformat() for ts in fmonths.values()]
    union_full = sorted({t for ts in fmonths.values() for t in get_universe(ts.date().isoformat())})
    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} "
          f"({len(master_cal)} days, {years:.1f}y); full membership union {len(union_full)}")
    print("  Warming point-in-time market-cap grid (raw close × EDGAR shares)...")
    prefetch_pit_market_caps(union_full, month_dates)

    month_members = {k: set(get_universe_top_n(ts.date().isoformat(), _TOP_N))
                     for k, ts in fmonths.items()}
    union = sorted(set().union(*month_members.values()))
    crossings_by_ticker, prices_wide, _ = load_all_data(
        prices_obj, fund, union, warmup_start=_WARMUP, quality_years=range(2009, 2018))
    print(f"  Top-{_TOP_N} union {len(union)}; with price data {len(crossings_by_ticker)}")

    rate_on_cal = load_fedfunds().reindex(master_cal, method="ffill").values.astype(float)

    res, metrics, tstats = {}, {}, {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for thr in (0.60, 0.65):
            r = simulate(crossings_by_ticker, prices_wide, master_cal, "flat", "fed_funds",
                         rate_on_cal, month_members, entry_threshold=thr)
            res[thr] = r
            metrics[thr] = series_metrics(r["daily_values"], years)
            tstats[thr] = trade_stats(r)
            m = metrics[thr]
            print(f"  thr {thr:.2f}: final ${m['final_value']:>9,.0f}  CAGR {m['cagr']:+.1%}  "
                  f"Sharpe {m['sharpe']:.2f}  trades {tstats[thr][0]}")

    spy_curve = spy_close.reindex(master_cal, method="ffill")
    spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL
    spy_m = series_metrics(spy_curve, years)

    div = "=" * 92
    print("\n" + div)
    print("OUT-OF-SAMPLE 2010-2017 — clean Top-100 PIT, flat 10% + Fed funds, $100k")
    print(div + "\n")
    print(f"  {'Variant':<20}  {'Final $':>10}  {'CAGR':>7}  {'Sharpe':>7}  {'MaxDD':>7}  "
          f"{'Trades':>6}  {'Win%':>5}  {'Sig/yr':>6}  {'AvgRet':>7}")
    print("  " + "-" * 88)
    for thr, name in [(0.60, "A · thr 0.60 (orig)"), (0.65, "B · thr 0.65 (cand)")]:
        m = metrics[thr]; n, win, sy, avg = tstats[thr]
        print(f"  {name:<20}  ${m['final_value']:>9,.0f}  {m['cagr']:>+6.1%}  {m['sharpe']:>7.2f}  "
              f"{m['max_dd']:>6.1%}  {n:>6}  {win:>4.0f}%  {sy:>6.1f}  {avg:>+6.1f}%")
    print(f"  {'SPY buy & hold':<20}  ${spy_m['final_value']:>9,.0f}  {spy_m['cagr']:>+6.1%}  "
          f"{spy_m['sharpe']:>7.2f}  {spy_m['max_dd']:>6.1%}  {'—':>6}  {'—':>5}  {'—':>6}  {'—':>7}")
    print()

    # ── Year-by-year ───────────────────────────────────────────────────────────
    print("  YEAR-BY-YEAR RETURN")
    print("  " + "-" * 88)
    print(f"    {'Year':<6}{'thr 0.60':>12}{'thr 0.65':>12}{'B − A':>10}{'SPY':>10}")
    for y in _YEARS:
        a = metrics[0.60]["annual"].get(y); b = metrics[0.65]["annual"].get(y)
        s = spy_m["annual"].get(y)
        if a is None:
            continue
        print(f"    {y:<6}{a:>+11.1%} {b:>+11.1%} {(b - a):>+9.1%} {s:>+9.1%}")
    print()

    # ── Score distribution (raw in-universe pool, 252d fwd return) ──────────────
    print("  SCORE DISTRIBUTION — raw in-universe signal pool (252d fwd return)")
    print("  " + "-" * 88)
    sim_px = prices_wide.reindex(master_cal, method="ffill")
    px_arr = sim_px.values.astype(float)
    col_map = {c: i for i, c in enumerate(sim_px.columns)}
    cal_n = len(master_cal)
    pool = []
    for ticker, crossings in crossings_by_ticker.items():
        ci = col_map.get(ticker)
        if ci is None:
            continue
        for ts, comp, price, dd in crossings:
            if not (master_cal[0] <= ts <= master_cal[-1]):
                continue
            if ticker not in month_members.get((ts.year, ts.month), ()):
                continue
            idx = master_cal.searchsorted(ts)
            if idx >= cal_n:
                continue
            ep = px_arr[idx, ci]; xp = px_arr[min(idx + 252, cal_n - 1), ci]
            if np.isfinite(ep) and ep > 0 and np.isfinite(xp):
                pool.append((comp, xp / ep - 1.0))
    bucket_avg = {}
    for label, lo, hi in [("0.60–0.64", 0.60, 0.65), ("0.65+", 0.65, 1.01)]:
        rets = [r for c, r in pool if lo <= c < hi]
        avg = (np.mean(rets) * 100) if rets else 0.0
        win = (np.mean([1 if r > 0 else 0 for r in rets]) * 100) if rets else 0.0
        bucket_avg[label] = avg
        print(f"    {label:<10}  {len(rets):>3} signals ({len(rets)/len(_YEARS):>4.1f}/yr)   "
              f"avg 252d ret {avg:>+6.1f}%   win {win:>3.0f}%")
    print()

    # ── Key question (honest, not just sign of the difference) ─────────────────
    A, B = metrics[0.60], metrics[0.65]
    cand_sy = tstats[0.65][2]
    d_cagr = B["cagr"] - A["cagr"]
    d_sharpe = B["sharpe"] - A["sharpe"]
    both_better = d_cagr > 0 and d_sharpe > 0
    freq_ok = cand_sy >= 3.0
    score_monotonic = bucket_avg.get("0.65+", 0) >= bucket_avg.get("0.60–0.64", 0)
    tiny = abs(d_cagr) < 0.01 and abs(d_sharpe) < 0.10
    robust = both_better and freq_ok and score_monotonic and not tiny

    print(div)
    print("KEY QUESTION — does thr 0.65 also beat 0.60 out-of-sample (2010-2017)?")
    print(div)
    print(f"  CAGR:   0.60 {A['cagr']:+.1%}  vs  0.65 {B['cagr']:+.1%}   ({d_cagr:+.1%})")
    print(f"  Sharpe: 0.60 {A['sharpe']:.2f}  vs  0.65 {B['sharpe']:.2f}   ({d_sharpe:+.2f})")
    print(f"  Max DD: 0.60 {A['max_dd']:+.1%}  vs  0.65 {B['max_dd']:+.1%}")
    print(f"  Candidate frequency: {cand_sy:.1f} signals/yr "
          f"({'OK' if freq_ok else 'BELOW the 3/yr reliability floor'})")
    print(f"  Score→return: 0.65+ avg {bucket_avg.get('0.65+',0):+.1f}% vs "
          f"0.60–0.64 {bucket_avg.get('0.60–0.64',0):+.1f}% "
          f"({'monotonic' if score_monotonic else 'INVERTED — higher score did WORSE here'})")
    print()
    if robust:
        print("  >>> ROBUST — 0.65 beats 0.60 on both metrics with a reliable signal count.")
    else:
        reasons = []
        if tiny:
            reasons.append("the gap is within noise")
        if not freq_ok:
            reasons.append(f"0.65 fires only {cand_sy:.1f}/yr (< 3)")
        if not score_monotonic:
            reasons.append("higher scores UNDER-performed here (the premise inverts)")
        if not both_better:
            reasons.append("0.65 does not win on both metrics")
        print("  >>> NOT CONFIRMED — " + "; ".join(reasons) + ".")
        print("      The 2018-2024 advantage of 0.65 does not replicate out-of-sample;")
        print("      treat it as overfitting and keep the threshold at 0.60.")
    print()

    # ── Chart ──────────────────────────────────────────────────────────────────
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    fig, ax = plt.subplots(figsize=(12, 7))
    ax.plot(res[0.60]["daily_values"].index, res[0.60]["daily_values"].values,
            label=f"thr 0.60 (${A['final_value']:,.0f})", color="#888888", lw=1.9)
    ax.plot(res[0.65]["daily_values"].index, res[0.65]["daily_values"].values,
            label=f"thr 0.65 (${B['final_value']:,.0f})", color="#1f77b4", lw=2.1)
    ax.plot(spy_curve.index, spy_curve.values,
            label=f"SPY (${spy_m['final_value']:,.0f})", color="#9467bd", lw=1.1, ls=":")
    ax.set_title("Out-of-sample 2010-2017 — threshold 0.60 vs 0.65, clean Top-100 PIT, flat 10% + Fed funds",
                 fontsize=11)
    ax.set_ylabel("Portfolio value"); ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(True, alpha=0.3); ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out = Path(__file__).parent.parent / "results" / "oos_2010_2017_threshold.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130); plt.close(fig)
    print(f"Chart written: {out}")


if __name__ == "__main__":
    main()
