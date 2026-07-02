#!/usr/bin/env python3
"""V1 vs V1-Score: does composite-score-based position sizing beat flat sizing?

Research question: does a higher composite score predict higher 252-day return?
If yes, dynamic position sizing is justified. If not, the score is only a binary
entry gate, not a sizing tool.

Baseline V1      : every signal gets 10% of portfolio, max 10 concurrent positions.
Variant V1-Score : position size set by the signal's composite score:
    0.60-0.69 ->  8% of portfolio
    0.70-0.79 -> 10% of portfolio
    0.80+     -> 12% of portfolio
  Constraint: total open exposure never exceeds 100% on any day. If cash is
  exhausted (can't fund the full intended size), no new position is opened.

Run conditions identical to the baseline: universe 2018-2024, same 50 tickers,
threshold 0.60, exit at day 252, no stop-loss, idle cash earns 0%.
"""
from __future__ import annotations

import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from scripts.run_portfolio_sim import (
    _INITIAL_CAP,
    _MIN_POSITION,
    _SIM_END,
    _SIM_START,
    _HOLD_DAYS,
    compute_metrics,
    load_all_data,
    spy_metrics,
)

# ── Score tiers ───────────────────────────────────────────────────────────────
def score_tier(comp: float) -> tuple[str, float]:
    """Return (category label, position-size fraction) for a composite score."""
    if comp >= 0.80:
        return "0.80+", 0.12
    if comp >= 0.70:
        return "0.70-0.79", 0.10
    return "0.60-0.69", 0.08


CATEGORIES = ["0.60-0.69", "0.70-0.79", "0.80+"]


# ── Parametric simulation ─────────────────────────────────────────────────────
def simulate(
    crossings_by_ticker: dict,
    prices_wide: pd.DataFrame,
    master_cal: pd.DatetimeIndex,
    mode: str,            # "flat" (V1) or "score" (V1-Score)
    max_pos: int,
) -> dict:
    """One portfolio run.

    flat  : alloc = min(port_val * 0.10, cash), capped at max_pos positions
            (reproduces the existing V1 baseline exactly).
    score : alloc = port_val * tier(comp); skipped entirely if cash can't fund
            the full intended size (the <=100% exposure constraint). No max_pos.
    """
    events_by_date: dict = defaultdict(list)
    for ticker, crossings in crossings_by_ticker.items():
        for ts, comp, price, dd in crossings:
            if _SIM_START <= ts <= master_cal[-1]:
                events_by_date[ts].append((ticker, comp, price, dd))

    sim_prices = prices_wide.reindex(master_cal, method="ffill")
    prices_arr = sim_prices.values.astype(float)
    col_map    = {c: i for i, c in enumerate(sim_prices.columns)}

    def _price(day_idx: int, ticker: str) -> float:
        ci = col_map.get(ticker)
        if ci is None:
            return float("nan")
        return float(prices_arr[day_idx, ci])

    def _port_val_at(day_idx: int, cash: float, positions: dict) -> float:
        v = cash
        for pos in positions.values():
            p = _price(day_idx, pos["ticker"])
            v += pos["shares"] * (p if not np.isnan(p) else pos["entry_price"])
        return v

    cash                      = _INITIAL_CAP
    open_pos: dict[int, dict] = {}
    last_entry_cal_idx: dict  = {}
    trades: list[dict]        = []
    skipped: list[dict]       = []
    daily_values              = np.zeros(len(master_cal))
    daily_cash_arr            = np.zeros(len(master_cal))
    pid_ctr                   = 0

    for day_idx, day in enumerate(master_cal):
        port_val = _port_val_at(day_idx, cash, open_pos)

        # 1. Exit at day 252
        for pid in [k for k, v in list(open_pos.items()) if v["exit_idx"] == day_idx]:
            pos = open_pos.pop(pid)
            ep  = _price(day_idx, pos["ticker"])
            if np.isnan(ep):
                ep = pos["entry_price"]
            cash += pos["shares"] * ep
            trades.append({
                "ticker":     pos["ticker"],
                "entry_date": pos["entry_date"].date(),
                "exit_date":  day.date(),
                "entry_price": pos["entry_price"],
                "exit_price":  ep,
                "cost":       pos["cost"],
                "pnl":        pos["shares"] * ep - pos["cost"],
                "ret":        ep / pos["entry_price"] - 1,
                "days_held":  day_idx - pos["entry_idx"],
                "comp":       pos["comp"],
                "category":   pos["category"],
                "size_pct":   pos["size_pct"],
                "actual_pct": pos["cost"] / pos["port_val_at_entry"],
                "status":     "CLOSED",
            })

        # 2. New signals (highest composite first)
        for ticker, comp, crossing_price, dd in sorted(
            events_by_date.get(day, []), key=lambda x: -x[1]
        ):
            if ticker in last_entry_cal_idx and \
               (day_idx - last_entry_cal_idx[ticker]) < _HOLD_DAYS:
                continue

            if len(open_pos) >= max_pos:
                skipped.append({"date": day.date(), "ticker": ticker,
                                "comp": comp, "reason": "capacity"})
                continue

            category, tier_pct = score_tier(comp)
            if mode == "flat":
                size_pct = 0.10
                alloc    = min(port_val * size_pct, cash)
            elif mode == "score_plus":
                # Default 10%; comp>=0.70 gets 12%. Extra comes from idle cash,
                # never from shrinking another position. If cash can't fund the
                # full intended size, open with whatever's left as long as at
                # least 5% of the portfolio is free.
                size_pct = 0.12 if comp >= 0.70 else 0.10
                desired  = port_val * size_pct
                if cash < port_val * 0.05:
                    skipped.append({"date": day.date(), "ticker": ticker,
                                    "comp": comp, "reason": "below_5pct_free"})
                    continue
                alloc = min(desired, cash)
            else:  # score
                size_pct = tier_pct
                desired  = port_val * size_pct
                # <=100% exposure: only open if cash can fund the full size
                if cash + 1e-9 < desired:
                    skipped.append({"date": day.date(), "ticker": ticker,
                                    "comp": comp, "reason": "cash_exhausted"})
                    continue
                alloc = desired

            if alloc < _MIN_POSITION:
                skipped.append({"date": day.date(), "ticker": ticker,
                                "comp": comp, "reason": "min_pos"})
                continue

            ep = _price(day_idx, ticker)
            if np.isnan(ep) or ep <= 0:
                ep = crossing_price
            if ep <= 0:
                continue

            pid_ctr += 1
            open_pos[pid_ctr] = {
                "ticker":     ticker,
                "entry_date": day,
                "entry_idx":  day_idx,
                "exit_idx":   min(day_idx + _HOLD_DAYS, len(master_cal) - 1),
                "entry_price": ep,
                "shares":     alloc / ep,
                "cost":       alloc,
                "comp":       comp,
                "category":   category,
                "size_pct":   size_pct,
                "port_val_at_entry": port_val,
            }
            last_entry_cal_idx[ticker] = day_idx
            cash -= alloc

        daily_values[day_idx]   = _port_val_at(day_idx, cash, open_pos)
        daily_cash_arr[day_idx] = cash

    # Mark remaining open positions at market on the last day
    last_idx = len(master_cal) - 1
    last_day = master_cal[last_idx]
    for pos in open_pos.values():
        ep = _price(last_idx, pos["ticker"])
        if np.isnan(ep):
            ep = pos["entry_price"]
        trades.append({
            "ticker":     pos["ticker"],
            "entry_date": pos["entry_date"].date(),
            "exit_date":  last_day.date(),
            "entry_price": pos["entry_price"],
            "exit_price":  ep,
            "cost":       pos["cost"],
            "pnl":        pos["shares"] * ep - pos["cost"],
            "ret":        ep / pos["entry_price"] - 1,
            "days_held":  last_idx - pos["entry_idx"],
            "comp":       pos["comp"],
            "category":   pos["category"],
            "size_pct":   pos["size_pct"],
            "actual_pct": pos["cost"] / pos["port_val_at_entry"],
            "status":     "OPEN (MTM 2024-12-30)",
        })

    trades.sort(key=lambda t: t["entry_date"])
    dv = pd.Series(daily_values,   index=master_cal)
    dc = pd.Series(daily_cash_arr, index=master_cal)
    return {
        "trades": trades, "skipped": skipped,
        "daily_values": dv, "daily_cash": dc,
        "final_value": float(dv.iloc[-1]),
    }


# ── Per-category analysis ─────────────────────────────────────────────────────
def category_stats(trades: list[dict]) -> dict:
    by_cat: dict[str, list] = {c: [] for c in CATEGORIES}
    for t in trades:
        by_cat[t["category"]].append(t)
    out = {}
    for cat in CATEGORIES:
        ts = by_cat[cat]
        rets = [t["ret"] for t in ts]
        out[cat] = {
            "n":        len(ts),
            "avg_ret":  float(np.mean(rets)) if rets else 0.0,
            "median":   float(np.median(rets)) if rets else 0.0,
            "win_rate": float(np.mean([1 if r > 0 else 0 for r in rets])) if rets else 0.0,
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
        v1     = simulate(crossings_by_ticker, prices_wide, master_cal, "flat", max_pos=10)
        vscore = simulate(crossings_by_ticker, prices_wide, master_cal, "score", max_pos=10_000)

    m1  = compute_metrics(v1["daily_values"],     _INITIAL_CAP)
    ms  = compute_metrics(vscore["daily_values"], _INITIAL_CAP)
    spy = spy_metrics(spy_close, master_cal)

    div = "=" * 78
    print(div)
    print("V1 (flat 10%) vs V1-Score (score-tiered sizing)   2018-01-02 - 2024-12-30")
    print(div)
    print()

    # ── 1. Headline comparison ────────────────────────────────────────────────
    print("SECTION 1 - HEADLINE: TOTAL RETURN & SHARPE")
    print("-" * 78)
    print(f"  {'Metric':<26}  {'V1 (flat)':>14}  {'V1-Score':>14}  {'SPY':>12}")
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
    d_ret = ms["total_ret"] - m1["total_ret"]
    d_shp = ms["sharpe"] - m1["sharpe"]
    print(f"  V1-Score vs V1:  total return {d_ret:+.1%},  Sharpe {d_shp:+.2f}")
    print()

    # ── 2. Per-category return (the research question) ────────────────────────
    # Categories are independent of sizing, so use V1-Score's trade set (same
    # entries as V1 modulo cash availability). Report both to be safe.
    print("SECTION 2 - AVERAGE 252-DAY RETURN BY SCORE CATEGORY")
    print("-" * 78)
    print("  Research question: does a higher composite score predict a higher")
    print("  252-day return? (uses V1-Score trades — entries by score tier)")
    print()
    cs = category_stats(vscore["trades"])
    print(f"  {'Category':<14}  {'Size%':>6}  {'#Pos':>5}  {'Avg Ret':>9}  {'Median':>9}  {'Win%':>7}")
    print(f"  {'-'*62}")
    size_by_cat = {"0.60-0.69": "8%", "0.70-0.79": "10%", "0.80+": "12%"}
    for cat in CATEGORIES:
        s = cs[cat]
        print(f"  {cat:<14}  {size_by_cat[cat]:>6}  {s['n']:>5}  "
              f"{s['avg_ret']:>+8.1%}  {s['median']:>+8.1%}  {s['win_rate']:>6.0%}")
    print()

    # Monotonicity check
    avgs = [cs[c]["avg_ret"] for c in CATEGORIES if cs[c]["n"] > 0]
    cats_present = [c for c in CATEGORIES if cs[c]["n"] > 0]
    monotone = all(avgs[i] <= avgs[i + 1] for i in range(len(avgs) - 1)) if len(avgs) > 1 else None
    print("  Monotonic (higher score -> higher avg return)?  "
          f"{'YES' if monotone else 'NO' if monotone is not None else 'N/A'}")
    if cats_present:
        print(f"    Order by avg return: " +
              ", ".join(f"{c} {cs[c]['avg_ret']:+.1%}" for c in cats_present))
    print()

    # ── 3. Verdict on dynamic sizing ──────────────────────────────────────────
    print("SECTION 3 - VERDICT")
    print("-" * 78)
    score_better = ms["total_ret"] > m1["total_ret"]
    print(f"  Did score-based sizing improve total return?  "
          f"{'YES' if score_better else 'NO'} ({d_ret:+.1%})")
    print(f"  Did score-based sizing improve Sharpe?        "
          f"{'YES' if d_shp > 0 else 'NO'} ({d_shp:+.2f})")
    print()
    if monotone:
        print("  Composite score IS monotonically related to forward return in this")
        print("  sample -> dynamic position sizing by score is JUSTIFIED.")
    elif monotone is False:
        print("  Composite score is NOT monotonically related to forward return here")
        print("  -> the score behaves as a binary entry gate, not a sizing signal.")
        print("  Any V1-Score vs V1 difference is an artifact of allocation, not edge.")
    print()

    # ── 4. Chart ──────────────────────────────────────────────────────────────
    out_path = Path(__file__).parent.parent / "data" / "score_sizing_equity_curve.png"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        spy_curve = spy_close.reindex(master_cal, method="ffill")
        spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP

        fig, ax = plt.subplots(figsize=(11, 6))
        ax.plot(v1["daily_values"].index,     v1["daily_values"].values,
                label=f"V1 flat 10%  (${m1['final_value']:,.0f})", lw=1.6)
        ax.plot(vscore["daily_values"].index, vscore["daily_values"].values,
                label=f"V1-Score tiered  (${ms['final_value']:,.0f})", lw=1.6)
        ax.plot(spy_curve.index, spy_curve.values,
                label=f"SPY buy & hold  (${spy['final_value']:,.0f})",
                lw=1.2, ls="--", color="gray")
        ax.set_title("Portfolio value: V1 vs V1-Score vs SPY (2018-2024)")
        ax.set_ylabel("Portfolio value ($)")
        ax.set_xlabel("Date")
        ax.legend(loc="upper left")
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda x, _: f"${x/1000:.0f}k"))
        fig.tight_layout()
        fig.savefig(out_path, dpi=120)
        print(f"  Equity-curve chart saved to: {out_path}")
    except Exception as e:
        print(f"  (chart skipped: {e})")
    print()
    print(div)


if __name__ == "__main__":
    main()
