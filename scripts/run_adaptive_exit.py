#!/usr/bin/env python3
"""Adaptive exit rules on the validated SPY-core conditional overlay.

The overlay (SPY core, signals gated to market DD >= 10%, 2-year sleeves) still
exits each sleeve on a FIXED clock, so it holds through give-backs. This
compares exit rules, all with the 504-session max hold as a backstop, on
rolling 5-year windows vs SPY (scripts/clean_sim.py, next-session fills):

  fixed      — exit at 504 sessions (baseline)
  trail_X    — exit when close <= peak-since-entry * (1 - X)
  sma50      — exit when close < 50-day SMA, after a 63-session minimum hold
               (momentum broken; the min hold avoids immediate whipsaw)
  recover    — exit when close >= the pre-dip 252-day high captured at entry
               (recovery complete: a principled take-profit, not an arbitrary %)

Reports beat-rate, median excess, median Sharpe, median drawdown, and how each
rule's exits split between the adaptive trigger, the hold backstop and delistings.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_sim as cs  # noqa: E402

GATE, HOLD, PCT, MAXP = 0.10, 504, 0.10, 10
MIN_HOLD_SMA = 63
WIN_LEN = 5
_OUT = ROOT / "validation" / "adaptive_exit.md"
CFG = cs.SimConfig(hold=HOLD, pct=PCT, max_pos=MAXP, core="spy", gate_dd=GATE)

RULES = [("fixed", None), ("trail_15", 0.15), ("trail_20", 0.20), ("trail_25", 0.25),
         ("trail_30", 0.30), ("sma50", None), ("recover", None)]


def _hooks(rule: str, param, sma: np.ndarray, hi: np.ndarray):
    """(on_open, exit_rule) callbacks for clean_sim.run for one rule/window."""
    if rule == "fixed":
        return None, None

    def on_open(p, di, mkt):
        p["peak"] = p["entry_price"]
        tgt = hi[di, mkt.col[p["ticker"]]]
        p["target"] = tgt if (not math.isnan(tgt) and tgt > p["entry_price"]) else float("inf")

    def trail(p, di, price, mkt):
        p["peak"] = max(p["peak"], price)
        return "trail" if price <= p["peak"] * (1 - param) else None

    def sma50(p, di, price, mkt):
        if di - p["entry_idx"] < MIN_HOLD_SMA:
            return None
        s = sma[di, mkt.col[p["ticker"]]]
        return "sma" if (not math.isnan(s) and price < s) else None

    def recover(p, di, price, mkt):
        return "recover" if price >= p["target"] else None

    fn = {"sma50": sma50, "recover": recover}.get(rule, trail)
    return on_open, fn


def _simulate(w: cs.Window, rule: str, param, sma50: np.ndarray, hi252: np.ndarray) -> cs.SimResult:
    on_open, exit_rule = _hooks(rule, param, sma50, hi252)
    return cs.run(w.mkt, w.events, CFG, exit_rule=exit_rule, on_open=on_open)


def _evaluate(rule, param, full: cs.Window, windows: list[cs.Window], aux: dict) -> dict:
    res = _simulate(full, rule, param, *aux[full.name])
    fm = cs.metrics(res.daily, full.cal)
    ev = cs.evaluate_rolling(
        windows, lambda w: _simulate(w, rule, param, *aux[w.name]).daily)
    r = np.array([t["ret"] for t in res.trades])
    row = {"rule": rule, "full_cagr": fm["cagr"], "full_sharpe": fm["sharpe"],
           "full_dd": fm["max_dd"], "exits": dict(res.exits),
           "avg_trade": float(r.mean()) if len(r) else 0.0,
           "win": float((r > 0).mean()) if len(r) else 0.0, "n_trades": len(r),
           **{k: ev[k] for k in ("beats", "n", "med_excess", "med_sharpe", "med_dd")}}
    print(f"  {rule:<9} full CAGR {fm['cagr']:+.1%} Sharpe {fm['sharpe']:.2f} DD "
          f"{fm['max_dd']:.0%} | roll beat {ev['beats']}/{ev['n']} exc {ev['med_excess']:+.1%} "
          f"Sharpe {ev['med_sharpe']:.2f} DD {ev['med_dd']:.0%} | exits {dict(res.exits)}",
          flush=True)
    return row


def _report(rows: list[dict], spy_med: float) -> str:
    L = ["# Adaptive exit rules on the SPY-core overlay (gate 10%, max hold 504)\n",
         f"Rolling {WIN_LEN}y windows vs SPY (SPY median rolling Sharpe {spy_med:.2f}). "
         "All rules keep the 504-session backstop; next-session fills; delisted names "
         "sold at their final print.\n",
         "| rule | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | "
         "med Sharpe | med DD | avg trade | win% | exits (full) |",
         "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|"]
    for r in rows:
        L.append(f"| {r['rule']} | {r['full_cagr']:+.1%} | {r['full_sharpe']:.2f} | "
                 f"{r['full_dd']:.0%} | {r['beats']}/{r['n']} | {r['med_excess']:+.1%} | "
                 f"{r['med_sharpe']:.2f} | {r['med_dd']:.0%} | {r['avg_trade']:+.1%} | "
                 f"{r['win']:.0%} | {r['exits']} |")
    best = max(rows, key=lambda r: (r["med_sharpe"], r["beats"]))
    base = next(r for r in rows if r["rule"] == "fixed")
    L.append("\n## Read\n")
    L.append(f"- Baseline (fixed 504): beat {base['beats']}/{base['n']}, median Sharpe "
             f"{base['med_sharpe']:.2f}, median DD {base['med_dd']:.0%}.")
    L.append(f"- Best by robust Sharpe: **{best['rule']}** — beat {best['beats']}/{best['n']}, "
             f"median Sharpe {best['med_sharpe']:.2f}, median DD {best['med_dd']:.0%}, "
             f"median excess {best['med_excess']:+.1%}.")
    L.append("- A rule only earns its place if it lifts median Sharpe / cuts drawdown "
             "WITHOUT lowering the beat-rate — trailing stops that fire too early "
             "clip recovery winners just like a take-profit did.")
    return "\n".join(L) + "\n"


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    full = cs.full_window(inp)
    windows = cs.rolling_windows(inp, WIN_LEN)
    spy_med = cs.spy_median_sharpe(windows)
    # Auxiliary panels on the full calendar (rolling stats need history before
    # each window starts), aligned per window.
    sma50 = inp.panel.df.rolling(50, min_periods=50).mean()
    hi252 = inp.panel.df.rolling(252, min_periods=60).max()
    aux = {w.name: (w.mkt.aux(sma50), w.mkt.aux(hi252)) for w in [full, *windows]}

    rows = [_evaluate(rule, param, full, windows, aux) for rule, param in RULES]
    _OUT.write_text(_report(rows, spy_med))
    best = max(rows, key=lambda r: (r["med_sharpe"], r["beats"]))
    print(f"\nBEST: {best['rule']}\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
