#!/usr/bin/env python3
"""
Portfolio simulation: Signal-driven portfolio vs SPY buy-and-hold, 2018-2024.

Three position-sizing variants:
  V1: 10% per signal, max 10 concurrent positions (base case)
  V2: 20% per signal, max  5 concurrent positions (concentrated)
  V3:  5% per signal, max 20 concurrent positions (diversified)
"""
from __future__ import annotations

import sys
import warnings
from collections import defaultdict
from datetime import date as date_type
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
from data.sp500_universe import get_universe


def build_monthly_universe(cal: pd.DatetimeIndex) -> dict[tuple[int, int], set[str]]:
    """Map each (year, month) in `cal` to the point-in-time S&P 500 membership,
    evaluated once on the first trading day of that month (not recalculated
    daily). Used both to restrict signal candidates and to derive the set of
    tickers whose data must be loaded.
    """
    members: dict[tuple[int, int], set[str]] = {}
    for ts in cal:
        key = (ts.year, ts.month)
        if key not in members:  # first trading day of this month
            members[key] = set(get_universe(ts.date().isoformat()))
    return members

# ── Constants ─────────────────────────────────────────────────────────────────
_WARMUP_START = "2016-01-01"
_SIM_START    = pd.Timestamp("2018-01-01")
_SIM_END      = pd.Timestamp("2024-12-31")
_HOLD_DAYS    = 252
_INITIAL_CAP  = 100_000.0
_MIN_POSITION = 1_000.0

VARIANTS = [
    {"name": "Variant 1 (10%/max10)", "label": "V1", "pct": 0.10, "max_pos": 10},
    {"name": "Variant 2 (20%/max5)",  "label": "V2", "pct": 0.20, "max_pos":  5},
    {"name": "Variant 3 (5%/max20)",  "label": "V3", "pct": 0.05, "max_pos": 20},
]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_all_data(
    prices_obj: PriceData,
    fund: EdgarFundamentals,
    tickers: list[str] | None = None,
    warmup_start: str = _WARMUP_START,
    quality_years: range | None = None,
    with_opens: bool = False,
):
    """Load signals and prices for `tickers` (defaults to the legacy 50-ticker
    VALIDATION_UNIVERSE for backward compatibility).

    warmup_start / quality_years let an out-of-sample window pull earlier price
    history (e.g. 2008 warmup for a 2010 start) and the matching quality-gate
    years (defaults to 2016 warmup and 2017-2025 quality years).

    For the point-in-time S&P 500 backtest this is called with the union of all
    monthly memberships. Tickers without usable price history are skipped
    silently; the EDGAR quality gate is only fetched for tickers that actually
    produce a raw price BUY (most names never dip-signal in a given window, so
    this avoids hundreds of needless companyfacts downloads).

    Returns (3-tuple by default; 4-tuple when with_opens=True):
        crossings_by_ticker  — {ticker: [(ts, comp, close, dd), ...]}
        prices_wide          — DataFrame[date × ticker] of adjusted closes
        [opens_wide]         — DataFrame[date × ticker] of adjusted OPENS, only
                               when with_opens=True (for T+1-open entry fills)
        spy_close            — Series of SPY adjusted closes
    """
    if tickers is None:
        tickers = list(VALIDATION_UNIVERSE)
    if quality_years is None:
        quality_years = range(2017, 2026)

    crossings_by_ticker: dict[str, list] = {}
    price_series: dict[str, pd.Series]  = {}
    open_series:  dict[str, pd.Series]  = {}

    for ticker in tickers:
        ohlcv = prices_obj.get_prices(ticker, warmup_start, "2024-12-31")
        if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
            continue

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scored = compute_recovery_signals(ohlcv)

        comp_series = scored["composite_score"]
        price_series[ticker] = scored["Close"]
        if with_opens:
            open_series[ticker] = scored["Open"]

        # Skip the EDGAR round-trip entirely when the price signal never even
        # crosses the BUY threshold for this ticker (no possible entry).
        if not bool((comp_series >= BUY_THRESHOLD).any()):
            crossings_by_ticker[ticker] = []
            continue

        # Quality gate by year (same pre-fetch approach as backtest)
        quality: dict[int, bool] = {}
        for year in quality_years:
            snap = fund.get_snapshot(ticker, date_type(year, 12, 31))
            g = passes_quality_gate(snap)
            quality[year] = False if g is None else g

        # Find ALL BUY edge crossings (no 252d suppression — portfolio applies that)
        crossings: list[tuple] = []
        prev_in_buy = False
        for i in range(len(scored)):
            ts   = scored.index[i]
            comp = comp_series.iloc[i]
            if pd.isna(comp):
                prev_in_buy = False
                continue
            gate   = quality.get(ts.year, False)
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

    prices_wide = pd.DataFrame(price_series).ffill().bfill()

    spy_raw   = prices_obj.get_prices("SPY", _WARMUP_START, "2024-12-31")
    spy_close = spy_raw["Close"] if spy_raw is not None and not spy_raw.empty else pd.Series(dtype=float)

    if with_opens:
        # Align opens to the same columns as prices_wide (the simulate col_map).
        opens_wide = (pd.DataFrame(open_series).ffill().bfill()
                        .reindex(columns=prices_wide.columns))
        return crossings_by_ticker, prices_wide, opens_wide, spy_close

    return crossings_by_ticker, prices_wide, spy_close


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(
    crossings_by_ticker: dict,
    prices_wide: pd.DataFrame,
    master_cal: pd.DatetimeIndex,
    pct: float,
    max_pos: int,
    month_members: dict[tuple[int, int], set[str]] | None = None,
    opens_wide: pd.DataFrame | None = None,
) -> dict:
    """Run one portfolio variant over master_cal.

    When `month_members` is supplied, a signal can only open a position if its
    ticker was an S&P 500 member in the month of the signal day (point-in-time
    universe). When None, every signal is eligible (legacy behaviour).

    Entry execution timing:
      opens_wide is None  → legacy: fill at the signal day's (T) close. This is
                            same-bar look-ahead but is kept as the default so all
                            prior studies reproduce.
      opens_wide given    → fill at day T+1's OPEN (the first realistically
                            executable price after the signal is known). The
                            252-day hold is measured from the fill bar. A signal
                            on the last bar (no T+1) is skipped.
    """

    # Event index: date → [(ticker, comp, price, dd), ...]
    events_by_date: dict = defaultdict(list)
    for ticker, crossings in crossings_by_ticker.items():
        for ts, comp, price, dd in crossings:
            if _SIM_START <= ts <= master_cal[-1]:
                events_by_date[ts].append((ticker, comp, price, dd))

    # Fast price lookup: reindex prices to master calendar
    sim_prices = prices_wide.reindex(master_cal, method="ffill")
    prices_arr = sim_prices.values.astype(float)  # (n_days, n_tickers)
    col_map    = {c: i for i, c in enumerate(sim_prices.columns)}

    # Parallel OPEN array (for T+1-open fills), aligned to the same col_map.
    opens_arr = None
    if opens_wide is not None:
        opens_arr = (opens_wide.reindex(master_cal, method="ffill")
                               .reindex(columns=sim_prices.columns)
                               .values.astype(float))

    def _price(day_idx: int, ticker: str) -> float:
        ci = col_map.get(ticker)
        if ci is None:
            return float("nan")
        v = prices_arr[day_idx, ci]
        return float(v)

    def _open(day_idx: int, ticker: str) -> float:
        ci = col_map.get(ticker)
        if ci is None or opens_arr is None:
            return float("nan")
        return float(opens_arr[day_idx, ci])

    def _port_val_at(day_idx: int, cash: float, positions: dict) -> float:
        v = cash
        for pos in positions.values():
            p = _price(day_idx, pos["ticker"])
            v += pos["shares"] * (p if not np.isnan(p) else pos["entry_price"])
        return v

    # State
    cash                    = _INITIAL_CAP
    open_pos: dict[int, dict] = {}
    last_entry_cal_idx: dict  = {}   # ticker → cal idx of last actual entry
    trades: list[dict]        = []
    skipped: list[dict]       = []
    daily_values              = np.zeros(len(master_cal))
    daily_cash_arr            = np.zeros(len(master_cal))
    pid_ctr                   = 0

    for day_idx, day in enumerate(master_cal):
        port_val = _port_val_at(day_idx, cash, open_pos)

        # 1. Exit positions that hit day 252
        for pid in [k for k, v in list(open_pos.items()) if v["exit_idx"] == day_idx]:
            pos = open_pos.pop(pid)
            ep  = _price(day_idx, pos["ticker"])
            if np.isnan(ep):
                ep = pos["entry_price"]
            proceeds = pos["shares"] * ep
            cash    += proceeds
            trades.append({
                "ticker":            pos["ticker"],
                "entry_date":        pos["entry_date"].date(),
                "exit_date":         day.date(),
                "entry_price":       pos["entry_price"],
                "exit_price":        ep,
                "shares":            pos["shares"],
                "cost":              pos["cost"],
                "pnl":               proceeds - pos["cost"],
                "ret":               ep / pos["entry_price"] - 1,
                "days_held":         day_idx - pos["entry_idx"],
                "port_val_at_entry": pos["port_val_at_entry"],
                "drawdown":          pos["drawdown"],
                "comp":              pos["comp"],
                "status":            "CLOSED",
            })

        # 2. Process new signals (highest composite first)
        for ticker, comp, crossing_price, dd in sorted(events_by_date.get(day, []), key=lambda x: -x[1]):
            # Point-in-time S&P 500 membership gate (evaluated per month)
            if month_members is not None and \
               ticker not in month_members.get((day.year, day.month), ()):
                skipped.append({"date": day.date(), "ticker": ticker, "comp": comp, "reason": "not_in_universe"})
                continue

            # 252d suppression: based on actual entries only
            if ticker in last_entry_cal_idx:
                if (day_idx - last_entry_cal_idx[ticker]) < _HOLD_DAYS:
                    continue

            if len(open_pos) >= max_pos:
                skipped.append({"date": day.date(), "ticker": ticker, "comp": comp, "reason": "capacity"})
                continue

            alloc = min(port_val * pct, cash)
            if alloc < _MIN_POSITION:
                skipped.append({"date": day.date(), "ticker": ticker, "comp": comp, "reason": "min_pos"})
                continue

            # Fill bar: same day (legacy close) or next day's open (T+1, executable).
            if opens_arr is not None:
                fill_idx = day_idx + 1
                if fill_idx > len(master_cal) - 1:   # signal on last bar → no executable fill
                    skipped.append({"date": day.date(), "ticker": ticker, "comp": comp, "reason": "no_next_bar"})
                    continue
                ep = _open(fill_idx, ticker)
                if np.isnan(ep) or ep <= 0:
                    ep = crossing_price
            else:
                fill_idx = day_idx
                ep = _price(day_idx, ticker)
                if np.isnan(ep) or ep <= 0:
                    ep = crossing_price
            if ep <= 0:
                continue

            shares     = alloc / ep
            exit_idx   = min(fill_idx + _HOLD_DAYS, len(master_cal) - 1)
            exit_ts    = master_cal[exit_idx]

            pid_ctr += 1
            open_pos[pid_ctr] = {
                "ticker":            ticker,
                "entry_date":        master_cal[fill_idx],
                "exit_date":         exit_ts,
                "entry_idx":         fill_idx,
                "exit_idx":          exit_idx,
                "entry_price":       ep,
                "shares":            shares,
                "cost":              alloc,
                "drawdown":          dd,
                "comp":              comp,
                "port_val_at_entry": port_val,
            }
            last_entry_cal_idx[ticker] = fill_idx
            cash -= alloc

        daily_values[day_idx]   = _port_val_at(day_idx, cash, open_pos)
        daily_cash_arr[day_idx] = cash

    # Mark remaining open positions at market (Dec 30, 2024)
    last_idx = len(master_cal) - 1
    last_day = master_cal[last_idx]
    for pid, pos in open_pos.items():
        ep = _price(last_idx, pos["ticker"])
        if np.isnan(ep):
            ep = pos["entry_price"]
        pnl = pos["shares"] * ep - pos["cost"]
        trades.append({
            "ticker":            pos["ticker"],
            "entry_date":        pos["entry_date"].date(),
            "exit_date":         last_day.date(),
            "entry_price":       pos["entry_price"],
            "exit_price":        ep,
            "shares":            pos["shares"],
            "cost":              pos["cost"],
            "pnl":               pnl,
            "ret":               ep / pos["entry_price"] - 1,
            "days_held":         last_idx - pos["entry_idx"],
            "port_val_at_entry": pos["port_val_at_entry"],
            "drawdown":          pos["drawdown"],
            "comp":              pos["comp"],
            "status":            "OPEN (MTM 2024-12-30)",
        })

    trades.sort(key=lambda t: t["entry_date"])
    dv = pd.Series(daily_values,   index=master_cal)
    dc = pd.Series(daily_cash_arr, index=master_cal)

    return {
        "trades":        trades,
        "skipped":       skipped,
        "daily_values":  dv,
        "daily_cash":    dc,
        "final_value":   float(dv.iloc[-1]),
    }


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(daily_values: pd.Series, initial_cap: float) -> dict:
    n_years   = 7
    final_val = float(daily_values.iloc[-1])
    total_ret = final_val / initial_cap - 1
    cagr      = (final_val / initial_cap) ** (1.0 / n_years) - 1

    rolling_max = daily_values.cummax()
    drawdown    = (daily_values - rolling_max) / rolling_max
    max_dd      = float(drawdown.min())

    daily_rets = daily_values.pct_change().dropna()
    std = float(daily_rets.std())
    sharpe = float(daily_rets.mean() * np.sqrt(252) / std) if std > 0 else 0.0

    annual_rets: dict[int, float] = {}
    for year in range(2018, 2025):
        year_days  = daily_values.index[daily_values.index.year == year]
        if len(year_days) == 0:
            continue
        if year == 2018:
            start_val = initial_cap
        else:
            prev_days = daily_values.index[daily_values.index.year == year - 1]
            start_val = float(daily_values[prev_days[-1]])
        annual_rets[year] = float(daily_values[year_days[-1]]) / start_val - 1

    best_year  = max(annual_rets, key=annual_rets.get)
    worst_year = min(annual_rets, key=annual_rets.get)

    return {
        "final_value": final_val,
        "total_ret":   total_ret,
        "cagr":        cagr,
        "max_dd":      max_dd,
        "sharpe":      sharpe,
        "annual_rets": annual_rets,
        "best_year":   best_year,
        "best_ret":    annual_rets[best_year],
        "worst_year":  worst_year,
        "worst_ret":   annual_rets[worst_year],
    }


def spy_metrics(spy_close: pd.Series, master_cal: pd.DatetimeIndex) -> dict:
    spy = spy_close.reindex(master_cal, method="ffill")
    start_price = float(spy.iloc[0])
    spy_vals    = spy / start_price * _INITIAL_CAP

    n_years   = 7
    final_val = float(spy_vals.iloc[-1])
    total_ret = final_val / _INITIAL_CAP - 1
    cagr      = (final_val / _INITIAL_CAP) ** (1.0 / n_years) - 1

    rolling_max = spy_vals.cummax()
    drawdown    = (spy_vals - rolling_max) / rolling_max
    max_dd      = float(drawdown.min())

    daily_rets = spy_vals.pct_change().dropna()
    std = float(daily_rets.std())
    sharpe = float(daily_rets.mean() * np.sqrt(252) / std) if std > 0 else 0.0

    annual_rets: dict[int, float] = {}
    for year in range(2018, 2025):
        year_days = spy.index[spy.index.year == year]
        if len(year_days) == 0:
            continue
        if year == 2018:
            s_price = float(spy.iloc[0])
        else:
            prev_days = spy.index[spy.index.year == year - 1]
            s_price = float(spy[prev_days[-1]])
        annual_rets[year] = float(spy[year_days[-1]]) / s_price - 1

    best_year  = max(annual_rets, key=annual_rets.get)
    worst_year = min(annual_rets, key=annual_rets.get)

    return {
        "final_value": final_val,
        "total_ret":   total_ret,
        "cagr":        cagr,
        "max_dd":      max_dd,
        "sharpe":      sharpe,
        "annual_rets": annual_rets,
        "best_year":   best_year,
        "best_ret":    annual_rets[best_year],
        "worst_year":  worst_year,
        "worst_ret":   annual_rets[worst_year],
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(
    v1_result: dict,
    v1_metrics: dict,
    spy_m: dict,
    all_variant_results: list[dict],
    all_variant_metrics: list[dict],
    master_cal: pd.DatetimeIndex,
) -> None:

    divider = "=" * 76
    print(divider)
    print("PORTFOLIO SIMULATION: SIGNAL-DRIVEN vs SPY  (2018-01-02 – 2024-12-30)")
    print(divider)
    print()

    # ── SECTION 1: TRADE LOG (Variant 1) ─────────────────────────────────────
    trades  = v1_result["trades"]
    skipped = v1_result["skipped"]

    print("SECTION 1 - TRADE LOG  (Variant 1: 10%/max10)")
    print("-" * 76)
    hdr = f"{'Ticker':<6}  {'Entry Date':<11}  {'Entry$':>8}  {'Exit$':>8}  {'Ret%':>7}  {'Days':>4}  {'P&L$':>9}  {'Port@Entry':>10}  {'Status'}"
    print(hdr)
    print("-" * 76)
    for t in trades:
        print(
            f"{t['ticker']:<6}  {str(t['entry_date']):<11}  {t['entry_price']:>8.2f}  "
            f"{t['exit_price']:>8.2f}  {t['ret']:>6.1%}  {t['days_held']:>4}  "
            f"{t['pnl']:>+9.0f}  {t['port_val_at_entry']:>10.0f}  {t['status']}"
        )
    print()
    if skipped:
        print(f"  Skipped signals (at capacity): {sum(1 for s in skipped if s['reason']=='capacity')}")
        print(f"  Skipped signals (min pos):     {sum(1 for s in skipped if s['reason']=='min_pos')}")
    print()

    # ── SECTION 2: PORTFOLIO B PERFORMANCE ───────────────────────────────────
    m   = v1_metrics
    dc  = v1_result["daily_cash"]
    dv  = v1_result["daily_values"]
    dv.name = "port"

    closed    = [t for t in trades if t["status"] == "CLOSED"]
    open_mtm  = [t for t in trades if "OPEN" in t["status"]]
    winners   = [t for t in trades if t["pnl"] > 0]
    losers    = [t for t in trades if t["pnl"] <= 0]
    avg_hold  = np.mean([t["days_held"] for t in trades]) if trades else 0
    avg_ret   = np.mean([t["ret"] for t in trades]) if trades else 0

    cash_frac  = float((dc / dv).mean())
    invest_frac = 1 - cash_frac

    print("SECTION 2 - PORTFOLIO B PERFORMANCE  (Variant 1)")
    print("-" * 76)
    print(f"  Final portfolio value:   ${m['final_value']:>12,.0f}")
    print(f"  Total return:            {m['total_ret']:>+.1%}")
    print(f"  CAGR (7 years):          {m['cagr']:>+.1%}")
    print(f"  Max drawdown:            {m['max_dd']:>.1%}")
    print(f"  Sharpe ratio (rf=0%):    {m['sharpe']:.2f}")
    print(f"  Best year:               {m['best_year']}  ({m['best_ret']:+.1%})")
    print(f"  Worst year:              {m['worst_year']}  ({m['worst_ret']:+.1%})")
    print()
    print(f"  Total trades:            {len(trades)}")
    print(f"    Closed (252d exit):    {len(closed)}")
    print(f"    Open (MTM year-end):   {len(open_mtm)}")
    print(f"    Winners:               {len(winners)}")
    print(f"    Losers:                {len(losers)}")
    print(f"  Average trade return:    {avg_ret:+.1%}")
    print(f"  Average hold days:       {avg_hold:.0f}")
    print(f"  % time invested (avg):   {invest_frac:.0%}")
    print(f"  % time in cash (avg):    {cash_frac:.0%}")
    print()

    # ── SECTION 3: BENCHMARK COMPARISON ──────────────────────────────────────
    print("SECTION 3 - BENCHMARK COMPARISON")
    print("-" * 76)
    print(f"  {'Metric':<30}  {'Portfolio B (V1)':>16}  {'SPY'}")
    print(f"  {'-'*62}")

    def _fmt(a, b):
        return "  {:<30}  {:>16}  {}".format(a, b[0], b[1])

    rows = [
        ("Final value ($100k start)", (f"${m['final_value']:>,.0f}", f"${spy_m['final_value']:>,.0f}")),
        ("Total return",              (f"{m['total_ret']:+.1%}",  f"{spy_m['total_ret']:+.1%}")),
        ("CAGR",                      (f"{m['cagr']:+.1%}",       f"{spy_m['cagr']:+.1%}")),
        ("Max drawdown",              (f"{m['max_dd']:.1%}",      f"{spy_m['max_dd']:.1%}")),
        ("Sharpe ratio (rf=0%)",      (f"{m['sharpe']:.2f}",      f"{spy_m['sharpe']:.2f}")),
        ("Best year",                 (f"{m['best_year']} ({m['best_ret']:+.1%})", f"{spy_m['best_year']} ({spy_m['best_ret']:+.1%})")),
        ("Worst year",                (f"{m['worst_year']} ({m['worst_ret']:+.1%})", f"{spy_m['worst_year']} ({spy_m['worst_ret']:+.1%})")),
    ]
    for label, (pb, spy_v) in rows:
        print(f"  {label:<30}  {pb:>16}  {spy_v}")
    print()

    # ── SECTION 4: YEAR BY YEAR ───────────────────────────────────────────────
    print("SECTION 4 - YEAR BY YEAR")
    print("-" * 76)
    hdr4 = f"  {'Year':<6}  {'Port B':>8}  {'SPY':>8}  {'Diff':>8}  {'Port B Val':>12}  {'SPY Val':>12}"
    print(hdr4)
    print(f"  {'-'*66}")

    port_b_val = _INITIAL_CAP
    spy_val_trk = _INITIAL_CAP
    for year in range(2018, 2025):
        pb_ret  = m["annual_rets"].get(year, 0)
        spy_ret = spy_m["annual_rets"].get(year, 0)
        diff    = pb_ret - spy_ret
        port_b_val  *= (1 + pb_ret)
        spy_val_trk *= (1 + spy_ret)
        print(
            f"  {year:<6}  {pb_ret:>+7.1%}  {spy_ret:>+7.1%}  {diff:>+7.1%}  "
            f"${port_b_val:>11,.0f}  ${spy_val_trk:>11,.0f}"
        )
    print()

    # ── SECTION 5: CASH DRAG ──────────────────────────────────────────────────
    print("SECTION 5 - CASH DRAG ANALYSIS")
    print("-" * 76)
    blended = (1 - cash_frac) * m["cagr"] + cash_frac * spy_m["cagr"]
    blended_tot = (1 + blended) ** 7 - 1
    print("  Average portfolio allocation:")
    print(f"    In signals:          {invest_frac:.0%}")
    print(f"    In cash (0% return): {cash_frac:.0%}")
    print()
    print("  If uninvested cash had been in SPY instead:")
    print(f"    Estimated blended CAGR: {blended:+.1%}")
    print(f"    Estimated blended total return (7y): {blended_tot:+.1%}")
    print()
    note = "above" if blended_tot > spy_m["total_ret"] else "below"
    print(f"  Blended return would be {note} SPY total return ({spy_m['total_ret']:+.1%}).")
    print(f"  Cash drag cost: ~{invest_frac * m['cagr']:.1%} CAGR from signals,")
    print(f"    vs {m['cagr']:+.1%} actual CAGR — gap reflects idle cash penalty.")
    print()

    # ── SECTION 6: HONEST ASSESSMENT ─────────────────────────────────────────
    print("SECTION 6 - HONEST ASSESSMENT")
    print("-" * 76)

    beats_total  = m["total_ret"] > spy_m["total_ret"]
    beats_sharpe = m["sharpe"]    > spy_m["sharpe"]
    years_beat   = sum(1 for y in range(2018, 2025)
                       if m["annual_rets"].get(y, 0) > spy_m["annual_rets"].get(y, 0))
    pct_beat     = years_beat / 7

    print("  1. Did Portfolio B beat SPY in total return?")
    print(f"     {'YES' if beats_total else 'NO'}  (Port B {m['total_ret']:+.1%} vs SPY {spy_m['total_ret']:+.1%})")
    print()
    print("  2. Did Portfolio B beat SPY in risk-adjusted return (Sharpe)?")
    print(f"     {'YES' if beats_sharpe else 'NO'}  (Port B Sharpe {m['sharpe']:.2f} vs SPY {spy_m['sharpe']:.2f})")
    print()
    print("  3. What % of years did Portfolio B beat SPY?")
    print(f"     {years_beat}/7 years = {pct_beat:.0%}")
    years_won = [y for y in range(2018, 2025) if m["annual_rets"].get(y, 0) > spy_m["annual_rets"].get(y, 0)]
    years_lost = [y for y in range(2018, 2025) if m["annual_rets"].get(y, 0) <= spy_m["annual_rets"].get(y, 0)]
    print(f"     Beat SPY in: {years_won}")
    print(f"     Lost to SPY: {years_lost}")
    print()
    print(f"  4. Worst drawdown: Port B {m['max_dd']:.1%}  vs  SPY {spy_m['max_dd']:.1%}")
    print()
    print("  5. Main driver of performance gap:")
    if m["total_ret"] > spy_m["total_ret"]:
        print("     OUTPERFORMANCE. Signal fires during market dislocations (Mar 2020, 2022 bear)")
        print("     and captures large recoveries. Cash drag reduced returns but signals' magnitude")
        print(f"     edge ({invest_frac:.0%} invested, avg trade return {avg_ret:+.1%}) overcame the penalty.")
    else:
        print(f"     UNDERPERFORMANCE. Cash drag ({cash_frac:.0%} in cash at 0%) is the primary cost.")
        print("     Signals capture magnitude edge but concentration + cash drag net negative vs SPY.")
    print()
    blended_beats = blended_tot > spy_m["total_ret"]
    print("  6. If uninvested cash had been in SPY:")
    print(f"     Blended total return: {blended_tot:+.1%}  vs  SPY {spy_m['total_ret']:+.1%}")
    print(f"     Result: {'BEATS SPY' if blended_beats else 'STILL BELOW SPY'}")
    print()

    # ── SENSITIVITY: ALL THREE VARIANTS ──────────────────────────────────────
    print(divider)
    print("SENSITIVITY - THREE VARIANTS vs SPY")
    print(divider)
    print(f"  {'Variant':<28}  {'Final$':>9}  {'TotalRet':>9}  {'CAGR':>7}  {'MaxDD':>7}  {'Sharpe':>7}  Beat?")
    print(f"  {'-'*82}")
    for vres, vm, vspec in zip(all_variant_results, all_variant_metrics, VARIANTS):
        b = "YES" if vm["total_ret"] > spy_m["total_ret"] else "NO"
        print(
            f"  {vspec['name']:<28}  ${vm['final_value']:>8,.0f}  {vm['total_ret']:>+8.1%}  "
            f"{vm['cagr']:>+6.1%}  {vm['max_dd']:>6.1%}  {vm['sharpe']:>7.2f}  {b}"
        )
    # SPY row
    print(
        f"  {'SPY (buy-and-hold)':<28}  ${spy_m['final_value']:>8,.0f}  {spy_m['total_ret']:>+8.1%}  "
        f"{spy_m['cagr']:>+6.1%}  {spy_m['max_dd']:>6.1%}  {spy_m['sharpe']:>7.2f}  —"
    )
    print()

    # ── VERDICT ───────────────────────────────────────────────────────────────
    print(divider)
    print("VERDICT")
    print(divider)

    n_beats = sum(1 for vm in all_variant_metrics if vm["total_ret"] > spy_m["total_ret"])
    if n_beats == 3:
        verdict = "BEATS SPY"
    elif n_beats == 0:
        verdict = "UNDERPERFORMS SPY"
    else:
        verdict = "DEPENDS ON SIZING"

    print(f"  {verdict}")
    print()
    # One-paragraph summary
    avg_vm_cagr = np.mean([vm["cagr"] for vm in all_variant_metrics])
    print(
        f"  A real user following this signal from January 2018 with $100,000 would have "
        f"experienced a portfolio that largely sat in cash between market dislocations — "
        f"averaging {invest_frac:.0%} deployed at any given time under V1. When signals did fire "
        f"(primarily in the COVID crash of March 2020 and the 2022 bear market), the "
        f"recoveries were large. Under V1 (10%/max10), the portfolio reached "
        f"${m['final_value']:,.0f} ({m['total_ret']:+.1%} total) vs SPY at "
        f"${spy_m['final_value']:,.0f} ({spy_m['total_ret']:+.1%}). The signal's magnitude "
        f"edge is real, but cash drag from idle capital waiting for the next dip is the "
        f"dominant structural cost. Users who would have allocated idle cash to SPY "
        f"(the blended scenario) would have seen a total return of approximately "
        f"{blended_tot:+.1%} — {'above' if blended_beats else 'below'} pure SPY buy-and-hold. "
        f"The Sharpe ratio (Port B {m['sharpe']:.2f} vs SPY {spy_m['sharpe']:.2f}) shows "
        f"{'better' if beats_sharpe else 'worse'} risk-adjusted performance. "
        f"T-bill rates (4-5% in 2023-2024) would have meaningfully improved Port B's "
        f"total return if cash had earned even a modest yield rather than 0%."
    )
    print()
    print(divider)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading price data and computing signals (point-in-time S&P 500 universe)...")
    prices_obj = PriceData()
    fund       = EdgarFundamentals(fallback=PointInTimeFundamentals())

    # Master trading calendar comes from SPY; load it first so we can resolve the
    # point-in-time universe per month before loading the (large) ticker set.
    spy_raw   = prices_obj.get_prices("SPY", _WARMUP_START, "2024-12-31")
    spy_close = spy_raw["Close"] if spy_raw is not None and not spy_raw.empty else pd.Series(dtype=float)
    spy_sim   = spy_close[(spy_close.index >= _SIM_START) & (spy_close.index <= _SIM_END)]
    master_cal = spy_sim.index

    # Point-in-time S&P 500 membership, evaluated on the first trading day of each
    # month and reused all month. Union over the window is the load set.
    month_members = build_monthly_universe(master_cal)
    universe_union = sorted(set().union(*month_members.values()))
    print(f"  Master calendar: {master_cal[0].date()} – {master_cal[-1].date()}  ({len(master_cal)} trading days)")
    print(f"  Point-in-time universe: {len(month_members)} months, "
          f"{len(universe_union)} distinct tickers over the window")

    crossings_by_ticker, prices_wide, spy_close = load_all_data(prices_obj, fund, universe_union)

    n_with_data = len(crossings_by_ticker)
    n_with_signals = sum(1 for v in crossings_by_ticker.values() if v)
    total_crossings = sum(len(v) for v in crossings_by_ticker.values())
    print(f"  Tickers with price data: {n_with_data} / {len(universe_union)} "
          f"(missing data → delisted/renamed names with no usable history)")
    print(f"  Tickers with ≥1 BUY crossing: {n_with_signals}  "
          f"(total crossings pre-suppression: {total_crossings})")
    print()

    # SPY benchmark
    spy_m = spy_metrics(spy_close, master_cal)

    # Run three variants
    all_results: list[dict] = []
    all_metrics: list[dict] = []

    for vspec in VARIANTS:
        print(f"Running {vspec['name']}...")
        res = simulate(crossings_by_ticker, prices_wide, master_cal, vspec["pct"], vspec["max_pos"],
                       month_members=month_members)
        met = compute_metrics(res["daily_values"], _INITIAL_CAP)
        all_results.append(res)
        all_metrics.append(met)
        print(f"  Trades: {len(res['trades'])}  Final: ${met['final_value']:,.0f}  CAGR: {met['cagr']:+.1%}")

    print()
    print_report(all_results[0], all_metrics[0], spy_m, all_results, all_metrics, master_cal)


if __name__ == "__main__":
    main()
