#!/usr/bin/env python3
"""Out-of-sample validation of the dislocation-gate threshold.

We must not trust gate=10% just because it topped the 2004-2024 in-sample run.
Two checks:

  A. Sensitivity / plateau — sweep a fine gate grid on rolling 5y windows. If
     8-12% all behave similarly, the threshold is a plateau, not a lucky spike.
  B. Train/test split — pick the gate with the best EXCESS CAGR over SPY on a
     train half, then measure that same gate on the untouched test half (both
     directions). A threshold chosen out-of-sample that still beats SPY
     in-test supports (does not prove) generalisation.

SPY-core + conditional overlay, 2-year sleeves, 10%×10, clean universe,
next-session fills (scripts/clean_sim.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_sim as cs  # noqa: E402

GATE_GRID = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20]
HOLD, PCT, MAXP = 504, 0.10, 10
WIN_LEN = 5
_OUT = ROOT / "validation" / "overlay_oos.md"
SPLITS = [  # (train name, train start, train end, test name, test start, test end)
    ("2004-2013", "2004-01-02", "2013-12-31", "2014-2024", "2014-01-01", "2024-12-31"),
    ("2014-2024", "2014-01-01", "2024-12-31", "2004-2013", "2004-01-02", "2013-12-31"),
]


def _sensitivity(windows: list[cs.Window]) -> list[dict]:
    print("\nA. threshold sensitivity (rolling 5y):", flush=True)
    out = []
    for gate in GATE_GRID:
        ev = cs.evaluate_rolling(
            windows, lambda w, g=gate: cs.overlay_series(w, g, HOLD, PCT, MAXP))
        out.append({"gate": gate, "beat": ev["beats"], "n": ev["n"],
                    "med_excess": ev["med_excess"], "med_sharpe": ev["med_sharpe"]})
        print(f"   gate={gate:.0%}: beat {ev['beats']}/{ev['n']}, med excess "
              f"{ev['med_excess']:+.1%}, med Sharpe {ev['med_sharpe']:.2f}", flush=True)
    return out


def _perf(w: cs.Window, gate: float) -> dict:
    m = cs.metrics(cs.overlay_series(w, gate, HOLD, PCT, MAXP), w.cal)
    return {"cagr": m["cagr"], "sharpe": m["sharpe"], "dd": m["max_dd"],
            "excess": m["cagr"] - w.spy["cagr"], "spy_sharpe": w.spy["sharpe"],
            "beat": m["cagr"] > w.spy["cagr"]}


def _train_test(inp: cs.Inputs) -> list[dict]:
    def half(name, a, b):
        cal = inp.cal[(inp.cal >= a) & (inp.cal <= b)]
        return cs.make_window(inp, cal, name)

    print("\nB. train/test split:", flush=True)
    out = []
    for tr_name, tr0, tr1, te_name, te0, te1 in SPLITS:
        w_tr, w_te = half(tr_name, tr0, tr1), half(te_name, te0, te1)
        train = {g: _perf(w_tr, g) for g in GATE_GRID}
        best = max(GATE_GRID, key=lambda g: train[g]["excess"])
        te = _perf(w_te, best)
        out.append({"train": tr_name, "test": te_name, "best_gate": best,
                    "train_excess": train[best]["excess"], "te": te})
        print(f"   train {tr_name} → best gate={best:.0%} (train excess "
              f"{train[best]['excess']:+.1%}); TEST {te_name}: excess {te['excess']:+.1%}, "
              f"Sharpe {te['sharpe']:.2f} vs SPY {te['spy_sharpe']:.2f}, "
              f"{'BEATS' if te['beat'] else 'loses'}", flush=True)
    return out


def _report(sens: list[dict], tt: list[dict], n_windows: int) -> str:
    L = ["# Out-of-sample validation of the dislocation gate\n",
         "SPY-core + conditional overlay, 2y sleeves, 10%×10, clean universe, next-session "
         "fills.\n",
         "## A. Threshold sensitivity — rolling 5y windows (is 10% a plateau?)\n",
         "| gate (market DD) | beat SPY | median excess | median Sharpe |",
         "|---|--:|--:|--:|"]
    for r in sens:
        L.append(f"| {r['gate']:.0%} | {r['beat']}/{r['n']} | {r['med_excess']:+.1%} "
                 f"| {r['med_sharpe']:.2f} |")
    L.append("\n## B. Train / test split (gate chosen on train, measured on test)\n")
    L.append("| train (pick gate) | best gate | train excess | test | test excess | "
             "test Sharpe | vs SPY | verdict |")
    L.append("|---|--:|--:|---|--:|--:|--:|:--|")
    for r in tt:
        te = r["te"]
        L.append(f"| {r['train']} | {r['best_gate']:.0%} | {r['train_excess']:+.1%} | "
                 f"{r['test']} | {te['excess']:+.1%} | {te['sharpe']:.2f} | "
                 f"{te['spy_sharpe']:.2f} | {'✅ BEATS' if te['beat'] else '❌ loses'} |")
    plateau = [r for r in sens if r["med_excess"] > 0 and r["beat"] >= n_windows * 0.6]
    plateau_str = ", ".join(f"{r['gate']:.0%}" for r in plateau) if plateau else "none"
    both = all(r["te"]["beat"] for r in tt)
    L.append("\n## Read\n")
    L.append(f"- Positive, majority-beating gate values: {plateau_str}. A plateau (rather "
             "than a lone 10% spike) is evidence of parameter INSENSITIVITY — it supports "
             "robustness but does not by itself establish that the gate is not fit.")
    L.append("- The train-chosen gate " + ("beats" if both else "does NOT beat") +
             " SPY on the untouched test half in both directions. With only two splits "
             "this is supporting evidence, not proof of generalisation.")
    L.append(f"- Caveats: the {n_windows} five-year windows overlap (each year appears in "
             "up to five of them), so they are far from independent samples; and the edge "
             "is concentrated in a handful of dislocations (2008-09, 2020, 2022), so the "
             "effective sample of regime events is small.")
    return "\n".join(L) + "\n"


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    windows = cs.rolling_windows(inp, WIN_LEN)
    sens = _sensitivity(windows)
    tt = _train_test(inp)
    _OUT.write_text(_report(sens, tt, len(windows)))
    print(f"\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
