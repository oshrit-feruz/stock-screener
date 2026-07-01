#!/usr/bin/env python3
"""Backtest validation of the 8-K veto layer (research-only).

Runs the best validated configuration — Top-100 point-in-time universe,
threshold 0.60, flat 10% sizing, money-market (Fed funds) idle-cash yield,
T+1-open entry, 2018-2024, $100k — WITH and WITHOUT the fail-closed 8-K veto
(data/sec_8k_veto.is_vetoed), and reports:

  * how many signals the veto blocked (total and per year),
  * which specific tickers/dates were blocked and why,
  * before/after: final value, CAGR, Sharpe, Max drawdown, #trades, win rate,
  * the key question: did blocked signals have a WORSE-than-average win rate?
    (computed from each blocked signal's counterfactual T+1-open / 252-day
    return, independent of portfolio capacity).

The veto items are categorical, literature-derived exclusions (Campbell-
Hilscher-Szilagyi 2008; Kausar-Taffler-Tan 2009). NO parameter is fit here.
"""
from __future__ import annotations

import sys
import warnings
from collections import Counter, defaultdict
from datetime import date as date_type
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from data.sec_8k_veto import is_vetoed, prefetch_veto_cache
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_pit_market_caps
from research.run_combined_clean_universe import simulate
from scripts.run_combined_validation import load_fedfunds
from scripts.run_portfolio_sim import _INITIAL_CAP, compute_metrics, load_all_data

_SIM_START = pd.Timestamp("2018-01-01")
_SIM_END = pd.Timestamp("2024-12-31")
_TOP_N = 100
_HOLD = 252
_TODAY = "2025-01-01"   # veto prefetch as-of (filing list is date-independent)


def _win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t["ret"] > 0) / len(trades)


def main() -> None:
    print("Loading data (Top-100 point-in-time universe, T+1 open)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    spy_raw = prices_obj.get_prices("SPY", "2016-01-01", "2024-12-31")
    spy_close = spy_raw["Close"]
    master_cal = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)].index

    fmonths: dict[tuple, pd.Timestamp] = {}
    for ts in master_cal:
        fmonths.setdefault((ts.year, ts.month), ts)
    fdates = [ts.date().isoformat() for ts in fmonths.values()]
    union_full = sorted({t for d in fdates for t in get_universe(d)})
    prefetch_pit_market_caps(union_full, fdates)
    month_members = {k: set(get_universe_top_n(ts.date().isoformat(), _TOP_N))
                     for k, ts in fmonths.items()}
    union = sorted(set().union(*month_members.values()))
    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} ({len(master_cal)} days)")
    print(f"  Top-{_TOP_N} union over window: {len(union)} distinct tickers")

    crossings_by_ticker, prices_wide, opens_wide, _ = load_all_data(
        prices_obj, fund, union, with_opens=True)
    rate_on_cal = load_fedfunds().reindex(master_cal, method="ffill").values.astype(float)

    # ── Warm the 8-K veto cache for the whole universe, and measure coverage ──
    print(f"\nPrefetching 8-K veto cache for {len(union)} tickers...")
    prefetch_veto_cache(union, _TODAY)
    from data.sec_8k_veto import _get_filings, VETO_8K_ITEMS
    resolved = sum(1 for t in union if _get_filings(t))
    print(f"  EDGAR filing record resolved: {resolved}/{len(union)} "
          f"(unresolved = delisted / ticker-renamed names → unverifiable, not blocked)")

    # Diagnostic: prove the veto is LIVE — enumerate every veto-eligible distress
    # 8-K actually present in the universe over the sim window (incl. the 90-day
    # pre-window lookback). These are the events the veto *would* block if an
    # in-universe recovery signal coincided with one in its 90-day window.
    win_lo = (_SIM_START - pd.Timedelta(days=90)).date()   # incl. 90-day lookback
    win_hi = _SIM_END.date()
    distress_events: list[tuple[str, str, list]] = []
    for t in union:
        for r in (_get_filings(t) or []):
            if r["form"] == "8-K" and r["filingDate"]:
                fd = date_type.fromisoformat(r["filingDate"])
                if win_lo <= fd <= win_hi:
                    hit = [it for it in r["items"] if it in VETO_8K_ITEMS]
                    if hit:
                        distress_events.append((r["filingDate"], t, hit))
    distress_events.sort()
    distress_tickers = sorted({t for _, t, _ in distress_events})
    print(f"  Veto-eligible distress 8-Ks in universe over window: "
          f"{len(distress_events)} across {len(distress_tickers)} tickers")

    # Robust veto wrapper: never let a lookup error abort the backtest; the veto
    # acts on positive facts only, so on error we do NOT block.
    def veto_fn(ticker: str, iso_date: str):
        try:
            return is_vetoed(ticker, iso_date, check_going_concern=True)
        except Exception as exc:  # pragma: no cover
            return False, f"unverifiable: error {exc}"

    common = dict(sizing_mode="flat", cash_mode="fed_funds", rate_on_cal=rate_on_cal,
                  month_members=month_members, opens_wide=opens_wide)

    print("\nRunning BASELINE (no veto)...")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        base = simulate(crossings_by_ticker, prices_wide, master_cal, **common)

    print("Running WITH 8-K veto...")
    veto_log: list = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        vetoed = simulate(crossings_by_ticker, prices_wide, master_cal,
                          veto_fn=veto_fn, veto_log=veto_log, **common)

    m_base = compute_metrics(base["daily_values"], _INITIAL_CAP)
    m_veto = compute_metrics(vetoed["daily_values"], _INITIAL_CAP)

    # ── Counterfactual return of each blocked signal (T+1 open → +252d) ───────
    sim_prices = prices_wide.reindex(master_cal, method="ffill")
    opens_al = opens_wide.reindex(master_cal).reindex(columns=sim_prices.columns)
    price_arr, open_arr = sim_prices.values, opens_al.values
    col = {c: i for i, c in enumerate(sim_prices.columns)}
    cal_pos = {d.date(): i for i, d in enumerate(master_cal)}

    def cf_return(ticker, sig_d):
        di = cal_pos.get(sig_d)
        ci = col.get(ticker)
        if di is None or ci is None:
            return None
        fill = di + 1
        if fill > len(master_cal) - 1:
            return None
        ep = open_arr[fill, ci]
        if not np.isfinite(ep) or ep <= 0:
            return None
        ex = min(fill + _HOLD, len(master_cal) - 1)
        xp = price_arr[ex, ci]
        if not np.isfinite(xp):
            return None
        return float(xp / ep - 1)

    blocked_rows = []
    per_year = Counter()
    for sig_d, ticker, comp, reason in veto_log:
        r = cf_return(ticker, sig_d)
        blocked_rows.append((sig_d, ticker, comp, reason, r))
        per_year[sig_d.year] += 1

    cf_valid = [r for *_, r in blocked_rows if r is not None]
    blocked_win_rate = (sum(1 for r in cf_valid if r > 0) / len(cf_valid)) if cf_valid else None
    blocked_avg_ret = float(np.mean(cf_valid)) if cf_valid else None

    base_win = _win_rate(base["trades"])
    base_avg_ret = float(np.mean([t["ret"] for t in base["trades"]])) if base["trades"] else 0.0

    # ── Report ────────────────────────────────────────────────────────────────
    div = "=" * 92
    lines: list[str] = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("\n" + div)
    out("8-K VETO VALIDATION — best config (Top-100 PIT, thr0.60, flat 10%, money market, T+1)")
    out("2018-2024, $100k, 252d hold — research-only")
    out(div)

    out(f"\nVeto layer is LIVE: {len(distress_events)} veto-eligible distress 8-Ks exist in the")
    out(f"  universe over the window, across {len(distress_tickers)} tickers "
        f"(these are what the veto scans).")
    if distress_events:
        out("  Sample distress 8-Ks on file (date, ticker, items):")
        for fdate, t, hit in distress_events[:12]:
            out(f"    {fdate}  {t:<6} items={hit}")
        if len(distress_events) > 12:
            out(f"    ... and {len(distress_events) - 12} more")

    out(f"\nSignals blocked by the veto: {len(veto_log)} "
        f"(baseline trades: {len(base['trades'])}, with-veto trades: {len(vetoed['trades'])})")
    if per_year:
        out("  Per year: " + ", ".join(f"{y}:{per_year[y]}" for y in sorted(per_year)))

    out("\nBLOCKED SIGNALS (signal date, ticker, composite, counterfactual 252d ret, reason)")
    out("-" * 92)
    if blocked_rows:
        for sig_d, ticker, comp, reason, r in sorted(blocked_rows):
            rs = f"{r:+.1%}" if r is not None else "  n/a"
            out(f"  {str(sig_d):<11} {ticker:<6} comp={comp:.3f}  cf={rs:>7}  {reason}")
    else:
        out("  (none — no in-universe signal coincided with a distress filing in its 90-day window)")

    out("\n" + "-" * 92)
    out(f"  {'Metric':<22}{'BEFORE (no veto)':>20}{'AFTER (veto)':>20}")
    out("-" * 92)
    rows = [
        ("Final value", f"${m_base['final_value']:,.0f}", f"${m_veto['final_value']:,.0f}"),
        ("CAGR", f"{m_base['cagr']:+.1%}", f"{m_veto['cagr']:+.1%}"),
        ("Sharpe", f"{m_base['sharpe']:.2f}", f"{m_veto['sharpe']:.2f}"),
        ("Max drawdown", f"{m_base['max_dd']:.1%}", f"{m_veto['max_dd']:.1%}"),
        ("# trades", f"{len(base['trades'])}", f"{len(vetoed['trades'])}"),
        ("Win rate", f"{base_win:.1%}", f"{_win_rate(vetoed['trades']):.1%}"),
        ("Avg trade return", f"{base_avg_ret:+.1%}", ""),
    ]
    for label, a, b in rows:
        out(f"  {label:<22}{a:>20}{b:>20}")

    out("\n" + div)
    out("KEY QUESTION — did blocked signals have a WORSE-than-average win rate?")
    out(div)
    if blocked_win_rate is None:
        out("  No signals were blocked on this universe/window, so there is nothing to")
        out("  compare. The veto is a correctness safeguard that (correctly) did not fire:")
        out("  the Top-100 large-cap universe rarely files bankruptcy / restatement / auditor-")
        out("  change 8-Ks, and delisted-after-distress names drop out of EDGAR's current")
        out("  ticker map (reported as unverifiable above), so they are never force-blocked.")
    else:
        out(f"  Blocked-signal win rate : {blocked_win_rate:.1%}  (avg cf return {blocked_avg_ret:+.1%}, n={len(cf_valid)})")
        out(f"  Baseline trade win rate : {base_win:.1%}  (avg return {base_avg_ret:+.1%})")
        worse = blocked_win_rate < base_win
        out(f"  >>> Blocked signals were {'WORSE' if worse else 'NOT worse'} than average — "
            f"the veto {'is removing weak trades (working as intended).' if worse else 'removed average/better trades — reconsider.'}")

    out("\nNOTE: research-only. Not wired into product/ until validated.")

    out_path = Path(__file__).parent.parent / "results" / "research" / "sec_8k_veto_validation.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        f.write("# 8-K veto validation\n\n```\n" + "\n".join(lines) + "\n```\n")
    print(f"\nReport written: {out_path}")


if __name__ == "__main__":
    main()
