"""Parameter optimization grid for the Recovery Entry Detector.

Runs 30 combinations (5 entry thresholds x 3 hold periods x 2 exit rules)
on in-sample data (2018-2024), identifies candidates, then performs an
out-of-sample check on the top 5 by CAGR (2025-01-01 to 2026-06-12).

IMPORTANT — in-sample optimization guardrails
  - These results are exploratory only.
  - Any candidate requires full Stage 5b revalidation before production use.
  - Do NOT change the production signal until revalidation is complete.
  - Signal parameters (weights, gate, dip tiers) remain FROZEN.
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from product.backtest.engine import _load_backtest_data, _simulate

# ── Grid definition ────────────────────────────────────────────────────────────

ENTRY_THRESHOLDS = [0.60, 0.65, 0.70, 0.75, 0.80]
HOLD_PERIODS     = [126, 189, 252]
EXIT_RULES       = ["A", "B"]   # A=no stop, B=-40% emergency stop

IS_START  = "2018-01-01"
IS_END    = "2024-12-31"
OOS_START = "2025-01-01"
OOS_END   = "2026-06-12"

POSITION_SIZE_PCT = 10.0
MAX_POSITIONS     = 10

BASELINE = (0.60, 252, "A")   # (entry_threshold, hold_days, exit_rule)

CANDIDATE_CRITERIA = {
    "min_trades":         10,
    "mean_ret_delta_pp":  3.0,   # must beat baseline mean_return by ≥ 3pp
    "sharpe_delta":       0.0,   # must beat or match baseline Sharpe
}


# ── Grid construction ──────────────────────────────────────────────────────────

def _build_params(entry_threshold, hold_days, exit_rule, start, end):
    return {
        "entry_threshold":  entry_threshold,
        "hold_days":        hold_days,
        "exit_rule":        exit_rule,
        "position_size_pct": POSITION_SIZE_PCT,
        "max_positions":    MAX_POSITIONS,
        "start_date":       start,
        "end_date":         end,
    }


def _combo_label(entry, hold, rule):
    return f"E{int(entry*100):02d}_H{hold}_{rule}"


# ── Output formatting ──────────────────────────────────────────────────────────

def _fmt(v, fmt=".1f", fallback="-"):
    if v is None:
        return fallback
    try:
        return format(v, fmt)
    except Exception:
        return str(v)


def _row(entry, hold, rule, s, is_baseline=False, is_candidate=False):
    flag = ""
    if is_baseline:
        flag = " [baseline]"
    elif is_candidate:
        flag = " *"
    return (
        f"  {entry:.2f}  {hold:3d}d  {rule}  | "
        f"{_fmt(s.get('cagr')):>7}% | "
        f"{_fmt(s.get('mean_return_pct'), '.2f'):>8}% | "
        f"{_fmt(s.get('sharpe'), '.2f'):>7} | "
        f"{_fmt(s.get('max_drawdown_pct'), '.1f'):>8}% | "
        f"{_fmt(s.get('pct_positive'), '.1f'):>6}% | "
        f"{s.get('n_trades', 0):>7} | "
        f"{_fmt(s.get('total_return_pct'), '.1f'):>10}%"
        f"{flag}"
    )


def _oos_row(entry, hold, rule, s):
    if "error" in s:
        return f"  {entry:.2f}  {hold:3d}d  {rule}  | ERROR: {s['error']}"
    sm = s["summary"]
    return (
        f"  {entry:.2f}  {hold:3d}d  {rule}  | "
        f"{_fmt(sm.get('cagr')):>7}% | "
        f"{_fmt(sm.get('mean_return_pct'), '.2f'):>8}% | "
        f"{_fmt(sm.get('sharpe'), '.2f'):>7} | "
        f"{_fmt(sm.get('max_drawdown_pct'), '.1f'):>8}% | "
        f"{sm.get('n_trades', 0):>7}"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    lines = []

    def p(s=""):
        lines.append(s)
        print(s)

    p("=" * 80)
    p("RECOVERY ENTRY DETECTOR — PARAMETER OPTIMIZATION GRID")
    p(f"In-sample: {IS_START} to {IS_END}")
    p("Universe:  50 tickers (VALIDATION_UNIVERSE)")
    p(f"Capital:   $100,000  |  Position size: {POSITION_SIZE_PCT}%  |  Max positions: {MAX_POSITIONS}")
    p(f"Grid:      {len(ENTRY_THRESHOLDS)} entry thresholds x {len(HOLD_PERIODS)} hold periods x {len(EXIT_RULES)} exit rules = {len(ENTRY_THRESHOLDS)*len(HOLD_PERIODS)*len(EXIT_RULES)} combinations")
    p(f"Baseline:  entry={BASELINE[0]:.2f}  hold={BASELINE[1]}d  exit={BASELINE[2]}")
    p()
    p("GUARDRAIL: Results are in-sample exploratory only.")
    p("Any candidate requires Stage 5b revalidation before production use.")
    p("Signal parameters (weights, gate, dip tiers) remain FROZEN.")
    p("=" * 80)
    p()

    # ── Run in-sample grid ─────────────────────────────────────────────────────
    p("Loading price data and computing signals for in-sample period ...")
    t0 = time.time()

    all_is_params = [
        _build_params(e, h, r, IS_START, IS_END)
        for e in ENTRY_THRESHOLDS
        for h in HOLD_PERIODS
        for r in EXIT_RULES
    ]

    is_end_date  = date.fromisoformat(IS_END)
    is_start_year = date.fromisoformat(IS_START).year
    preloaded_is = _load_backtest_data(is_end_date, is_start_year, is_end_date.year)

    p(f"Data loaded in {time.time()-t0:.0f}s. Running {len(all_is_params)} simulations ...")
    t1 = time.time()

    is_results = [_simulate(preloaded_is, p_) for p_ in all_is_params]

    p(f"Simulations complete in {time.time()-t1:.0f}s.")
    p()

    # Pair each result with its params
    combos = list(zip(
        [(e, h, r) for e in ENTRY_THRESHOLDS for h in HOLD_PERIODS for r in EXIT_RULES],
        is_results,
    ))

    # Extract summary; mark errors
    def _summary(res):
        if "error" in res:
            return None
        return res["summary"]

    # Locate baseline
    baseline_res = next(
        (res for (e, h, r), res in combos if (e, h, r) == BASELINE),
        None,
    )
    baseline_sm  = _summary(baseline_res) if baseline_res else None
    baseline_cagr      = baseline_sm["cagr"]         if baseline_sm else None
    baseline_mean_ret  = baseline_sm["mean_return_pct"] if baseline_sm else None
    baseline_sharpe    = baseline_sm["sharpe"]        if baseline_sm else None

    # Sort by CAGR descending
    def _sort_key(item):
        (_, _, _), res = item
        sm = _summary(res)
        return sm["cagr"] if sm else -9999

    sorted_combos = sorted(combos, key=_sort_key, reverse=True)

    # ── Section 1: Full results table ─────────────────────────────────────────
    p("-" * 80)
    p("SECTION 1 — FULL IN-SAMPLE RESULTS (sorted by CAGR, descending)")
    p("-" * 80)
    header = (
        "  Entry  Hold   Rule | "
        "   CAGR  |  MeanRet |  Sharpe |  MaxDD   |   Win%  | Trades  |  TotRet"
    )
    p(header)
    p("  " + "-" * 77)

    candidates = []
    for (e, h, r), res in sorted_combos:
        sm = _summary(res)
        if sm is None:
            err = res.get("error", "unknown error")
            p(f"  {e:.2f}  {h:3d}d  {r}  | ERROR: {err}")
            continue

        is_bl  = (e, h, r) == BASELINE
        is_cand = False

        if baseline_sm is not None and not is_bl:
            is_cand = (
                sm["n_trades"] >= CANDIDATE_CRITERIA["min_trades"]
                and sm["mean_return_pct"] >= baseline_mean_ret + CANDIDATE_CRITERIA["mean_ret_delta_pp"]
                and sm["sharpe"] >= baseline_sharpe + CANDIDATE_CRITERIA["sharpe_delta"]
            )
        if is_cand:
            candidates.append(((e, h, r), sm))

        p(_row(e, h, r, sm, is_baseline=is_bl, is_candidate=is_cand))

    p()
    if baseline_sm:
        p(f"  Baseline ({BASELINE[0]:.2f}, {BASELINE[1]}d, {BASELINE[2]}): "
          f"CAGR={_fmt(baseline_cagr)}%  MeanRet={_fmt(baseline_mean_ret, '.2f')}%  "
          f"Sharpe={_fmt(baseline_sharpe, '.2f')}")
    p(f"  * = candidate (n_trades >= {CANDIDATE_CRITERIA['min_trades']}, "
      f"mean_ret >= baseline+{CANDIDATE_CRITERIA['mean_ret_delta_pp']}pp, "
      f"Sharpe >= baseline)")

    # ── Section 2: Candidates ──────────────────────────────────────────────────
    p()
    p("-" * 80)
    p("SECTION 2 — CANDIDATE COMBINATIONS")
    p("-" * 80)

    if not candidates:
        p("  No combinations met all three candidate criteria.")
        p(f"  Criteria: n_trades >= {CANDIDATE_CRITERIA['min_trades']}, "
          f"mean_ret >= {_fmt(baseline_mean_ret, '.2f')}% + {CANDIDATE_CRITERIA['mean_ret_delta_pp']}pp, "
          f"Sharpe >= {_fmt(baseline_sharpe, '.2f')}")
    else:
        p(f"  {len(candidates)} candidate(s) found:")
        p()
        for i, ((e, h, r), sm) in enumerate(candidates, 1):
            p(f"  Candidate {i}: entry={e:.2f}  hold={h}d  exit={r}")
            p(f"    CAGR:          {_fmt(sm['cagr'])}%  (baseline: {_fmt(baseline_cagr)}%)")
            p(f"    Mean return:   {_fmt(sm['mean_return_pct'], '.2f')}%  (baseline: {_fmt(baseline_mean_ret, '.2f')}%)")
            p(f"    Sharpe:        {_fmt(sm['sharpe'], '.2f')}  (baseline: {_fmt(baseline_sharpe, '.2f')})")
            p(f"    Max drawdown:  {_fmt(sm['max_drawdown_pct'])}%")
            p(f"    Win rate:      {_fmt(sm['pct_positive'])}%")
            p(f"    N trades:      {sm['n_trades']}")
            p(f"    Avg hold:      {_fmt(sm['avg_hold_days'])} days")
            p(f"    SPY CAGR:      {_fmt(sm.get('spy_cagr'))}%  |  Beat SPY: {sm.get('beat_spy')}")
            p()

    # ── Section 3: Key research questions ─────────────────────────────────────
    p("-" * 80)
    p("SECTION 3 — KEY RESEARCH QUESTIONS")
    p("-" * 80)
    p()

    # Q1: Does tighter entry threshold (higher score) improve returns?
    p("Q1. Does a higher entry threshold (stricter filter) improve returns?")
    q1_data = {}
    for (e, h, r), res in combos:
        sm = _summary(res)
        if sm:
            q1_data.setdefault(e, []).append(sm["cagr"])
    p("    Entry threshold -> avg CAGR across hold periods and exit rules:")
    for e in sorted(q1_data):
        vals = q1_data[e]
        avg  = sum(vals) / len(vals)
        best = max(vals)
        p(f"      {e:.2f}: avg={avg:+.1f}%  best={best:+.1f}%  (n={len(vals)})")
    p()

    # Q2: Does hold period matter?
    p("Q2. Does hold period affect outcomes?")
    q2_data = {}
    for (e, h, r), res in combos:
        sm = _summary(res)
        if sm:
            q2_data.setdefault(h, []).append(sm["cagr"])
    p("    Hold period -> avg CAGR across entry thresholds and exit rules:")
    for h in sorted(q2_data):
        vals = q2_data[h]
        avg  = sum(vals) / len(vals)
        best = max(vals)
        p(f"      {h}d: avg={avg:+.1f}%  best={best:+.1f}%  (n={len(vals)})")
    p()

    # Q3: Does the emergency stop (Rule B) help?
    p("Q3. Does the -40% emergency stop (Rule B) improve risk-adjusted returns vs Rule A?")
    q3_a_cagrs  = [_summary(r)["cagr"]   for (e,h,ru),r in combos if ru=="A" and _summary(r)]
    q3_b_cagrs  = [_summary(r)["cagr"]   for (e,h,ru),r in combos if ru=="B" and _summary(r)]
    q3_a_sharpe = [_summary(r)["sharpe"] for (e,h,ru),r in combos if ru=="A" and _summary(r)]
    q3_b_sharpe = [_summary(r)["sharpe"] for (e,h,ru),r in combos if ru=="B" and _summary(r)]
    q3_a_dd     = [_summary(r)["max_drawdown_pct"] for (e,h,ru),r in combos if ru=="A" and _summary(r)]
    q3_b_dd     = [_summary(r)["max_drawdown_pct"] for (e,h,ru),r in combos if ru=="B" and _summary(r)]
    def _avg(lst): return sum(lst)/len(lst) if lst else float("nan")
    p(f"    Rule A (no stop):    avg CAGR={_avg(q3_a_cagrs):+.1f}%  avg Sharpe={_avg(q3_a_sharpe):.2f}  avg MaxDD={_avg(q3_a_dd):.1f}%")
    p(f"    Rule B (-40% stop):  avg CAGR={_avg(q3_b_cagrs):+.1f}%  avg Sharpe={_avg(q3_b_sharpe):.2f}  avg MaxDD={_avg(q3_b_dd):.1f}%")
    p()

    # Q4: Trade-off between n_trades and mean return?
    p("Q4. Is there a trade-off between signal frequency (n_trades) and mean return per trade?")
    q4_rows = sorted(
        [(e, sm["n_trades"], sm["mean_return_pct"])
         for (e,h,r), res in combos
         for sm in [_summary(res)] if sm],
        key=lambda x: x[0]
    )
    p("    (showing mean across all hold/exit combinations per entry threshold)")
    q4_by_entry = {}
    for e, nt, mr in q4_rows:
        q4_by_entry.setdefault(e, []).append((nt, mr))
    for e in sorted(q4_by_entry):
        vals = q4_by_entry[e]
        avg_nt = _avg([v[0] for v in vals])
        avg_mr = _avg([v[1] for v in vals])
        p(f"      entry={e:.2f}: avg n_trades={avg_nt:.0f}  avg mean_return={avg_mr:+.2f}%")
    p()

    # Q5: Which combo offers best Sharpe (risk-adjusted)?
    p("Q5. Which combination has the best risk-adjusted return (Sharpe)?")
    sharpe_ranked = sorted(
        [((e,h,r), sm) for (e,h,r),res in combos for sm in [_summary(res)] if sm],
        key=lambda x: x[1]["sharpe"], reverse=True
    )
    p("    Top 5 by Sharpe ratio:")
    for (e,h,r), sm in sharpe_ranked[:5]:
        bl_flag = " [baseline]" if (e,h,r)==BASELINE else ""
        p(f"      entry={e:.2f}  hold={h}d  rule={r}:  "
          f"Sharpe={sm['sharpe']:.2f}  CAGR={_fmt(sm['cagr'])}%  MaxDD={_fmt(sm['max_drawdown_pct'])}%{bl_flag}")
    p()

    # ── Section 4: Recommendation ──────────────────────────────────────────────
    p("-" * 80)
    p("SECTION 4 — RECOMMENDATION")
    p("-" * 80)
    p()

    # Best by CAGR
    best_cagr_combo, best_cagr_sm = sorted_combos[0][0], _summary(sorted_combos[0][1])
    # Best by Sharpe
    best_sharpe_combo, best_sharpe_sm = sharpe_ranked[0]

    p("Top combination by CAGR:")
    if best_cagr_sm:
        e,h,r = best_cagr_combo
        p(f"  entry={e:.2f}  hold={h}d  exit={r}  ->  "
          f"CAGR={_fmt(best_cagr_sm['cagr'])}%  Sharpe={_fmt(best_cagr_sm['sharpe'], '.2f')}  "
          f"MaxDD={_fmt(best_cagr_sm['max_drawdown_pct'])}%  n_trades={best_cagr_sm['n_trades']}")
    p()
    p("Top combination by Sharpe:")
    if best_sharpe_sm:
        e,h,r = best_sharpe_combo
        p(f"  entry={e:.2f}  hold={h}d  exit={r}  ->  "
          f"CAGR={_fmt(best_sharpe_sm['cagr'])}%  Sharpe={_fmt(best_sharpe_sm['sharpe'], '.2f')}  "
          f"MaxDD={_fmt(best_sharpe_sm['max_drawdown_pct'])}%  n_trades={best_sharpe_sm['n_trades']}")
    p()

    if candidates:
        p(f"Candidates that cleared the 3-criterion bar ({len(candidates)}):")
        for i, ((e,h,r), sm) in enumerate(candidates, 1):
            p(f"  {i}. entry={e:.2f}  hold={h}d  exit={r}  "
              f"CAGR={_fmt(sm['cagr'])}%  Sharpe={_fmt(sm['sharpe'],'.2f')}")
        p()
    else:
        p("No combinations cleared all three candidate criteria in-sample.")
        p("The baseline configuration (entry=0.60, hold=252d, exit=A) is retained.")
        p()

    p("IMPORTANT: These are in-sample (2018-2024) results only.")
    p("Any parameter change requires full Stage 5b revalidation before production.")
    p("Do not change production signal parameters until revalidation is complete.")
    p()

    # ── OOS check: top 5 by CAGR ──────────────────────────────────────────────
    p("-" * 80)
    p("SECTION 5 — OUT-OF-SAMPLE CHECK (top 5 by in-sample CAGR)")
    p(f"  OOS period: {OOS_START} to {OOS_END}")
    p("-" * 80)
    p()

    top5 = [c for c in sorted_combos[:5] if _summary(c[1]) is not None]

    p("Loading price data for OOS period ...")
    t2 = time.time()
    oos_end_date  = date.fromisoformat(OOS_END)
    oos_start_year = date.fromisoformat(OOS_START).year
    preloaded_oos = _load_backtest_data(oos_end_date, oos_start_year, oos_end_date.year)
    p(f"OOS data loaded in {time.time()-t2:.0f}s.")
    p()

    header_oos = (
        "  Entry  Hold   Rule | "
        "   CAGR  |  MeanRet |  Sharpe |  MaxDD   | Trades  |  (IS CAGR)"
    )
    p(header_oos)
    p("  " + "-" * 70)

    for (e, h, r), is_res in top5:
        oos_params = _build_params(e, h, r, OOS_START, OOS_END)
        oos_res    = _simulate(preloaded_oos, oos_params)
        is_sm      = _summary(is_res)

        if "error" in oos_res:
            p(f"  {e:.2f}  {h:3d}d  {r}  | ERROR: {oos_res['error']}  "
              f"(IS CAGR: {_fmt(is_sm['cagr'] if is_sm else None)}%)")
            continue

        oos_sm = oos_res["summary"]
        p(
            f"  {e:.2f}  {h:3d}d  {r}  | "
            f"{_fmt(oos_sm.get('cagr')):>7}% | "
            f"{_fmt(oos_sm.get('mean_return_pct'), '.2f'):>8}% | "
            f"{_fmt(oos_sm.get('sharpe'), '.2f'):>7} | "
            f"{_fmt(oos_sm.get('max_drawdown_pct'), '.1f'):>8}% | "
            f"{oos_sm.get('n_trades', 0):>7}  | "
            f"({_fmt(is_sm['cagr'] if is_sm else None)}%)"
        )
    p()
    p("NOTE: OOS period is short (~18 months). Results are indicative only.")
    p("A large drop from IS to OOS CAGR is a warning sign of in-sample overfitting.")
    p()

    # ── SPY reference ──────────────────────────────────────────────────────────
    if baseline_sm:
        p("-" * 80)
        p("REFERENCE: SPY benchmark (from baseline simulation)")
        p(f"  IS  SPY CAGR:  {_fmt(baseline_sm.get('spy_cagr'))}%  "
          f"total return={_fmt(baseline_sm.get('spy_total_return_pct'))}%")
        p()

    elapsed = time.time() - t0
    p(f"Total run time: {elapsed:.0f}s")
    p()
    p("=" * 80)

    # ── Save to file ───────────────────────────────────────────────────────────
    out_path = _ROOT / "results" / "parameter_optimization.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
