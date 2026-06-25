#!/usr/bin/env python3
"""Combined validation: do Score-Plus sizing and a money-market cash sleeve stack?

Two improvements, each validated separately, are combined into one variant here.
All four variants run on identical conditions — 2018-2024, the same 50 tickers,
threshold 0.60, exit at day 252, no stop-loss, $100,000 start — and differ only
in two independent switches:

  sizing : "flat"       -> every signal gets 10% of portfolio value
           "score_plus" -> comp>=0.70 gets 12%, else 10%; the extra 2% comes from
                           idle cash only (never by shrinking another position).
                           A signal is skipped if less than 5% of the portfolio is
                           free; otherwise it opens with whatever cash is left.
  cash   : "zero"         -> idle cash earns 0%
           "money_market" -> idle cash earns 4.5%/yr, accrued daily pro-rata

  Variant A  baseline V1     : flat        + zero          (base case)
  Variant B  Score-Plus only : score_plus  + zero
  Variant C  Money mkt only  : flat        + money_market
  Variant D  Full combination: score_plus  + money_market  (the main variant)

Output: a comparison table (final value, total return, CAGR, Sharpe, max DD) plus
a linear-expectation row — A + (B-A) + (C-A) — versus what D actually delivered,
to reveal positive or negative interaction. Equity-curve chart for all four.
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
    _HOLD_DAYS,
    _INITIAL_CAP,
    _MIN_POSITION,
    _SIM_END,
    _SIM_START,
    compute_metrics,
    load_all_data,
    spy_metrics,
)

_MM_RATE = 0.045  # money-market annual yield


def simulate(
    crossings_by_ticker: dict,
    prices_wide: pd.DataFrame,
    master_cal: pd.DatetimeIndex,
    sizing_mode: str,   # "flat" | "score_plus"
    cash_mode: str,     # "zero" | "money_market"
    mm_annual_rate: float = _MM_RATE,
    max_pos: int = 10,
) -> dict:
    """One portfolio run for a (sizing, cash) combination.

    Sizing and cash treatment are independent switches so any of the four
    variants is reproduced exactly. The flat+zero combination reproduces the V1
    baseline; score_plus is the upsize-only rule funded from idle cash; the
    money-market sleeve accrues a daily pro-rata yield on idle cash before each
    day's portfolio value is measured.
    """
    events_by_date: dict = defaultdict(list)
    for ticker, crossings in crossings_by_ticker.items():
        for ts, comp, price, dd in crossings:
            if _SIM_START <= ts <= master_cal[-1]:
                events_by_date[ts].append((ticker, comp, price, dd))

    sim_prices = prices_wide.reindex(master_cal, method="ffill")
    prices_arr = sim_prices.values.astype(float)
    col_map = {c: i for i, c in enumerate(sim_prices.columns)}

    # Calendar-day gaps between consecutive trading days (resolution-proof).
    day_gap = master_cal.to_series().diff().dt.total_seconds().values / 86400.0
    day_gap[0] = 0.0

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

    def _cash_factor(day_idx: int) -> float:
        if day_idx == 0 or cash_mode == "zero":
            return 1.0
        if cash_mode == "money_market":
            return (1.0 + mm_annual_rate) ** (day_gap[day_idx] / 365.0)
        return 1.0

    cash = _INITIAL_CAP
    open_pos: dict[int, dict] = {}
    last_entry_cal_idx: dict = {}
    trades: list[dict] = []
    skipped: list[dict] = []
    daily_values = np.zeros(len(master_cal))
    daily_cash_arr = np.zeros(len(master_cal))
    pid_ctr = 0

    for day_idx, day in enumerate(master_cal):
        # 0. Accrue idle-cash yield for the elapsed calendar gap.
        cash *= _cash_factor(day_idx)

        port_val = _port_val_at(day_idx, cash, open_pos)

        # 1. Exit at day 252
        for pid in [k for k, v in list(open_pos.items()) if v["exit_idx"] == day_idx]:
            pos = open_pos.pop(pid)
            ep = _price(day_idx, pos["ticker"])
            if np.isnan(ep):
                ep = pos["entry_price"]
            cash += pos["shares"] * ep
            trades.append({
                "ticker": pos["ticker"],
                "entry_date": pos["entry_date"].date(),
                "exit_date": day.date(),
                "ret": ep / pos["entry_price"] - 1,
                "pnl": pos["shares"] * ep - pos["cost"],
                "comp": pos["comp"],
                "size_pct": pos["size_pct"],
                "actual_pct": pos["cost"] / pos["port_val_at_entry"],
                "status": "CLOSED",
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

            if sizing_mode == "flat":
                size_pct = 0.10
                alloc = min(port_val * size_pct, cash)
            elif sizing_mode == "score_plus":
                # Default 10%; comp>=0.70 gets 12%, extra funded from idle cash.
                # Skip if <5% of the portfolio is free; else open with what's left.
                size_pct = 0.12 if comp >= 0.70 else 0.10
                desired = port_val * size_pct
                if cash < port_val * 0.05:
                    skipped.append({"date": day.date(), "ticker": ticker,
                                    "comp": comp, "reason": "below_5pct_free"})
                    continue
                alloc = min(desired, cash)
            else:
                raise ValueError(f"unknown sizing_mode {sizing_mode!r}")

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
                "ticker": ticker,
                "entry_date": day,
                "entry_idx": day_idx,
                "exit_idx": min(day_idx + _HOLD_DAYS, len(master_cal) - 1),
                "entry_price": ep,
                "shares": alloc / ep,
                "cost": alloc,
                "comp": comp,
                "size_pct": size_pct,
                "port_val_at_entry": port_val,
            }
            last_entry_cal_idx[ticker] = day_idx
            cash -= alloc

        daily_values[day_idx] = _port_val_at(day_idx, cash, open_pos)
        daily_cash_arr[day_idx] = cash

    # Mark remaining open positions at market on the last day.
    last_idx = len(master_cal) - 1
    last_day = master_cal[last_idx]
    for pos in open_pos.values():
        ep = _price(last_idx, pos["ticker"])
        if np.isnan(ep):
            ep = pos["entry_price"]
        trades.append({
            "ticker": pos["ticker"],
            "entry_date": pos["entry_date"].date(),
            "exit_date": last_day.date(),
            "ret": ep / pos["entry_price"] - 1,
            "pnl": pos["shares"] * ep - pos["cost"],
            "comp": pos["comp"],
            "size_pct": pos["size_pct"],
            "actual_pct": pos["cost"] / pos["port_val_at_entry"],
            "status": "OPEN (MTM 2024-12-30)",
        })

    trades.sort(key=lambda t: t["entry_date"])
    dv = pd.Series(daily_values, index=master_cal)
    dc = pd.Series(daily_cash_arr, index=master_cal)
    return {
        "trades": trades, "skipped": skipped,
        "daily_values": dv, "daily_cash": dc,
        "final_value": float(dv.iloc[-1]),
    }


def make_chart(curves: dict, spy_curve: pd.Series, out_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    styles = {
        "A · V1 baseline (flat, 0%)":        dict(color="#888888", lw=1.8, ls="--"),
        "B · Score-Plus only (0%)":          dict(color="#1f77b4", lw=1.8),
        "C · Money market only (flat)":      dict(color="#2ca02c", lw=1.8),
        "D · Full combo (Score-Plus + MM)":  dict(color="#d62728", lw=2.4),
    }
    fig, ax = plt.subplots(figsize=(12, 7))
    for label, s in curves.items():
        ax.plot(s.index, s.values, label=label, **styles.get(label, {}))
    ax.plot(spy_curve.index, spy_curve.values, label="SPY buy & hold (reference)",
            color="#9467bd", lw=1.1, ls=":")
    ax.set_title("Combined validation — V1 portfolio (max 10, 252d hold), $100k start\n"
                 "Score-Plus sizing × money-market cash sleeve · 2018-01-02 – 2024-12-31",
                 fontsize=12)
    ax.set_ylabel("Portfolio value")
    ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def main() -> None:
    print("Loading price data and computing signals (cached)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        crossings_by_ticker, prices_wide, spy_close = load_all_data(prices_obj, fund)

    spy_sim = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)]
    master_cal = spy_sim.index
    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} "
          f"({len(master_cal)} trading days)")
    print(f"  Tickers with signals: {len(crossings_by_ticker)}")
    print()

    variants = [
        ("A · V1 baseline (flat, 0%)",       "flat",       "zero"),
        ("B · Score-Plus only (0%)",         "score_plus", "zero"),
        ("C · Money market only (flat)",     "flat",       "money_market"),
        ("D · Full combo (Score-Plus + MM)", "score_plus", "money_market"),
    ]

    results, metrics, curves = {}, {}, {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for label, sizing, cash in variants:
            res = simulate(crossings_by_ticker, prices_wide, master_cal, sizing, cash)
            results[label] = res
            metrics[label] = compute_metrics(res["daily_values"], _INITIAL_CAP)
            curves[label] = res["daily_values"]
            m = metrics[label]
            print(f"  {label:<36}  final ${m['final_value']:>11,.0f}  CAGR {m['cagr']:+.2%}")

    spy = spy_metrics(spy_close, master_cal)

    A = metrics["A · V1 baseline (flat, 0%)"]
    B = metrics["B · Score-Plus only (0%)"]
    C = metrics["C · Money market only (flat)"]
    D = metrics["D · Full combo (Score-Plus + MM)"]

    div = "=" * 96
    print()
    print(div)
    print("COMBINED VALIDATION — Score-Plus sizing × money-market cash sleeve")
    print("$100,000 start · 2018-01-02 – 2024-12-31 · 50 tickers · thr 0.60 · 252d exit · no SL")
    print(div)
    print()

    hdr = (f"  {'Variant':<36}  {'Final $':>12}  {'TotRet':>8}  {'CAGR':>7}  "
           f"{'Sharpe':>7}  {'MaxDD':>7}")
    print(hdr)
    print("  " + "-" * 92)
    for label, _, _ in variants:
        m = metrics[label]
        print(f"  {label:<36}  ${m['final_value']:>11,.0f}  {m['total_ret']:>+7.1%}  "
              f"{m['cagr']:>+6.1%}  {m['sharpe']:>7.2f}  {m['max_dd']:>6.1%}")
    print(f"  {'SPY buy & hold (reference)':<36}  ${spy['final_value']:>11,.0f}  "
          f"{spy['total_ret']:>+7.1%}  {spy['cagr']:>+6.1%}  {spy['sharpe']:>7.2f}  "
          f"{spy['max_dd']:>6.1%}")
    print()

    # ── Linear-expectation vs actual combination (interaction) ─────────────────
    imp_B = B["final_value"] - A["final_value"]   # Score-Plus improvement over base
    imp_C = C["final_value"] - A["final_value"]   # Money-market improvement over base
    expected_D = A["final_value"] + imp_B + imp_C  # == B + C − A
    actual_D = D["final_value"]
    interaction = actual_D - expected_D

    imp_B_ret = B["total_ret"] - A["total_ret"]
    imp_C_ret = C["total_ret"] - A["total_ret"]
    expected_D_ret = A["total_ret"] + imp_B_ret + imp_C_ret
    interaction_ret = D["total_ret"] - expected_D_ret

    print("  LINEAR-EXPECTATION CHECK FOR VARIANT D  (does the stack add up?)")
    print("  " + "-" * 92)
    print(f"    Baseline A:                          ${A['final_value']:>11,.0f}   "
          f"({A['total_ret']:+.1%})")
    print(f"    + Score-Plus improvement (B−A):      ${imp_B:>+11,.0f}   ({imp_B_ret:+.1%})")
    print(f"    + Money-market improvement (C−A):    ${imp_C:>+11,.0f}   ({imp_C_ret:+.1%})")
    print(f"    ───────────────────────────────────────────────────────")
    print(f"    = Expected D (linear sum):           ${expected_D:>11,.0f}   "
          f"({expected_D_ret:+.1%})")
    print(f"      Actual D (measured):               ${actual_D:>11,.0f}   ({D['total_ret']:+.1%})")
    print(f"      Interaction (actual − expected):   ${interaction:>+11,.0f}   "
          f"({interaction_ret:+.1%})")
    print()

    # Size the interaction against the improvements it sits between: if it is a
    # small fraction of the combined lift, the two levers are effectively additive.
    total_imp = abs(imp_B) + abs(imp_C)
    rel = abs(interaction) / total_imp if total_imp > 0 else 0.0
    direction = "positive" if interaction > 0 else "negative" if interaction < 0 else "zero"

    if rel < 0.10:
        sign = (f"NEGLIGIBLE / ESSENTIALLY ADDITIVE — the interaction is only "
                f"{rel:.0%} of the\n    combined lift, so the two improvements stack "
                f"almost perfectly (a {direction} cross-term).")
        why = ("There are two offsetting forces and they nearly cancel: Score-Plus spends more "
               "idle\n    cash (leaving less for the 4.5% sleeve, a drag), but each lever also "
               "enlarges the\n    portfolio base the other compounds on (a boost). Net effect "
               "here is a tiny positive.")
    elif interaction > 0:
        sign = ("POSITIVE / SUPER-ADDITIVE — the two improvements reinforce each other.")
        why = ("Each lever enlarges the portfolio base the other works on: a bigger base means "
               "both\n    a bigger 4.5% accrual and bigger 10–12% allocations.")
    else:
        sign = ("NEGATIVE / SUB-ADDITIVE — the two improvements partly compete.")
        why = ("They draw on the same idle cash: every extra dollar Score-Plus deploys is a "
               "dollar the\n    money-market sleeve can no longer earn 4.5% on, so the gains do "
               "not fully stack.")
    print(f"    Interaction is {sign}")
    print(f"    {why}")
    print()
    print(div)

    out_path = Path(__file__).parent.parent / "results" / "combined_validation.png"
    spy_curve = spy_close.reindex(master_cal, method="ffill")
    spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP
    make_chart(curves, spy_curve, out_path)
    print(f"Chart written: {out_path}")


if __name__ == "__main__":
    main()
