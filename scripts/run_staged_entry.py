#!/usr/bin/env python3
"""Staged / confirmed entry on the validated SPY-core overlay.

The last open lever is drawdown, and part of it is sleeve-level knife-catching:
the signal fires on "recovery detected", which inside a crash is often a
dead-cat bounce bought before the real bottom. Two remedies, on the validated
config (SPY core, gate 10%, fixed 504-session hold, 10%×10), all with the
next-session fill of scripts/clean_sim.py as the earliest possible entry:

  baseline     — full sleeve at the first session after the signal
  staged_N_S   — split the sleeve into N tranches bought S sessions apart
                 (dollar-cost into the recovery; hold clock runs from tranche 1)
  confirm_K    — wait K more sessions; enter the full sleeve only if the close
                 is still >= the first post-signal close (bottom confirmed),
                 else skip
  staged+conf  — confirm at K, then stage the sleeve from there

Portfolio metrics on rolling 5y windows vs SPY, plus sleeve-level stats (avg /
worst trade, share of trades below -30%) — the level staged entry should help.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import clean_sim as cs  # noqa: E402

GATE, HOLD, PCT, MAXP = 0.10, 504, 0.10, 10
WIN_LEN = 5
_OUT = ROOT / "validation" / "staged_entry.md"

# (label, n_tranches, step_sessions, confirm_sessions)
VARIANTS = [
    ("baseline",        1, 0,  0),
    ("staged_3x10d",    3, 10, 0),
    ("staged_4x5d",     4, 5,  0),
    ("staged_5x10d",    5, 10, 0),
    ("confirm_5d",      1, 0,  5),
    ("confirm_10d",     1, 0,  10),
    ("confirm_20d",     1, 0,  20),
    ("confirm10+stg3",  3, 10, 10),
]


class _StagedBook:
    """Overlay book whose sleeves are filled through scheduled tranches."""

    def __init__(self, w: cs.Window, n_tr: int, step: int, confirm: int):
        self.w, self.mkt = w, w.mkt
        self.n_tr, self.step, self.confirm = n_tr, step, confirm
        self.port = cs.Portfolio(self.mkt, "spy")
        self.pending: list[dict] = []     # scheduled fills
        self.key_pid: dict[str, int] = {}  # sleeve key -> pid once opened
        self.skipped = 0

    # ── helpers ──
    def _committed(self) -> int:
        """Open sleeves + sleeves reserved by pending fills that have not opened."""
        reserved = {q["key"] for q in self.pending if q["key"] not in self.key_pid}
        return len(self.port.pos) + len(reserved)

    def _schedule(self, di: int, ticker: str, key: str, dollars: float, n_tr: int,
                  step: int, check=None) -> None:
        for j in range(n_tr):
            self.pending.append({"di": di + j * step, "ticker": ticker, "key": key,
                                 "dollars": dollars / n_tr, "check": check})

    def _fill(self, q: dict, di: int) -> None:
        price = self.mkt.px(di, q["ticker"])
        if not price > 0:
            return
        if q["check"] is not None:                     # confirmation gate
            if price < q["check"]:
                self.skipped += 1
                return
            if self.n_tr > 1:                          # confirmed -> stage from here
                self._schedule(di + self.step, q["ticker"], q["key"],
                               q["dollars"] * (self.n_tr - 1) / self.n_tr, self.n_tr - 1,
                               self.step)
                q = {**q, "dollars": q["dollars"] / self.n_tr}
        dollars = min(q["dollars"], self.port.idle(di))
        if dollars < 1.0:
            return
        pid = self.key_pid.get(q["key"])
        if pid is None:
            if di + HOLD > self.mkt.n - 1:             # cannot complete from here
                self.port.excluded += 1
                return
            p = self.port.open(di, q["ticker"], dollars, price, HOLD)
            p["key"] = q["key"]
            self.key_pid[q["key"]] = p["pid"]
        elif pid in self.port.pos:
            self.port.add_to(pid, di, dollars, price)
        # else: sleeve already closed -> late tranche dropped

    def _exits(self, di: int) -> None:
        for pid in list(self.port.pos):
            p = self.port.pos[pid]
            price = self.mkt.px(di, p["ticker"])
            kind = "hold" if di >= p["exit_idx"] else (
                "delist" if self.mkt.delists_on(di, p["ticker"]) else None)
            if kind is None:
                continue
            self.port.close(di, pid, price if price == price else p["entry_price"], kind)
            self.pending = [q for q in self.pending if q["key"] != p["key"]]

    def _signal(self, ev: cs.Event, di: int) -> None:
        t = ev.ticker
        if self.port.locked(t, di, HOLD) or self._committed() >= MAXP:
            return
        price = self.mkt.px(di, t)
        if not price > 0:
            return
        alloc = self.port.value(di) * PCT
        if alloc < cs.MIN_POSITION:
            return
        first = di + self.confirm
        if first + HOLD > self.mkt.n - 1:
            self.port.excluded += 1
            return
        key = f"{t}|{di}"
        self.port.last_entry[t] = di                  # reserve the name
        if self.confirm > 0:
            self.pending.append({"di": first, "ticker": t, "key": key, "dollars": alloc,
                                 "check": price})
        else:
            self._schedule(di, t, key, alloc, self.n_tr, self.step)

    def run(self) -> tuple[pd.Series, dict]:
        daily = np.zeros(self.mkt.n)
        for di in range(self.mkt.n):
            self._exits(di)
            if self.mkt.dd is not None:
                for ev in self.w.events.get(di, ()):
                    if self.mkt.dd[ev.sig_idx] >= GATE:
                        self._signal(ev, di)
            due = [q for q in self.pending if q["di"] == di]
            self.pending = [q for q in self.pending if q["di"] != di]
            for q in due:
                self._fill(q, di)
            daily[di] = self.port.value(di)
        r = np.array([t["ret"] for t in self.port.trades])
        stats = {"n": len(r), "avg": float(r.mean()) if len(r) else 0.0,
                 "worst": float(r.min()) if len(r) else 0.0,
                 "win": float((r > 0).mean()) if len(r) else 0.0,
                 "big_loss": float((r < -0.30).mean()) if len(r) else 0.0,
                 "skipped": self.skipped}
        return pd.Series(daily, index=self.mkt.cal), stats


def _evaluate(label, n_tr, step, confirm, full: cs.Window, windows: list[cs.Window]) -> dict:
    series, st = _StagedBook(full, n_tr, step, confirm).run()
    fm = cs.metrics(series, full.cal)
    ev = cs.evaluate_rolling(windows, lambda w: _StagedBook(w, n_tr, step, confirm).run()[0])
    row = {"label": label, "full_cagr": fm["cagr"], "full_sharpe": fm["sharpe"],
           "full_dd": fm["max_dd"], **st,
           **{k: ev[k] for k in ("beats", "n", "med_excess", "med_sharpe", "med_dd")}}
    print(f"  {label:<15} full CAGR {fm['cagr']:+.1%} Sh {fm['sharpe']:.2f} DD {fm['max_dd']:.0%} "
          f"| roll beat {ev['beats']}/{ev['n']} exc {ev['med_excess']:+.1%} Sh "
          f"{ev['med_sharpe']:.2f} | sleeves n={st['n']} avg {st['avg']:+.1%} worst "
          f"{st['worst']:+.0%} big-loss {st['big_loss']:.0%} skipped {st['skipped']}", flush=True)
    return row


_MIN_SHARPE_GAIN = 0.03   # below this a variant is a tie with the baseline, not a win


def _pick_best(rows: list[dict]) -> dict:
    """Baseline unless a variant lifts median Sharpe by a meaningful margin
    without lowering the beat-rate or the average sleeve return."""
    b = next(r for r in rows if r["label"] == "baseline")
    better = [r for r in rows
              if r["med_sharpe"] >= b["med_sharpe"] + _MIN_SHARPE_GAIN
              and r["beats"] >= b["beats"] and r["avg"] >= b["avg"]]
    return max(better, key=lambda r: r["med_sharpe"]) if better else b


def _report(rows: list[dict], spy_med: float) -> str:
    L = ["# Staged / confirmed entry on the SPY-core overlay (gate 10%, fixed 504)\n",
         f"Rolling {WIN_LEN}y windows vs SPY (SPY median rolling Sharpe {spy_med:.2f}). "
         "Sleeve stats are over the full 2004-2024 run. Baseline = full sleeve at the "
         "first session after the signal.\n",
         "| variant | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | "
         "med Sharpe | med DD | sleeves | avg trade | worst | <−30% | skipped |",
         "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for r in rows:
        L.append(f"| {r['label']} | {r['full_cagr']:+.1%} | {r['full_sharpe']:.2f} | "
                 f"{r['full_dd']:.0%} | {r['beats']}/{r['n']} | {r['med_excess']:+.1%} | "
                 f"{r['med_sharpe']:.2f} | {r['med_dd']:.0%} | {r['n']} | {r['avg']:+.1%} | "
                 f"{r['worst']:+.0%} | {r['big_loss']:.0%} | {r['skipped']} |")
    b = next(r for r in rows if r["label"] == "baseline")
    best = _pick_best(rows)
    L.append("\n## Read\n")
    L.append(f"- Baseline: beat {b['beats']}/{b['n']}, median Sharpe {b['med_sharpe']:.2f}, "
             f"median DD {b['med_dd']:.0%}, avg sleeve {b['avg']:+.1%}, worst {b['worst']:+.0%}, "
             f"{b['big_loss']:.0%} of sleeves lose >30%.")
    L.append(f"- Best: **{best['label']}** — beat {best['beats']}/{best['n']}, "
             f"median Sharpe {best['med_sharpe']:.2f}, median DD {best['med_dd']:.0%}, avg sleeve "
             f"{best['avg']:+.1%}, worst {best['worst']:+.0%}, {best['big_loss']:.0%} lose >30%. "
             "(A variant counts as better only if it lifts median Sharpe by "
             f"≥{_MIN_SHARPE_GAIN:.2f} without lowering the beat-rate or the average sleeve; "
             "smaller gaps are noise.)")
    L.append("- Staging should show up as a smaller share of >30% losers and a better "
             "worst trade; confirmation as fewer knife-catches (skipped) — the question is "
             "whether either buys that without giving back the beat-rate.")
    return "\n".join(L) + "\n"


def main():
    print("Loading clean inputs...", flush=True)
    inp = cs.load_inputs()
    full = cs.full_window(inp)
    windows = cs.rolling_windows(inp, WIN_LEN)
    spy_med = cs.spy_median_sharpe(windows)
    rows = [_evaluate(*v, full, windows) for v in VARIANTS]
    _OUT.write_text(_report(rows, spy_med))
    print(f"\nBEST: {_pick_best(rows)['label']}\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
