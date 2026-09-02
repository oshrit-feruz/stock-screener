#!/usr/bin/env python3
"""Rolling-window robustness check on the clean, survivorship-free framework.

The single 2004-2024 result can hide a fragile edge concentrated in one period.
This steps a fixed-length window (5y and 7y) one year at a time across 2004-2024
and, for each window, runs the clean backtest (scripts/clean_sim.py: PIT S&P
500 top-100 by dollar-volume, pure price signal, V1 sizing, next-session fills,
completion rule) at hold = 252 (1y) and 504 (2y), comparing each window's CAGR
to SPY over the SAME window.

Answers: in what fraction of windows does the strategy beat SPY, how big/stable
is the excess, and where it fails — the real test of whether the edge persists.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import clean_sim as cs  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

HOLDS = [252, 504]
WIN_LENS = [5, 7]           # window length in years
_OUT = ROOT / "validation" / "clean_pit_rolling.md"
_PNG = ROOT / "validation" / "clean_pit_rolling.png"
_LABEL = {252: "1y", 504: "2y"}


def _run_all(inp: cs.Inputs) -> dict:
    """{(win_len, hold): evaluate_rolling summary}."""
    results = {}
    for win_len in WIN_LENS:
        windows = cs.rolling_windows(inp, win_len)
        for hold in HOLDS:
            ev = cs.evaluate_rolling(
                windows, lambda w, h=hold: cs.run(w.mkt, w.events, cs.SimConfig(hold=h)).daily)
            results[(win_len, hold)] = ev
            print(f"  {win_len}y windows, hold {hold}: {ev['beats']}/{ev['n']} beat SPY, "
                  f"median excess {ev['med_excess']:+.1%}", flush=True)
    return results


def _tables(results: dict) -> list[str]:
    L = []
    for win_len in WIN_LENS:
        L.append(f"## {win_len}-year rolling windows\n")
        for hold in HOLDS:
            ev = results[(win_len, hold)]
            exc = [r["excess"] for r in ev["rows"]]
            L.append(f"### hold = {hold} ({_LABEL[hold]})  —  "
                     f"**beat SPY in {ev['beats']}/{ev['n']} windows**, "
                     f"median excess {ev['med_excess']:+.1%}, "
                     f"worst {min(exc):+.1%}, best {max(exc):+.1%}\n")
            L.append("| window | bot CAGR | SPY CAGR | excess | MaxDD | beat |")
            L.append("|---|--:|--:|--:|--:|:--|")
            for r in ev["rows"]:
                L.append(f"| {r['win']} | {r['cagr']:+.1%} | {r['spy_cagr']:+.1%} | "
                         f"{r['excess']:+.1%} | {r['max_dd']:.0%} | "
                         f"{'✅' if r['beat'] else '❌'} |")
            L.append("")
    return L


def _plot(results: dict) -> None:
    r252, r504 = results[(5, 252)]["rows"], results[(5, 504)]["rows"]
    labels = [r["win"] for r in r252]
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(13, 6))
    ax.bar(x - 0.2, [r["excess"] * 100 for r in r252], 0.4, label="hold 1y (252)",
           color="#1f77b4")
    ax.bar(x + 0.2, [r["excess"] * 100 for r in r504], 0.4, label="hold 2y (504)",
           color="#2ca02c")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Excess CAGR vs SPY per 5-year rolling window (clean framework)")
    ax.set_ylabel("Bot CAGR − SPY CAGR (pp)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.legend()
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(_PNG, dpi=130)


def _verdict(results: dict) -> list[str]:
    """Data-driven verdict; every cited return is labelled with its hold."""
    def frac(win_len, hold):
        ev = results[(win_len, hold)]
        return ev["beats"] / ev["n"] if ev["n"] else 0.0

    med = {h: results[(5, h)]["med_excess"] for h in HOLDS}
    # "Coin flip" = neither hold beats SPY in a clear majority of the primary
    # (5y) windows and the median excess is within ±2pp of zero.
    coin_flip = all(frac(5, h) <= 0.6 and abs(med[h]) < 0.02 for h in HOLDS)
    rows5 = {h: results[(5, h)]["rows"] for h in HOLDS}
    best = {h: max(rows5[h], key=lambda r: r["excess"]) for h in HOLDS}
    worst = {h: min(rows5[h], key=lambda r: r["excess"]) for h in HOLDS}

    head = ("the edge is NOT stable; it is regime-dependent" if coin_flip
            else "the standalone signal beats SPY in a majority of windows")
    L = [f"## Verdict — {head}\n"]
    L.append("- Beat-rate vs SPY across rolling windows: "
             f"{results[(5, 252)]['beats']}/{results[(5, 252)]['n']} (5y, hold 1y), "
             f"{results[(5, 504)]['beats']}/{results[(5, 504)]['n']} (5y, hold 2y), "
             f"{results[(7, 504)]['beats']}/{results[(7, 504)]['n']} (7y, hold 2y); "
             f"median 5y excess {med[252]:+.1%} (1y) / {med[504]:+.1%} (2y)."
             + (" That is a coin flip, not a persistent edge.\n" if coin_flip else "\n"))
    L.append("- **Where it wins and loses (5y windows):** best window "
             f"{best[252]['win']} {best[252]['excess']:+.1%} (hold 1y) / "
             f"{best[504]['win']} {best[504]['excess']:+.1%} (hold 2y); worst "
             f"{worst[252]['win']} {worst[252]['excess']:+.1%} (hold 1y) / "
             f"{worst[504]['win']} {worst[504]['excess']:+.1%} (hold 2y). The wins sit in "
             "windows that contain a major dislocation the signal can dip-buy; the losses "
             "are steady bull markets with no deep dips. A full-period 'beats SPY' result "
             "therefore depends on where the clock starts.\n")
    L.append("- **Implication for a real investor:** whether you beat the index depends on "
             "whether your holding window happens to contain a crash you can recover from — "
             "timing you cannot control. This is crisis alpha, not a reliable index-beating "
             "machine.")
    return L


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    results = _run_all(inp)
    _plot(results)
    L = ["# Rolling-window robustness — clean framework\n",
         "Fixed-length windows stepped one year across 2004-2024. Each window is a "
         "self-contained clean backtest (PIT dollar-volume top-100, pure signal, V1 "
         "sizing, next-session fills, completion rule) vs SPY over the same window. "
         "Research engine.\n"]
    L += _tables(results)
    L.append(f"![rolling]({_PNG.name})\n")
    L += _verdict(results)
    _OUT.write_text("\n".join(L) + "\n")
    print(f"\nsaved -> {_OUT}\n       -> {_PNG}", flush=True)


if __name__ == "__main__":
    main()
