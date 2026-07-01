#!/usr/bin/env python3
"""Combined validation on the CLEAN universe (Top-100 point-in-time S&P 500).

Brings together the three separately-validated improvements on a survivorship-
free universe, for the final pre-merge read:

  A  Clean baseline   : Top-100 PIT, flat 10%,            idle cash 0%
  B  Money market     : Top-100 PIT, flat 10%,            idle cash = Fed funds
  C  Score-Plus       : Top-100 PIT, score_plus sizing,   idle cash 0%
  D  Full combination : Top-100 PIT, score_plus sizing,   idle cash = Fed funds   (primary)

Universe is point-in-time (membership from fja05680) restricted each month to the
100 largest by point-in-time market cap (raw close × EDGAR shares, 90-day lag).
Score-Plus: composite ≥ 0.70 gets 12% (else 10%), the extra funded from idle cash
only, skip if < 5% free. Fed funds: real historical FEDFUNDS from FRED, accrued
daily pro-rata. Same params throughout: 2018-2024, 252d exit, no SL, $100k, max 10.
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
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_pit_market_caps
from scripts.run_combined_validation import load_fedfunds
from scripts.run_portfolio_sim import (
    _HOLD_DAYS,
    _INITIAL_CAP,
    _MIN_POSITION,
    compute_metrics,
    load_all_data,
    spy_metrics,
)

_SIM_START = pd.Timestamp("2018-01-01")
_SIM_END = pd.Timestamp("2024-12-31")
_TOP_N = 100


def simulate(crossings_by_ticker, prices_wide, master_cal, sizing_mode, cash_mode,
             rate_on_cal, month_members, max_pos=10, regime_ok=None, blocked_log=None,
             entry_threshold=0.0, opens_wide=None, veto_fn=None, veto_log=None):
    """One run: membership gate × sizing (flat|score_plus) × cash (zero|fed_funds).

    regime_ok: optional bool array aligned to master_cal. When supplied, no NEW
    positions open on days where regime_ok[day]==False (existing positions are
    untouched; cash still accrues). Genuine new signals blocked this way are
    appended to blocked_log as (date, ticker) when blocked_log is not None.

    veto_fn: optional fail-closed 8-K veto callable (ticker, iso_date) ->
    (blocked, reason). Applied at the entry-commit point (an otherwise
    executable would-be trade); a True result skips the entry and, when
    veto_log is not None, appends (signal_date, ticker, comp, reason). Default
    None → no veto (prior studies reproduce exactly).
    """
    events_by_date = defaultdict(list)
    for ticker, crossings in crossings_by_ticker.items():
        for ts, comp, price, dd in crossings:
            if master_cal[0] <= ts <= master_cal[-1]:  # bound to this sim window
                events_by_date[ts].append((ticker, comp, price, dd))

    sim_prices = prices_wide.reindex(master_cal, method="ffill")
    prices_arr = sim_prices.values.astype(float)
    col_map = {c: i for i, c in enumerate(sim_prices.columns)}
    day_gap = master_cal.to_series().diff().dt.total_seconds().values / 86400.0
    day_gap[0] = 0.0

    # Parallel OPEN array for T+1-open fills (opt-in; None → legacy close fills).
    opens_arr = None
    if opens_wide is not None:
        opens_arr = (opens_wide.reindex(master_cal)
                               .reindex(columns=sim_prices.columns)
                               .values.astype(float))

    def _price(di, t):
        ci = col_map.get(t)
        return float(prices_arr[di, ci]) if ci is not None else float("nan")

    def _open(di, t):
        ci = col_map.get(t)
        return float(opens_arr[di, ci]) if (ci is not None and opens_arr is not None) else float("nan")

    def _port_val(di, cash, pos):
        v = cash
        for p in pos.values():
            px = _price(di, p["ticker"])
            v += p["shares"] * (px if not np.isnan(px) else p["entry_price"])
        return v

    def _cash_factor(di):
        if di == 0 or cash_mode == "zero":
            return 1.0
        r = rate_on_cal[di]
        if not np.isfinite(r):
            r = 0.0
        return (1.0 + r) ** (day_gap[di] / 365.0)

    cash = _INITIAL_CAP
    open_pos, last_entry, trades = {}, {}, []
    daily_values = np.zeros(len(master_cal))
    pid = 0

    for di, day in enumerate(master_cal):
        cash *= _cash_factor(di)

        # Exit positions scheduled for today
        for k in [k for k, v in list(open_pos.items()) if v["exit_idx"] == di]:
            p = open_pos.pop(k)
            ep = _price(di, p["ticker"])
            if np.isnan(ep):
                ep = p["entry_price"]
            cash += p["shares"] * ep
            trades.append({"ticker": p["ticker"], "entry_date": p["entry_date"].date(),
                           "ret": ep / p["entry_price"] - 1, "comp": p["comp"]})

        # Compute today's portfolio value BEFORE processing new signals
        # (so T+1 fills don't appear in today's portfolio)
        port_val = _port_val(di, cash, open_pos)
        daily_values[di] = port_val

        # Process new signals (may create positions for today or T+1)
        regime_blocked = regime_ok is not None and not bool(regime_ok[di])
        for ticker, comp, crossing_price, dd in sorted(events_by_date.get(day, []),
                                                       key=lambda x: -x[1]):
            if comp < entry_threshold:
                continue
            if ticker not in month_members.get((day.year, day.month), ()):
                continue
            fresh = not (ticker in last_entry and (di - last_entry[ticker]) < _HOLD_DAYS)
            if regime_blocked:
                # Block new entries during a bear regime; log genuine new signals.
                if fresh and blocked_log is not None:
                    blocked_log.append((day, ticker))
                continue
            if not fresh:
                continue
            if len(open_pos) >= max_pos:
                continue

            if sizing_mode == "flat":
                alloc = min(port_val * 0.10, cash)
            else:  # score_plus
                size_pct = 0.12 if comp >= 0.70 else 0.10
                if cash < port_val * 0.05:
                    continue
                alloc = min(port_val * size_pct, cash)
            if alloc < _MIN_POSITION:
                continue

            # Fill bar: same-day close (legacy) or next day's open (T+1, executable).
            if opens_arr is not None:
                fill_di = di + 1
                if fill_di > len(master_cal) - 1:   # signal on last bar → no fill
                    continue
                ep = _open(fill_di, ticker)
                if np.isnan(ep) or ep <= 0:
                    continue  # skip fills when next open is unavailable
            else:
                fill_di = di
                ep = _price(di, ticker)
                if np.isnan(ep) or ep <= 0:
                    ep = crossing_price
            if ep <= 0:
                continue
            # Fail-closed 8-K veto: block this otherwise-executable entry if the
            # ticker carries a recent distress filing as of the signal day.
            if veto_fn is not None:
                blocked, reason = veto_fn(ticker, day.date().isoformat())
                if blocked:
                    if veto_log is not None:
                        veto_log.append((day.date(), ticker, comp, reason))
                    continue
            pid += 1
            open_pos[pid] = {"ticker": ticker, "entry_date": master_cal[fill_di], "entry_price": ep,
                             "comp": comp, "shares": alloc / ep,
                             "exit_idx": min(fill_di + _HOLD_DAYS, len(master_cal) - 1)}
            last_entry[ticker] = fill_di
            cash -= alloc

    return {"daily_values": pd.Series(daily_values, index=master_cal), "trades": trades,
            "final_value": float(daily_values[-1])}


def main():
    print("Loading data (Top-100 point-in-time universe)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    spy_raw = prices_obj.get_prices("SPY", "2016-01-01", "2024-12-31")
    spy_close = spy_raw["Close"]
    master_cal = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)].index

    # Top-100 PIT membership per month (first trading day), reused all month.
    fmonths = {}
    for ts in master_cal:
        fmonths.setdefault((ts.year, ts.month), ts)
    union_full = sorted({t for ts in fmonths.values()
                         for t in get_universe(ts.date().isoformat())})
    prefetch_pit_market_caps(union_full, [ts.date().isoformat() for ts in fmonths.values()])
    month_members = {k: set(get_universe_top_n(ts.date().isoformat(), _TOP_N))
                     for k, ts in fmonths.items()}
    union = sorted(set().union(*month_members.values()))
    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} ({len(master_cal)} days)")
    print(f"  Top-{_TOP_N} union over window: {len(union)} distinct tickers")

    crossings_by_ticker, prices_wide, _ = load_all_data(prices_obj, fund, union)
    print(f"  Tickers with price data: {len(crossings_by_ticker)}")

    rate_on_cal = load_fedfunds().reindex(master_cal, method="ffill").values.astype(float)

    variants = [
        ("A · Clean baseline (flat, 0%)",        "flat",       "zero"),
        ("B · Money market (flat, FedFunds)",    "flat",       "fed_funds"),
        ("C · Score-Plus (0%)",                  "score_plus", "zero"),
        ("D · Full combo (Score-Plus+FedFunds)", "score_plus", "fed_funds"),
    ]
    metrics, curves = {}, {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for label, sizing, cash in variants:
            res = simulate(crossings_by_ticker, prices_wide, master_cal,
                           sizing, cash, rate_on_cal, month_members)
            metrics[label] = compute_metrics(res["daily_values"], _INITIAL_CAP)
            curves[label] = res["daily_values"]
            m = metrics[label]
            print(f"  {label:<40} final ${m['final_value']:>10,.0f}  "
                  f"CAGR {m['cagr']:+.1%}  Sharpe {m['sharpe']:.2f}")

    spy_m = spy_metrics(spy_close, master_cal)
    A = metrics[variants[0][0]]; B = metrics[variants[1][0]]
    C = metrics[variants[2][0]]; D = metrics[variants[3][0]]

    div = "=" * 94
    print("\n" + div)
    print("COMBINED VALIDATION on CLEAN universe (Top-100 PIT) — 2018-2024, $100k, V1 10%/max10/252d")
    print(div + "\n")
    print(f"  {'Variant':<40}  {'Final $':>11}  {'TotRet':>8}  {'CAGR':>7}  {'Sharpe':>7}  {'MaxDD':>7}")
    print("  " + "-" * 90)
    for label, _, _ in variants:
        m = metrics[label]
        print(f"  {label:<40}  ${m['final_value']:>10,.0f}  {m['total_ret']:>+7.1%}  "
              f"{m['cagr']:>+6.1%}  {m['sharpe']:>7.2f}  {m['max_dd']:>6.1%}")
    print(f"  {'SPY buy & hold':<40}  ${spy_m['final_value']:>10,.0f}  {spy_m['total_ret']:>+7.1%}  "
          f"{spy_m['cagr']:>+6.1%}  {spy_m['sharpe']:>7.2f}  {spy_m['max_dd']:>6.1%}")
    print()

    # ── Linearity check ────────────────────────────────────────────────────────
    impB = B["final_value"] - A["final_value"]
    impC = C["final_value"] - A["final_value"]
    expected = A["final_value"] + impB + impC
    inter = D["final_value"] - expected
    print("  LINEARITY CHECK — is D still additive on the clean universe?")
    print("  " + "-" * 90)
    print(f"    Baseline A:                      ${A['final_value']:>11,.0f}")
    print(f"    + Money-market lift (B−A):       ${impB:>+11,.0f}")
    print(f"    + Score-Plus lift  (C−A):        ${impC:>+11,.0f}")
    print(f"    = Expected D (linear):           ${expected:>11,.0f}")
    print(f"      Actual D:                      ${D['final_value']:>11,.0f}")
    print(f"      Interaction (actual−expected): ${inter:>+11,.0f}  "
          f"({'additive' if abs(inter) < 0.10 * (abs(impB)+abs(impC) or 1) else 'non-additive'})")
    print()

    # ── Key question ───────────────────────────────────────────────────────────
    beats = D["cagr"] > spy_m["cagr"] and D["sharpe"] > spy_m["sharpe"]
    print(div)
    print("KEY QUESTION — does D beat SPY on BOTH CAGR and Sharpe (clean universe)?")
    print(div)
    print(f"  D:   CAGR {D['cagr']:+.1%}  Sharpe {D['sharpe']:.2f}")
    print(f"  SPY: CAGR {spy_m['cagr']:+.1%}  Sharpe {spy_m['sharpe']:.2f}")
    print(f"  >>> {'YES' if beats else 'NO'} — D "
          f"{'beats' if beats else 'does NOT beat'} SPY on both, survivorship-free.")
    print()

    # ── Chart ──────────────────────────────────────────────────────────────────
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    styles = {
        variants[0][0]: dict(color="#888888", lw=1.8, ls="--"),
        variants[1][0]: dict(color="#2ca02c", lw=1.8),
        variants[2][0]: dict(color="#1f77b4", lw=1.8),
        variants[3][0]: dict(color="#d62728", lw=2.4),
    }
    spy_curve = spy_close.reindex(master_cal, method="ffill")
    spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP
    fig, ax = plt.subplots(figsize=(12, 7))
    for label, s in curves.items():
        ax.plot(s.index, s.values, label=label, **styles.get(label, {}))
    ax.plot(spy_curve.index, spy_curve.values, label="SPY buy & hold",
            color="#9467bd", lw=1.1, ls=":")
    ax.set_title("Combined validation on CLEAN universe (Top-100 PIT) — 2018-2024, $100k\n"
                 "Score-Plus sizing × Fed funds cash sleeve", fontsize=12)
    ax.set_ylabel("Portfolio value"); ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(True, alpha=0.3); ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out = Path(__file__).parent.parent / "results" / "research" / "combined_clean_universe.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130); plt.close(fig)
    print(f"Chart written: {out}")


if __name__ == "__main__":
    main()
