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


def _load_backtest_data(end_date: date, quality_start_year: int, quality_end_year: int) -> dict:
    """Pre-load OHLCV, compute signals, and pre-fetch quality gates for all tickers.

    Expensive step done once for batch runs — amortizes across all simulations.
    """
    prices       = PriceData()
    fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

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

    spy_ohlcv = prices.get_prices("SPY", _WARMUP_START, end_date.isoformat())

    quality_cache: dict[tuple, bool | None] = {}
    for ticker in scored_data:
        for year in range(quality_start_year, quality_end_year + 1):
            try:
                snap = fundamentals.get_snapshot(ticker, date(year, 12, 31))
                quality_cache[(ticker, year)] = passes_quality_gate(snap)
            except Exception:
                quality_cache[(ticker, year)] = None

    return {
        "scored_data":   scored_data,
        "spy_ohlcv":     spy_ohlcv,
        "quality_cache": quality_cache,
    }


def _simulate(preloaded: dict, params: dict) -> dict:
    """Run portfolio simulation with pre-loaded data.

    Supports two exit modes:
      hold_days + exit_rule  — for parameter optimization grid
      exit_mode + exit_threshold — for legacy UI simulator (backward compat)

    exit_rule "A": hold for hold_days trading days, no stop-loss.
    exit_rule "B": hold for hold_days trading days; emergency exit at -40% from entry.
    """
    scored_data   = preloaded["scored_data"]
    spy_ohlcv     = preloaded["spy_ohlcv"]
    quality_cache = preloaded["quality_cache"]

    if not scored_data:
        return {"error": "No price data available"}

    entry_threshold = float(params.get("entry_threshold", 0.60))
    pos_size_pct    = float(params.get("position_size_pct", 10.0)) / 100.0
    max_positions   = int(params.get("max_positions", 10))
    start_date = (
        date.fromisoformat(params["start_date"])
        if isinstance(params.get("start_date"), str)
        else params["start_date"]
    )
    end_date = (
        date.fromisoformat(params["end_date"])
        if isinstance(params.get("end_date"), str)
        else params["end_date"]
    )

    # Optimization mode overrides when hold_days is explicitly set
    hold_days_param: Optional[int] = params.get("hold_days")
    exit_rule:        str           = params.get("exit_rule", "A")

    # Legacy UI mode params (only used when hold_days_param is None)
    exit_mode:      str   = params.get("exit_mode", "252d_only")
    exit_threshold: float = float(params.get("exit_threshold", 0.40))

    # Take profit — 0 means disabled; value is a fraction (e.g. 0.30 = +30%)
    tp_raw = params.get("take_profit_pct", 0.0)
    take_profit_pct: float = float(tp_raw) / 100.0 if float(tp_raw or 0) > 1 else float(tp_raw or 0)

    # Stop loss — 0 means disabled; value is a fraction (e.g. 0.20 = exit at -20%)
    sl_raw = params.get("stop_loss_pct", 0.0)
    stop_loss_pct: float = float(sl_raw) / 100.0 if float(sl_raw or 0) > 1 else float(sl_raw or 0)

    # Trailing stop — 0 means disabled; exits when price drops X% from peak since entry
    ts_raw = params.get("trailing_stop_pct", 0.0)
    trailing_stop_pct: float = float(ts_raw) / 100.0 if float(ts_raw or 0) > 1 else float(ts_raw or 0)

    # ── Trading calendar ───────────────────────────────────────────────────────
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

    start_year = start_date.year
    end_year   = end_date.year

    # ── Portfolio state ────────────────────────────────────────────────────────
    cash      = _INITIAL_CAPITAL
    positions: dict[str, dict] = {}
    trades:    list[dict]       = []
    n_signals       = 0
    n_days_invested = 0
    portfolio_daily: dict[date, float] = {}
    daily_pv: list[float] = []
    prev_buy_set: set[str] = set()

    def _cur_price(tkr: str, ts: pd.Timestamp) -> Optional[float]:
        sc = scored_data.get(tkr)
        if sc is None:
            return None
        mask = sc.index <= ts
        if not mask.any():
            return None
        row = sc.loc[mask].iloc[-1]
        if (ts.date() - row.name.date()).days > 5:
            return None
        return _safe_float(row["Close"])

    for today in trading_dates:
        today_ts = pd.Timestamp(today)

        # ── Portfolio value at today's close ───────────────────────────────
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

            # Update peak price (high-water mark since entry)
            if cp > pos.get("peak_price", pos["entry_price"]):
                pos["peak_price"] = cp

            reason: Optional[str] = None

            gain        = cp / pos["entry_price"] - 1
            peak        = pos.get("peak_price", pos["entry_price"])
            from_peak   = cp / peak - 1  # always ≤ 0 on the day it triggers

            # TP, SL, and trailing stop fire before any other exit rule
            if take_profit_pct > 0 and gain >= take_profit_pct:
                reason = "take_profit"
            elif stop_loss_pct > 0 and gain <= -stop_loss_pct:
                reason = "stop_loss"
            elif trailing_stop_pct > 0 and from_peak <= -trailing_stop_pct:
                reason = "trailing_stop"
            elif hold_days_param is not None:
                # Optimization mode
                if hold >= hold_days_param:
                    reason = f"{hold_days_param}d"
                elif exit_rule == "B" and gain <= -0.40:
                    reason = "stop_loss"
            else:
                # Legacy UI mode
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

        # ── Identify today's BUY signals ───────────────────────────────────
        today_buy_set:    set[str]   = set()
        today_candidates: list[tuple] = []

        for tkr, sc in scored_data.items():
            if tkr in positions:
                continue
            mask = sc.index <= today_ts
            if not mask.any():
                continue
            row = sc.loc[mask].iloc[-1]
            if row.name.date() != today:
                continue
            comp = _safe_float(row.get("composite_score"))
            if comp is None or comp < entry_threshold:
                continue
            gate = quality_cache.get((tkr, today.year))
            if gate is not True:
                continue
            cp = _safe_float(row["Close"])
            if cp is None or cp <= 0:
                continue
            today_buy_set.add(tkr)
            today_candidates.append((tkr, comp, cp))

        n_signals += len(today_buy_set - prev_buy_set)
        prev_buy_set = today_buy_set

        # ── Open new positions ─────────────────────────────────────────────
        capacity = max_positions - len(positions)
        if capacity > 0 and today_candidates:
            today_candidates.sort(key=lambda x: -x[1])
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
                    "peak_price":     cp,
                    "shares":         shares,
                    "position_value": alloc,
                }

    # ── Realized-only value: open positions returned at cost (no unrealized P&L) ─
    final_value_realized = cash + sum(pos["shares"] * pos["entry_price"] for pos in positions.values())

    # ── Force-close remaining open positions at end_date ──────────────────────
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

    # ── Summary metrics ────────────────────────────────────────────────────────
    final_value      = cash
    total_return_pct = (final_value / _INITIAL_CAPITAL - 1) * 100
    years            = max(1.0, (end_date - start_date).days / 365.25)
    cagr             = ((final_value / _INITIAL_CAPITAL) ** (1.0 / years) - 1) * 100

    total_return_realized_pct = (final_value_realized / _INITIAL_CAPITAL - 1) * 100
    cagr_realized             = ((final_value_realized / _INITIAL_CAPITAL) ** (1.0 / years) - 1) * 100

    pv_arr = np.array(daily_pv, dtype=float)
    dr     = np.diff(pv_arr) / pv_arr[:-1]
    sharpe = float(dr.mean() / dr.std() * np.sqrt(252)) if len(dr) > 1 and dr.std() > 1e-10 else 0.0

    peaks  = np.maximum.accumulate(pv_arr)
    dd_arr = (pv_arr - peaks) / peaks
    max_dd = float(np.min(dd_arr) * 100) if len(dd_arr) > 0 else 0.0

    n_trades     = len(trades)
    rets         = [t["return_pct"] for t in trades]
    mean_ret     = float(np.mean(rets))                                   if rets   else 0.0
    pct_pos      = float(sum(1 for r in rets if r > 0) / max(1, n_trades) * 100)
    avg_hold     = float(np.mean([t["hold_days"] for t in trades]))        if trades else 0.0
    pct_thresh         = float(sum(1 for t in trades if t["exit_reason"] == "threshold")
                               / max(1, n_trades) * 100)
    pct_stop_loss      = float(sum(1 for t in trades if t["exit_reason"] == "stop_loss")
                               / max(1, n_trades) * 100)
    pct_trailing_stop  = float(sum(1 for t in trades if t["exit_reason"] == "trailing_stop")
                               / max(1, n_trades) * 100)
    pct_take_profit    = float(sum(1 for t in trades if t["exit_reason"] == "take_profit")
                               / max(1, n_trades) * 100)
    pct_time_inv = n_days_invested / max(1, len(trading_dates)) * 100

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
        best_yr  = max(port_yearly, key=port_yearly.get)
        worst_yr = min(port_yearly, key=port_yearly.get)
        best_year  = {"year": best_yr,  "return_pct": port_yearly[best_yr]}
        worst_year = {"year": worst_yr, "return_pct": port_yearly[worst_yr]}

    if n_trades < _MIN_SIGNALS:
        return {
            "error":    f"Too few signals to draw conclusions ({n_trades} trades < {_MIN_SIGNALS} required). "
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
            "pct_stop_loss":          round(pct_stop_loss, 1),
            "pct_trailing_stop":      round(pct_trailing_stop, 1),
            "pct_take_profit":        round(pct_take_profit, 1),
            "mean_return_pct":        round(mean_ret, 2),
            "pct_positive":           round(pct_pos, 1),
            "final_portfolio":              int(round(final_value)),
            "final_portfolio_realized":    int(round(final_value_realized)),
            "total_return_pct":            round(total_return_pct, 1),
            "total_return_realized_pct":   round(total_return_realized_pct, 1),
            "cagr":                        round(cagr, 1),
            "cagr_realized":              round(cagr_realized, 1),
            "spy_total_return_pct":        round(spy_ret, 1)      if spy_ret      is not None else None,
            "spy_cagr":                    round(spy_cagr_val, 1) if spy_cagr_val is not None else None,
            "beat_spy":                    bool(final_value > spy_final)          if spy_final is not None else None,
            "beat_spy_realized":           bool(final_value_realized > spy_final) if spy_final is not None else None,
            "sharpe":                 round(sharpe, 2),
            "max_drawdown_pct":       round(max_dd, 1),
            "best_year":              best_year,
            "worst_year":             worst_year,
            "pct_time_invested":      round(pct_time_inv, 1),
        },
        "trades":   sorted(trades, key=lambda t: t["entry_date"]),
        "yearly":   yearly,
        "spy_comparison": {
            "final_spy":                  int(round(spy_final))                          if spy_final is not None else None,
            "final_portfolio":            int(round(final_value)),
            "final_portfolio_realized":   int(round(final_value_realized)),
            "difference":                 int(round(final_value - spy_final))            if spy_final is not None else None,
            "difference_realized":        int(round(final_value_realized - spy_final))   if spy_final is not None else None,
        },
    }


def run_backtest_batch(params_list: list) -> list:
    """Run multiple simulations sharing one data load pass.

    All params must use the same start_date / end_date range.
    """
    if not params_list:
        return []

    end_date_str = params_list[0].get("end_date", "2024-12-31")
    end_date     = date.fromisoformat(end_date_str)
    start_year   = date.fromisoformat(params_list[0].get("start_date", "2018-01-01")).year
    end_year     = end_date.year

    preloaded = _load_backtest_data(end_date, start_year, end_year)
    return [_simulate(preloaded, p) for p in params_list]


def run_backtest(params: dict) -> dict:
    """Run a single backtest simulation (backward-compatible entry point)."""
    end_date_str = params.get("end_date", "2026-06-12")
    end_date     = date.fromisoformat(end_date_str)
    start_year   = date.fromisoformat(params.get("start_date", "2018-01-01")).year
    end_year     = end_date.year

    preloaded = _load_backtest_data(end_date, start_year, end_year)
    return _simulate(preloaded, params)
