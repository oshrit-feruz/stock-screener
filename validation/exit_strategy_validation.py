#!/usr/bin/env python3
"""
Exit Strategy Validation — Stage 5b Recovery Entry Detector.

Loads the same HIGH entry universe, builds 252-day forward price paths,
and simulates 5 exit families.  Produces:
  results/exit_strategy_results.txt
  validation/stage_5b_exit_selection.md
"""
from __future__ import annotations

import sys
from datetime import date
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from core.signals.recovery_score import (
    BUY_THRESHOLD,
    compute_recovery_signals,
    passes_quality_gate,
)

# ── ground-truth constants (Stage 5b, do NOT recompute) ──────────────────────
_RANDOM_MEAN  = 0.223    # 22.3% — RANDOM 252d mean
_BH_MEAN      = 0.344    # 34.4% — HIGH 252d buy-and-hold mean
_SPREAD_FLOOR = 0.080    # eligibility: spread_vs_random > 8pp

_WARMUP_START = "2016-01-01"
_START_DATE   = "2018-01-01"
_END_DATE     = "2024-12-31"

_RESULTS_DIR = Path(__file__).parent.parent / "results"
_DOCS_DIR    = Path(__file__).parent


# ── data loading ──────────────────────────────────────────────────────────────

def _prefetch_quality(ticker: str, fundamentals) -> dict[int, bool | None]:
    result: dict[int, bool | None] = {}
    if fundamentals is None:
        return result
    for year in range(2017, 2026):
        snap = fundamentals.get_snapshot(ticker, date(year, 12, 31))
        result[year] = passes_quality_gate(snap)
    return result


def load_high_entries(prices: PriceData, fundamentals) -> list[dict]:
    """Return HIGH-signal entries with their forward 252-day price-ratio paths."""
    entries = []
    start_ts = pd.Timestamp(_START_DATE)
    end_ts   = pd.Timestamp(_END_DATE)

    for ticker in VALIDATION_UNIVERSE:
        ohlcv = prices.get_prices(ticker, _WARMUP_START, _END_DATE)
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
            continue

        scored    = compute_recovery_signals(ohlcv)
        close_arr = scored["Close"].to_numpy(dtype=float)
        comp_arr  = scored["composite_score"].to_numpy(dtype=float)
        quality   = _prefetch_quality(ticker, fundamentals)

        for i, (ts, comp) in enumerate(zip(scored.index, comp_arr)):
            if ts < start_ts or ts > end_ts:
                continue
            gate = quality.get(ts.year)
            if gate is not True:
                continue
            if np.isnan(comp) or comp < BUY_THRESHOLD:
                continue
            ep = close_arr[i]
            if ep <= 0:
                continue

            ahead = close_arr[i + 1 : min(i + 253, len(close_arr))]
            if len(ahead) == 0:
                continue

            entries.append({
                "ticker": ticker,
                "date":   ts.date(),
                "path":   (ahead / ep),   # numpy array of price/entry
                "n_days": len(ahead),
            })

    return entries


def build_matrices(entries: list[dict]) -> tuple:
    """
    Filter to full-252-day entries, return:
        full_entries, paths (N,252), rmin (N,252), rmax (N,252)
    """
    full = [e for e in entries if e["n_days"] >= 252]
    N    = len(full)
    paths = np.vstack([e["path"][:252] for e in full])  # (N, 252)
    rmin  = np.minimum.accumulate(paths, axis=1)
    rmax  = np.maximum.accumulate(paths, axis=1)
    return full, paths, rmin, rmax


# ── metric computation ────────────────────────────────────────────────────────

def metrics(name: str, family: str,
            rets: np.ndarray, mins: np.ndarray, hold: np.ndarray,
            n_excluded: int = 0) -> dict:
    r, m, h = np.asarray(rets, float), np.asarray(mins, float), np.asarray(hold, float)
    n          = len(r)
    mean_r     = float(np.mean(r))
    spread     = mean_r - _RANDOM_MEAN
    capture    = mean_r / _BH_MEAN
    pct_pos    = float(np.mean(r > 0))
    pct_el     = float(np.mean(r < 0))
    pct_hit20  = float(np.mean(m <= 0.80))
    ux         = capture * (1 - pct_hit20) * (1 - pct_el)
    return dict(
        name       = name,
        family     = family,
        n          = n,
        nexcl      = n_excluded,
        mean       = mean_r,
        median     = float(np.median(r)),
        std        = float(np.std(r)),
        pct_pos    = pct_pos,
        p10        = float(np.percentile(r, 10)),
        p90        = float(np.percentile(r, 90)),
        pct_hit20  = pct_hit20,
        pct_el     = pct_el,
        avg_hold   = float(np.mean(h)),
        spread     = spread,
        capture    = capture,
        ux         = ux,
        eligible   = (spread >= _SPREAD_FLOOR),
    )


# ── Family 1 — time-based, no stop ───────────────────────────────────────────

def family1_time(paths: np.ndarray, rmin: np.ndarray, n_total: int) -> list[dict]:
    N, T = paths.shape
    results = []
    for horizon in [63, 126, 189, 252]:
        idx  = horizon - 1
        rets = paths[:, idx] - 1
        mins = rmin[:, idx]
        hold = np.full(N, horizon, dtype=float)
        nexcl = n_total - N if horizon == 252 else 0
        results.append(metrics(f"Hold {horizon:3d}d", "F1", rets, mins, hold, nexcl))
    return results


# ── Family 2 — wide hard stop, no time limit ─────────────────────────────────

def family2_hard_stop(paths: np.ndarray, rmin: np.ndarray) -> list[dict]:
    N, T = paths.shape
    results = []
    for stop_lvl in [0.25, 0.30, 0.35, 0.40]:
        sp        = 1 - stop_lvl
        below     = paths <= sp
        stopped   = below.any(axis=1)
        first_hit = np.where(stopped, np.argmax(below, axis=1), T - 1)

        rets = paths[np.arange(N), first_hit] - 1
        mins = rmin[np.arange(N), first_hit]
        hold = (first_hit + 1).astype(float)

        # extra: % of 252d-winners stopped out
        bh_win    = paths[:, -1] > 1.0
        pct_ws    = float(np.mean(stopped & bh_win)) / float(max(np.mean(bh_win), 1e-9))

        m = metrics(f"Stop {int(stop_lvl*100):2d}%", "F2", rets, mins, hold)
        m["pct_stopped"]          = float(np.mean(stopped))
        m["pct_winners_stopped"]  = pct_ws
        results.append(m)
    return results


# ── Family 3 — trailing stop after profit trigger ────────────────────────────

def family3_trailing(paths: np.ndarray, rmax: np.ndarray, rmin: np.ndarray) -> list[dict]:
    N, T = paths.shape
    results = []
    for profit_trg in [0.10, 0.15, 0.20]:
        for trail_pct in [0.08, 0.10, 0.15]:
            trigger     = 1 + profit_trg
            above_trg   = paths >= trigger
            armed_ever  = above_trg.any(axis=1)
            first_arm   = np.where(armed_ever, np.argmax(above_trg, axis=1), T)

            trail_level = rmax * (1 - trail_pct)   # running peak since entry
            below_trail = paths <= trail_level

            rets = np.empty(N)
            mins = np.empty(N)
            hold = np.empty(N)
            n_armed = 0
            ret_arm_stop: list[float] = []
            ret_never_arm: list[float] = []

            for i in range(N):
                arm = int(first_arm[i])
                if arm >= T:
                    # Never reached trigger — exit at 252d
                    rets[i] = paths[i, -1] - 1
                    mins[i] = rmin[i, -1]
                    hold[i] = T
                    ret_never_arm.append(rets[i])
                    continue

                n_armed += 1
                after = below_trail[i, arm:]
                if after.any():
                    stop_day = arm + int(np.argmax(after))
                    rets[i]  = paths[i, stop_day] - 1
                    mins[i]  = rmin[i, stop_day]
                    hold[i]  = stop_day + 1
                    ret_arm_stop.append(rets[i])
                else:
                    rets[i]  = paths[i, -1] - 1
                    mins[i]  = rmin[i, -1]
                    hold[i]  = T

            name = f"Arm+{int(profit_trg*100)}% Tr{int(trail_pct*100)}%"
            m = metrics(name, "F3", rets, mins, hold)
            m["pct_armed"]        = float(n_armed / N)
            m["mean_armed_stop"]  = float(np.mean(ret_arm_stop))  if ret_arm_stop  else None
            m["mean_never_armed"] = float(np.mean(ret_never_arm)) if ret_never_arm else None
            results.append(m)
    return results


# ── Family 4 — hybrid: time + emergency stop ─────────────────────────────────

def family4_hybrid(paths: np.ndarray, rmin: np.ndarray) -> list[dict]:
    N, T = paths.shape
    results = []
    for horizon in [126, 189, 252]:
        for emerg in [0.30, 0.40]:
            ep     = 1 - emerg
            below  = paths[:, :horizon] <= ep
            stop_f = below.any(axis=1)
            f_stop = np.where(stop_f, np.argmax(below, axis=1), horizon - 1)

            exit_idx = np.where(stop_f, f_stop, horizon - 1)
            rets = paths[np.arange(N), exit_idx] - 1
            mins = rmin[np.arange(N), exit_idx]
            hold = (exit_idx + 1).astype(float)

            pct_stop = float(np.mean(stop_f))
            ms  = float(np.mean(rets[stop_f]))   if stop_f.any()  else None
            msv = float(np.mean(rets[~stop_f]))  if (~stop_f).any() else None

            m = metrics(f"H{horizon}d E{int(emerg*100)}%", "F4", rets, mins, hold)
            m["pct_stopped"]      = pct_stop
            m["mean_if_stopped"]  = ms
            m["mean_if_survived"] = msv
            results.append(m)
    return results


# ── Family 5 — profit target + emergency stop ────────────────────────────────

def family5_target_stop(paths: np.ndarray, rmin: np.ndarray) -> list[dict]:
    N, T = paths.shape
    results = []
    for target in [0.20, 0.25, 0.30]:
        for emerg in [0.30, 0.40]:
            tp   = 1 + target
            ep   = 1 - emerg

            th   = paths >= tp
            sh   = paths <= ep
            ft   = np.where(th.any(axis=1), np.argmax(th, axis=1), T)
            fs   = np.where(sh.any(axis=1), np.argmax(sh, axis=1), T)

            via_tgt  = th.any(axis=1) & (ft <= fs)
            via_stop = sh.any(axis=1) & (fs < ft)
            via_time = ~th.any(axis=1) & ~sh.any(axis=1)

            exit_idx = np.full(N, T - 1, dtype=int)
            fired    = via_tgt | via_stop
            exit_idx[fired] = np.minimum(ft[fired], fs[fired])

            rets = paths[np.arange(N), exit_idx] - 1
            mins = rmin[np.arange(N), exit_idx]
            hold = (exit_idx + 1).astype(float)

            mt  = float(np.mean(rets[via_tgt]))  if via_tgt.any()  else None
            ms  = float(np.mean(rets[via_stop])) if via_stop.any() else None
            mti = float(np.mean(rets[via_time])) if via_time.any() else None

            name = f"Tgt+{int(target*100)}% E{int(emerg*100)}%"
            m = metrics(name, "F5", rets, mins, hold)
            m["pct_via_target"] = float(np.mean(via_tgt))
            m["pct_via_stop"]   = float(np.mean(via_stop))
            m["pct_via_time"]   = float(np.mean(via_time))
            m["mean_via_target"] = mt
            m["mean_via_stop"]   = ms
            m["mean_via_time"]   = mti
            results.append(m)
    return results


# ── output formatting ─────────────────────────────────────────────────────────

_HDR = (
    f"{'Variant':<16} {'n':>5}  {'Mean':>7} {'Median':>7} {'%Pos':>6}  "
    f"{'P10':>7} {'P90':>7}  {'%Hit20':>7} {'%ExL':>6}  "
    f"{'Hold':>5}  {'Spread':>7} {'Capt':>6}  {'UX':>6}  E?"
)
_SEP = "-" * len(_HDR)


def _pct(v: float | None, w: int = 7) -> str:
    if v is None:
        return " " * (w - 4) + " N/A"
    return f"{v:+{w}.1%}"


def _row(m: dict) -> str:
    e = "YES" if m["eligible"] else " NO"
    return (
        f"{m['name']:<16} {m['n']:>5}  "
        f"{_pct(m['mean'])} {_pct(m['median'])} {m['pct_pos']:>6.1%}  "
        f"{_pct(m['p10'])} {_pct(m['p90'])}  "
        f"{m['pct_hit20']:>7.1%} {m['pct_el']:>6.1%}  "
        f"{m['avg_hold']:>5.0f}  "
        f"{_pct(m['spread'])} {m['capture']:>6.1%}  "
        f"{m['ux']:>6.3f}  {e}"
    )


def write_family_block(buf: StringIO, title: str, results: list[dict],
                       extra_lines: list[str] | None = None) -> None:
    buf.write(f"\n{'='*70}\n{title}\n{'='*70}\n")
    buf.write(_HDR + "\n" + _SEP + "\n")
    for m in results:
        buf.write(_row(m) + "\n")
    if extra_lines:
        buf.write(_SEP + "\n")
        for line in extra_lines:
            buf.write(line + "\n")


def extra_f2(results: list[dict]) -> list[str]:
    lines = ["% of eventual 252d-winners stopped out:"]
    for m in results:
        lines.append(
            f"  {m['name']:<16}  {m['pct_stopped']:5.1%} stopped  |  "
            f"{m['pct_winners_stopped']:5.1%} of 252d-winners stopped"
        )
    return lines


def extra_f3(results: list[dict]) -> list[str]:
    lines = [f"  {'Variant':<16} {'%Armed':>8}  {'Mean(armed+stop)':>17}  {'Mean(never armed)':>18}"]
    for m in results:
        ms  = f"{m['mean_armed_stop']:+.1%}"  if m["mean_armed_stop"]  is not None else "  N/A"
        mna = f"{m['mean_never_armed']:+.1%}" if m["mean_never_armed"] is not None else "  N/A"
        lines.append(f"  {m['name']:<16} {m['pct_armed']:>8.1%}  {ms:>17}  {mna:>18}")
    return lines


def extra_f4(results: list[dict]) -> list[str]:
    lines = [f"  {'Variant':<16} {'%Stopped':>9}  {'Mean(stopped)':>14}  {'Mean(survived)':>15}"]
    for m in results:
        ms  = f"{m['mean_if_stopped']:+.1%}"  if m["mean_if_stopped"]  is not None else "  N/A"
        msv = f"{m['mean_if_survived']:+.1%}" if m["mean_if_survived"] is not None else "  N/A"
        lines.append(f"  {m['name']:<16} {m['pct_stopped']:>9.1%}  {ms:>14}  {msv:>15}")
    return lines


def extra_f5(results: list[dict]) -> list[str]:
    lines = [f"  {'Variant':<16} {'%Target':>8} {'%Stop':>7} {'%Time':>7}  "
             f"{'Mean(tgt)':>10} {'Mean(stop)':>11} {'Mean(time)':>11}"]
    for m in results:
        mt  = f"{m['mean_via_target']:+.1%}" if m["mean_via_target"] is not None else "  N/A"
        ms  = f"{m['mean_via_stop']:+.1%}"   if m["mean_via_stop"]   is not None else "  N/A"
        mti = f"{m['mean_via_time']:+.1%}"   if m["mean_via_time"]   is not None else "  N/A"
        lines.append(
            f"  {m['name']:<16} {m['pct_via_target']:>8.1%} {m['pct_via_stop']:>7.1%} "
            f"{m['pct_via_time']:>7.1%}  {mt:>10} {ms:>11} {mti:>11}"
        )
    return lines


def write_ranking(buf: StringIO, all_results: list[dict]) -> dict:
    buf.write(f"\n{'='*70}\nFINAL RANKING\n{'='*70}\n")
    buf.write(f"Eligibility constraint: spread_vs_random >= +{_SPREAD_FLOOR:.0%}\n")
    buf.write(f"  (i.e. mean_return >= {_RANDOM_MEAN + _SPREAD_FLOOR:.1%})\n\n")

    eligible = [m for m in all_results if m["eligible"]]
    ranked   = sorted(eligible, key=lambda x: x["ux"], reverse=True)

    buf.write("TOP 5 ELIGIBLE BY UX SCORE:\n")
    buf.write(_HDR + "\n" + _SEP + "\n")
    for m in ranked[:5]:
        buf.write(_row(m) + "\n")

    # best per family
    buf.write("\nBEST ELIGIBLE PER FAMILY:\n")
    buf.write(_HDR + "\n" + _SEP + "\n")
    for fam in ["F1", "F2", "F3", "F4", "F5"]:
        fam_elig = [m for m in eligible if m["family"] == fam]
        if fam_elig:
            best = max(fam_elig, key=lambda x: x["ux"])
            buf.write(_row(best) + "\n")

    # reference: max return
    max_ret = max(all_results, key=lambda x: x["mean"])
    buf.write("\nREFERENCE — MAX RETURN (ignoring UX):\n")
    buf.write(_HDR + "\n" + _SEP + "\n")
    buf.write(_row(max_ret) + "\n")

    # buy-and-hold from actual data (Hold 252d)
    bh = next((m for m in all_results if "252" in m["name"] and m["family"] == "F1"), None)
    if bh:
        buf.write("\nREFERENCE — BUY-AND-HOLD 252d:\n")
        buf.write(_HDR + "\n" + _SEP + "\n")
        buf.write(_row(bh) + "\n")

    # max safety (lowest pct_hit20 among eligible)
    safest = min(eligible, key=lambda x: x["pct_hit20"])
    buf.write("\nREFERENCE — MAX SAFETY (lowest %Hit-20% among eligible):\n")
    buf.write(_HDR + "\n" + _SEP + "\n")
    buf.write(_row(safest) + "\n")

    # ineligible variants
    ineligible = sorted(
        [m for m in all_results if not m["eligible"]],
        key=lambda x: x["spread"], reverse=True
    )
    if ineligible:
        buf.write(f"\nINELIGIBLE VARIANTS (spread < +{_SPREAD_FLOOR:.0%}) — listed for reference:\n")
        buf.write(_HDR + "\n" + _SEP + "\n")
        for m in ineligible:
            buf.write(_row(m) + "\n")

    winner = ranked[0] if ranked else None
    return {"winner": winner, "ranked": ranked, "bh": bh, "safest": safest}


# ── decision document ─────────────────────────────────────────────────────────

def write_decision_doc(winner: dict, bh: dict, safest: dict, all_results: list[dict]) -> str:
    """Generate validation/stage_5b_exit_selection.md content."""
    naive_stop = next(
        (m for m in all_results if "Stop 10" in m["name"]), None
    )
    # If -10% stop doesn't exist (we start at -25%), use the tightest stop
    tightest_stop = min(
        [m for m in all_results if m["family"] == "F2"],
        key=lambda x: x["mean"],
        default=None
    )

    w = winner
    b = bh

    def pct(v):
        return f"{v:+.1%}" if v is not None else "N/A"

    doc = StringIO()
    doc.write("# Stage 5b — Exit Strategy Selection\n\n")
    doc.write("## Context\n\n")
    doc.write(
        "The Recovery Entry Detector has a validated **magnitude edge** (+34.4% vs +22.3% RANDOM, "
        "+12.2pp spread) with no directional edge (win-rate ≈ random). "
        "60.9% of entries touch -20% from entry at some point before 252d. "
        "The naive -10% hard stop fires on 60.9% of entries that would have averaged +67% if held — "
        "it systematically destroys the edge.\n\n"
        "This analysis evaluates 5 exit families across "
        f"{len(all_results)} variants to find the exit that best preserves the magnitude edge "
        "while reducing user pain.\n\n"
    )

    doc.write("## Recommended Exit Rule\n\n")
    if w:
        doc.write(f"**{w['name']}** (Family {w['family']})\n\n")
        doc.write(_decode_variant(w) + "\n\n")
        doc.write(f"| Metric | Winner | Buy&Hold 252d |\n")
        doc.write(f"|--------|--------|---------------|\n")
        metrics_compare = [
            ("Mean 252d return",    pct(w["mean"]),      pct(b["mean"] if b else None)),
            ("Spread vs RANDOM",    pct(w["spread"]),    pct(b["spread"] if b else None)),
            ("%Pos exits",          f"{w['pct_pos']:.1%}", f"{b['pct_pos']:.1%}" if b else "N/A"),
            ("%Touched -20%",       f"{w['pct_hit20']:.1%}", f"{b['pct_hit20']:.1%}" if b else "N/A"),
            ("%Exit at loss",       f"{w['pct_el']:.1%}",    f"{b['pct_el']:.1%}" if b else "N/A"),
            ("Avg hold (days)",     f"{w['avg_hold']:.0f}",  f"{b['avg_hold']:.0f}" if b else "N/A"),
            ("Capture of edge",     f"{w['capture']:.1%}",   f"{b['capture']:.1%}" if b else "N/A"),
            ("UX score",            f"{w['ux']:.3f}",        f"{b['ux']:.3f}" if b else "N/A"),
        ]
        for row in metrics_compare:
            doc.write(f"| {row[0]} | {row[1]} | {row[2]} |\n")
    doc.write("\n")

    doc.write("## Why It Wins\n\n")
    if w and b:
        edge_kept = w["mean"] / _BH_MEAN
        pain_reduction = b["pct_hit20"] - w["pct_hit20"]
        doc.write(
            f"The winner retains **{edge_kept:.0%} of the buy-and-hold edge** "
            f"({pct(w['mean'])} mean vs {pct(b['mean'])} buy-and-hold) "
            f"while reducing -20% drawdown touches from {b['pct_hit20']:.1%} to {w['pct_hit20']:.1%} "
            f"(a **{pain_reduction:.1%}pp reduction in pain**).\n\n"
        )
        if tightest_stop:
            doc.write(
                f"Compared to the tightest eligible hard stop ({tightest_stop['name']}): "
                f"the winner achieves {pct(w['mean'] - tightest_stop['mean'])} higher mean return "
                f"and {w['pct_el'] - tightest_stop['pct_el']:+.1%}pp change in loss-exit rate.\n\n"
            )

    doc.write("## Trade-off Accepted\n\n")
    if w and b:
        edge_given_up = _BH_MEAN - w["mean"]
        pain_reduced  = b["pct_hit20"] - w["pct_hit20"]
        doc.write(
            f"- Edge given up vs pure buy-and-hold: **{edge_given_up:.1%}pp** "
            f"({pct(_BH_MEAN)} → {pct(w['mean'])}).\n"
            f"- Pain removed (% touching -20%): **{pain_reduced:.1%}pp** "
            f"({b['pct_hit20']:.1%} → {w['pct_hit20']:.1%}).\n"
            f"- Loss-exit rate change: {b['pct_el']:.1%} → {w['pct_el']:.1%}.\n"
            f"- Avg hold reduced: {b['avg_hold']:.0f}d → {w['avg_hold']:.0f}d.\n\n"
        )

    doc.write("## UI Implication\n\n")
    if w:
        doc.write(_ui_copy(w) + "\n\n")

    doc.write("## Fallback Behavior\n\n")
    doc.write(_fallback_copy(w) + "\n\n")

    doc.write("## Full Ranking (Top 5 Eligible by UX)\n\n")
    doc.write("| Rank | Variant | Family | Mean | Spread | Capture | UX |\n")
    doc.write("|------|---------|--------|------|--------|---------|----|\n")
    ranked_elig = sorted(
        [m for m in all_results if m["eligible"]], key=lambda x: x["ux"], reverse=True
    )
    for rank, m in enumerate(ranked_elig[:5], 1):
        doc.write(
            f"| {rank} | {m['name']} | {m['family']} | "
            f"{pct(m['mean'])} | {pct(m['spread'])} | "
            f"{m['capture']:.1%} | {m['ux']:.3f} |\n"
        )

    return doc.getvalue()


def _decode_variant(m: dict) -> str:
    """Human-readable description of the exit rule."""
    name = m["name"]
    fam  = m["family"]
    if fam == "F1":
        days = int(m["avg_hold"])
        return f"Hold for **{days} trading days** (~{days//21} months). Exit on the scheduled date regardless of price."
    if fam == "F2":
        stop = int(100 - round(1 + m["spread"]))  # rough parse — just say what it is
        pct_s = int(round((1 - m["n"] / m["n"]) * 100)) if False else ""
        return f"Hold to 252d with a hard stop at the level in the variant name. Exit at stop or 252d."
    if fam == "F3":
        import re
        arm_m  = re.search(r"Arm\+(\d+)%",  name)
        trail_m = re.search(r"Tr(\d+)%",    name)
        arm   = arm_m.group(1)   if arm_m   else "?"
        trail = trail_m.group(1) if trail_m else "?"
        return (
            f"Do nothing until the position gains +{arm}%. "
            f"Once armed, trail a -{trail}% stop below the running peak. "
            f"If never armed, hold to 252d."
        )
    if fam == "F4":
        import re
        hm = re.search(r"H(\d+)d",  name)
        em = re.search(r"E(\d+)%",  name)
        h  = hm.group(1) if hm else "?"
        e  = em.group(1) if em else "?"
        return (
            f"Hold to {h} trading days, but exit early if price drops -{e}% below entry "
            f"(emergency stop). Normal exit at the {h}d horizon."
        )
    if fam == "F5":
        import re
        tm = re.search(r"Tgt\+(\d+)%", name)
        em = re.search(r"E(\d+)%",     name)
        t  = tm.group(1) if tm else "?"
        e  = em.group(1) if em else "?"
        return (
            f"Exit when position gains +{t}% (profit target) OR drops -{e}% (emergency stop), "
            f"whichever comes first. If neither triggers, hold to 252d."
        )
    return name


def _ui_copy(w: dict) -> str:
    fam = w["family"]
    mean_str = f"{w['mean']:.0%}"
    hold_str = f"{w['avg_hold']:.0f}"

    if fam == "F1":
        return (
            f"> \"This position is planned for a **{hold_str}-day hold** (~{int(w['avg_hold'])//21} months). "
            f"The signal targets an average return of {mean_str} over this period, but the path "
            f"will be bumpy — expect drawdowns before the recovery. "
            f"We will alert you at the exit date.\""
        )
    if fam in ("F3", "F4", "F5"):
        return (
            f"> \"This position targets **{mean_str} average return**. "
            f"We hold it patiently — no stop-loss is set initially, as the signal's edge comes from "
            f"riding the full recovery. "
            f"An exit alert fires when the exit condition is met or after {hold_str} days on average.\""
        )
    return (
        f"> \"This position targets **{mean_str} average return** over approximately {hold_str} trading days. "
        f"The path may be volatile before the recovery. We will alert you at the planned exit.\""
    )


def _fallback_copy(w: dict) -> str:
    fam = w["family"]
    if fam == "F1":
        days = int(w["avg_hold"])
        return (
            f"If the user takes no action, the app sends an **exit alert at day {days}** with the "
            f"current price and realized return. The user confirms or defers by 5 trading days."
        )
    if fam == "F3":
        return (
            "If the user takes no action:\n"
            "- A notification fires when the position first reaches the arming threshold.\n"
            "- A second notification fires when the trailing stop is triggered.\n"
            "- If neither fires within 252 days, an exit alert is sent at day 252.\n"
            "The user can always override and exit manually."
        )
    if fam in ("F4", "F5"):
        return (
            "If the user takes no action:\n"
            "- An emergency alert fires immediately if the emergency stop level is breached.\n"
            "- A normal exit alert fires at the horizon date (or target, if applicable).\n"
            "The user can always override and exit manually."
        )
    return "Exit alert fires at the planned exit date or trigger. User can always override."


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading prices and EDGAR fundamentals...")
    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    print("Building HIGH entry set...")
    all_entries = load_high_entries(prices, fundamentals)
    print(f"  Total HIGH entries:      {len(all_entries)}")

    full_entries, paths, rmin, rmax = build_matrices(all_entries)
    N_total = len(all_entries)
    N_full  = len(full_entries)
    print(f"  Full-252d entries (used): {N_full}  "
          f"({N_total - N_full} excluded — insufficient forward path)")

    print("Simulating exit families...")
    r1 = family1_time(paths, rmin, N_total)
    r2 = family2_hard_stop(paths, rmin)
    r3 = family3_trailing(paths, rmax, rmin)
    r4 = family4_hybrid(paths, rmin)
    r5 = family5_target_stop(paths, rmin)
    all_results = r1 + r2 + r3 + r4 + r5
    print(f"  {len(all_results)} variants computed.")

    # ── write results file ────────────────────────────────────────────────────
    buf = StringIO()
    buf.write("=" * 70 + "\n")
    buf.write("EXIT STRATEGY VALIDATION — Stage 5b Recovery Entry Detector\n")
    buf.write("=" * 70 + "\n")
    buf.write(f"Universe:           {len(VALIDATION_UNIVERSE)} tickers, 2018-2024\n")
    buf.write(f"Total HIGH entries: {N_total}\n")
    buf.write(f"Full-path entries:  {N_full} (used for all variants)\n")
    buf.write(f"Excluded:           {N_total - N_full} (insufficient forward data)\n")
    buf.write(f"RANDOM baseline:    {_RANDOM_MEAN:.1%}  BH baseline: {_BH_MEAN:.1%}\n")
    buf.write(f"Eligibility floor:  spread_vs_random >= +{_SPREAD_FLOOR:.0%}\n")
    buf.write("\nColumn guide:\n")
    buf.write("  %Hit20 = % of entries that touched -20% below entry before exit\n")
    buf.write("  %ExL   = % of entries with realized exit return < 0\n")
    buf.write("  Capt   = mean_return / 34.4% (fraction of BH edge kept)\n")
    buf.write("  UX     = Capt × (1-%Hit20) × (1-%ExL)\n")
    buf.write("  E?     = YES if spread_vs_random >= +8pp (eligible)\n")

    write_family_block(buf, "FAMILY 1 — TIME-BASED, NO STOP", r1)
    write_family_block(buf, "FAMILY 2 — WIDE HARD STOP, NO TIME LIMIT", r2, extra_f2(r2))
    write_family_block(buf, "FAMILY 3 — TRAILING STOP AFTER PROFIT TRIGGER", r3, extra_f3(r3))
    write_family_block(buf, "FAMILY 4 — HYBRID: TIME + EMERGENCY STOP", r4, extra_f4(r4))
    write_family_block(buf, "FAMILY 5 — PROFIT TARGET + EMERGENCY STOP", r5, extra_f5(r5))

    ranking = write_ranking(buf, all_results)

    output = buf.getvalue()
    print(output)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / "exit_strategy_results.txt"
    out_path.write_text(output)
    print(f"\nResults saved to {out_path}")

    # ── write decision doc ────────────────────────────────────────────────────
    winner = ranking["winner"]
    bh     = ranking["bh"]
    safest = ranking["safest"]

    if winner:
        doc_content = write_decision_doc(winner, bh, safest, all_results)
        doc_path = _DOCS_DIR / "stage_5b_exit_selection.md"
        doc_path.write_text(doc_content)
        print(f"Decision doc saved to {doc_path}")
        print(f"\n{'='*60}")
        print(f"RECOMMENDED EXIT RULE: {winner['name']}")
        print(_decode_variant(winner))
        print(f"  Mean: {winner['mean']:+.1%}  Spread: {winner['spread']:+.1%}  "
              f"UX: {winner['ux']:.3f}  %Hit-20: {winner['pct_hit20']:.1%}")
    else:
        print("WARNING: No eligible variants found.")


if __name__ == "__main__":
    main()
