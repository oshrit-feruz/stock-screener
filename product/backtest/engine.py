"""Backtest engine: simulate the recovery detector signal with custom parameters.

Causal — no look-ahead bias. Price signals computed from rolling history only.
Quality gate is evaluated point-in-time as of each position's actual entry date
(fundamentals.get_snapshot applies the publication lag), not the year-end.

Signal parameters (weights, BUY_THRESHOLD) are frozen — only entry/exit
thresholds and portfolio construction rules are configurable here.
"""
from __future__ import annotations

import logging
import warnings
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from config.tickers import VALIDATION_UNIVERSE
from core.data.edgar import EdgarFundamentals
from core.data.eodhd_fundamentals import EODHDFundamentals
from core.data.prices import PriceData
from core.signals.recovery_score import compute_recovery_signals, passes_quality_gate
from data.sp500_universe import get_universe, get_universe_top_n, prefetch_pit_market_caps
from scripts.run_combined_validation import load_fedfunds

logger = logging.getLogger(__name__)

_WARMUP_START    = "2016-01-01"
_INITIAL_CAPITAL = 100_000.0
_MIN_SIGNALS     = 5          # below this, results are not meaningful
_UNIVERSE_N      = 100        # point-in-time Top-N by market cap, rebuilt monthly


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (isinstance(f, float) and np.isnan(f)) else f
    except Exception:
        return None


def _downcast(df: pd.DataFrame) -> pd.DataFrame:
    """Downcast float64->float32 and int64->int32 in place (returns df for chaining).

    Retained OHLCV + signal DataFrames are the dominant memory cost when the
    universe/date-range is large (each column is 2x float32 vs float64). Prices
    are $0.01-$100k range and scores are 0-1 — float32's ~7 significant digits
    is far more precision than the signal math or position sizing needs, so this
    only affects memory, not results (verified against the float64 baseline).
    Volume fits int32 (max ~2.1B; no single-day US equity volume gets close).
    """
    for col in df.columns:
        if df[col].dtype == np.float64:
            df[col] = df[col].astype(np.float32)
        elif df[col].dtype == np.int64:
            df[col] = df[col].astype(np.int32)
    return df


def _load_backtest_data(end_date: date, quality_start_year: int, quality_end_year: int,
                        start_date: Optional[date] = None) -> dict:
    """Pre-load OHLCV, compute signals, and pre-fetch quality gates for all tickers.

    Expensive step done once for batch runs — amortizes across all simulations.
    """
    prices       = PriceData()
    # EDGAR primary; EODHD (requests, not yfinance) as the fallback so the quality
    # gate never hangs on yfinance's TLS failure on Render. See eodhd_fundamentals.
    fundamentals = EdgarFundamentals(fallback=EODHDFundamentals())

    # Fetch start: at least 365 days before backtest start for warmup, but never after end_date
    if start_date is not None:
        warmup_start = min(date.fromisoformat(_WARMUP_START), start_date - timedelta(days=365))
    else:
        warmup_start = date.fromisoformat(_WARMUP_START)
    fetch_start = warmup_start.isoformat()

    # SPY first — its trading calendar defines the first-of-month rebuild dates.
    spy_ohlcv = prices.get_prices("SPY", fetch_start, end_date.isoformat())
    if spy_ohlcv is not None and not spy_ohlcv.empty:
        spy_ohlcv = _downcast(spy_ohlcv)

    # ── Point-in-time Top-100 universe, rebuilt on the first trading day of each
    #    month and reused all month — consistent with the research harness
    #    (research/run_combined_clean_universe.py). Tickers to preload = the union
    #    of every month's membership over the backtest window. ──────────────────
    sim_start = start_date if start_date is not None else warmup_start
    if spy_ohlcv is not None and not spy_ohlcv.empty:
        cal = [d for d in spy_ohlcv.index if sim_start <= d.date() <= end_date]
    else:
        cal = []

    fmonths: dict[tuple, pd.Timestamp] = {}
    for ts in cal:
        fmonths.setdefault((ts.year, ts.month), ts)

    month_members: dict[tuple, set] = {}
    if fmonths:
        fdates = [ts.date().isoformat() for ts in fmonths.values()]
        # Prefetch point-in-time market caps for the full membership pool once,
        # so the per-month Top-N ranking is cheap (reuses sp500_universe helpers).
        try:
            union_full = sorted({t for d in fdates for t in get_universe(d)})
            prefetch_pit_market_caps(union_full, fdates)
        except Exception:
            pass
        for key, ts in fmonths.items():
            try:
                month_members[key] = set(get_universe_top_n(ts.date().isoformat(), _UNIVERSE_N))
            except Exception:
                month_members[key] = set()

    universe = sorted(set().union(*month_members.values())) if month_members else list(VALIDATION_UNIVERSE)

    # Cold-cache guard: the point-in-time Top-N ranking needs the raw-price cache
    # (data/cache/prices_raw) to compute market caps. That cache is gitignored, so
    # a fresh deploy (e.g. Render) has no raw prices → get_universe_top_n returns
    # [] for every month → month_members is a truthy dict of EMPTY sets → the union
    # above is empty and the `else VALIDATION_UNIVERSE` branch never fires. Fall
    # back to the fixed universe and clear month_members so _simulate does an
    # ungated full-universe scan (an empty set would otherwise gate out everything).
    n_members = sum(1 for s in month_members.values() if s)
    if not universe:
        logger.warning(
            "Backtest universe empty (PIT Top-%d ranking produced 0 members over "
            "%d months — cold market-cap cache?); falling back to VALIDATION_UNIVERSE "
            "(%d tickers), ungated.",
            _UNIVERSE_N, len(month_members), len(VALIDATION_UNIVERSE),
        )
        universe = list(VALIDATION_UNIVERSE)
        month_members = {}
    # WARNING (not INFO) so it surfaces under uvicorn's default log config on
    # Render: 167 => true Top-100 loaded; 50 => cold-cache fallback.
    logger.warning(
        "Backtest universe: %d tickers (%d months with non-empty PIT membership).",
        len(universe), n_members,
    )

    scored_data: dict[str, pd.DataFrame] = {}
    for ticker in universe:
        try:
            ohlcv = prices.get_prices(ticker, fetch_start, end_date.isoformat())
            if ohlcv is None or ohlcv.empty or len(ohlcv) < 252:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                scored = compute_recovery_signals(ohlcv)
            # Downcast AFTER signal computation (compute_recovery_signals is
            # shared with the screener/research paths — left untouched — so the
            # float32 memory optimization stays local to what the backtest
            # retains for the whole run).
            scored_data[ticker] = _downcast(scored)
        except Exception:
            continue
    logger.warning("Backtest data loaded: %d/%d tickers scored; entering simulation.",
                   len(scored_data), len(universe))

    # ── Idle-cash yield: real historical Fed Funds Rate (FRED FEDFUNDS). Reuses
    #    load_fedfunds from the research money-market study — not reimplemented. ─
    try:
        fedfunds = load_fedfunds()
    except Exception:
        fedfunds = None

    return {
        "scored_data":   scored_data,
        "spy_ohlcv":     spy_ohlcv,
        "fundamentals":  fundamentals,
        "month_members": month_members,
        "fedfunds":      fedfunds,
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
    fundamentals  = preloaded["fundamentals"]
    month_members = preloaded.get("month_members") or {}
    fedfunds      = preloaded.get("fedfunds")

    # Quality gate evaluated point-in-time as of the actual entry date.
    # get_snapshot applies the publication lag internally, so only fundamentals
    # already public on `entry_date` are used (no look-ahead). Memoized per
    # (ticker, date) since each candidate is re-checked only on its entry day.
    _gate_memo: dict[tuple, bool | None] = {}

    def _gate_at(tkr: str, entry_date: date) -> bool | None:
        key = (tkr, entry_date)
        if key not in _gate_memo:
            try:
                snap = fundamentals.get_snapshot(tkr, entry_date)
                _gate_memo[key] = passes_quality_gate(snap)
            except Exception:
                _gate_memo[key] = None
        return _gate_memo[key]

    if not scored_data:
        return {"error": "No price data found for this date range. The backtest universe covers large-cap US equities with reliable data from 2010 onwards — try a start date of 2010 or later."}

    # ── Per-ticker column arrays for O(log n) point-in-time lookups ─────────────
    # The day loop below repeatedly needs "the last row of a ticker at or before
    # date T" — for portfolio valuation, exit checks, and the entry scan. Done the
    # obvious way (`sc.loc[sc.index <= ts].iloc[-1]`) that materializes a fresh
    # copy of the ticker's ENTIRE history-up-to-T, every ticker, every day: O(days²)
    # per ticker, which turns a 15-year run into 15+ minutes (a 3-year run spends
    # ~42s of 59s inside pandas .loc row-copying). Instead precompute each ticker's
    # index (as int64 ns) plus its Close/composite_score columns as plain numpy
    # once, then find the row with np.searchsorted — O(log n), no per-lookup copy.
    # Values are read back through _safe_float exactly as before, so results are
    # bit-for-bit identical to the row-based path.
    _cols_cache: dict[str, Optional[tuple]] = {}

    def _cols(tkr: str):
        if tkr not in _cols_cache:
            sc = scored_data.get(tkr)
            if sc is None or sc.empty:
                _cols_cache[tkr] = None
            else:
                close = sc["Close"].to_numpy(dtype="float64")
                if "composite_score" in sc.columns:
                    comp = sc["composite_score"].to_numpy(dtype="float64")
                else:
                    comp = np.full(len(sc), np.nan)
                # sc.index is a sorted, unique DatetimeIndex; its own searchsorted
                # is unit-safe (the frames come back as datetime64[us], so a raw
                # np.searchsorted against ts.value in ns would silently mismatch).
                _cols_cache[tkr] = (close, comp, sc.index)
        return _cols_cache[tkr]

    def _pos_at(tkr: str, ts: pd.Timestamp):
        """Integer position of the last row of `tkr` with index <= ts, or
        (None, None) if the ticker is absent or has no such row. Mirrors
        `sc.loc[sc.index <= ts].iloc[-1]` exactly (index is sorted & unique)."""
        cols = _cols(tkr)
        if cols is None:
            return None, None
        pos = int(cols[2].searchsorted(ts, side="right")) - 1
        if pos < 0:
            return None, None
        return pos, cols

    entry_threshold = float(params.get("entry_threshold", 0.60))
    pos_size_pct    = float(params.get("position_size_pct", 10.0)) / 100.0
    max_positions   = int(params.get("max_positions", 10))
    # Entry execution timing. "next_open" (default) fills at day T+1's open — the
    # first realistically executable price after the signal (composite score on
    # day T) is known at T's close. "close" reproduces the legacy same-bar fill
    # (look-ahead) for comparison. The 252-day exit is time-based and unchanged.
    entry_fill      = str(params.get("entry_fill", "next_open"))
    if entry_fill not in ("next_open", "close"):
        return {"error": f"Invalid entry_fill mode '{entry_fill}'. Must be 'next_open' or 'close'."}
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
        return {"error": "No trading dates found in the specified range. Try a start date of 2010 or later."}

    start_year = start_date.year
    end_year   = end_date.year

    # ── Idle-cash yield schedule ────────────────────────────────────────────────
    # Cash not deployed in an open position earns the annualized Fed Funds Rate,
    # accrued pro-rata by calendar days between trading bars — the same
    # (1 + r) ** (days / 365) accrual the research money-market study uses.
    cal_index = pd.DatetimeIndex([pd.Timestamp(d) for d in trading_dates])
    if fedfunds is not None and len(fedfunds) > 0:
        rate_on_cal = fedfunds.reindex(cal_index, method="ffill").values.astype(float)
    else:
        rate_on_cal = np.zeros(len(cal_index))
    day_gap = cal_index.to_series().diff().dt.total_seconds().values / 86400.0
    if len(day_gap) > 0:
        day_gap[0] = 0.0

    # ── Portfolio state ────────────────────────────────────────────────────────
    cash      = _INITIAL_CAPITAL
    positions: dict[str, dict] = {}
    trades:    list[dict]       = []
    missed_capital: list[dict]  = []
    n_signals       = 0
    n_days_invested = 0
    portfolio_daily: dict[date, float] = {}
    daily_pv: list[float] = []
    daily_util: list[float] = []   # fraction of portfolio invested each day
    prev_buy_set: set[str] = set()

    def _cur_price(tkr: str, ts: pd.Timestamp) -> Optional[float]:
        pos, cols = _pos_at(tkr, ts)
        if pos is None:
            return None
        if (ts.date() - cols[2][pos].date()).days > 5:
            return None
        return _safe_float(cols[0][pos])

    for di, today in enumerate(trading_dates):
        today_ts = pd.Timestamp(today)

        # ── Accrue idle-cash yield since the previous bar ──────────────────
        if di > 0:
            r = rate_on_cal[di]
            if not np.isfinite(r):
                r = 0.0
            cash *= (1.0 + r) ** (day_gap[di] / 365.0)

        # ── Portfolio value at today's close ───────────────────────────────
        pos_value = 0.0
        for tkr, pos in positions.items():
            cp = _cur_price(tkr, today_ts)
            pos_value += (pos["shares"] * cp) if cp else pos["position_value"]
        pv = cash + pos_value
        portfolio_daily[today] = pv
        daily_pv.append(pv)
        daily_util.append(pos_value / pv if pv > 0 else 0.0)
        if positions:
            n_days_invested += 1

        # ── Check exits ────────────────────────────────────────────────────
        to_close: list[tuple] = []
        for tkr, pos in positions.items():
            ipos, cols = _pos_at(tkr, today_ts)
            if ipos is None:
                continue
            cp   = _safe_float(cols[0][ipos])
            if cp is None:
                continue
            comp = _safe_float(cols[1][ipos])
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
                "pnl_usd":     round((cp - pos["entry_price"]) * pos["shares"], 2),
                "exit_reason": reason,
            })

        # ── Identify today's BUY signals ───────────────────────────────────
        today_buy_set:    set[str]   = set()
        today_candidates: list[tuple] = []

        members_today = month_members.get((today.year, today.month)) if month_members else None
        for tkr in scored_data:
            if tkr in positions:
                continue
            # Only consider this month's point-in-time Top-100 members (when a
            # membership map is present; empty map → legacy full-universe scan).
            if members_today is not None and tkr not in members_today:
                continue
            pos, cols = _pos_at(tkr, today_ts)
            if pos is None:
                continue
            if cols[2][pos].date() != today:
                continue
            comp = _safe_float(cols[1][pos])
            if comp is None or comp < entry_threshold:
                continue
            gate = _gate_at(tkr, today)
            if gate is not True:
                continue
            cp = _safe_float(cols[0][pos])
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

                # Fill price/date: next bar's open (T+1, executable) or today's
                # close (legacy same-bar). cp is today's close from the candidate.
                if entry_fill == "next_open":
                    sc = scored_data.get(tkr)
                    future = sc.index[sc.index > today_ts] if sc is not None else []
                    if len(future) == 0:
                        continue  # no next bar → not executable without look-ahead
                    fill_ts    = future[0]
                    fill_price = _safe_float(sc.loc[fill_ts, "Open"])
                    if fill_price is None or fill_price <= 0:
                        continue
                    fill_date = fill_ts.date()
                else:
                    fill_price, fill_date = cp, today

                desired_alloc = pv * pos_size_pct
                if cash < desired_alloc:
                    missed_capital.append({
                        "ticker":     tkr,
                        "date":       today.isoformat(),
                        "composite":  round(comp, 3),
                        "cash_avail": round(cash, 2),
                        "needed":     round(desired_alloc, 2),
                    })
                    continue
                shares = desired_alloc / fill_price
                cash  -= desired_alloc
                positions[tkr] = {
                    "entry_date":     fill_date,
                    "entry_price":    fill_price,
                    "peak_price":     fill_price,
                    "shares":         shares,
                    "position_value": desired_alloc,
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
            "pnl_usd":     round((cp - pos["entry_price"]) * pos["shares"], 2),
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
    pct_time_inv     = n_days_invested / max(1, len(trading_dates)) * 100
    avg_capital_util = float(np.mean(daily_util) * 100) if daily_util else 0.0
    n_missed_capital = len(missed_capital)

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
            "avg_capital_utilization": round(avg_capital_util, 1),
            "n_missed_capital":       n_missed_capital,
        },
        "trades":          sorted(trades, key=lambda t: t["entry_date"]),
        "missed_capital":  sorted(missed_capital, key=lambda m: m["date"]),
        "yearly":          yearly,
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

    end_date_str   = params_list[0].get("end_date", "2024-12-31")
    start_date_str = params_list[0].get("start_date", "2018-01-01")
    end_date   = date.fromisoformat(end_date_str)
    start_date = date.fromisoformat(start_date_str)

    if end_date <= start_date:
        return [{"error": "end_date must be after start_date"} for _ in params_list]

    start_year = start_date.year
    end_year   = end_date.year

    preloaded = _load_backtest_data(end_date, start_year, end_year, start_date)
    return [_simulate(preloaded, p) for p in params_list]


def run_backtest(params: dict) -> dict:
    """Run a single backtest simulation (backward-compatible entry point)."""
    end_date_str   = params.get("end_date", "2026-06-12")
    start_date_str = params.get("start_date", "2018-01-01")
    end_date   = date.fromisoformat(end_date_str)
    start_date = date.fromisoformat(start_date_str)

    if end_date <= start_date:
        return {"error": "end_date must be after start_date"}

    start_year = start_date.year
    end_year   = end_date.year

    preloaded = _load_backtest_data(end_date, start_year, end_year, start_date)
    return _simulate(preloaded, params)
