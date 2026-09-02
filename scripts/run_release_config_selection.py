#!/usr/bin/env python3
"""Pick the best STANDALONE config to release by robust risk-adjusted return.

The full-window backtest is misleading (see run_clean_pit_rolling), so candidate
configurations are judged on their DISTRIBUTION of Sharpe across rolling 5-year
windows (2004-2024): median Sharpe (higher = better), dispersion (lower = more
stable), worst-window Sharpe, median excess CAGR vs SPY, and median drawdown.
Ranked by median rolling Sharpe. Pure price signal, clean survivorship-free
universe, next-session fills. Take-profit is excluded (the sweep showed it only
hurts).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_sim as cs  # noqa: E402

WIN_LEN = 5
_OUT = ROOT / "validation" / "release_config_selection.md"

# (hold, stop, pct, max_pos, label)
CANDIDATES = [
    (252, 0.00, 0.10, 10, "default 1y / 10%×10"),
    (378, 0.00, 0.10, 10, "1.5y / 10%×10"),
    (504, 0.00, 0.10, 10, "2y / 10%×10"),
    (756, 0.00, 0.10, 10, "3y / 10%×10"),
    (252, 0.40, 0.10, 10, "1y / SL−40% / 10%×10"),
    (504, 0.40, 0.10, 10, "2y / SL−40% / 10%×10"),
    (504, 0.30, 0.10, 10, "2y / SL−30% / 10%×10"),
    (504, 0.00, 0.05, 20, "2y / 5%×20 (diversified)"),
    (504, 0.00, 0.20, 5,  "2y / 20%×5 (concentrated)"),
    (504, 0.40, 0.20, 5,  "2y / SL−40% / 20%×5"),
    (756, 0.00, 0.20, 5,  "3y / 20%×5 (concentrated)"),
]


def _evaluate(windows: list[cs.Window], hold, stop, pct, mp, label) -> dict:
    cfg = cs.SimConfig(hold=hold, stop=stop, pct=pct, max_pos=mp)
    ev = cs.evaluate_rolling(windows, lambda w: cs.run(w.mkt, w.events, cfg).daily)
    row = {"label": label, "hold": hold, "stop": stop, "pct": pct, "mp": mp,
           "med_sharpe": ev["med_sharpe"], "std_sharpe": ev["std_sharpe"],
           "min_sharpe": ev["min_sharpe"], "med_excess": ev["med_excess"],
           "med_dd": ev["med_dd"], "beat_sharpe": ev["beats_sharpe"], "n_win": ev["n"]}
    print(f"  {label:<28} med Sharpe {row['med_sharpe']:.2f} (±{row['std_sharpe']:.2f}, "
          f"worst {row['min_sharpe']:.2f})  med excess {row['med_excess']:+.1%}  "
          f"med DD {row['med_dd']:.0%}", flush=True)
    return row


def _report(rows: list[dict], n_win: int, spy_med_sharpe: float) -> str:
    L = ["# Best standalone config to release — ranked by robust risk-adjusted return\n",
         f"Candidate configs judged on their distribution of Sharpe across "
         f"**{n_win} rolling {WIN_LEN}-year windows** (2004-2024), clean "
         f"survivorship-free universe, pure price signal, next-session fills. Ranked by "
         f"**median rolling Sharpe**. SPY's median rolling Sharpe over the same windows: "
         f"**{spy_med_sharpe:.2f}**.\n",
         "| rank | config | med Sharpe | ±std | worst | med excess vs SPY | med DD | "
         "beat-SPY-Sharpe |",
         "|---|---|--:|--:|--:|--:|--:|--:|"]
    for i, r in enumerate(rows, 1):
        L.append(f"| {i} | {r['label']} | **{r['med_sharpe']:.2f}** | {r['std_sharpe']:.2f} "
                 f"| {r['min_sharpe']:.2f} | {r['med_excess']:+.1%} | {r['med_dd']:.0%} "
                 f"| {r['beat_sharpe']}/{r['n_win']} |")
    win = rows[0]
    L.append(f"\n## Winner: **{win['label']}**\n")
    L.append(f"Median rolling Sharpe {win['med_sharpe']:.2f} vs SPY {spy_med_sharpe:.2f}, "
             f"std {win['std_sharpe']:.2f}, worst-window Sharpe {win['min_sharpe']:.2f}, "
             f"median drawdown {win['med_dd']:.0%}, beats SPY's Sharpe in "
             f"{win['beat_sharpe']}/{win['n_win']} windows (ranked on median Sharpe only; "
             "the other columns are context, not tie-breakers).\n")
    verdict = ("below" if win["med_sharpe"] < spy_med_sharpe else "above")
    L.append(f"Reminder: the winner's median rolling Sharpe is {verdict} SPY's, and the "
             "standalone edge is regime-dependent (per run_clean_pit_rolling). This is the "
             "most stable risk-adjusted STANDALONE config to ship IF building on the signal "
             "regardless — not a claim that it beats the index. The SPY-core overlay "
             "(run_spy_overlay) is the deployment that does.")
    return "\n".join(L) + "\n"


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    windows = cs.rolling_windows(inp, WIN_LEN)
    spy_med = cs.spy_median_sharpe(windows)
    print(f"  {len(windows)} rolling {WIN_LEN}y windows; SPY median Sharpe {spy_med:.2f}\n",
          flush=True)

    rows = [_evaluate(windows, *c) for c in CANDIDATES]
    rows.sort(key=lambda r: -r["med_sharpe"])
    _OUT.write_text(_report(rows, len(windows), spy_med))
    print(f"\nWINNER: {rows[0]['label']}  (med Sharpe {rows[0]['med_sharpe']:.2f})", flush=True)
    print(f"saved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
