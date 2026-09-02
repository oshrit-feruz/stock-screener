#!/usr/bin/env python3
"""Staged / confirmed entry on the validated SPY-core overlay.

The last open lever is drawdown, and part of it is sleeve-level knife-catching:
the signal fires on "recovery detected", which inside a crash is often a
dead-cat bounce bought before the real bottom. Two remedies, on the validated
config (SPY core, gate 10%, fixed 504-day hold, 10%×10):

  baseline     — full sleeve on the signal day
  staged_N_S   — split the sleeve into N tranches bought S trading days apart
                 (dollar-cost into the recovery; hold clock runs from tranche 1)
  confirm_K    — wait K days; enter the full sleeve only if the close is still
                 >= the signal-day close (bottom confirmed), else skip
  staged+conf  — confirm at K, then stage the sleeve from there

Portfolio metrics on rolling 5y windows vs SPY, plus sleeve-level stats (avg /
worst trade, share of trades below -30%) — the level staged entry should help.
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
WIN_LEN = 5
_OUT = ROOT / "validation" / "staged_entry.md"

# (label, n_tranches, step_days, confirm_days)
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


def simulate(elig, panel, cal, spy_px, dd, n_tr, step, confirm):
    events = defaultdict(list)
    w0, w1 = cal[0], cal[-1]
    for t, cross in elig.items():
        for ts, comp, close in cross:
            if w0 <= ts <= w1:
                events[ts].append((t, comp, close))
    prices = panel.reindex(cal, method="ffill")
    arr = prices.values.astype(float)
    col = {c: i for i, c in enumerate(prices.columns)}
    spy = spy_px.reindex(cal, method="ffill").values.astype(float)
    ddv = dd.reindex(cal).values.astype(float)
    last_idx = len(cal) - 1

    def px(di, t):
        ci = col.get(t)
        return float(arr[di, ci]) if ci is not None else float("nan")

    shares_spy = _INITIAL / spy[0]
    pos: dict[int, dict] = {}          # open sleeves
    pending: list[dict] = []           # scheduled fills: {di, ticker, dollars, pid|None, ...}
    last_entry: dict[str, int] = {}
    dvals = np.zeros(len(cal))
    pid = 0
    rets, skipped = [], 0

    def total(di):
        v = shares_spy * spy[di]
        for p in pos.values():
            q = px(di, p["ticker"])
            v += p["shares"] * (q if not np.isnan(q) else p["avg_px"])
        return v

    def n_committed():
        return len(pos) + len({p["key"] for p in pending if p["pid"] is None})

    for di in range(len(cal)):
        day = cal[di]
        # 1. exits (fixed clock from first fill)
        for k in [k for k, v in list(pos.items()) if di >= v["exit_idx"]]:
            p = pos.pop(k)
            ep = px(di, p["ticker"])
            if np.isnan(ep):
                ep = p["avg_px"]
            shares_spy += (p["shares"] * ep) / spy[di]
            rets.append(ep / p["avg_px"] - 1)
            pending[:] = [q for q in pending if q.get("pid") != k]  # drop late tranches
        # 2. scheduled fills due today
        due = [q for q in pending if q["di"] == di]
        pending[:] = [q for q in pending if q["di"] != di]
        for q in due:
            price = px(di, q["ticker"])
            if np.isnan(price) or price <= 0:
                continue
            if q.get("check") is not None:            # confirmation gate
                if price < q["check"]:
                    skipped += 1
                    continue
                # confirmed → open sleeve now (and stage the rest if requested)
                if q["n_tr"] > 1:
                    dollars = q["dollars"] / q["n_tr"]
                    for j in range(1, q["n_tr"]):
                        pending.append({"di": di + j * q["step"], "ticker": q["ticker"],
                                        "dollars": dollars, "pid": None, "key": q["key"],
                                        "check": None, "n_tr": 1, "step": 0, "attach": q["key"]})
                else:
                    dollars = q["dollars"]
                q = {**q, "dollars": dollars}
            dollars = min(q["dollars"], shares_spy * spy[di])
            if dollars < 1.0:
                continue
            sh = dollars / price
            shares_spy -= dollars / spy[di]
            attach = q.get("attach") or q.get("pid")
            existing = next((k for k, v in pos.items() if v["key"] == q["key"]), None)
            if existing is not None:
                p = pos[existing]
                p["avg_px"] = (p["avg_px"] * p["shares"] + price * sh) / (p["shares"] + sh)
                p["shares"] += sh
            else:
                if di + HOLD > last_idx:
                    shares_spy += dollars / spy[di]   # cannot complete → undo
                    continue
                pid += 1
                pos[pid] = {"ticker": q["ticker"], "exit_idx": di + HOLD, "shares": sh,
                            "avg_px": price, "key": q["key"]}
                last_entry[q["ticker"]] = di
                for r in pending:
                    if r["key"] == q["key"]:
                        r["pid"] = pid
        # 3. new signals (gated)
        if ddv[di] >= D:
            for t, comp, cprice in sorted(events.get(day, []), key=lambda x: -x[1]):
                if t in last_entry and (di - last_entry[t]) < HOLD:
                    continue
                if n_committed() >= MAXP:
                    continue
                ep = px(di, t)
                if np.isnan(ep) or ep <= 0:
                    ep = cprice
                if ep <= 0:
                    continue
                tv = total(di)
                alloc = tv * PCT
                if alloc < _MIN_POS:
                    continue
                key = f"{t}|{di}"
                if confirm > 0:
                    if di + confirm + HOLD > last_idx:
                        continue
                    pending.append({"di": di + confirm, "ticker": t, "dollars": alloc,
                                    "pid": None, "key": key, "check": ep,
                                    "n_tr": n_tr, "step": step})
                else:
                    if di + HOLD > last_idx:
                        continue
                    per = alloc / n_tr
                    for j in range(n_tr):
                        pending.append({"di": di + j * step, "ticker": t, "dollars": per,
                                        "pid": None, "key": key, "check": None,
                                        "n_tr": 1, "step": 0})
                last_entry[t] = di   # reserve so the same name is not re-signalled
                # fill tranche 0 today when step schedule starts at di
                due0 = [q for q in pending if q["di"] == di and q["key"] == key]
                for q in due0:
                    pending.remove(q)
                    dollars = min(q["dollars"], shares_spy * spy[di])
                    if dollars < 1.0:
                        continue
                    sh = dollars / ep
                    shares_spy -= dollars / spy[di]
                    pid += 1
                    pos[pid] = {"ticker": t, "exit_idx": di + HOLD, "shares": sh,
                                "avg_px": ep, "key": key}
                    for r in pending:
                        if r["key"] == key:
                            r["pid"] = pid
        dvals[di] = total(di)
    r = np.array(rets)
    stats = {"n": len(r), "avg": float(r.mean()) if len(r) else 0.0,
             "worst": float(r.min()) if len(r) else 0.0,
             "win": float((r > 0).mean()) if len(r) else 0.0,
             "big_loss": float((r < -0.30).mean()) if len(r) else 0.0, "skipped": skipped}
    return pd.Series(dvals, index=cal), stats


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
    for label, n_tr, step, confirm in VARIANTS:
        full, st = simulate(elig, panel, full_cal, spy_px, dd, n_tr, step, confirm)
        fm = base.metrics(full, full_cal)
        exc, shp, dds, beats = [], [], [], 0
        for n, cw in windows:
            ser, _ = simulate(elig, panel, cw, spy_px, dd, n_tr, step, confirm)
            m = base.metrics(ser, cw); sm = spy_by_win[n]
            exc.append(m["cagr"] - sm["cagr"]); shp.append(m["sharpe"]); dds.append(m["max_dd"])
            if m["cagr"] > sm["cagr"]:
                beats += 1
        rows.append({"label": label, "full_cagr": fm["cagr"], "full_sharpe": fm["sharpe"],
                     "full_dd": fm["max_dd"], "beats": beats, "n": len(windows),
                     "med_excess": float(np.median(exc)), "med_sharpe": float(np.median(shp)),
                     "med_dd": float(np.median(dds)), **st})
        print(f"  {label:<15} full CAGR {fm['cagr']:+.1%} Sh {fm['sharpe']:.2f} DD {fm['max_dd']:.0%} "
              f"| roll beat {beats}/{len(windows)} exc {np.median(exc):+.1%} Sh {np.median(shp):.2f} "
              f"| sleeves n={st['n']} avg {st['avg']:+.1%} worst {st['worst']:+.0%} "
              f"big-loss {st['big_loss']:.0%} skipped {st['skipped']}", flush=True)

    L = ["# Staged / confirmed entry on the SPY-core overlay (gate 10%, fixed 504)\n",
         f"Rolling {WIN_LEN}y windows vs SPY (SPY median rolling Sharpe {spy_med:.2f}). "
         "Sleeve stats are over the full 2004-2024 run.\n",
         "| variant | full CAGR | full Sharpe | full MaxDD | roll beat | med excess | "
         "med Sharpe | med DD | sleeves | avg trade | worst | <−30% | skipped |",
         "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for r in rows:
        L.append(f"| {r['label']} | {r['full_cagr']:+.1%} | {r['full_sharpe']:.2f} | "
                 f"{r['full_dd']:.0%} | {r['beats']}/{r['n']} | {r['med_excess']:+.1%} | "
                 f"{r['med_sharpe']:.2f} | {r['med_dd']:.0%} | {r['n']} | {r['avg']:+.1%} | "
                 f"{r['worst']:+.0%} | {r['big_loss']:.0%} | {r['skipped']} |")
    b = next(r for r in rows if r["label"] == "baseline")
    best = max(rows, key=lambda r: (r["med_sharpe"], r["beats"]))
    L.append("\n## Read\n")
    L.append(f"- Baseline: beat {b['beats']}/{b['n']}, median Sharpe {b['med_sharpe']:.2f}, "
             f"median DD {b['med_dd']:.0%}, avg sleeve {b['avg']:+.1%}, worst {b['worst']:+.0%}, "
             f"{b['big_loss']:.0%} of sleeves lose >30%.")
    L.append(f"- Best by robust Sharpe: **{best['label']}** — beat {best['beats']}/{best['n']}, "
             f"median Sharpe {best['med_sharpe']:.2f}, median DD {best['med_dd']:.0%}, avg sleeve "
             f"{best['avg']:+.1%}, worst {best['worst']:+.0%}, {best['big_loss']:.0%} lose >30%.")
    L.append("- Staging should show up as a smaller share of >30% losers and a better "
             "worst trade; confirmation as fewer knife-catches (skipped) — the question is "
             "whether either buys that without giving back the beat-rate.")
    _OUT.write_text("\n".join(L) + "\n")
    print(f"\nBEST: {best['label']}\nsaved -> {_OUT}", flush=True)


if __name__ == "__main__":
    main()
