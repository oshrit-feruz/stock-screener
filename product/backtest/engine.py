"""Backtest engine: simulate the recovery detector signal with custom parameters.

Causal — no look-ahead bias. Price signals computed from rolling history only.
Quality gate uses annual snapshots keyed to the year of the entry date.

Signal parameters (weights, BUY_THRESHOLD) are frozen — only entry/exit
thresholds and portfolio construction rules are configurable here.
"""
from __future__ import annotations

import warnings
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.fundamentals import PointInTimeFundamentals
from core.data.prices import PriceData
from core.signals.recovery_score import compute_recovery_signals, passes_quality_gate

_WARMUP_START    = "2016-01-01"
_INITIAL_CAPITAL = 100_000.0
_MIN_SIGNALS     = 5          # below this, results are not meaningful


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (isinstance(f, float) and np.isnan(f)) else f
    except Exception:
        return None


def run_backtest(params: dict) -> dict:
    """Run a full backtest simulation with the given parameters.

    Returns a dict with keys: params, summary, trades, yearly, spy_comparison.
    On validation failure returns {"error": "..."}.
    """
    entry_threshold = float(params.get("entry_threshold", 0.80))
    exit_threshold  = float(params.get("exit_threshold", 0.40))
    exit_mode       = params.get("exit_mode", "252d_only")
    pos_size_pct    = float(params.get("position_size_pct", 10.0)) / 100.0
    max_positions   = int(params.get("max_positions", 10))
    start_date      = date.fromisoformat(params.get("start_date", "2018-01-01"))
    end_date        = date.fromisoformat(params.get("end_date", "2026-06-12"))

    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    # ── 1. Pre-compute signals for all tickers ────────────────────────────────
    scored_data: dict[str, pd.DataFrame] = {}
    for ticker in VALIDATION_UNIVERSE:
        try:
            ohlcv = prices.get_prices(ticker, _WARMUP_START, end_date.isoformat())
            if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                scored = compute_recovery_signals(ohlcv)
            scored_data[ticker] = scored
        except Exception:
            continue

    if not scored_data:
        return {"error": "No price data available"}

    # ── 2. Build trading calendar from SPY ─────────────────────────────────────
    spy_ohlcv = prices.get_prices("SPY", _WARMUP_START, end_date.isoformat())
    if spy_ohlcv is not None and not spy_ohlcv.empty:
        trading_dates = [
            d.date() for d in spy_ohlcv.index
            if start_date <= d.date() <= end_date
        ]
    else:
        first_scored = next(iter(scored_data.values()))
        trading_dates = [
            d.date() for d in first_scored.index
            if start_date <= d.date() <= end_date
        ]

    if not trading_dates:
        return {"error": "No trading dates found in the specified range"}

    # ── 3. Pre-fetch quality gates by (ticker, year) ──────────────────────────
    start_year = start_date.year
    end_year   = end_date.year

    quality_cache: dict[tuple, bool | None] = {}
    for ticker in scored_data:
        for year in range(start_year, end_year + 1):
            try:
                snap = fundamentals.get_snapshot(ticker, date(year, 12, 31))
                quality_cache[(ticker, year)] = passes_quality_gate(snap)
            except Exception:
                quality_cache[(ticker, year)] = None

    # ── 4. Walk through time ──────────────────────────────────────────────────
    cash      = _INITIAL_CAPITAL
    positions: dict[str, dict] = {}   # ticker → {entry_date, entry_price, shares, position_value}
    trades:    list[dict]       = []
    n_signals       = 0
    n_days_invested = 0

    portfolio_daily: dict[date, float] = {}
    daily_pv: list[float] = []
    prev_buy_set: set[str] = set()    # track new BUY transitions

    def _cur_price(tkr: str, ts: pd.Timestamp) -> Optional[float]:
        sc = scored_data.get(tkr)
        if sc is None:
            return None
        mask = sc.index <= ts
        if not mask.any():
            return None
        row = sc.loc[mask].iloc[-1]
        # Accept price only if within 5 calendar days of today (handles thin trading)
        if (ts.date() - row.name.date()).days > 5:
            return None
        return _safe_float(row["Close"])

    for today in trading_dates:
        today_ts = pd.Timestamp(today)

        # ── Portfolio value at today's close ──────────────────────────────
        pos_value = 0.0
        for tkr, pos in positions.items():
            cp = _cur_price(tkr, today_ts)
            pos_value += (pos["shares"] * cp) if cp else pos["position_value"]
        pv = cash + pos_value
        portfolio_daily[today] = pv
        daily_pv.append(pv)
        if positions:
            n_days_invested += 1

        # ── Check exits ────────────────────────────────────────────────────
        to_close: list[tuple] = []
        for tkr, pos in positions.items():
            sc = scored_data.get(tkr)
            if sc is None:
                continue
            mask = sc.index <= today_ts
            if not mask.any():
                continue
            row  = sc.loc[mask].iloc[-1]
            cp   = _safe_float(row["Close"])
            if cp is None:
                continue
            comp = _safe_float(row.get("composite_score"))
            hold = int(np.busday_count(pos["entry_date"].isoformat(), today.isoformat()))

            reason: Optional[str] = None
            if exit_mode == "252d_only":
                if hold >= 252:
                    reason = "252d"
            elif exit_mode == "threshold_or_252d":
                if hold >= 252:
                    reason = "252d"
                elif comp is not None and comp < exit_threshold:
                    reason = "threshold"
            elif exit_mode == "threshold_only":
                if hold >= 504:
                    reason = "504d_cap"
                elif comp is not None and comp < exit_threshold:
                    reason = "threshold"

            if reason is not None:
                to_close.append((tkr, cp, hold, reason))

        for tkr, cp, hold, reason in to_close:
            pos  = positions.pop(tkr)
            ret  = cp / pos["entry_price"] - 1
            cash += pos["shares"] * cp
            trades.append({
                "ticker":      tkr,
                "entry_date":  pos["entry_date"].isoformat(),
                "entry_price": round(pos["entry_price"], 2),
                "exit_date":   today.isoformat(),
                "exit_price":  round(cp, 2),
                "hold_days":   hold,
                "return_pct":  round(ret * 100, 2),
                "exit_reason": reason,
            })

        # ── Identify today's BUY signals (after exits, to allow same-day reentry)
        today_buy_set:    set[str]   = set()
        today_candidates: list[tuple] = []

        for tkr, sc in scored_data.items():
            if tkr in positions:
                continue
            mask = sc.index <= today_ts
            if not mask.any():
                continue
            row = sc.loc[mask].iloc[-1]
            # Only enter on the exact trading day (causal — no stale carry-over)
            if row.name.date() != today:
                continue
            comp = _safe_float(row.get("composite_score"))
            if comp is None or comp < entry_threshold:
                continue
            gate = quality_cache.get((tkr, today.year))
            if gate is not True:     # fail-closed: None treated as False
                continue
            cp = _safe_float(row["Close"])
            if cp is None or cp <= 0:
                continue
            today_buy_set.add(tkr)
            today_candidates.append((tkr, comp, cp))

        # New signals = first day of BUY episode (transitions from non-BUY to BUY)
        n_signals += len(today_buy_set - prev_buy_set)
        prev_buy_set = today_buy_set

        # ── Open new positions ─────────────────────────────────────────────
        capacity = max_positions - len(positions)
        if capacity > 0 and today_candidates:
            today_candidates.sort(key=lambda x: -x[1])   # highest composite first
            for tkr, comp, cp in today_candidates[:capacity]:
                if len(positions) >= max_positions:
                    break
                alloc = min(pv * pos_size_pct, cash)
                if alloc < 1.0:
                    continue
                shares = alloc / cp
                cash  -= alloc
                positions[tkr] = {
                    "entry_date":     today,
                    "entry_price":    cp,
                    "shares":         shares,
                    "position_value": alloc,
                }

    # ── Force-close any remaining open positions at end_date ─────────────────
    last_date = trading_dates[-1]
    last_ts   = pd.Timestamp(last_date)
    for tkr, pos in list(positions.items()):
        cp = _cur_price(tkr, last_ts) or pos["entry_price"]
        hold = int(np.busday_count(pos["entry_date"].isoformat(), last_date.isoformat()))
        ret  = cp / pos["entry_price"] - 1
        cash += pos["shares"] * cp
        trades.append({
            "ticker":      tkr,
            "entry_date":  pos["entry_date"].isoformat(),
            "entry_price": round(pos["entry_price"], 2),
            "exit_date":   last_date.isoformat(),
            "exit_price":  round(cp, 2),
            "hold_days":   hold,
            "return_pct":  round(ret * 100, 2),
            "exit_reason": "open_at_end",
        })
    positions.clear()

    # ── 5. Compute summary metrics ─────────────────────────────────────────────
    final_value      = cash
    total_return_pct = (final_value / _INITIAL_CAPITAL - 1) * 100
    years            = max(1.0, (end_date - start_date).days / 365.25)
    cagr             = ((final_value / _INITIAL_CAPITAL) ** (1.0 / years) - 1) * 100

    # Sharpe (annualized, excess return over 0 cash)
    pv_arr = np.array(daily_pv, dtype=float)
    dr     = np.diff(pv_arr) / pv_arr[:-1]
    sharpe = float(dr.mean() / dr.std() * np.sqrt(252)) if len(dr) > 1 and dr.std() > 1e-10 else 0.0

    # Max drawdown
    peaks     = np.maximum.accumulate(pv_arr)
    dd_arr    = (pv_arr - peaks) / peaks
    max_dd    = float(np.min(dd_arr) * 100) if len(dd_arr) > 0 else 0.0

    # Trade statistics
    n_trades      = len(trades)
    rets          = [t["return_pct"] for t in trades]
    mean_ret      = float(np.mean(rets))                                    if rets else 0.0
    pct_pos       = float(sum(1 for r in rets if r > 0) / max(1, n_trades) * 100)
    avg_hold      = float(np.mean([t["hold_days"] for t in trades]))         if trades else 0.0
    pct_thresh    = float(sum(1 for t in trades if t["exit_reason"] == "threshold") / max(1, n_trades) * 100)
    pct_time_inv  = n_days_invested / max(1, len(trading_dates)) * 100

    # SPY metrics
    spy_ret = spy_cagr_val = spy_final = None
    spy_yearly: dict[int, float] = {}

    if spy_ohlcv is not None and not spy_ohlcv.empty:
        spy_range = spy_ohlcv[
            (spy_ohlcv.index >= pd.Timestamp(start_date)) &
            (spy_ohlcv.index <= pd.Timestamp(end_date))
        ]
        if not spy_range.empty:
            s0 = float(spy_range["Close"].iloc[0])
            s1 = float(spy_range["Close"].iloc[-1])
            spy_ret      = (s1 / s0 - 1) * 100
            spy_final    = _INITIAL_CAPITAL * (s1 / s0)
            spy_cagr_val = ((s1 / s0) ** (1.0 / years) - 1) * 100
            for yr in range(start_year, end_year + 1):
                yr_data = spy_ohlcv[spy_ohlcv.index.year == yr]
                if yr_data.empty:
                    continue
                spy_yearly[yr] = round(
                    (float(yr_data["Close"].iloc[-1]) / float(yr_data["Close"].iloc[0]) - 1) * 100, 1
                )

    # Yearly portfolio returns
    port_yearly: dict[int, float] = {}
    for yr in range(start_year, end_year + 1):
        yr_days = [d for d in portfolio_daily if d.year == yr]
        if len(yr_days) < 2:
            continue
        y0 = portfolio_daily[min(yr_days)]
        y1 = portfolio_daily[max(yr_days)]
        port_yearly[yr] = round((y1 / y0 - 1) * 100, 1)

    all_years = sorted(set(list(port_yearly) + list(spy_yearly)))
    yearly = [
        {
            "year":             yr,
            "portfolio_return": port_yearly.get(yr),
            "spy_return":       spy_yearly.get(yr),
        }
        for yr in all_years
    ]

    best_year = worst_year = None
    if port_yearly:
        best_yr   = max(port_yearly, key=port_yearly.get)
        worst_yr  = min(port_yearly, key=port_yearly.get)
        best_year  = {"year": best_yr,  "return_pct": port_yearly[best_yr]}
        worst_year = {"year": worst_yr, "return_pct": port_yearly[worst_yr]}

    # Warn if too few signals
    if n_trades < _MIN_SIGNALS:
        return {
            "error":   f"Too few signals to draw conclusions ({n_trades} trades < {_MIN_SIGNALS} required). "
                       "Try a lower entry threshold.",
            "n_trades": n_trades,
            "params":   params,
        }

    return {
        "params": params,
        "summary": {
            "n_signals":              n_signals,
            "n_trades":               n_trades,
            "avg_hold_days":          round(avg_hold, 1),
            "pct_exit_via_threshold": round(pct_thresh, 1),
            "mean_return_pct":        round(mean_ret, 2),
            "pct_positive":           round(pct_pos, 1),
            "final_portfolio":        int(round(final_value)),
            "total_return_pct":       round(total_return_pct, 1),
            "cagr":                   round(cagr, 1),
            "spy_total_return_pct":   round(spy_ret, 1)      if spy_ret      is not None else None,
            "spy_cagr":               round(spy_cagr_val, 1) if spy_cagr_val is not None else None,
            "beat_spy":               bool(final_value > spy_final) if spy_final is not None else None,
            "sharpe":                 round(sharpe, 2),
            "max_drawdown_pct":       round(max_dd, 1),
            "best_year":              best_year,
            "worst_year":             worst_year,
            "pct_time_invested":      round(pct_time_inv, 1),
        },
        "trades":   sorted(trades, key=lambda t: t["entry_date"]),
        "yearly":   yearly,
        "spy_comparison": {
            "final_spy":       int(round(spy_final))               if spy_final is not None else None,
            "final_portfolio": int(round(final_value)),
            "difference":      int(round(final_value - spy_final)) if spy_final is not None else None,
        },
    }
