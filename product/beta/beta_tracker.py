"""Beta tracking — observation-only performance tracking of OPENED positions.

Focused on positions that were actually opened (data/positions/*.json), NOT on
every signal. For each open and closed position it computes:

  * current (unrealized) or final (realized) return,
  * what SPY did over the same period (entry → today / entry → exit),
  * what a money-market sleeve (historical Fed Funds Rate, the same
    (1 + r) ** (days / 365) accrual used elsewhere in the codebase) would have
    returned over the same period.

This module is the SINGLE source of truth consumed by both outputs:
  * the accumulating markdown report (data/beta_tracking/beta_log.md), refreshed
    on every run_daily.py run, and
  * the /api/beta/dashboard endpoint (same data as JSON).

It is strictly observation-only: it reads the persisted position files and
market data; it never opens, closes, sizes, or otherwise touches trading logic.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.data.prices import PriceData  # noqa: E402
from scripts.run_combined_validation import load_fedfunds  # noqa: E402

logger = logging.getLogger(__name__)

_ROOT        = Path(__file__).parent.parent.parent
_OPEN_FILE   = _ROOT / "data" / "positions" / "open_positions.json"
_CLOSED_FILE = _ROOT / "data" / "positions" / "closed_positions.json"
_REPORT_DIR  = _ROOT / "data" / "beta_tracking"
_REPORT_FILE = _REPORT_DIR / "beta_log.md"

_HOLD_TARGET = 252   # frozen 252-day hold, for the "days (of 252)" display only


# ── data loading ────────────────────────────────────────────────────────────

def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("beta: could not read %s: %s", path, exc)
        return []


# ── market-data helpers (all fail soft → None, never raise) ─────────────────

def _latest_close(ticker: str, as_of: date, prices: PriceData) -> Optional[float]:
    """Most recent close on or before `as_of` (mirrors the API's _current_price)."""
    try:
        ohlcv = prices.get_prices(ticker, (as_of - timedelta(days=10)).isoformat(),
                                  as_of.isoformat())
        if ohlcv is not None and not ohlcv.empty:
            return float(ohlcv["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _spy_return(entry: date, end: date, prices: PriceData) -> Optional[float]:
    """SPY total price return from the first close on/after `entry` to the last
    close on/before `end`. Same-day (or reversed) window → 0.0."""
    if end <= entry:
        return 0.0
    try:
        ohlcv = prices.get_prices("SPY", entry.isoformat(), end.isoformat())
        if ohlcv is None or ohlcv.empty:
            return None
        close = ohlcv["Close"].dropna()
        if len(close) < 2:
            return None
        return float(close.iloc[-1] / close.iloc[0] - 1.0)
    except Exception:
        return None


def _mm_return(entry: date, end: date, fedfunds: Optional[pd.Series]) -> Optional[float]:
    """Money-market return over [entry, end]: idle cash accruing the historical
    Fed Funds Rate pro-rata per calendar day, (1 + r) ** (days / 365) — the same
    accrual the backtest/research money-market sleeve uses. Same-day → 0.0."""
    if end <= entry:
        return 0.0
    if fedfunds is None or len(fedfunds) == 0:
        return None
    days = pd.date_range(entry, end, freq="D")[1:]   # accrue each calendar day after entry
    if len(days) == 0:
        return 0.0
    rates = fedfunds.reindex(days, method="ffill").values.astype(float)
    rates = np.where(np.isfinite(rates), rates, 0.0)
    # prod((1 + r) ** (1/365)) = exp(sum(ln(1 + r)) / 365)
    return float(np.exp(np.log1p(rates).sum() / 365.0) - 1.0)


def _pct(x: Optional[float]) -> Optional[float]:
    return round(x * 100, 2) if x is not None else None


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round((a - b) * 100, 2) if (a is not None and b is not None) else None


# ── per-position builders ───────────────────────────────────────────────────

def _build_open(p: dict, as_of: date, prices: PriceData,
                fedfunds: Optional[pd.Series]) -> dict:
    ticker      = p["ticker"]
    entry       = date.fromisoformat(p["entry_date"])
    entry_price = float(p["entry_price"])
    cur         = _latest_close(ticker, as_of, prices)
    ret         = (cur / entry_price - 1.0) if (cur and entry_price) else None
    days_held   = int(np.busday_count(entry.isoformat(), as_of.isoformat()))
    spy         = _spy_return(entry, as_of, prices)
    mm          = _mm_return(entry, as_of, fedfunds)
    return {
        "ticker":         ticker,
        "status":         "open",
        "entry_date":     entry.isoformat(),
        "entry_price":    round(entry_price, 2),
        "allocation":     p.get("allocation"),   # not stored today → usually None
        "as_of_date":     as_of.isoformat(),
        "current_price":  round(cur, 2) if cur is not None else None,
        "return_pct":     _pct(ret),
        "days_held":      days_held,
        "hold_target":    _HOLD_TARGET,
        "days_remaining": max(0, _HOLD_TARGET - days_held),
        "spy_return_pct": _pct(spy),
        "mm_return_pct":  _pct(mm),
        "vs_spy_pct":     _diff(ret, spy),
        "vs_mm_pct":      _diff(ret, mm),
    }


def _build_closed(p: dict, prices: PriceData,
                  fedfunds: Optional[pd.Series]) -> dict:
    ticker      = p["ticker"]
    entry       = date.fromisoformat(p["entry_date"])
    entry_price = float(p["entry_price"])
    exit_date   = date.fromisoformat(p["exit_date"])
    exit_price  = float(p["exit_price"])
    realized    = p.get("realized_return")
    if realized is None and entry_price:
        realized = exit_price / entry_price - 1.0
    days_held = int(p.get("days_held",
                          np.busday_count(entry.isoformat(), exit_date.isoformat())))
    spy = _spy_return(entry, exit_date, prices)
    mm  = _mm_return(entry, exit_date, fedfunds)
    return {
        "ticker":         ticker,
        "status":         "closed",
        "entry_date":     entry.isoformat(),
        "entry_price":    round(entry_price, 2),
        "allocation":     p.get("allocation"),
        "exit_date":      exit_date.isoformat(),
        "exit_price":     round(exit_price, 2),
        "return_pct":     _pct(realized),
        "days_held":      days_held,
        "hold_target":    _HOLD_TARGET,
        "spy_return_pct": _pct(spy),
        "mm_return_pct":  _pct(mm),
        "vs_spy_pct":     _diff(realized, spy),
        "vs_mm_pct":      _diff(realized, mm),
    }


def _avg(items: list[dict], key: str) -> Optional[float]:
    vals = [i[key] for i in items if i.get(key) is not None]
    return round(float(np.mean(vals)), 2) if vals else None


# ── public: shared data builder ─────────────────────────────────────────────

def build_beta_data(as_of_date: Optional[date] = None,
                    prices: Optional[PriceData] = None) -> dict:
    """Build the full beta-tracking dataset (summary + open + closed positions).

    This is the single structure rendered to markdown AND returned by the API.
    Observation-only: reads the persisted position files; never mutates them.
    """
    as_of  = as_of_date or date.today()
    prices = prices or PriceData()
    try:
        fedfunds = load_fedfunds()
    except Exception as exc:
        logger.warning("beta: Fed Funds Rate unavailable (%s); money-market comparison omitted", exc)
        fedfunds = None

    open_list   = [_build_open(p, as_of, prices, fedfunds) for p in _load(_OPEN_FILE)]
    closed_list = [_build_closed(p, prices, fedfunds)      for p in _load(_CLOSED_FILE)]

    entries = [i["entry_date"] for i in (open_list + closed_list)]
    beta_start = min(entries) if entries else None

    closed_aggregate = None
    if closed_list:
        closed_aggregate = {
            "count":               len(closed_list),
            "strategy_return_pct": _avg(closed_list, "return_pct"),
            "spy_return_pct":      _avg(closed_list, "spy_return_pct"),
            "mm_return_pct":       _avg(closed_list, "mm_return_pct"),
        }

    return {
        "as_of_date":       as_of.isoformat(),
        "beta_start":       beta_start,
        "hold_target_days": _HOLD_TARGET,
        "summary": {
            "total_opened":     len(open_list) + len(closed_list),
            "open":             len(open_list),
            "closed":           len(closed_list),
            "closed_aggregate": closed_aggregate,
        },
        "open_positions":   open_list,
        "closed_positions": closed_list,
    }


# ── markdown rendering ──────────────────────────────────────────────────────

def _f(x: Optional[float]) -> str:
    return f"{x:+.2f}%" if x is not None else "n/a"


def _open_table(rows: list[dict]) -> list[str]:
    out = ["| Ticker | Entry date | Entry $ | Current $ | Days (of 252) | Return | SPY | Money-mkt | vs SPY | vs MM |",
           "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for r in rows:
        cur = f"{r['current_price']:.2f}" if r["current_price"] is not None else "n/a"
        out.append(
            f"| {r['ticker']} | {r['entry_date']} | {r['entry_price']:.2f} | {cur} | "
            f"{r['days_held']}/{_HOLD_TARGET} | {_f(r['return_pct'])} | {_f(r['spy_return_pct'])} | "
            f"{_f(r['mm_return_pct'])} | {_f(r['vs_spy_pct'])} | {_f(r['vs_mm_pct'])} |"
        )
    return out


def _closed_table(rows: list[dict]) -> list[str]:
    out = ["| Ticker | Entry date | Exit date | Entry $ | Exit $ | Days held | Return | SPY | Money-mkt | vs SPY | vs MM |",
           "|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for r in rows:
        out.append(
            f"| {r['ticker']} | {r['entry_date']} | {r['exit_date']} | {r['entry_price']:.2f} | "
            f"{r['exit_price']:.2f} | {r['days_held']} | {_f(r['return_pct'])} | {_f(r['spy_return_pct'])} | "
            f"{_f(r['mm_return_pct'])} | {_f(r['vs_spy_pct'])} | {_f(r['vs_mm_pct'])} |"
        )
    return out


def render_markdown(data: dict) -> str:
    s = data["summary"]
    L: list[str] = []
    L.append("# Beta tracking — opened positions")
    L.append("")
    L.append(f"_As of {data['as_of_date']}"
             + (f" · beta start {data['beta_start']}" if data["beta_start"] else "")
             + " · observation-only (does not affect trading logic)._")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append(f"- Positions opened since beta start: **{s['total_opened']}**")
    L.append(f"- Currently open: **{s['open']}**")
    L.append(f"- Closed (completed 252-day hold): **{s['closed']}**")
    ca = s["closed_aggregate"]
    if ca:
        L.append(f"- Closed aggregate (avg of {ca['count']}): "
                 f"strategy **{_f(ca['strategy_return_pct'])}** · "
                 f"SPY **{_f(ca['spy_return_pct'])}** · "
                 f"money-market **{_f(ca['mm_return_pct'])}**")
    L.append("")

    if s["total_opened"] == 0:
        L.append("_No positions opened yet — nothing to track. "
                 "This report refreshes on every daily run and will populate once "
                 "the first position opens._")
        return "\n".join(L) + "\n"

    L.append("## Open positions")
    L.append("")
    L += _open_table(data["open_positions"]) if data["open_positions"] else ["_None currently open._"]
    L.append("")
    L.append("## Closed positions")
    L.append("")
    L += _closed_table(data["closed_positions"]) if data["closed_positions"] else ["_None closed yet._"]
    L.append("")
    return "\n".join(L) + "\n"


# ── public: write the report (called by run_daily) ─────────────────────────

def write_report(as_of_date: Optional[date] = None,
                 prices: Optional[PriceData] = None) -> Path:
    """Refresh data/beta_tracking/beta_log.md from the current persisted state.

    Returns the report path. Safe to call on every run (trading day or not);
    it only reads position files and market data.
    """
    data = build_beta_data(as_of_date, prices)
    md = render_markdown(data)
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    _REPORT_FILE.write_text(md)
    logger.info("Beta report refreshed — %d opened (%d open, %d closed) → %s",
                data["summary"]["total_opened"], data["summary"]["open"],
                data["summary"]["closed"], _REPORT_FILE)
    return _REPORT_FILE


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    path = write_report()
    print(path.read_text())
