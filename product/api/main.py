"""FastAPI backend for Recovery Detector MVP.

Wraps existing backend modules — no signal logic is reimplemented here.
All signal parameters remain frozen; this is a thin HTTP adapter layer.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

from core.data.prices import PriceData  # noqa: E402
from core.signals.recovery_score import BUY_THRESHOLD  # noqa: E402
from product.alerts.alert_templates import (  # noqa: E402
    _interp_expected_return,
    _pct_rank,
)
from product.backtest.engine import run_backtest  # noqa: E402
from product.beta.beta_tracker import build_beta_data  # noqa: E402
from product.exit.exit_tracker import ExitTracker  # noqa: E402
from product.screener.daily_screener import ScreenerRow, run_screener  # noqa: E402
from scripts.fetch_release_cache import fetch_and_extract as _fetch_release_cache  # noqa: E402
from scripts.seed_cache import seed as _seed_cache  # noqa: E402


def _warm_screener_cache() -> None:
    """Run screener in background at startup; populate memory + disk cache."""
    global _sc_data, _sc_ts, _sc_warming
    with _sc_lock:
        _sc_warming = True
    # Logged at WARNING because this thread competes with any in-flight backtest
    # for the free tier's fractional vCPU — a backtest submitted right after a
    # deploy runs at roughly half speed until this line's matching "finished"
    # appears. Without these two lines that slowdown is invisible in the logs.
    logger.warning("STARTUP %s: screener warm-up started (shares CPU with any running backtest)",
                   _BUILD_MARKER)
    t0 = time.time()
    try:
        result = run_screener()
        data = {
            "as_of":        result.as_of_date.isoformat(),
            "buy_signals":  [_row_to_dict(r) for r in result.buy_signals],
            "full_ranking": [_row_to_dict(r) for r in result.full_ranking],
        }
        with _sc_lock:
            _sc_data = data
            _sc_ts = time.time()
        logger.warning("STARTUP %s: screener warm-up finished in %.0fs", _BUILD_MARKER, time.time() - t0)
    except Exception:
        logger.warning("STARTUP %s: screener warm-up failed after %.0fs", _BUILD_MARKER, time.time() - t0)
    finally:
        with _sc_lock:
            _sc_warming = False


# Bumped on each diagnostic push so the deployed commit is identifiable in the
# Render logs (if this marker is absent from startup, Render did not redeploy).
_BUILD_MARKER = "perf-v3"  # includes PR #35 (cache-key fix) + #36 (O(log n) engine)


def _startup_cache_report() -> None:
    """Log, at WARNING, what the cache looks like after seeding — so a cold Render
    boot is fully diagnosable from stdout: build marker, whether the committed
    seed tree is on the runtime filesystem, and the resulting grid/price coverage.
    "months seeded" is the number of distinct PIT market-cap months on disk after
    seeding — 0 here is the direct cause of the "0 members / fallback-50" bug."""
    root = Path(__file__).resolve().parent.parent.parent
    seed_dir = root / "data" / "seed_cache"
    cache = root / "data" / "cache"
    grid = cache / "pit_market_cap" / "pit_market_caps.json"
    months = 0
    try:
        if grid.exists():
            months = len({k.rsplit("|", 1)[1] for k in json.loads(grid.read_text())})
    except Exception:
        pass
    prices = len(list((cache / "prices").glob("*.pkl"))) if (cache / "prices").is_dir() else 0
    seed_files = sum(1 for _ in seed_dir.rglob("*") if _.is_file()) if seed_dir.is_dir() else 0
    logger.warning(
        "STARTUP %s: seed_cache tree present=%s (%d files) | after seed: "
        "data/cache prices=%d pkl, pit_market_cap months seeded=%d%s",
        _BUILD_MARKER, seed_dir.is_dir(), seed_files, prices, months,
        " -- WARNING: 0 months means the ranking cache is empty; Simulator "
        "will fall back to a 50-ticker static universe" if months == 0 else "",
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Safety-net path: render.yaml's buildCommand normally downloads the release
    # cache and seeds data/cache/ at BUILD time, before the app process starts —
    # so this is usually a fast idempotent no-op (manifest.json already present).
    # It only does real work when render.yaml wasn't honored (a dashboard-created,
    # non-Blueprint Render service ignores it), which is exactly the scenario that
    # caused the original "0 members / fallback-50" bug — so this must not depend
    # on the build step having run. Both calls fail open: any error is logged and
    # startup continues regardless (a cold cache means a slower/fallback-universe
    # backtest, not a crash).
    logger.warning("STARTUP %s: lifespan starting — attempting release-cache fetch", _BUILD_MARKER)
    try:
        fetched = _fetch_release_cache()
        logger.warning("STARTUP %s: release-cache fetch returned present=%s", _BUILD_MARKER, fetched)
    except Exception:
        logger.exception("STARTUP %s: release-cache fetch raised", _BUILD_MARKER)
    try:
        n = _seed_cache()
        logger.warning("STARTUP %s: seed_cache.seed() copied %d file(s)", _BUILD_MARKER, n)
    except Exception:
        logger.exception("STARTUP %s: cache seeding raised", _BUILD_MARKER)
    _startup_cache_report()
    threading.Thread(target=_warm_screener_cache, daemon=True).start()
    yield


app = FastAPI(title="Recovery Detector API", version="1.0", lifespan=_lifespan)

# Restrict CORS in production by setting ALLOWED_ORIGINS (comma-separated).
# Falls back to "*" for local development.
_allowed_origins = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
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
_sc_lock = threading.Lock()  # guards _sc_data / _sc_ts / _sc_warming
_sc_data: dict | None = None
_sc_ts: float = 0.0
_sc_warming = False          # True while background scan is running

# Backtest job store — the backtest can run for minutes (large universe / cold
# cache), well past any HTTP proxy timeout, so /api/backtest kicks it off in a
# background thread and returns a job_id immediately; the client polls for the
# result. In-memory only (no DB): fine for a single-instance deploy, and a lost
# job on restart just means the user re-submits.
_bt_lock = threading.Lock()  # guards _bt_jobs and _bt_semaphore
_bt_jobs: dict[str, dict] = {}
_BT_JOB_TTL_SECONDS = 3600   # stale jobs are pruned lazily on each new submission
# Measured peak RSS for a single full-window (2010-2026, 229-ticker true Top-100)
# backtest is ~430MB against a 512MB free-tier ceiling (~82MB headroom) — so this
# MUST stay at 1. A cap of 2+ risks two full-window requests running concurrently
# (2 x 430MB = 860MB, guaranteed OOM); 3 (an earlier value here) would be worse.
_bt_semaphore = threading.Semaphore(1)  # cap concurrent backtest threads at 1


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
    # Default matches the production BUY_THRESHOLD so a default backtest
    # replicates live screener behavior (imported to prevent future drift).
    entry_threshold:  float = BUY_THRESHOLD
    exit_threshold:   float = 0.40
    exit_mode:        str   = "252d_only"   # "252d_only" | "threshold_or_252d" | "threshold_only"
    take_profit_pct:  float = 0.0           # 0 = disabled; e.g. 30 = exit at +30%
    stop_loss_pct:    float = 0.0           # 0 = disabled; e.g. 20 = exit at -20%
    trailing_stop_pct: float = 0.0         # 0 = disabled; e.g. 25 = exit 25% below peak
    position_size_pct: float = 10.0
    max_positions:    int   = 10
    start_date:       str   = "2018-01-01"
    # Default to today so the backtest end does not silently freeze in time.
    end_date:         str   = Field(default_factory=lambda: date.today().isoformat())


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
        "veto_reason":     r.veto_reason,
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
        return (
            "You are in the normal drawdown zone. "
            "Median entry experiences -15% before recovery. Hold."
        )
    if ret < 0.20:
        return (
            "You are tracking well. Average at 12 months: +49.2%. "
            "Early gains do not guarantee final returns — stay the course."
        )
    return (
        "You are ahead of 80% of historical entries at this stage. "
        "Average at 12 months is +49.2%. "
        "Consider your exit plan as you approach day 252."
    )


def _load_open_positions(raise_on_corrupt: bool = False) -> list:
    """Read open positions, tolerating a missing file.

    If raise_on_corrupt=True, raises an exception for unreadable/corrupt files
    instead of returning an empty list (useful for close_position).
    """
    if not _OPEN_FILE.exists():
        return []
    try:
        data = json.loads(_OPEN_FILE.read_text())
        if not isinstance(data, list):
            if raise_on_corrupt:
                raise ValueError("Storage file is not a valid list")
            return []
        return data
    except Exception as exc:
        if raise_on_corrupt:
            raise
        print(f"[WARN] failed to read {_OPEN_FILE}: {exc}")
        return []


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
    global _sc_data, _sc_ts, _sc_warming
    with _sc_lock:
        # Return memory cache if fresh
        if _sc_data and time.time() - _sc_ts < 3600:
            return _sc_data
        # Background warming still running — return immediately, client retries
        if _sc_warming:
            return {"warming": True, "message": "Screener is warming up, please wait…"}
        # Mark refresh as in-flight before releasing lock
        _sc_warming = True
    # No cache and not warming — run synchronously (should be fast from disk cache)
    try:
        result = run_screener()
        data = {
            "as_of":        result.as_of_date.isoformat(),
            "buy_signals":  [_row_to_dict(r) for r in result.buy_signals],
            "full_ranking": [_row_to_dict(r) for r in result.full_ranking],
        }
        with _sc_lock:
            _sc_data = data
            _sc_ts = time.time()
            _sc_warming = False
        return data
    except Exception:
        with _sc_lock:
            _sc_warming = False
        raise


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
    open_raw = _load_open_positions()
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


@app.get("/api/beta/dashboard")
def beta_dashboard() -> dict:
    """Read-only beta-tracking dashboard: for every OPENED position (open and
    closed), the current/realized return plus the SPY and money-market (Fed
    Funds) comparison over the same period, and a running summary.

    Same data as the accumulating report (data/beta_tracking/beta_log.md).
    Observation-only — it never touches signal, sizing, or trading logic.
    Path is under the existing /api/* convention (task suggested /beta/dashboard).
    """
    try:
        return build_beta_data()
    except Exception as exc:  # never 500 the dashboard on a transient data issue
        logger.error("Beta dashboard failed to build data: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Beta data temporarily unavailable. Please try again later."
        ) from exc


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
    try:
        open_raw = _load_open_positions(raise_on_corrupt=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")
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


# ── Backtest simulator (async job queue) ────────────────────────────────────────
#
# The backtest can run for minutes on a large universe / cold cache — far past
# any HTTP proxy or platform request timeout (this is what caused the Render
# hangs). POST kicks off the run in a background thread and returns a job_id
# immediately (202); the client polls GET .../{job_id} for the result.

_SIM_MIN_START = date(2010, 1, 1)  # EDGAR lacks pre-2009 shares data for PIT ranking
# Upper bound = the prebuilt cache's last date (seed_cache manifest sim_end, and
# the UI date-picker's max in product/web/index.html). Requests past this have no
# cached prices for the tail, so every universe ticker would live-refetch its
# entire history — the exact slow "still running for minutes" path the cache
# exists to avoid. The UI already caps the picker here; this server-side guard
# makes the boundary real for stale clients / direct API callers, returning a
# clean 400 instead of a silent slow refetch. Bump this (and the UI max, and the
# cache) together whenever the prebuilt cache is extended.
_SIM_MAX_END = date(2026, 6, 30)


def _prune_old_jobs(now: float) -> None:
    """Drop jobs older than the TTL. Called with _bt_lock held."""
    stale = [jid for jid, j in _bt_jobs.items() if now - j["created"] > _BT_JOB_TTL_SECONDS]
    for jid in stale:
        del _bt_jobs[jid]


def _run_backtest_job(job_id: str, params: dict) -> None:
    logger.warning("BACKTEST %s: job %s started (start=%s end=%s thr=%s)",
                   _BUILD_MARKER, job_id, params["start_date"], params["end_date"],
                   params["entry_threshold"])
    t0 = time.time()
    try:
        result = run_backtest(params)
        logger.warning("BACKTEST %s: job %s run_backtest returned in %.1fs (error=%s)",
                       _BUILD_MARKER, job_id, time.time() - t0, "error" in result)
        with _bt_lock:
            job = _bt_jobs.get(job_id)
            if job is None:
                return  # pruned mid-run (TTL far exceeds any realistic run time)
            if "error" in result:
                job["status"] = "error"
                job["error"] = result["error"]
            else:
                job["status"] = "done"
                job["result"] = result
    except Exception as exc:
        # run_backtest already fails closed internally; this is a last-resort
        # guard so a job can never be stuck "running" forever on an unexpected
        # crash (which would otherwise poll forever with no explanation).
        logger.exception("BACKTEST %s: job %s crashed", _BUILD_MARKER, job_id)
        with _bt_lock:
            job = _bt_jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = f"Internal error: {exc}"
    finally:
        _bt_semaphore.release()


@app.post("/api/backtest", status_code=202)
def backtest(body: BacktestParams) -> dict:
    # FIRST line: proves the request reaches the handler at all. If this never
    # appears in the logs, the request is dying before the app (proxy/timeout/
    # routing), not inside run_backtest.
    logger.warning("BACKTEST %s: handler reached (start=%s end=%s thr=%s)",
                   _BUILD_MARKER, body.start_date, body.end_date, body.entry_threshold)
    try:
        start = date.fromisoformat(body.start_date)
        end   = date.fromisoformat(body.end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {exc}")
    if end <= start:
        raise HTTPException(status_code=400, detail="End date must be after start date")
    if start < _SIM_MIN_START:
        raise HTTPException(
            status_code=400,
            detail="Simulator covers 2010 onward — EDGAR lacks pre-2009 shares "
                   "data for Top-100 ranking",
        )
    if end > _SIM_MAX_END:
        raise HTTPException(
            status_code=400,
            detail=f"Simulator data currently runs through {_SIM_MAX_END.isoformat()}. "
                   f"Pick an end date on or before {_SIM_MAX_END.isoformat()}.",
        )

    params = {
        "entry_threshold":  body.entry_threshold,
        "exit_threshold":   body.exit_threshold,
        "exit_mode":        body.exit_mode,
        "take_profit_pct":  body.take_profit_pct,
        "stop_loss_pct":    body.stop_loss_pct,
        "trailing_stop_pct": body.trailing_stop_pct,
        "position_size_pct": body.position_size_pct,
        "max_positions":    body.max_positions,
        "start_date":       body.start_date,
        "end_date":         body.end_date,
    }
    # Fail fast if at concurrency limit instead of queuing unboundedly
    if not _bt_semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Backtest capacity reached. Please wait for an in-flight backtest to complete."
        )
    job_id = uuid.uuid4().hex
    now = time.time()
    with _bt_lock:
        _prune_old_jobs(now)
        _bt_jobs[job_id] = {"status": "running", "result": None, "error": None, "created": now}
    threading.Thread(target=_run_backtest_job, args=(job_id, params), daemon=True).start()
    return {"job_id": job_id, "status": "running"}


@app.get("/api/backtest/{job_id}")
def backtest_status(job_id: str) -> dict:
    with _bt_lock:
        job = _bt_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown or expired job_id")
        if job["status"] == "done":
            return {"job_id": job_id, "status": "done", **job["result"]}
        if job["status"] == "error":
            return {"job_id": job_id, "status": "error", "detail": job["error"]}
        return {"job_id": job_id, "status": "running"}


# ── Static files — mount LAST so API routes take priority ─────────────────────
app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
