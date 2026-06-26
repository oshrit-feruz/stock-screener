#!/usr/bin/env python3
"""Combined validation over 2010-2024 with a real historical Fed funds cash sleeve.

Two separately-validated improvements are combined into one variant and tested
over the full 2010-2024 window (adding the 2008-09 recovery — exactly the kind of
dislocation the signal is meant to catch). All four variants run on identical
conditions — same 50 tickers, threshold 0.60, exit at day 252, no stop-loss,
$100,000 start, max 10 concurrent positions — differing only in two switches:

  sizing : "flat"       -> every signal gets 10% of portfolio value
           "score_plus" -> comp>=0.70 gets 12%, else 10%; the extra 2% comes from
                           idle cash only (never by shrinking another position).
                           Skip if < 5% of the portfolio is free, else open with
                           whatever cash is left.
  cash   : "zero"       -> idle cash earns 0%
           "fed_funds"  -> idle cash earns the REAL effective federal funds rate in
                           force on each date (FRED series FEDFUNDS), accrued daily
                           pro-rata. No fixed assumption — the rate varies from ~0%
                           (2010-2015, 2020-21) to ~5% (2023-24).

  Variant A  baseline V1     : flat        + zero       (base case)
  Variant B  Score-Plus only : score_plus  + zero
  Variant C  Fed funds only  : flat        + fed_funds
  Variant D  Full combination: score_plus  + fed_funds  (the main variant)

Two caveats handled explicitly in the report:
  * EDGAR fundamentals can be sparse before ~2015. The quality gate's existing
    fail-closed logic is UNCHANGED: a missing fundamental -> gate False -> signal
    rejected, exactly as in the baseline.
  * Survivorship bias: the 50 tickers are firms that survived to 2024, so results
    (especially pre-2015) are optimistic. Flagged in the output.
"""
from __future__ import annotations

import io
import sys
import time
import warnings
from collections import defaultdict
from datetime import date as date_type
from pathlib import Path

import numpy as np
import pandas as pd
import requests

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
from scripts.run_portfolio_sim import _HOLD_DAYS, _INITIAL_CAP, _MIN_POSITION

# ── Window ────────────────────────────────────────────────────────────────────
_WARMUP_START = "2008-01-01"          # >=1y before sim start for signal warmup
_FETCH_END = "2024-12-31"
_SIM_START = pd.Timestamp("2010-01-01")
_SIM_END = pd.Timestamp("2024-12-31")
_QUALITY_YEARS = range(2009, 2026)

_FRED_CACHE = Path(__file__).parent.parent / "data" / "cache" / "fred"
_FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS"
_FRED_TTL = 7 * 86400


# ── Fed funds (real historical rate) ──────────────────────────────────────────

def load_fedfunds() -> pd.Series:
    """Monthly effective federal funds rate as a fraction (e.g. 0.0533), indexed
    by observation date. Cached locally; pulled from FRED's keyless CSV export."""
    _FRED_CACHE.mkdir(parents=True, exist_ok=True)
    path = _FRED_CACHE / "FEDFUNDS.csv"
    text = None
    if path.exists() and (time.time() - path.stat().st_mtime) < _FRED_TTL:
        text = path.read_text()
    if text is None:
        r = requests.get(_FRED_URL, timeout=30)
        r.raise_for_status()
        text = r.text
        path.write_text(text)
    df = pd.read_csv(io.StringIO(text))
    date_col = df.columns[0]               # "observation_date"
    df[date_col] = pd.to_datetime(df[date_col])
    s = pd.Series(df["FEDFUNDS"].astype(float).values, index=df[date_col]) / 100.0
    return s.dropna().sort_index()


# ── Data loading (parameterised window; fail-closed quality gate unchanged) ────

def load_all_data(prices_obj: PriceData, fund: EdgarFundamentals):
    """Signals + prices for all tickers over the warmup..fetch-end window.

    Mirrors scripts.run_portfolio_sim.load_all_data but with the 2008 warmup and
    the wider quality-year range. The quality gate keeps its fail-closed rule:
    a None result (missing/ambiguous fundamental) maps to False -> reject.
    """
    crossings_by_ticker: dict[str, list] = {}
    price_series: dict[str, pd.Series] = {}

    for ticker in VALIDATION_UNIVERSE:
        ohlcv = prices_obj.get_prices(ticker, _WARMUP_START, _FETCH_END)
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scored = compute_recovery_signals(ohlcv)

        quality: dict[int, bool] = {}
        for year in _QUALITY_YEARS:
            snap = fund.get_snapshot(ticker, date_type(year, 12, 31))
            g = passes_quality_gate(snap)
            quality[year] = False if g is None else g   # fail-closed (unchanged)

        crossings: list[tuple] = []
        prev_in_buy = False
        for i in range(len(scored)):
            ts = scored.index[i]
            comp = scored["composite_score"].iloc[i]
            if pd.isna(comp):
                prev_in_buy = False
                continue
            gate = quality.get(ts.year, False)
            in_buy = bool(comp >= BUY_THRESHOLD and gate)
            if in_buy and not prev_in_buy:
                crossings.append((
                    ts,
                    float(comp),
                    float(scored["Close"].iloc[i]),
                    float(scored["drawdown_52w"].iloc[i]),
                ))
            prev_in_buy = in_buy

        crossings_by_ticker[ticker] = crossings
        price_series[ticker] = scored["Close"]

    prices_wide = pd.DataFrame(price_series).ffill().bfill()
    spy_raw = prices_obj.get_prices("SPY", _WARMUP_START, _FETCH_END)
    spy_close = spy_raw["Close"] if spy_raw is not None and not spy_raw.empty else pd.Series(dtype=float)
    return crossings_by_ticker, prices_wide, spy_close


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(
    crossings_by_ticker: dict,
    prices_wide: pd.DataFrame,
    master_cal: pd.DatetimeIndex,
    sizing_mode: str,           # "flat" | "score_plus"
    cash_mode: str,             # "zero" | "fed_funds"
    rate_on_cal: np.ndarray,    # annual rate (fraction) in force on each cal day
    max_pos: int = 10,
) -> dict:
    """One portfolio run for a (sizing, cash) combination. Idle cash accrues the
    date-varying Fed funds rate (when cash_mode='fed_funds') before each day's
    portfolio value is measured."""
    events_by_date: dict = defaultdict(list)
    for ticker, crossings in crossings_by_ticker.items():
        for ts, comp, price, dd in crossings:
            if _SIM_START <= ts <= master_cal[-1]:
                events_by_date[ts].append((ticker, comp, price, dd))

    sim_prices = prices_wide.reindex(master_cal, method="ffill")
    prices_arr = sim_prices.values.astype(float)
    col_map = {c: i for i, c in enumerate(sim_prices.columns)}

    day_gap = master_cal.to_series().diff().dt.total_seconds().values / 86400.0
    day_gap[0] = 0.0

    def _price(day_idx: int, ticker: str) -> float:
        ci = col_map.get(ticker)
        if ci is None:
            return float("nan")
        return float(prices_arr[day_idx, ci])

    def _port_val_at(day_idx: int, cash: float, positions: dict) -> float:
        v = cash
        for pos in positions.values():
            p = _price(day_idx, pos["ticker"])
            v += pos["shares"] * (p if not np.isnan(p) else pos["entry_price"])
        return v

    def _cash_factor(day_idx: int) -> float:
        if day_idx == 0 or cash_mode == "zero":
            return 1.0
        if cash_mode == "fed_funds":
            r = rate_on_cal[day_idx]
            if not np.isfinite(r):
                r = 0.0
            return (1.0 + r) ** (day_gap[day_idx] / 365.0)
        return 1.0

    cash = _INITIAL_CAP
    open_pos: dict[int, dict] = {}
    last_entry_cal_idx: dict = {}
    trades: list[dict] = []
    skipped: list[dict] = []
    daily_values = np.zeros(len(master_cal))
    daily_cash_arr = np.zeros(len(master_cal))
    pid_ctr = 0

    for day_idx, day in enumerate(master_cal):
        cash *= _cash_factor(day_idx)
        port_val = _port_val_at(day_idx, cash, open_pos)

        # Exit at day 252
        for pid in [k for k, v in list(open_pos.items()) if v["exit_idx"] == day_idx]:
            pos = open_pos.pop(pid)
            ep = _price(day_idx, pos["ticker"])
            if np.isnan(ep):
                ep = pos["entry_price"]
            cash += pos["shares"] * ep
            trades.append({
                "ticker": pos["ticker"],
                "entry_date": pos["entry_date"].date(),
                "exit_date": day.date(),
                "ret": ep / pos["entry_price"] - 1,
                "pnl": pos["shares"] * ep - pos["cost"],
                "comp": pos["comp"],
                "size_pct": pos["size_pct"],
                "actual_pct": pos["cost"] / pos["port_val_at_entry"],
                "status": "CLOSED",
            })

        # New signals (highest composite first)
        for ticker, comp, crossing_price, dd in sorted(
            events_by_date.get(day, []), key=lambda x: -x[1]
        ):
            if ticker in last_entry_cal_idx and \
               (day_idx - last_entry_cal_idx[ticker]) < _HOLD_DAYS:
                continue
            if len(open_pos) >= max_pos:
                skipped.append({"date": day.date(), "ticker": ticker,
                                "comp": comp, "reason": "capacity"})
                continue

            if sizing_mode == "flat":
                size_pct = 0.10
                alloc = min(port_val * size_pct, cash)
            elif sizing_mode == "score_plus":
                size_pct = 0.12 if comp >= 0.70 else 0.10
                desired = port_val * size_pct
                if cash < port_val * 0.05:
                    skipped.append({"date": day.date(), "ticker": ticker,
                                    "comp": comp, "reason": "below_5pct_free"})
                    continue
                alloc = min(desired, cash)
            else:
                raise ValueError(f"unknown sizing_mode {sizing_mode!r}")

            if alloc < _MIN_POSITION:
                skipped.append({"date": day.date(), "ticker": ticker,
                                "comp": comp, "reason": "min_pos"})
                continue

            ep = _price(day_idx, ticker)
            if np.isnan(ep) or ep <= 0:
                ep = crossing_price
            if ep <= 0:
                continue

            pid_ctr += 1
            open_pos[pid_ctr] = {
                "ticker": ticker,
                "entry_date": day,
                "entry_idx": day_idx,
                "exit_idx": min(day_idx + _HOLD_DAYS, len(master_cal) - 1),
                "entry_price": ep,
                "shares": alloc / ep,
                "cost": alloc,
                "comp": comp,
                "size_pct": size_pct,
                "port_val_at_entry": port_val,
            }
            last_entry_cal_idx[ticker] = day_idx
            cash -= alloc

        daily_values[day_idx] = _port_val_at(day_idx, cash, open_pos)
        daily_cash_arr[day_idx] = cash

    last_idx = len(master_cal) - 1
    last_day = master_cal[last_idx]
    for pos in open_pos.values():
        ep = _price(last_idx, pos["ticker"])
        if np.isnan(ep):
            ep = pos["entry_price"]
        trades.append({
            "ticker": pos["ticker"],
            "entry_date": pos["entry_date"].date(),
            "exit_date": last_day.date(),
            "ret": ep / pos["entry_price"] - 1,
            "pnl": pos["shares"] * ep - pos["cost"],
            "comp": pos["comp"],
            "size_pct": pos["size_pct"],
            "actual_pct": pos["cost"] / pos["port_val_at_entry"],
            "status": "OPEN (MTM 2024-12-31)",
        })

    trades.sort(key=lambda t: t["entry_date"])
    dv = pd.Series(daily_values, index=master_cal)
    dc = pd.Series(daily_cash_arr, index=master_cal)
    return {"trades": trades, "skipped": skipped, "daily_values": dv,
            "daily_cash": dc, "final_value": float(dv.iloc[-1])}


# ── Metrics (span-aware; the imported compute_metrics hardcodes 7y/2018) ───────

def series_metrics(dv: pd.Series, initial_cap: float, years: float) -> dict:
    final_val = float(dv.iloc[-1])
    total_ret = final_val / initial_cap - 1
    cagr = (final_val / initial_cap) ** (1.0 / years) - 1
    rmax = dv.cummax()
    max_dd = float(((dv - rmax) / rmax).min())
    rets = dv.pct_change().dropna()
    std = float(rets.std())
    sharpe = float(rets.mean() * np.sqrt(252) / std) if std > 0 else 0.0
    return {"final_value": final_val, "total_ret": total_ret, "cagr": cagr,
            "max_dd": max_dd, "sharpe": sharpe}


def make_chart(curves: dict, spy_curve: pd.Series, out_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    styles = {
        "A · V1 baseline (flat, 0%)":         dict(color="#888888", lw=1.8, ls="--"),
        "B · Score-Plus only (0%)":           dict(color="#1f77b4", lw=1.8),
        "C · Fed funds only (flat)":          dict(color="#2ca02c", lw=1.8),
        "D · Full combo (Score-Plus + FF)":   dict(color="#d62728", lw=2.4),
    }
    fig, ax = plt.subplots(figsize=(12, 7))
    for label, s in curves.items():
        ax.plot(s.index, s.values, label=label, **styles.get(label, {}))
    ax.plot(spy_curve.index, spy_curve.values, label="SPY buy & hold (reference)",
            color="#9467bd", lw=1.1, ls=":")
    ax.set_title("Combined validation 2010-2024 — V1 portfolio (max 10, 252d hold), $100k start\n"
                 "Score-Plus sizing × real historical Fed funds cash sleeve",
                 fontsize=12)
    ax.set_ylabel("Portfolio value")
    ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)


def main() -> None:
    print("Loading price data, signals, and Fed funds history (cached)...")
    prices_obj = PriceData()
    fund = EdgarFundamentals(fallback=PointInTimeFundamentals())

    crossings_by_ticker, prices_wide, spy_close = load_all_data(prices_obj, fund)

    spy_sim = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)]
    master_cal = spy_sim.index
    years = (master_cal[-1] - master_cal[0]).days / 365.25

    # Real Fed funds rate aligned to the trading calendar (monthly value ffwd).
    ff_monthly = load_fedfunds()
    rate_on_cal = ff_monthly.reindex(master_cal, method="ffill").values.astype(float)

    n_with_signals = sum(1 for v in crossings_by_ticker.values() if v)
    print(f"  Calendar: {master_cal[0].date()} – {master_cal[-1].date()} "
          f"({len(master_cal)} trading days, {years:.2f}y)")
    print(f"  Tickers loaded: {len(crossings_by_ticker)}  "
          f"(with >=1 BUY crossing: {n_with_signals})")
    print(f"  Fed funds on calendar: {np.nanmin(rate_on_cal):.2%} … "
          f"{np.nanmax(rate_on_cal):.2%}  (mean {np.nanmean(rate_on_cal):.2%})")
    print()

    variants = [
        ("A · V1 baseline (flat, 0%)",        "flat",       "zero"),
        ("B · Score-Plus only (0%)",          "score_plus", "zero"),
        ("C · Fed funds only (flat)",         "flat",       "fed_funds"),
        ("D · Full combo (Score-Plus + FF)",  "score_plus", "fed_funds"),
    ]

    metrics, curves = {}, {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for label, sizing, cash in variants:
            res = simulate(crossings_by_ticker, prices_wide, master_cal,
                           sizing, cash, rate_on_cal)
            metrics[label] = series_metrics(res["daily_values"], _INITIAL_CAP, years)
            curves[label] = res["daily_values"]
            m = metrics[label]
            print(f"  {label:<36}  final ${m['final_value']:>12,.0f}  CAGR {m['cagr']:+.2%}")

    spy_curve = spy_close.reindex(master_cal, method="ffill")
    spy_curve = spy_curve / float(spy_curve.iloc[0]) * _INITIAL_CAP
    spy_m = series_metrics(spy_curve, _INITIAL_CAP, years)

    A = metrics["A · V1 baseline (flat, 0%)"]
    B = metrics["B · Score-Plus only (0%)"]
    C = metrics["C · Fed funds only (flat)"]
    D = metrics["D · Full combo (Score-Plus + FF)"]

    div = "=" * 96
    print()
    print(div)
    print("COMBINED VALIDATION 2010-2024 — Score-Plus sizing × real Fed funds cash sleeve")
    print("$100,000 start · 2010-01-04 – 2024-12-31 · 50 tickers · thr 0.60 · 252d exit · no SL")
    print(div)
    print()

    print(f"  {'Variant':<36}  {'Final $':>13}  {'TotRet':>9}  {'CAGR':>7}  "
          f"{'Sharpe':>7}  {'MaxDD':>7}")
    print("  " + "-" * 94)
    for label, _, _ in variants:
        m = metrics[label]
        print(f"  {label:<36}  ${m['final_value']:>12,.0f}  {m['total_ret']:>+8.1%}  "
              f"{m['cagr']:>+6.1%}  {m['sharpe']:>7.2f}  {m['max_dd']:>6.1%}")
    print(f"  {'SPY buy & hold (reference)':<36}  ${spy_m['final_value']:>12,.0f}  "
          f"{spy_m['total_ret']:>+8.1%}  {spy_m['cagr']:>+6.1%}  {spy_m['sharpe']:>7.2f}  "
          f"{spy_m['max_dd']:>6.1%}")
    print()

    # ── Average actual Fed funds rate by year (validation) ─────────────────────
    rate_series = pd.Series(rate_on_cal, index=master_cal)
    print("  AVERAGE ACTUAL FED FUNDS RATE BY YEAR  (rate the sim applied to idle cash)")
    print("  " + "-" * 94)
    by_year = rate_series.groupby(rate_series.index.year).mean()
    yrs = list(by_year.index)
    for i in range(0, len(yrs), 5):
        chunk = yrs[i:i + 5]
        line_labels = "    " + "  ".join(f"{y:>7}" for y in chunk)
        line_vals = "    " + "  ".join(f"{by_year[y]*100:>6.2f}%" for y in chunk)
        print(line_labels)
        print(line_vals)
    print()

    # ── Linear-expectation check (interaction) ─────────────────────────────────
    imp_B = B["final_value"] - A["final_value"]
    imp_C = C["final_value"] - A["final_value"]
    expected_D = A["final_value"] + imp_B + imp_C
    actual_D = D["final_value"]
    interaction = actual_D - expected_D
    imp_B_ret = B["total_ret"] - A["total_ret"]
    imp_C_ret = C["total_ret"] - A["total_ret"]
    expected_D_ret = A["total_ret"] + imp_B_ret + imp_C_ret
    interaction_ret = D["total_ret"] - expected_D_ret

    print("  LINEAR-EXPECTATION CHECK FOR VARIANT D  (does the stack add up?)")
    print("  " + "-" * 94)
    print(f"    Baseline A:                          ${A['final_value']:>12,.0f}   "
          f"({A['total_ret']:+.1%})")
    print(f"    + Score-Plus improvement (B−A):      ${imp_B:>+12,.0f}   ({imp_B_ret:+.1%})")
    print(f"    + Fed-funds improvement (C−A):       ${imp_C:>+12,.0f}   ({imp_C_ret:+.1%})")
    print(f"    ────────────────────────────────────────────────────────")
    print(f"    = Expected D (linear sum):           ${expected_D:>12,.0f}   ({expected_D_ret:+.1%})")
    print(f"      Actual D (measured):               ${actual_D:>12,.0f}   ({D['total_ret']:+.1%})")
    print(f"      Interaction (actual − expected):   ${interaction:>+12,.0f}   ({interaction_ret:+.1%})")
    print()
    total_imp = abs(imp_B) + abs(imp_C)
    rel = abs(interaction) / total_imp if total_imp > 0 else 0.0
    direction = "positive" if interaction > 0 else "negative" if interaction < 0 else "zero"
    if rel < 0.10:
        print(f"    Interaction is NEGLIGIBLE / ESSENTIALLY ADDITIVE — only {rel:.0%} of the "
              f"combined lift\n    (a {direction} cross-term): more deployment by Score-Plus "
              f"leaves less cash for the\n    sleeve (drag), but each lever enlarges the base "
              f"the other compounds on (boost); near-cancel.")
    elif interaction > 0:
        print("    Interaction is POSITIVE / SUPER-ADDITIVE — the levers reinforce each other.")
    else:
        print("    Interaction is NEGATIVE / SUB-ADDITIVE — the levers compete for the same idle cash.")
    print()

    # ── Caveats ────────────────────────────────────────────────────────────────
    print(div)
    print("CAVEATS — read before trusting these numbers")
    print(div)
    print("  1. SURVIVORSHIP BIAS. The 50-ticker universe is hand-picked from firms that")
    print("     SURVIVED to 2024 (AAPL, NVDA, JPM, …). Companies that blew up, were delisted,")
    print("     or were acquired after a crash are absent. The recovery signal therefore only")
    print("     ever sees names that, by construction, eventually recovered — it never bets on")
    print("     a 'dip' that went to zero. This inflates returns, and the distortion is WORST")
    print("     in the early years: the pre-2015 results in particular should be read as")
    print("     optimistic upper bounds, not as achievable performance.")
    print()
    print("  2. EDGAR fundamentals thin out before ~2015. Where a fundamental needed by the")
    print("     quality gate is missing, the EXISTING fail-closed logic rejects the signal")
    print("     (gate=None → False). That is left unchanged here, so some otherwise-valid")
    print("     early signals are simply dropped — a conservative effect on the count of")
    print("     trades, but one that further thins the already-thin early sample.")
    print()
    print(div)

    out_path = Path(__file__).parent.parent / "results" / "combined_validation_2010_2024.png"
    make_chart(curves, spy_curve, out_path)
    print(f"Chart written: {out_path}")


if __name__ == "__main__":
    main()
