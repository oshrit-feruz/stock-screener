#!/usr/bin/env python3
"""Test the core hypothesis: the signal's alpha is sparse (crisis-recovery only),
so deploy it as a conditional OVERLAY on an SPY core instead of a standalone
always-on strategy.

Model (scripts/clean_sim.py, core="spy"): the portfolio is always fully
invested in SPY (no idle cash -> no drag). When the market is in a dislocation
(SPY drawdown from its trailing 252-session high >= gate, measured on the
signal day), eligible recovery signals fill at the next session's close —
funded by rotating OUT of SPY, up to max_pos concurrent sleeves. When a sleeve
exits (after `hold` sessions, or at a delisted name's final print) its proceeds
rotate back into SPY. gate = None means "always deploy" (isolates the cash-drag
fix); gate > 0 adds regime gating (isolates the sparse-alpha fix).

Judged on rolling 5-year windows vs pure SPY: does gating the signal to
dislocations finally produce a STABLE edge, or is it still a coin flip?
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_sim as cs  # noqa: E402

WIN_LEN = 5
_OUT = ROOT / "validation" / "spy_overlay.md"

# (gate: market DD threshold or None, hold, pct, max_pos, label)
CONFIGS = [
    (None, 504, 0.10, 10, "always-deploy (idle→SPY, no gate)"),
    (0.10, 504, 0.10, 10, "gate: market DD ≥ 10%"),
    (0.15, 504, 0.10, 10, "gate: market DD ≥ 15%"),
    (0.20, 504, 0.10, 10, "gate: market DD ≥ 20%"),
    (0.15, 504, 0.20, 10, "gate ≥15% / 20% sleeves"),
]


def _evaluate(full: cs.Window, windows: list[cs.Window], gate, hold, pct, mp, label) -> dict:
    fm = cs.metrics(cs.overlay_series(full, gate, hold, pct, mp), full.cal)
    ev = cs.evaluate_rolling(windows, lambda w: cs.overlay_series(w, gate, hold, pct, mp))
    row = {"label": label, "gate": gate, "full_cagr": fm["cagr"], "full_sharpe": fm["sharpe"],
           "full_dd": fm["max_dd"], **{k: ev[k] for k in ("beats", "n", "med_excess",
                                                              "med_sharpe", "med_dd")}}
    print(f"  {label:<34} full CAGR {fm['cagr']:+.1%} (Sharpe {fm['sharpe']:.2f}, "
          f"DD {fm['max_dd']:.0%}) | rolling beat {ev['beats']}/{ev['n']}, "
          f"med excess {ev['med_excess']:+.1%}, med Sharpe {ev['med_sharpe']:.2f}", flush=True)
    return row


def _report(rows: list[dict], full: cs.Window, spy_med_sharpe: float) -> str:
    best = max(rows, key=lambda r: (r["beats"], r["med_sharpe"]))
    L = ["# SPY-core + conditional signal overlay vs pure SPY\n",
         "Always fully invested in SPY; recovery signals fire only when the market is in a "
         "dislocation (SPY drawdown ≥ gate from its trailing 252-session high on the signal "
         "day), fill at the next session's close funded by rotating out of SPY, and rotate "
         "back on exit. Clean survivorship-free signal universe, 2-year sleeves, delisted "
         f"names sold at their final print. **SPY over {full.name}: CAGR "
         f"{full.spy['cagr']:+.1%}, Sharpe {full.spy['sharpe']:.2f}, MaxDD "
         f"{full.spy['max_dd']:.0%}. SPY median rolling {WIN_LEN}y Sharpe "
         f"{spy_med_sharpe:.2f}.**\n",
         "| config | full CAGR | full Sharpe | full MaxDD | rolling beat-SPY | "
         "med excess | med Sharpe | med DD |", "|---|--:|--:|--:|--:|--:|--:|--:|"]
    for r in rows:
        L.append(f"| {r['label']} | {r['full_cagr']:+.1%} | {r['full_sharpe']:.2f} | "
                 f"{r['full_dd']:.0%} | {r['beats']}/{r['n']} | {r['med_excess']:+.1%} | "
                 f"{r['med_sharpe']:.2f} | {r['med_dd']:.0%} |")
    L.append("\n## Read\n")
    L.append(f"- SPY's own rolling {WIN_LEN}y Sharpe is {spy_med_sharpe:.2f}; a config only "
             "helps if it beats SPY in a clear majority of windows AND lifts the median "
             "Sharpe above that.")
    L.append(f"- Best config: **{best['label']}** — beats SPY in {best['beats']}/{best['n']} "
             f"windows, median excess {best['med_excess']:+.1%}, median Sharpe "
             f"{best['med_sharpe']:.2f} (vs SPY {spy_med_sharpe:.2f}).")
    L.append("- Compare to the STANDALONE signal (run_clean_pit_rolling). If the overlay "
             "lifts the beat-rate and median excess clearly above the standalone's, the "
             "sparse-alpha / wrong-deployment diagnosis holds.")
    return "\n".join(L) + "\n"


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    full = cs.full_window(inp)
    windows = cs.rolling_windows(inp, WIN_LEN)
    spy_med = cs.spy_median_sharpe(windows)
    print(f"  {len(windows)} rolling {WIN_LEN}y windows; SPY median Sharpe {spy_med:.2f}\n",
          flush=True)
    rows = [_evaluate(full, windows, *c) for c in CONFIGS]
    _OUT.write_text(_report(rows, full, spy_med))
    print(f"\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
