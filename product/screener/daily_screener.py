"""Daily screener: scan the point-in-time Top-100 universe as of today and
return BUY signals.

The universe is the 100 largest S&P 500 members by point-in-time market cap
as of the run date (data.sp500_universe.get_universe_top_n), evaluated once
per run. Reuses existing signal logic from core.signals.recovery_score and
core.data.edgar — no signal logic is reimplemented here.

Signal parameters (FROZEN — do not modify):
  Weights:       dip=50%  momentum=30%  volume=20%
  BUY threshold: 0.60
  Gate:          fail-closed (gate=None treated as False)
  Exit rule:     Hold 252 trading days. No stop-loss. (enforced by exit_tracker)
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional

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
from data.sp500_universe import get_universe_top_n  # noqa: E402

# Point-in-time universe size: the 100 largest S&P 500 members by market cap as
# of each run date (matches the research harness). Rebuilt once per run.
_UNIVERSE_N = 100

# 252 trading days are required because dip_score uses close.rolling(252).max();
# with fewer rows high_52w is NaN → composite NaN → always INSUFFICIENT_DATA.
_MIN_HISTORY = 252
_WARMUP_START = "2016-01-01"
_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "screener_cache"

logger = logging.getLogger(__name__)


def _cache_path(as_of: date) -> Path:
    return _CACHE_DIR / f"{as_of.isoformat()}.json"


def _load_disk_cache(as_of: date) -> "ScreenerResult | None":
    path = _cache_path(as_of)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        def _row(d: dict) -> ScreenerRow:
            return ScreenerRow(**d)
        full_ranking = [_row(r) for r in data["full_ranking"]]
        return ScreenerResult(
            as_of_date   = date.fromisoformat(data["as_of_date"]),
            buy_signals  = [_row(r) for r in data["buy_signals"]],
            full_ranking = full_ranking,
            vetoed       = [r for r in full_ranking if r.signal == "VETO"],
        )
    except Exception as exc:
        logger.warning("screener disk cache load failed: %s", exc)
        return None


def _save_disk_cache(result: "ScreenerResult") -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(result.as_of_date)
        with open(path, "w") as f:
            json.dump({
                "as_of_date":  result.as_of_date.isoformat(),
                "buy_signals": [asdict(r) for r in result.buy_signals],
                "full_ranking": [asdict(r) for r in result.full_ranking],
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
    gate: Optional[bool]       # True=pass, False=fail, None=unknown→treated as False
    signal: str                # "BUY" | "WATCH" | "SKIP" | "INSUFFICIENT_DATA" | "VETO"
    veto_reason: Optional[str] = None  # set when signal == "VETO" (8-K veto)


@dataclass
class ScreenerResult:
    """Full output of one screener run."""

    as_of_date: date
    buy_signals: List[ScreenerRow]    # signal == "BUY", sorted by composite desc
    full_ranking: List[ScreenerRow]   # all tickers, sorted by composite desc
    vetoed: List[ScreenerRow] = field(default_factory=list)  # signal == "VETO"


def _classify(composite: Optional[float], gate: Optional[bool]) -> str:
    """Map (composite, gate) to signal string using frozen thresholds.

    Gate=None is treated as False (fail-closed): we won't recommend a buy
    without confirmed fundamental data.
    """
    if composite is None:
        return "INSUFFICIENT_DATA"
    effective_gate = gate if gate is not None else False
    if effective_gate is False:
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

    Returns:
        ScreenerResult with buy_signals, full_ranking, and vetoed.

    Error handling:
        - Universe lookup failure → warning logged, empty result returned.
        - Ticker with < 252 rows of price history → skipped, warning logged.
        - Ticker with no EDGAR / fundamentals data → gate = False (fail-closed).
        - Any unexpected exception per ticker → skipped, warning logged.
    """
    if as_of_date is None:
        as_of_date = date.today()

    # Return disk-cached result immediately if today's run already completed
    cached = _load_disk_cache(as_of_date)
    if cached is not None:
        logger.info("screener: returning disk-cached result for %s", as_of_date)
        return cached

    if prices is None:
        prices = PriceData()
    if fundamentals is None:
        fundamentals = EdgarFundamentals(fallback=PointInTimeFundamentals())

    # Build the point-in-time Top-100 universe once for this run.
    try:
        universe = get_universe_top_n(as_of_date.isoformat(), _UNIVERSE_N)
    except Exception as exc:
        logger.warning("screener: universe lookup failed for %s — %s", as_of_date, exc)
        universe = []
    logger.info("Daily screener starting — %s, scanning %d tickers", as_of_date, len(universe))

    rows: List[ScreenerRow] = []

    for ticker in universe:
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
        as_of_date   = as_of_date,
        buy_signals  = buy_signals,
        full_ranking = rows,
        vetoed       = vetoed,
    )
    _save_disk_cache(result)
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
