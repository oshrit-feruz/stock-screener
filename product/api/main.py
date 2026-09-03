"""FastAPI backend for Recovery Detector MVP.

Wraps existing backend modules — no signal logic is reimplemented here.
All signal parameters remain frozen; this is a thin HTTP adapter layer.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import re as _re
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
from product.satellite_policy import SCHEMA_VERSION  # noqa: E402
from product.screener.daily_screener import (  # noqa: E402
    ScreenerRow,
    _load_disk_cache,
    _universe_fingerprint,
    run_screener,
)
from product.screener.universe_list import UniverseListError, load_universe_list  # noqa: E402
from scripts.fetch_release_cache import fetch_and_extract as _fetch_release_cache  # noqa: E402
from scripts.seed_cache import seed as _seed_cache  # noqa: E402


def _backtest_active() -> bool:
    with _bt_lock:
        return any(j.get("status") == "running" for j in _bt_jobs.values())


# Cap on how long the warm-up will defer to backtests in total. Backtests get
# strict priority, but back-to-back submissions must not starve the screener
# cache forever — after this budget the warm proceeds concurrently (previous
# behavior) rather than never finishing.
_WARM_YIELD_BUDGET_SECONDS = 900.0


def _make_backtest_yield_fn():
    """Cooperative yield for the screener warm-up: blocks (in 5s naps) while a
    backtest job is running. run_screener calls it between tickers, so the warm
    releases the free tier's fractional vCPU to the user-facing backtest within
    one ticker's work instead of competing with it for the whole scan."""
    paused_total = [0.0]

    def _yield() -> None:
        logged = False
        while _backtest_active() and paused_total[0] < _WARM_YIELD_BUDGET_SECONDS:
            if not logged:
                logged = True
                logger.warning("STARTUP %s: screener warm-up paused — yielding CPU to a running backtest",
                               _BUILD_MARKER)
            time.sleep(5.0)
            paused_total[0] += 5.0
        if logged:
            logger.warning("STARTUP %s: screener warm-up resumed (paused %.0fs total so far)",
                           _BUILD_MARKER, paused_total[0])

    return _yield


def _warm_today_is_cheap() -> bool:
    """True when the startup screener warm can run without a point-in-time
    market-cap recomputation: either today's screener result is already on
    disk (run_screener returns it instantly), or today's date appears in the
    prebuilt PIT grid.

    Today is virtually never a grid date (the grid holds first-trading-day-of-
    month keys), so on a typical deploy the warm's get_universe_top_n(today)
    recomputes market caps for ALL ~503 members. Measured cost of that path:
    +188MB RSS (grid churn + EDGAR facts accumulation) at boot, concurrent
    with any user backtest — the direct cause of the 512MB OOM restarts.
    Skipping leaves the served screener output UNCHANGED: /api/screener's
    on-demand path still computes for today exactly as before, just on first
    request instead of automatically at boot alongside a backtest.

    The grid check is a substring scan of the JSON file, NOT a parse — the
    parsed grid dict costs 51MB of RSS and must not be loaded just for this.
    """
    today = date.today()
    if (_ROOT / "data" / "screener_cache" / f"{today.isoformat()}.json").exists():
        return True
    grid = _ROOT / "data" / "cache" / "pit_market_cap" / "pit_market_caps.json"
    try:
        return f"|{today.isoformat()}\"" in grid.read_text()
    except Exception as exc:  # noqa: BLE001
        # Intentional fail-open: skip warm-up, never crash startup on a grid-read error.
        logger.debug("STARTUP %s: pit grid check failed — %s", _BUILD_MARKER, exc)
        return False


def _warm_screener_cache() -> None:
    """Run screener in background at startup; populate memory + disk cache."""
    global _sc_data, _sc_ts, _sc_warming, _sc_universe_fp, _sc_warm_started
    if not _warm_today_is_cheap():
        logger.warning(
            "STARTUP %s: screener warm-up SKIPPED — today is not covered by the "
            "prebuilt PIT grid, so warming would recompute market caps for the "
            "full membership at boot (measured ~+190MB RSS, concurrent with any "
            "backtest — the 512MB OOM cause). The screener will compute on the "
            "first /api/screener request instead; served results are unchanged.",
            _BUILD_MARKER,
        )
        return
    with _sc_lock:
        _sc_warming = True
        # Stamp it here too, so a warm thread killed mid-scan cannot wedge the
        # flag for the life of the process.
        _sc_warm_started = time.time()
    logger.warning("STARTUP %s: screener warm-up started (yields CPU to backtests between tickers)",
                   _BUILD_MARKER)
    t0 = time.time()
    try:
        _yield_fn = _make_backtest_yield_fn()
        _yield_fn()  # a backtest already running at boot delays the warm entirely
        result = run_screener(yield_fn=_yield_fn)
        data = _screener_payload(result, computed_on=date.today())
        with _sc_lock:
            _sc_data = data
            _sc_ts = time.time()
            # Stamp the universe this warm ran under, so the first request can
            # actually reuse it. Without this the fingerprint would be None and
            # every warm result would be discarded on the next request.
            _sc_universe_fp = _universe_fingerprint(load_universe_list())
        logger.warning("STARTUP %s: screener warm-up finished in %.0fs (incl. any yield pauses)",
                       _BUILD_MARKER, time.time() - t0)
    except Exception:
        logger.warning("STARTUP %s: screener warm-up failed after %.0fs", _BUILD_MARKER, time.time() - t0)
    finally:
        with _sc_lock:
            _sc_warming = False


# Bumped on each diagnostic push so the deployed commit is identifiable in the
# Render logs (if this marker is absent from startup, Render did not redeploy).
_BUILD_MARKER = "perf-v3"  # includes PR #35 (cache-key fix) + #36 (O(log n) engine)
_PKL_GLOB = "*.pkl"  # a cached price frame, as PriceData writes them


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
    prices = len(list((cache / "prices").glob(_PKL_GLOB))) if (cache / "prices").is_dir() else 0
    seed_files = sum(1 for _ in seed_dir.rglob("*") if _.is_file()) if seed_dir.is_dir() else 0
    logger.warning(
        "STARTUP %s: seed_cache tree present=%s (%d files) | after seed: "
        "data/cache prices=%d pkl, pit_market_cap months seeded=%d%s",
        _BUILD_MARKER, seed_dir.is_dir(), seed_files, prices, months,
        " -- WARNING: 0 months means the ranking cache is empty; Simulator "
        "will fall back to a 50-ticker static universe" if months == 0 else "",
    )
    _report_cache_readable(cache / "prices")
    _report_universe_list()


def _report_cache_readable(prices_dir: Path) -> None:
    """Prove the seeded price pickles can actually be LOADED, not just counted.

    A count of 230 files read healthy while every one of them failed to
    unpickle: the release cache is pickled by pandas 3, and a deploy
    environment still carrying pandas 2 cannot reconstruct those frames. The
    Simulator then reported "No price data" on a fully seeded cache, with
    nothing in the startup report to say why. One real load of one file, with
    the pandas/numpy versions beside it, is the line that tells the two apart.
    """
    import numpy as np
    import pandas as pd
    sample = next(iter(sorted(prices_dir.glob(_PKL_GLOB))), None) if prices_dir.is_dir() else None
    if sample is None:
        logger.warning("STARTUP %s: pandas %s / numpy %s; no price pickle to probe",
                       _BUILD_MARKER, pd.__version__, np.__version__)
        return
    try:
        with open(sample, "rb") as f:
            df = pickle.load(f)
        logger.warning("STARTUP %s: pandas %s / numpy %s; price cache readable — %s loads "
                       "(%d rows)", _BUILD_MARKER, pd.__version__, np.__version__,
                       sample.name, len(df))
    except Exception as exc:
        logger.warning(
            "STARTUP %s: pandas %s / numpy %s; price cache UNREADABLE — %s fails to load: %s: %s "
            "-- the seed was pickled by a newer pandas than this environment runs; every "
            "cached price will be skipped and the Simulator will fetch live or report "
            "\"No price data\". Fix: deploy with the pinned pandas (requirements.txt) and clear "
            "the build cache.",
            _BUILD_MARKER, pd.__version__, np.__version__, sample.name,
            type(exc).__name__, str(exc)[:160],
        )


def _report_universe_list() -> None:
    """Log the monthly universe list's state at boot.

    This is the screener's ONLY input on this service (docs/ARCHITECTURE.md:
    Actions computes, Render reads), so it is the thing worth monitoring. The
    previous report counted data/cache/prices and the PIT grid — both of which
    read healthy for 7.5 weeks while the screener returned zero tickers every
    day, because neither was the dependency that was actually missing. A health
    check that watches the wrong directory is worse than none: it produces
    confident green lights over a broken system.
    """
    try:
        ul = load_universe_list()
    except UniverseListError as exc:
        logger.warning(
            "STARTUP %s: universe list UNUSABLE — %s "
            "/api/screener will return 503 until the monthly-universe workflow "
            "commits a valid data/universe/current.json to main.",
            _BUILD_MARKER, exc,
        )
        return
    logger.warning(
        "STARTUP %s: universe list OK — %d tickers, as_of %s (%d days old)%s",
        _BUILD_MARKER, len(ul.tickers), ul.as_of, ul.age_days,
        " -- WARNING: LATE, the monthly rebuild has not run for the current month"
        if ul.is_late else "",
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
_sc_universe_fp: str | None = None   # universe fingerprint _sc_data was computed under
_sc_warm_started: float = 0.0        # when the in-flight scan began (0 = none)

# How long an in-flight scan may hold the single-flight flag before another
# request may take over. _sc_warming is cleared in a `finally`, which does NOT
# run when the thread holding it dies without unwinding — a worker timeout, or
# the OOM killer taking the process mid-scan. When that happens the flag wedges
# True forever and EVERY later request answers {"warming": true} while nothing
# is actually computing. That is not a hypothetical: it is the observed
# production symptom.
_SC_WARM_TIMEOUT = 900.0

# Whether this process may run the full 100-ticker scan inside a request.
# Default OFF. docs/ARCHITECTURE.md: GitHub Actions computes, Render reads. A
# scan in the request path on the 512MB tier is what the OOM kills came from,
# and it is precisely the "(re)compute expensive things itself" the split
# forbids. Set SCREENER_ONDEMAND_SCAN=1 to restore the old behaviour.
_ALLOW_ONDEMAND_SCAN = os.environ.get("SCREENER_ONDEMAND_SCAN", "").strip().lower() in {"1", "true", "yes"}


class ScreenerStateUnavailable(RuntimeError):
    """No precomputed screener state, and this process may not compute one."""

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
    # Default to today, capped at _SIM_MAX_END — otherwise every request that
    # omits end_date would hit the cap check below and get a 400 once today
    # passes the simulator's data boundary.
    end_date:         str   = Field(default_factory=lambda: min(date.today(), _SIM_MAX_END).isoformat())


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
        # Overlay fields (additive; consumers that pick known keys are unaffected).
        "active":           r.active,
        "target_exit_date": r.target_exit_date,
    }


def _screener_payload(result, computed_on: Optional[date] = None) -> dict:
    """The /api/screener body. One builder for the startup warm, the request
    path and the published daily-state result, so they can never publish
    different shapes.

    Additive on purpose: `as_of`, `computed_on`, `buy_signals` and
    `full_ranking` keep their exact meaning and position; everything new sits
    beside them. `computed_on` is the day the result was produced (defaults to
    `as_of` when the caller has no better provenance).
    """
    as_of = result.as_of_date.isoformat()
    return {
        "schema_version":   SCHEMA_VERSION,
        "as_of":            as_of,
        "computed_on":      (computed_on or result.as_of_date).isoformat(),
        "market_regime":    result.market_regime,      # null when SPY was unavailable
        "satellite_policy": result.satellite_policy,
        "buy_signals":      [_row_to_dict(r) for r in result.buy_signals],
        "full_ranking":     [_row_to_dict(r) for r in result.full_ranking],
    }


def _current_price(ticker: str, prices: PriceData) -> Optional[float]:
    today = date.today()
    try:
        # Current-price context: a stale close here is displayed as the price
        # NOW, so bound it (the empty-frame refusal falls through to None).
        ohlcv = prices.get_prices(ticker, str(today - timedelta(days=10)), today.isoformat(),
                                  max_stale_tdays=PriceData.CURRENT_MAX_STALE_TDAYS)
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

# Where the daily screener workflow publishes its output. The workflow pushes
# data/screener_cache/<date>.json to the automation/daily-state branch (never
# main, so no redeploy per day); this service pulls the one file it needs over
# raw.githubusercontent.com. Overridable for tests and for a future move.
#
# Two operational caveats, deliberately documented here rather than discovered:
#   * raw.githubusercontent.com caches responses for ~5 minutes, so a freshly
#     pushed result can take a few minutes to become visible. At a daily
#     cadence that is noise.
#   * raw fetch on a PRIVATE repo returns 404 — indistinguishable from a
#     missing file. If this repo ever goes private, every fetch will miss and
#     the endpoint will 503; the 503 text and the log line below both name
#     this failure mode so it is diagnosed in one read instead of re-derived.
_DAILY_STATE_RAW_BASE = os.environ.get(
    "DAILY_STATE_RAW_BASE",
    "https://raw.githubusercontent.com/oshrit-feruz/stock-screener/automation/daily-state",
)

# How far back a published daily result may be and still be served. The daily
# run fires weekdays at 11:30 UTC, so "today's file" does not exist on
# weekends, holidays, or any weekday morning before ~11:35 UTC. 4 calendar
# days covers a long weekend plus the Monday-morning gap. This is BOUNDED
# staleness with provenance — the response carries computed_on so a client
# showing Friday's scan on Sunday says so — not a silent fallback.
_DAILY_STATE_LOOKBACK_DAYS = 4


def _fetch_published_daily_result(as_of: date) -> bool:
    """Fetch <as_of>.json from the daily-state branch into the local
    data/screener_cache/, atomically. Returns True if a file was written.
    Every failure mode logs and returns False — the caller falls through to
    the honest 503, never to a scan."""
    url = f"{_DAILY_STATE_RAW_BASE}/data/screener_cache/{as_of.isoformat()}.json"
    try:
        resp = requests.get(url, timeout=10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("screener: daily-state fetch failed for %s: %s", url, str(exc)[:200])
        return False
    if resp.status_code == 404:
        # Expected for weekends/holidays/not-yet-run mornings. Also what a
        # private repo returns for EVERY file — see _DAILY_STATE_RAW_BASE.
        logger.info("screener: no published daily result at %s (404)", url)
        return False
    if resp.status_code != 200:
        logger.warning("screener: daily-state fetch HTTP %s for %s", resp.status_code, url)
        return False
    try:
        payload = resp.json()   # reject non-JSON before it can poison the cache
        _sc_cache_dir = _screener_cache_dir()
        _sc_cache_dir.mkdir(parents=True, exist_ok=True)
        target = _sc_cache_dir / f"{as_of.isoformat()}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, target)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("screener: failed to store fetched daily result from %s", url)
        return False


def _screener_cache_dir():
    from product.screener.daily_screener import _CACHE_DIR
    return _CACHE_DIR


def _load_recent_published_result(universe_fp: str):
    """Newest usable result within the lookback window: (result, computed_on),
    or (None, None). Local disk first, then one fetch from the daily-state
    branch per missing date. Fingerprint mismatches are discarded by
    _load_disk_cache — a result computed under a superseded universe is not
    'slightly stale', it is wrong."""
    for delta in range(_DAILY_STATE_LOOKBACK_DAYS + 1):
        d = date.today() - timedelta(days=delta)
        try:
            cached = _load_disk_cache(d, universe_fp)
        except Exception:
            logger.exception("screener: disk-cache read failed for %s", d)
            cached = None
        if cached is None and _fetch_published_daily_result(d):
            try:
                cached = _load_disk_cache(d, universe_fp)
            except Exception:
                logger.exception("screener: fetched daily result unreadable for %s", d)
                cached = None
        if cached is not None:
            return cached, d
    return None, None


def _get_screener_data() -> dict:
    global _sc_data, _sc_ts, _sc_warming, _sc_universe_fp
    # Validate the universe BEFORE any cache short-circuit, and bind the cache
    # to it. Two distinct failures are covered:
    #   * list becomes unusable -> raises -> the route answers 503 instead of
    #     serving an hour-old 200 with no indication;
    #   * list is replaced (month boundary) -> fingerprint mismatch -> recompute,
    #     rather than serving results for a superseded universe.
    # Validity alone is not enough: a usable list does not prove _sc_data was
    # computed from THAT list.
    ulist = load_universe_list()
    universe_fp = _universe_fingerprint(ulist)
    global _sc_warm_started
    with _sc_lock:
        # Return memory cache if fresh AND from the same universe
        if _sc_data and time.time() - _sc_ts < 3600 and _sc_universe_fp == universe_fp:
            return _sc_data
        # Background warming still running — return immediately, client retries.
        # Unless the flag has wedged: see _SC_WARM_TIMEOUT. Without this, a scan
        # killed mid-flight (worker timeout, OOM) leaves the flag True with no
        # computation behind it and the endpoint answers "warming" forever.
        if _sc_warming and (time.time() - _sc_warm_started) < _SC_WARM_TIMEOUT:
            return {"warming": True, "message": "Screener is warming up, please wait…"}
        if _sc_warming:
            logger.warning(
                "screener: in-flight scan flag has been set for %.0fs with no result "
                "(> %.0fs timeout) — assuming the worker died mid-scan and reclaiming it.",
                time.time() - _sc_warm_started, _SC_WARM_TIMEOUT,
            )
        # Mark refresh as in-flight before releasing lock
        _sc_warming = True
        _sc_warm_started = time.time()

    # Prefer precomputed state. Local disk first, then the daily-state branch —
    # a JSON load / one small HTTP GET, fingerprint compare, no scan — safe on
    # the 512MB tier and the path the producer/consumer split intends.
    cached, computed_on = _load_recent_published_result(universe_fp)
    if cached is not None:
        if computed_on != date.today():
            logger.info("screener: serving result computed on %s (today is %s)",
                        computed_on, date.today())
        # Provenance, not decoration: the newest available result may be days
        # old (weekend, holiday, pre-run morning). The client shows computed_on.
        data = _screener_payload(cached, computed_on=computed_on)
        with _sc_lock:
            _sc_data = data
            _sc_ts = time.time()
            _sc_universe_fp = universe_fp
            _sc_warming = False
        return data

    if not _ALLOW_ONDEMAND_SCAN:
        with _sc_lock:
            _sc_warming = False
        raise ScreenerStateUnavailable(
            f"No published screener result within the last "
            f"{_DAILY_STATE_LOOKBACK_DAYS} days, and this service does not scan "
            "on demand. Results are produced by the daily screener workflow in "
            "GitHub Actions and published to the automation/daily-state branch; "
            "check that the workflow is running and pushing data/screener_cache. "
            "(If the repository is private, the raw-file fetch returns 404 for "
            "everything — that failure mode looks exactly like this.) "
            "See docs/ARCHITECTURE.md. Set SCREENER_ONDEMAND_SCAN=1 to "
            "re-enable in-process scanning."
        )

    try:
        result = run_screener()
        data = _screener_payload(result, computed_on=date.today())
        with _sc_lock:
            _sc_data = data
            _sc_ts = time.time()
            _sc_universe_fp = universe_fp
            _sc_warming = False
        return data
    except Exception:
        with _sc_lock:
            _sc_warming = False
        raise


# ── API routes ─────────────────────────────────────────────────────────────────

# One process-wide EDGAR client for the fundamentals report. Same integration
# the screener/backtest already use (shares outstanding, quality gate) — no new
# data dependency. Its facts memo holds PRUNED slices (a few KB per ticker,
# LRU-capped), so this stays far inside the 512MB budget: one small HTTP GET
# on a cold ticker, dict lookups after.
_edgar_report_client = None
_edgar_report_lock = threading.Lock()


def _get_edgar_report_client():
    global _edgar_report_client
    with _edgar_report_lock:
        if _edgar_report_client is None:
            from core.data.edgar import EdgarFundamentals
            _edgar_report_client = EdgarFundamentals()
        return _edgar_report_client


_TICKER_RE = _re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


@app.get("/api/stock/{ticker}/fundamentals")
def stock_fundamentals(ticker: str) -> dict:
    """Fundamental highlights for one ticker, straight from SEC EDGAR.

    Honest-status contract: "ok" carries real filed figures with their filing
    date and form; anything missing or unparsable is "unavailable" with a
    reason — never an estimated or fabricated number. Display-only and NOT
    point-in-time (newest filing on record, no 90-day lag) — see
    EdgarFundamentals.get_revenue_report; signals must keep using the PIT path.
    """
    t = ticker.upper().strip()
    if not _TICKER_RE.match(t):
        return {"ticker": ticker, "status": "unavailable",
                "reason": "Invalid ticker symbol."}
    try:
        report = _get_edgar_report_client().get_revenue_report(t)
    except Exception:
        logger.exception("fundamentals report failed for %s", t)
        report = None
    if report is None:
        return {"ticker": t, "status": "unavailable",
                "reason": "No usable EDGAR filing data for this ticker (not SEC-listed, "
                          "no annual revenue on file, or EDGAR unreachable)."}
    return {
        "ticker": t,
        "status": "ok",
        "revenue": {
            "value":      report["revenue"],
            "period_end": report["period_end"],
            "yoy_pct":    report["yoy_pct"],
        },
        "filing": {
            "filed": report["filed"],
            "form":  report["form"],
        },
        "source": "SEC EDGAR companyfacts",
    }


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "as_of": date.today().isoformat()}


@app.get("/api/screener")
def screener() -> dict:
    try:
        return _get_screener_data()
    except ScreenerStateUnavailable as exc:
        # Same honesty rule as below: no state is a 503 with the reason, never a
        # 200 carrying an empty ranking.
        raise HTTPException(status_code=503, detail=str(exc))
    except UniverseListError as exc:
        # Loud by design. The universe list is produced by GitHub Actions and
        # committed to main; if it is missing or stale this service has nothing
        # legitimate to screen. Returning 503 with the reason is the honest
        # answer — serving an empty ranking as a 200 is what hid this exact
        # failure for 7.5 weeks.
        raise HTTPException(status_code=503, detail=str(exc))


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


_seed_lock = threading.Lock()


def _ensure_seed_cache() -> None:
    """Retry the release-cache fetch + seed if the price cache is still empty.

    The startup fetch fails open on any transient error (the GitHub API's
    per-IP rate limit answered 403 on one boot), and until now the only way
    back to a warm cache was a redeploy. A backtest on an empty cache is the
    moment that matters, so it retries once here — a no-op the instant the
    seed is present (fetch_and_extract returns on the manifest), serialized so
    two concurrent jobs do not both download. Fails open like startup does.
    """
    root = Path(__file__).resolve().parent.parent.parent
    prices = root / "data" / "cache" / "prices"
    if prices.is_dir() and any(prices.glob(_PKL_GLOB)):
        return
    with _seed_lock:
        if prices.is_dir() and any(prices.glob(_PKL_GLOB)):
            return
        logger.warning("BACKTEST %s: price cache is empty — retrying the release-cache "
                       "fetch before the run", _BUILD_MARKER)
        try:
            present = _fetch_release_cache()
            n = _seed_cache() if present else 0
            logger.warning("BACKTEST %s: release-cache retry present=%s, seeded %d file(s)",
                           _BUILD_MARKER, present, n)
        except Exception:
            logger.exception("BACKTEST %s: release-cache retry raised", _BUILD_MARKER)


def _run_backtest_job(job_id: str, params: dict) -> None:
    logger.warning("BACKTEST %s: job %s started (start=%s end=%s thr=%s)",
                   _BUILD_MARKER, job_id, params["start_date"], params["end_date"],
                   params["entry_threshold"])
    t0 = time.time()
    try:
        _ensure_seed_cache()
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
