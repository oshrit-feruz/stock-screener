"""FastAPI backend for Recovery Detector MVP.

Wraps existing backend modules — no signal logic is reimplemented here.
All signal parameters remain frozen; this is a thin HTTP adapter layer.
"""
from __future__ import annotations

import json
import time
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

from product.screener.daily_screener import run_screener, ScreenerRow
from product.exit.exit_tracker import ExitTracker
from product.alerts.alert_templates import _pct_rank, _interp_expected_return
from core.data.prices import PriceData

app = FastAPI(title="Recovery Detector API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_DATA_DIR      = _ROOT / "data"
_WATCHLIST     = _DATA_DIR / "user_watchlist.json"
_ALERTS_DIR    = _DATA_DIR / "alerts"
_OPEN_FILE     = _DATA_DIR / "positions" / "open_positions.json"
_CLOSED_FILE   = _DATA_DIR / "positions" / "closed_positions.json"
_WEB_DIR       = Path(__file__).parent.parent / "web"

# Server-side screener cache (1 hour)
_sc_data: dict | None = None
_sc_ts: float = 0.0


# ── Pydantic models ────────────────────────────────────────────────────────────

class WatchlistIn(BaseModel):
    tickers: List[str]

class OpenPositionIn(BaseModel):
    ticker: str
    entry_price: float
    entry_date: Optional[str] = None

class ClosePositionIn(BaseModel):
    ticker: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _row_to_dict(r: ScreenerRow) -> dict:
    return {
        "ticker":          r.ticker,
        "price":           round(r.current_price, 2),
        "high_52w":        round(r.high_52w, 2),
        "drawdown_pct":    round(abs(r.drawdown_pct) * 100, 1),
        "composite_score": round(r.composite_score, 4) if r.composite_score is not None else None,
        "dip_score":       round(r.dip_score, 4)       if r.dip_score       is not None else None,
        "momentum_score":  round(r.momentum_score, 4)  if r.momentum_score  is not None else None,
        "volume_score":    round(r.volume_score, 4)     if r.volume_score    is not None else None,
        "gate":            r.gate,
        "signal":          r.signal,
    }


def _current_price(ticker: str, prices: PriceData) -> Optional[float]:
    today = date.today()
    try:
        ohlcv = prices.get_prices(ticker, str(today - timedelta(days=10)), today.isoformat())
        if ohlcv is not None and not ohlcv.empty:
            return float(ohlcv["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _context_msg(ret: float) -> str:
    if ret < -0.20:
        return (
            "You are in the bottom quartile. This happens to 25% of entries. "
            "78% of entries at this level still finished positive at 12 months. "
            "Re-check the thesis: is the company still fundamentally sound?"
        )
    if ret < 0.0:
        return "You are in the normal drawdown zone. Median entry experiences -15% before recovery. Hold."
    if ret < 0.20:
        return "You are tracking well. Average at 12 months: +49.2%. Early gains do not guarantee final returns — stay the course."
    return "You are ahead of 80% of historical entries at this stage. Average at 12 months is +49.2%. Consider your exit plan as you approach day 252."


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "as_of": date.today().isoformat()}


@app.get("/api/screener")
def screener() -> dict:
    global _sc_data, _sc_ts
    if _sc_data and time.time() - _sc_ts < 3600:
        return _sc_data
    result = run_screener()
    _sc_data = {
        "as_of":        result.as_of_date.isoformat(),
        "buy_signals":  [_row_to_dict(r) for r in result.buy_signals],
        "full_ranking": [_row_to_dict(r) for r in result.full_ranking],
    }
    _sc_ts = time.time()
    return _sc_data


@app.get("/api/watchlist")
def get_watchlist() -> dict:
    if not _WATCHLIST.exists():
        return {"tickers": []}
    return {"tickers": json.loads(_WATCHLIST.read_text()).get("watchlist", [])}


@app.post("/api/watchlist")
def save_watchlist(body: WatchlistIn) -> dict:
    _WATCHLIST.write_text(json.dumps({"watchlist": body.tickers}, indent=2))
    return {"success": True, "tickers": body.tickers}


@app.get("/api/alerts")
def get_alerts() -> dict:
    if not _ALERTS_DIR.exists():
        return {"alerts": []}
    cutoff = date.today() - timedelta(days=30)
    alerts: list = []
    for f in sorted(_ALERTS_DIR.glob("*_alerts.json"), reverse=True):
        try:
            date_str = f.stem.replace("_alerts", "")
            if date.fromisoformat(date_str) < cutoff:
                continue
            for a in json.loads(f.read_text()):
                a["date"] = date_str
                alerts.append(a)
        except Exception:
            continue
    return {"alerts": alerts}


@app.get("/api/positions")
def get_positions() -> dict:
    open_raw = json.loads(_OPEN_FILE.read_text()) if _OPEN_FILE.exists() else []
    prices = PriceData()
    today  = date.today()
    result = []
    for p in open_raw:
        ticker      = p["ticker"]
        entry_date  = date.fromisoformat(p["entry_date"])
        entry_price = float(p["entry_price"])
        cur_price   = _current_price(ticker, prices)
        days_held   = int(np.busday_count(entry_date.isoformat(), today.isoformat()))
        exp_ret     = _interp_expected_return(days_held)
        if cur_price is not None:
            ret     = cur_price / entry_price - 1
            pct_r   = _pct_rank(ret)
            context = _context_msg(ret)
        else:
            ret     = None
            pct_r   = None
            context = None
        result.append({
            "ticker":              ticker,
            "entry_date":          entry_date.isoformat(),
            "entry_price":         entry_price,
            "current_price":       round(cur_price, 2) if cur_price else None,
            "current_return_pct":  round(ret * 100, 1) if ret is not None else None,
            "days_held":           days_held,
            "days_remaining":      max(0, 252 - days_held),
            "percentile_rank":     pct_r,
            "expected_return_pct": round(exp_ret * 100, 1),
            "context_message":     context,
            "signal_composite":    p.get("signal_composite"),
            "signal_drawdown":     p.get("signal_drawdown"),
        })
    return {"positions": result}


@app.post("/api/positions/open")
def open_position(body: OpenPositionIn) -> dict:
    tracker    = ExitTracker()
    entry_date = date.fromisoformat(body.entry_date) if body.entry_date else date.today()
    tracker.open_position(
        ticker      = body.ticker.upper(),
        entry_date  = entry_date,
        entry_price = body.entry_price,
    )
    return {"success": True}


@app.post("/api/positions/close")
def close_position(body: ClosePositionIn) -> dict:
    ticker   = body.ticker.upper()
    open_raw = json.loads(_OPEN_FILE.read_text()) if _OPEN_FILE.exists() else []
    pos      = next((p for p in open_raw if p["ticker"] == ticker), None)
    if not pos:
        raise HTTPException(status_code=404, detail=f"Position {ticker} not found")

    prices      = PriceData()
    today       = date.today()
    cur_price   = _current_price(ticker, prices) or float(pos["entry_price"])
    entry_price = float(pos["entry_price"])
    entry_date  = date.fromisoformat(pos["entry_date"])
    days_held   = int(np.busday_count(entry_date.isoformat(), today.isoformat()))
    final_ret   = cur_price / entry_price - 1

    closed = json.loads(_CLOSED_FILE.read_text()) if _CLOSED_FILE.exists() else []
    closed.append({
        **pos,
        "exit_date":       today.isoformat(),
        "exit_price":      cur_price,
        "realized_return": final_ret,
        "days_held":       days_held,
    })
    remaining = [p for p in open_raw if p["ticker"] != ticker]

    _OPEN_FILE.write_text(json.dumps(remaining, indent=2))
    _CLOSED_FILE.write_text(json.dumps(closed, indent=2))

    return {"success": True, "final_return_pct": round(final_ret * 100, 1)}


# ── Static files — mount LAST so API routes take priority ─────────────────────
app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
