"""Daily screener: scan the point-in-time Top-100 universe as of today and
return BUY signals.

The universe is the 100 largest S&P 500 members by point-in-time
dollar-volume (data.sp500_universe.get_universe_top_n — survivorship-free: it
needs only prices, so delisted members rank too, unlike a market-cap ranking
via EDGAR shares), ranked MONTHLY by scripts/build_universe_list.py in GitHub
Actions and read here from data/universe/current.json (see
docs/ARCHITECTURE.md). This process never ranks and never substitutes a
fallback universe. Monthly matches the rebuild cadence of the validated
backtest (product/backtest/engine.py's _UNIVERSE_N, "rebuilt monthly") — live
and backtest must stay in lockstep or the live results are no longer
attributable to the backtested strategy.

Reuses existing signal logic from core.signals.recovery_score and
core.data.edgar — no signal logic is reimplemented here.

Signal parameters (FROZEN — do not modify):
  Weights:       dip=50%  momentum=30%  volume=20%
  BUY threshold: 0.60
  Gate:          fail-open (only an explicit gate=False demotes to SKIP; gate=None
                 passes on the signal alone, so the gate adds no survivorship bias)
  Exit rule:     Hold HOLD_TRADING_DAYS (504, ~2y). No stop-loss, no take-profit.
                 (product/satellite_policy.py; enforced by exit_tracker)
  Overlay:       every payload carries `market_regime` (SPY drawdown from its
                 trailing 252-day high, gate 10%) and `satellite_policy`; each
                 row carries `active` (BUY && in_dislocation) and
                 `target_exit_date`. Signals are never suppressed by the regime —
                 `active` says whether NOW is the time to deploy a sleeve.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import warnings
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.data.edgar import EdgarFundamentals  # noqa: E402
from core.data.fundamentals import PointInTimeFundamentals  # noqa: E402
from core.data.prices import PriceData  # noqa: E402
from core.signals.recovery_score import (  # noqa: E402
    BUY_THRESHOLD,
    compute_recovery_signals,
    passes_quality_gate,
)
from data.sec_8k_veto import is_vetoed  # noqa: E402
from product.satellite_policy import (  # noqa: E402
    is_active,
    market_regime,
    policy_dict,
    target_exit_date,
)
from product.screener.universe_list import load_universe_list  # noqa: E402

# Point-in-time universe size: the 100 largest S&P 500 members by dollar-volume
# as of the monthly list's as-of date (matches the research harness).
_UNIVERSE_N = 100

# 252 trading days are required because dip_score uses close.rolling(252).max();
# with fewer rows high_52w is NaN → composite NaN → always INSUFFICIENT_DATA.
_MIN_HISTORY = 252
_WARMUP_START = "2016-01-01"
_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "screener_cache"

logger = logging.getLogger(__name__)


def _cache_path(as_of: date) -> Path:
    return _CACHE_DIR / f"{as_of.isoformat()}.json"


def _universe_fingerprint(ulist) -> str:
    """Stable digest of the universe a cached result was computed under.

    Keyed on the ticker set as well as the as-of date: two lists could share an
    as-of if one were regenerated, and the cached result is only reusable when
    the actual scanned set matches.
    """
    payload = ulist.as_of.isoformat() + "|" + ",".join(sorted(ulist.tickers))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _load_disk_cache(as_of: date, universe_fp: str) -> "ScreenerResult | None":
    """Return today's cached result, but only if it was computed under the SAME
    universe. Validating the list before the cache lookup is not sufficient on
    its own: at a month boundary the daily run can cache a result under the old
    list minutes before the new one lands, and every later read that day would
    serve results for a superseded universe. A fingerprint mismatch recomputes.
    """
    path = _cache_path(as_of)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        cached_fp = data.get("universe_fingerprint")
        if cached_fp != universe_fp:
            logger.info(
                "screener: discarding disk cache for %s — computed under a different "
                "universe (cached %s, current %s); recomputing.",
                as_of, cached_fp or "<none>", universe_fp,
            )
            return None
        def _row(d: dict) -> ScreenerRow:
            return ScreenerRow(**d)
        full_ranking = [_row(r) for r in data["full_ranking"]]
        return ScreenerResult(
            as_of_date   = date.fromisoformat(data["as_of_date"]),
            buy_signals  = [_row(r) for r in data["buy_signals"]],
            full_ranking = full_ranking,
            vetoed       = [r for r in full_ranking if r.signal == "VETO"],
            # Cache files written before the overlay fields existed still load:
            # a missing regime is honestly None, the policy is the current one.
            market_regime    = data.get("market_regime"),
            satellite_policy = data.get("satellite_policy") or policy_dict(),
        )
    except Exception as exc:
        logger.warning("screener disk cache load failed: %s", exc)
        return None


def _save_disk_cache(result: "ScreenerResult", universe_fp: str) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(result.as_of_date)
        with open(path, "w") as f:
            json.dump({
                "as_of_date":           result.as_of_date.isoformat(),
                "universe_fingerprint": universe_fp,
                "buy_signals":          [asdict(r) for r in result.buy_signals],
                "full_ranking":         [asdict(r) for r in result.full_ranking],
                "market_regime":        result.market_regime,
                "satellite_policy":     result.satellite_policy,
            }, f)
    except Exception as exc:
        logger.warning("screener disk cache save failed: %s", exc)


@dataclass
class ScreenerRow:
    """One row in the screener output table."""

    ticker: str
    current_price: float
    high_52w: float
    drawdown_pct: float        # fraction, e.g. 0.45 means 45% below 52w high
    dip_score: Optional[float]
    momentum_score: Optional[float]
    volume_score: Optional[float]
    composite_score: Optional[float]
    gate: Optional[bool]       # True=pass, False=fail, None=unknown→passes (fail-open)
    signal: str                # "BUY" | "WATCH" | "SKIP" | "INSUFFICIENT_DATA" | "VETO"
    veto_reason: Optional[str] = None  # set when signal == "VETO" (8-K veto)
    # Overlay fields (additive; None when the market regime is unknown).
    active: Optional[bool] = None            # BUY && market in dislocation → deploy a sleeve now
    target_exit_date: Optional[str] = None   # ISO date = as_of + HOLD_TRADING_DAYS weekdays


@dataclass
class ScreenerResult:
    """Full output of one screener run."""

    as_of_date: date
    buy_signals: List[ScreenerRow]    # signal == "BUY", sorted by composite desc
    full_ranking: List[ScreenerRow]   # all tickers, sorted by composite desc
    vetoed: List[ScreenerRow] = field(default_factory=list)  # signal == "VETO"
    # Overlay context published with every result (additive).
    market_regime: Optional[dict] = None                     # None when SPY was unavailable
    satellite_policy: dict = field(default_factory=policy_dict)


def _classify(composite: Optional[float], gate: Optional[bool]) -> str:
    """Map (composite, gate) to signal string using frozen thresholds.

    Fail-OPEN on the quality gate: only an explicit fundamental FAIL (gate is
    False) demotes a candidate to SKIP. Gate=None (no confirmed fundamentals —
    e.g. a delisted name with no EDGAR CIK) passes through on the price signal
    alone, so the gate does not re-introduce survivorship bias into the
    dollar-volume universe. Quality still filters names where data exists.
    """
    if composite is None:
        return "INSUFFICIENT_DATA"
    if gate is False:
        return "SKIP"
    if composite >= BUY_THRESHOLD:
        return "BUY"
    return "WATCH"


def _safe_float(val) -> Optional[float]:
    """Return float or None for NaN / None values."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def run_screener(
    as_of_date: Optional[date] = None,
    prices: Optional[PriceData] = None,
    fundamentals: Optional[EdgarFundamentals] = None,
    apply_8k_veto: bool = True,
    yield_fn: Optional[Callable[[], None]] = None,
) -> ScreenerResult:
    """Scan the point-in-time Top-100 universe and return BUY signals plus the
    full ranked table.

    Args:
        as_of_date:    Date to evaluate signals for. Defaults to today.
        prices:        PriceData instance (created with default cache if None).
        fundamentals:  EdgarFundamentals instance (created if None).
        apply_8k_veto: When True (default), a would-be BUY is blocked (signal →
                       "VETO") if data.sec_8k_veto.is_vetoed flags a recent
                       distress 8-K / going-concern filing as of the run date.
                       Fact-only and fail-safe: a lookup error never blocks.
        yield_fn:      Optional cooperative-yield hook, called once per ticker.
                       The API's startup cache warm passes a function that
                       blocks while a backtest job is running, so the warm
                       (a nice-to-have) never competes with a user-facing
                       backtest for the free tier's fractional vCPU. None
                       (the default, used by the daily run/CLI) is a no-op.

    Returns:
        ScreenerResult with buy_signals, full_ranking, and vetoed.

    Raises:
        UniverseListError: the monthly universe list is missing, malformed,
            empty or stale. Deliberately NOT caught — a screener that cannot
            establish what to scan must fail, not return an empty result. (It
            used to do the latter, and reported "0 signals" as a successful run
            every day from 2026-07-01 to 2026-08-23.)

    Error handling:
        - Ticker with < 252 rows of price history → skipped, warning logged.
        - Ticker with no EDGAR / fundamentals data → gate = None → passes on the
          signal alone (fail-open); only an explicit gate = False demotes to SKIP.
        - Any unexpected exception per ticker → skipped, warning logged.
    """
    if as_of_date is None:
        as_of_date = date.today()

    # Universe validation happens BEFORE the cache lookup, deliberately.
    # Checking the cache first would make the loud-failure guarantee conditional:
    # a result cached while the universe was broken would keep being served with
    # no validation at all — the same "looks like a successful run" shape as the
    # original bug. Validating first makes the invariant unconditional: this
    # function never returns without a usable universe.
    ulist = load_universe_list(today=as_of_date)

    # Return disk-cached result immediately if today's run already completed
    universe_fp = _universe_fingerprint(ulist)
    cached = _load_disk_cache(as_of_date, universe_fp)
    if cached is not None:
        logger.info("screener: returning disk-cached result for %s", as_of_date)
        return cached

    if prices is None:
        prices = PriceData()
    if fundamentals is None:
        fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    # Universe source: the monthly Top-N list produced by GitHub Actions and
    # committed to main (docs/ARCHITECTURE.md — Actions computes, this process
    # only reads). No market-cap ranking happens here.
    #
    # There is deliberately NO fallback. load_universe_list raises on a missing,
    # malformed, empty or stale list, and that exception is allowed to propagate:
    # run_daily.py exits non-zero and the workflow goes red. The previous code
    # caught this at WARNING and set `universe = []`, which reported "0 signals"
    # as a successful run every day from 2026-07-01 to 2026-08-23 — a scan of
    # nothing must look like a failure, not like a quiet day in the market.
    # (ulist was loaded above, before the cache lookup.)
    universe = ulist.tickers
    if ulist.is_late:
        logger.warning(
            "screener: universe list is LATE — as_of %s is %d days old (previous month). "
            "Scanning last month's Top-%d; the monthly-universe workflow needs attention.",
            ulist.as_of, ulist.age_days, _UNIVERSE_N,
        )
    logger.info("Daily screener starting — %s, scanning %d tickers (universe as_of %s)",
                as_of_date, len(universe), ulist.as_of)

    # Market regime once per run: SPY drawdown from its trailing 252-day high.
    # None (SPY unavailable) is published as null, never invented; per-row
    # `active` is then None too, while the signal itself is unaffected.
    regime = market_regime(prices, as_of_date, _WARMUP_START)
    if regime is None:
        logger.warning("screener: market regime unknown for %s (no SPY data) — "
                       "publishing market_regime=null, active=null", as_of_date)
    else:
        logger.info("Market regime %s: SPY %.1f%% below trailing high — %s",
                    as_of_date, regime["spy_dd_from_high"] * 100,
                    "DISLOCATION (sleeves active)" if regime["in_dislocation"]
                    else "calm (satellite parked in core)")
    exit_target = target_exit_date(as_of_date).isoformat()

    rows: List[ScreenerRow] = []

    for ticker in universe:
        if yield_fn is not None:
            yield_fn()
        try:
            ohlcv = prices.get_prices(ticker, _WARMUP_START, as_of_date.isoformat())
            if ohlcv is None or ohlcv.empty:
                logger.warning("%s: no price data returned", ticker)
                continue
            if len(ohlcv) < _MIN_HISTORY:
                logger.warning("%s: insufficient history (%d rows < %d)", ticker, len(ohlcv), _MIN_HISTORY)
                continue

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                scored = compute_recovery_signals(ohlcv)

            mask = scored.index <= pd.Timestamp(as_of_date)
            if not mask.any():
                logger.warning("%s: no data on or before %s", ticker, as_of_date)
                continue

            last = scored.loc[mask].iloc[-1]

            snap = fundamentals.get_snapshot(ticker, as_of_date)
            gate = passes_quality_gate(snap)

            comp = _safe_float(last.get("composite_score"))
            signal = _classify(comp, gate)

            # 8-K veto: block an otherwise-actionable BUY when the ticker carries
            # a recent distress filing as of the run date. Fact-only and
            # fail-safe — a lookup error never blocks (returns not-vetoed).
            veto_reason: Optional[str] = None
            if signal == "BUY" and apply_8k_veto:
                try:
                    blocked, reason = is_vetoed(ticker, as_of_date.isoformat())
                except Exception as exc:
                    blocked, reason = False, f"unverifiable: error {exc}"
                if blocked:
                    signal = "VETO"
                    veto_reason = reason
                    logger.info("8-K veto: %s — %s", ticker, reason)

            if signal == "BUY":
                dd = _safe_float(last.get("drawdown_52w")) or 0.0
                logger.info(f"Signal: {ticker} score={(comp or 0.0):.2f} dip={dd:.1%}")

            rows.append(ScreenerRow(
                ticker         = ticker,
                current_price  = float(last["Close"]),
                high_52w       = _safe_float(last.get("high_52w")) or float(last["Close"]),
                drawdown_pct   = _safe_float(last.get("drawdown_52w")) or 0.0,
                dip_score      = _safe_float(last.get("dip_score")),
                momentum_score = _safe_float(last.get("momentum_score")),
                volume_score   = _safe_float(last.get("volume_score")),
                composite_score = comp,
                gate           = gate,
                signal         = signal,
                veto_reason    = veto_reason,
                active         = is_active(signal, regime),
                target_exit_date = exit_target if signal == "BUY" else None,
            ))

        except Exception as exc:
            logger.warning("%s: unexpected error — %s", ticker, exc)
            continue

    rows.sort(key=lambda r: (r.composite_score is None, -(r.composite_score or 0)))

    buy_signals = [r for r in rows if r.signal == "BUY"]
    vetoed      = [r for r in rows if r.signal == "VETO"]

    # The screener identifies (and vetoes) signals; it does not open positions,
    # so positions-opened is 0 here. The daily run (product/run_daily.py) owns
    # the run-level summary.
    logger.info("Daily screener complete — %d signals, %d positions opened, %d vetoed",
                len(buy_signals), 0, len(vetoed))

    result = ScreenerResult(
        as_of_date       = as_of_date,
        buy_signals      = buy_signals,
        full_ranking     = rows,
        vetoed           = vetoed,
        market_regime    = regime,
        satellite_policy = policy_dict(),
    )
    _save_disk_cache(result, universe_fp)
    return result


def _log_table(result: ScreenerResult) -> None:
    """Log the full screener ranking table (one row per ticker)."""
    logger.info("DAILY SCREENER  as_of=%s  universe=%d tickers",
                result.as_of_date, len(result.full_ranking))
    logger.info("BUY signals: %d", len(result.buy_signals))

    hdr = f"{'Ticker':<6}  {'Price':>8}  {'52wH':>8}  {'DD%':>6}  {'Dip':>5}  {'Mom':>5}  {'Vol':>5}  {'Comp':>5}  {'Gate':<5}  Signal"
    logger.info(hdr)
    logger.info("-" * len(hdr))

    for r in result.full_ranking:
        dip  = f"{r.dip_score:.2f}"  if r.dip_score  is not None else "  N/A"
        mom  = f"{r.momentum_score:.2f}" if r.momentum_score is not None else "  N/A"
        vol  = f"{r.volume_score:.2f}"   if r.volume_score   is not None else "  N/A"
        comp = f"{r.composite_score:.2f}" if r.composite_score is not None else "  N/A"
        gate_str = "yes" if r.gate is True else ("no" if r.gate is False else "N/A")
        logger.info(
            f"{r.ticker:<6}  {r.current_price:>8.2f}  {r.high_52w:>8.2f}  "
            f"{r.drawdown_pct:>5.1%}  {dip:>5}  {mom:>5}  {vol:>5}  {comp:>5}  "
            f"{gate_str:<5}  {r.signal}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_screener()
    _log_table(result)
