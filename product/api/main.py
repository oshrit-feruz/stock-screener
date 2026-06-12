"""FastAPI backend for Recovery Detector MVP.

Wraps existing backend modules — no signal logic is reimplemented here.
All signal parameters remain frozen; this is a thin HTTP adapter layer.
"""
from __future__ import annotations

import json
import os
import time
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

from product.screener.daily_screener import run_screener, ScreenerRow
from product.exit.exit_tracker import ExitTracker
from product.alerts.alert_templates import _pct_rank, _interp_expected_return
from product.backtest.engine import run_backtest
from core.data.prices import PriceData

app = FastAPI(title="Recovery Detector API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_DATA_DIR      = _ROOT / "data"
_ALERTS_DIR    = _DATA_DIR / "alerts"
_OPEN_FILE     = _DATA_DIR / "positions" / "open_positions.json"
_CLOSED_FILE   = _DATA_DIR / "positions" / "closed_positions.json"
_PORTFOLIO_FILE = _DATA_DIR / "portfolio" / "portfolio.json"
_WEB_DIR       = Path(__file__).parent.parent / "web"

# Server-side screener cache (1 hour)
_sc_data: dict | None = None
_sc_ts: float = 0.0


# ── Pydantic models ────────────────────────────────────────────────────────────

class OpenPositionIn(BaseModel):
    ticker: str
    entry_price: float
    entry_date: Optional[str] = None

class ClosePositionIn(BaseModel):
    ticker: str

class PortfolioHolding(BaseModel):
    ticker: str
    entry_price: Optional[float] = None   # if None, use current price as baseline
    alert_up_pct: float = 20.0
    alert_down_pct: float = 10.0

class PortfolioIn(BaseModel):
    holdings: List[PortfolioHolding]

class BacktestParams(BaseModel):
    entry_threshold:  float = 0.80
    exit_threshold:   float = 0.40
    exit_mode:        str   = "252d_only"   # "252d_only" | "threshold_or_252d" | "threshold_only"
    take_profit_pct:  float = 0.0           # 0 = disabled; e.g. 30 = exit at +30%
    position_size_pct: float = 10.0
    max_positions:    int   = 10
    start_date:       str   = "2018-01-01"
    end_date:         str   = "2026-06-12"


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


def _load_portfolio() -> list:
    if not _PORTFOLIO_FILE.exists():
        return []
    try:
        data = json.loads(_PORTFOLIO_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fetch_news(ticker: str, api_key: str) -> Optional[dict]:
    """Fetch top headline for ticker from NewsAPI.org. Returns None on any failure."""
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":          ticker,
                "sortBy":     "publishedAt",
                "pageSize":   1,
                "language":   "en",
                "from":       (date.today() - timedelta(days=1)).isoformat(),
                "apiKey":     api_key,
            },
            timeout=5,
        )
        data = resp.json()
        articles = data.get("articles", [])
        if articles:
            a = articles[0]
            return {"headline": a.get("title", ""), "url": a.get("url", ""), "source": a.get("source", {}).get("name", "")}
    except Exception:
        pass
    return None


# ── Screener cache helper ──────────────────────────────────────────────────────

def _get_screener_data() -> dict:
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


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "as_of": date.today().isoformat()}


@app.get("/api/screener")
def screener() -> dict:
    return _get_screener_data()


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


# ── Portfolio endpoints ────────────────────────────────────────────────────────

@app.get("/api/portfolio")
def get_portfolio() -> dict:
    holdings = _load_portfolio()
    prices   = PriceData()
    today    = date.today()
    result   = []
    for h in holdings:
        ticker      = h["ticker"]
        entry_price = h.get("entry_price")
        cur_price   = _current_price(ticker, prices)

        # If no entry price stored, use current price as baseline (0% return)
        if entry_price is None:
            entry_price = cur_price

        ret_pct = None
        if cur_price is not None and entry_price is not None:
            ret_pct = round((cur_price / entry_price - 1) * 100, 1)

        result.append({
            "ticker":         ticker,
            "entry_price":    entry_price,
            "current_price":  round(cur_price, 2) if cur_price else None,
            "current_return_pct": ret_pct,
            "alert_up_pct":   h.get("alert_up_pct", 20.0),
            "alert_down_pct": h.get("alert_down_pct", 10.0),
        })
    return {"holdings": result}


@app.post("/api/portfolio")
def save_portfolio(body: PortfolioIn) -> dict:
    _PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "ticker":        h.ticker.upper(),
            "entry_price":   h.entry_price,
            "alert_up_pct":  h.alert_up_pct,
            "alert_down_pct": h.alert_down_pct,
        }
        for h in body.holdings
    ]
    _PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))
    return {"success": True, "count": len(data)}


@app.get("/api/portfolio/alerts")
def portfolio_alerts() -> dict:
    holdings  = _load_portfolio()
    if not holdings:
        return {"alerts": []}

    prices    = PriceData()
    today     = date.today()
    news_key  = os.environ.get("NEWS_API_KEY", "")
    sc        = _get_screener_data()
    buy_set   = {s["ticker"] for s in sc.get("buy_signals", [])}

    alerts = []
    for h in holdings:
        ticker      = h["ticker"]
        entry_price = h.get("entry_price")
        cur_price   = _current_price(ticker, prices)
        alert_up    = float(h.get("alert_up_pct", 20.0))
        alert_down  = float(h.get("alert_down_pct", 10.0))

        if cur_price is None or entry_price is None:
            continue

        ret_pct = (cur_price / entry_price - 1) * 100

        # Price target up
        if ret_pct >= alert_up:
            alerts.append({
                "ticker":       ticker,
                "type":         "PRICE_TARGET_UP",
                "headline":     f"{ticker} is up {ret_pct:+.1f}% from your entry",
                "body":         (
                    f"{ticker} is up {ret_pct:+.1f}% from your entry of ${entry_price:.2f}.\n"
                    f"You set an alert at +{alert_up:.0f}%.\n"
                    f"Consider reviewing your position."
                ),
                "current_return_pct": round(ret_pct, 1),
            })
        # Price target down
        elif ret_pct <= -alert_down:
            alerts.append({
                "ticker":       ticker,
                "type":         "PRICE_TARGET_DOWN",
                "headline":     f"{ticker} is down {ret_pct:+.1f}% from your entry",
                "body":         (
                    f"{ticker} is down {abs(ret_pct):.1f}% from your entry of ${entry_price:.2f}.\n"
                    f"You set an alert at -{alert_down:.0f}%.\n"
                    f"Re-check the thesis: is the company still fundamentally sound?"
                ),
                "current_return_pct": round(ret_pct, 1),
            })

        # Recovery signal on held ticker
        if ticker in buy_set:
            sig = next((s for s in sc["buy_signals"] if s["ticker"] == ticker), {})
            alerts.append({
                "ticker":   ticker,
                "type":     "SIGNAL_ON_HELD_TICKER",
                "headline": f"{ticker} (held) has triggered a recovery signal",
                "body":     (
                    f"{ticker} — which you hold — has triggered a recovery signal today.\n"
                    f"Down {sig.get('drawdown_pct', '?')}% from 52-week high.\n"
                    f"Signal score: {sig.get('composite_score', 0):.2f}\n\n"
                    f"This does not change your current position.\n"
                    f"It means the signal sees a recovery setup forming."
                ),
                "composite_score": sig.get("composite_score"),
                "drawdown_pct":    sig.get("drawdown_pct"),
            })

        # News alert
        if news_key:
            article = _fetch_news(ticker, news_key)
            if article and article.get("headline"):
                alerts.append({
                    "ticker":   ticker,
                    "type":     "NEWS",
                    "headline": f"{ticker}: {article['headline']}",
                    "body":     (
                        f"{ticker}: {article['headline']}\n"
                        f"Source: {article.get('source', '')}\n\n"
                        f"This may affect your position. No action required from the signal.\n"
                        f"Past performance does not guarantee future results."
                    ),
                    "url":      article.get("url", ""),
                    "source":   article.get("source", ""),
                })

    return {"alerts": alerts}


# ── Backtest simulator ─────────────────────────────────────────────────────────

@app.post("/api/backtest")
def backtest(body: BacktestParams) -> dict:
    params = {
        "entry_threshold":  body.entry_threshold,
        "exit_threshold":   body.exit_threshold,
        "exit_mode":        body.exit_mode,
        "position_size_pct": body.position_size_pct,
        "max_positions":    body.max_positions,
        "start_date":       body.start_date,
        "end_date":         body.end_date,
    }
    result = run_backtest(params)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Static files — mount LAST so API routes take priority ─────────────────────
app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
