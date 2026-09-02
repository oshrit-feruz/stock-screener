#!/usr/bin/env python3
"""Adaptive exit rules on the validated SPY-core conditional overlay.

The overlay (SPY core, signals gated to market DD >= 10%, 2-year sleeves) beats
SPY in 14/17 rolling windows but still exits each sleeve on a FIXED clock, so it
holds through give-backs (2015, 2022). This compares exit rules, all with the
504-day max hold as a backstop, on rolling 5-year windows vs SPY:

  fixed      — exit at 504 days (baseline)
  trail_X    — exit when close <= peak-since-entry * (1 - X)
  sma50      — exit when close < 50-day SMA, after a 63-day minimum hold
               (momentum broken; the min hold avoids immediate whipsaw)
  recover    — exit when close >= the pre-dip 252-day high captured at entry
               (recovery complete: a principled take-profit, not an arbitrary %)

Reports beat-rate, median excess, median Sharpe, median drawdown, and how each
rule's exits split between the adaptive trigger and the 504-day backstop.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

import run_clean_pit_backtest as base  # noqa: E402
from core.data.eodhd import fetch_eod  # noqa: E402

_INITIAL = base._INITIAL_CAP
_MIN_POS = base._MIN_POSITION
D, HOLD, PCT, MAXP = 0.10, 504, 0.10, 10
MIN_HOLD_SMA = 63
WIN_LEN = 5
_OUT = ROOT / "validation" / "adaptive_exit.md"

RULES = [("fixed", None), ("trail_15", 0.15), ("trail_20", 0.20), ("trail_25", 0.25),
         ("trail_30", 0.30), ("sma50", None), ("recover", None)]


def simulate(elig, panel, sma50, high252, cal, spy_px, dd, rule, param):
    events = defaultdict(list)
    w0, w1 = cal[0], cal[-1]
    for t, cross in elig.items():
        for ts, comp, close in cross:
            if w0 <= ts <= w1:
                events[ts].append((t, comp, close))
    prices = panel.reindex(cal, method="ffill")
    arr = prices.values.astype(float)
    sma = sma50.reindex(cal, method="ffill").values.astype(float)
    hi = high252.reindex(cal, method="ffill").values.astype(float)
    col = {c: i for i, c in enumerate(prices.columns)}
    spy = spy_px.reindex(cal, method="ffill").values.astype(float)
    ddv = dd.reindex(cal).values.astype(float)
    last_idx = len(cal) - 1

    def px(di, t):
        ci = col.get(t)
        return float(arr[di, ci]) if ci is not None else float("nan")

    shares_spy = _INITIAL / spy[0]
    pos: dict[int, dict] = {}
    last_entry: dict[str, int] = {}
    dvals = np.zeros(len(cal))
    pid = 0
    exits = defaultdict(int)
    trade_rets = []

    def total(di):
        v = shares_spy * spy[di]
        for p in pos.values():
            q = px(di, p["ticker"])
            v += p["shares"] * (q if not np.isnan(q) else p["entry_price"])
        return v

    for di in range(len(cal)):
        day = cal[di]
        for k in list(pos.keys()):
            p = pos[k]
            price = px(di, p["ticker"])
            if np.isnan(price):
                continue
            p["peak"] = max(p["peak"], price)
            held = di - p["entry_idx"]
            kind = None
            if di >= p["exit_idx"]:
                kind = "backstop"
            elif rule.startswith("trail") and price <= p["peak"] * (1 - param):
                kind = "trail"
            elif rule == "sma50" and held >= MIN_HOLD_SMA:
                ci = col.get(p["ticker"])
                s = sma[di, ci] if ci is not None else float("nan")
                if not np.isnan(s) and price < s:
                    kind = "sma"
            elif rule == "recover" and price >= p["target"]:
                kind = "recover"
            if kind:
                pos.pop(k)
                shares_spy += (p["shares"] * price) / spy[di]
                exits[kind] += 1
                trade_rets.append(price / p["entry_price"] - 1)
        if ddv[di] >= D:
            for t, comp, cprice in sorted(events.get(day, []), key=lambda x: -x[1]):
                if di + HOLD > last_idx:
                    continue
                if t in last_entry and (di - last_entry[t]) < HOLD:
                    continue
                if len(pos) >= MAXP:
                    continue
                tv = total(di)
                alloc = min(tv * PCT, shares_spy * spy[di])
                if alloc < _MIN_POS:
                    continue
                ep = px(di, t)
                if np.isnan(ep) or ep <= 0:
                    ep = cprice
                if ep <= 0:
                    continue
                ci = col.get(t)
                tgt = hi[di, ci] if ci is not None else float("nan")
                if np.isnan(tgt) or tgt <= ep:
                    tgt = float("inf")
                pid += 1
                pos[pid] = {"ticker": t, "entry_idx": di, "exit_idx": di + HOLD,
                            "entry_price": ep, "shares": alloc / ep, "peak": ep,
                            "target": tgt}
                last_entry[t] = di
                shares_spy -= alloc / spy[di]
        dvals[di] = total(di)
    return pd.Series(dvals, index=cal), dict(exits), trade_rets


def main():
    print("Loading...", flush=True)
    data = base.build_intermediate()
    topn = base.top_n_by_rebal(data)
    elig = base.eligible_crossings(data, topn)
    spy = fetch_eod("SPY.US", base._FETCH_START, base._FETCH_END, adjust=True)
    full_cal = spy["Close"].index
    full_cal = full_cal[(full_cal >= pd.Timestamp("2004-01-02")) & (full_cal <= base._SIM_END)]
    elig = base.snap_events_to_calendar(elig, full_cal)
    panel = base._load_close_panel(sorted(elig), full_cal)
    sma50 = panel.rolling(50, min_periods=50).mean()
    high252 = panel.rolling(252, min_periods=60).max()
    spy_px = spy["Close"]
    s = spy_px.reindex(full_cal, method="ffill")
    dd = (s.rolling(252, min_periods=1).max() - s) / s.rolling(252, min_periods=1).max()

    windows = []
    for sy in range(2004, 2025 - WIN_LEN + 1):
        w0 = pd.Timestamp(f"{sy}-01-01"); w1 = pd.Timestamp(f"{sy+WIN_LEN-1}-12-31")
        cw = full_cal[(full_cal >= w0) & (full_cal <= w1)]
        if len(cw) >= 252:
            windows.append((f"{sy}-{sy+WIN_LEN-1}", cw))
    spy_by_win = {n: base.spy_metrics(cw) for n, cw in windows}
    spy_med = float(np.median([spy_by_win[n]["sharpe"] for n, _ in windows]))

    rows = []
    for rule, param in RULES:
        full, ex_full, rets = simulate(elig, panel, sma50, high252, full_cal, spy_px, dd, rule, param)
        fm = base.metrics(full, full_cal)
        exc, shp, dds, beats = [], [], [], 0
        for n, cw in windows:
            ser, _, _ = simulate(elig, panel, sma50, high252, cw, spy_px, dd, rule, param)
            m = base.metrics(ser, cw); sm = spy_by_win[n]
            exc.append(m["cagr"] - sm["cagr"]); shp.append(m["sharpe"]); dds.append(m["max_dd"])
            if m["cagr"] > sm["cagr"]:
                beats += 1
        r = np.array(rets)
        rows.append({"rule": rule, "full_cagr": fm["cagr"], "full_sharpe": fm["sharpe"],
                     "full_dd": fm["max_dd"], "beats": beats, "n": len(windows),
                     "med_excess": float(np.median(exc)), "med_sharpe": float(np.median(shp)),
                     "med_dd": float(np.median(dds)), "exits": ex_full,
                     "avg_trade": float(r.mean()) if len(r) else 0.0,
                     "win": float((r > 0).mean()) if len(r) else 0.0, "n_trades": len(r)})
        print(f"  {rule:<9} full CAGR {fm['cagr']:+.1%} Sharpe {fm['sharpe']:.2f} DD {fm['max_dd']:.0%} "
              f"| roll beat {beats}/{len(windows)} exc {np.median(exc):+.1%} Sharpe "
              f"{np.median(shp):.2f} DD {np.median(dds):.0%} | exits {ex_full}", flush=True)

    L = ["# Adaptive exit rules on the SPY-core overlay (gate 10%, max hold 504)\n",
         f"Rolling {WIN_LEN}y windows vs SPY (SPY median rolling Sharpe {spy_med:.2f}). "
         "All rules keep the 504-day backstop.\n",
         "| rule | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | "
         "med Sharpe | med DD | avg trade | win% | exits (full) |",
         "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|"]
    for r in rows:
        L.append(f"| {r['rule']} | {r['full_cagr']:+.1%} | {r['full_sharpe']:.2f} | "
                 f"{r['full_dd']:.0%} | {r['beats']}/{r['n']} | {r['med_excess']:+.1%} | "
                 f"{r['med_sharpe']:.2f} | {r['med_dd']:.0%} | {r['avg_trade']:+.1%} | "
                 f"{r['win']:.0%} | {r['exits']} |")
    best = max(rows, key=lambda r: (r["med_sharpe"], r["beats"]))
    base_r = next(r for r in rows if r["rule"] == "fixed")
    L.append(f"\n## Read\n")
    L.append(f"- Baseline (fixed 504): beat {base_r['beats']}/{base_r['n']}, median Sharpe "
             f"{base_r['med_sharpe']:.2f}, median DD {base_r['med_dd']:.0%}.")
    L.append(f"- Best by robust Sharpe: **{best['rule']}** — beat {best['beats']}/{best['n']}, "
             f"median Sharpe {best['med_sharpe']:.2f}, median DD {best['med_dd']:.0%}, "
             f"median excess {best['med_excess']:+.1%}.")
    L.append("- A rule only earns its place if it lifts median Sharpe / cuts drawdown "
             "WITHOUT lowering the beat-rate — trailing stops that fire too early "
             "clip recovery winners just like a take-profit did.")
    _OUT.write_text("\n".join(L) + "\n")
    print(f"\nBEST: {best['rule']}\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
