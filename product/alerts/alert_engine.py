"""Alert engine: detect new BUY signals and generate user-facing alert objects.

Compares today's screener output against the persisted prior-day state to
identify tickers that newly crossed the BUY threshold. Does NOT send
notifications -- that is the responsibility of the delivery layer.
Produces structured Alert objects and persists screener state to disk.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.data.prices import PriceData
from product.alerts.alert_templates import (
    format_new_buy_alert,
    format_price_alert,
    format_signal_on_held_ticker,
)
from product.screener.daily_screener import ScreenerRow, run_screener

_STATE_DIR = Path(__file__).parent.parent.parent / "data" / "screener_state"

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    """A single actionable alert for one ticker."""

    ticker: str
    entry_date: date
    entry_price: float
    drawdown_pct: float          # fraction, e.g. 0.45 = 45% below 52w high
    composite_score: float
    dip_score: float
    momentum_score: float
    volume_score: float
    gate_passed: bool
    signal_type: str             # "NEW_BUY"
    formatted_message: str       # full formatted text block from alert_templates


@dataclass
class PortfolioAlert:
    """A single alert generated from a user's portfolio holding."""

    ticker: str
    alert_type: str       # PRICE_TARGET_UP | PRICE_TARGET_DOWN | SIGNAL_ON_HELD_TICKER
    headline: str
    body: str


@dataclass
class AlertEngineResult:
    """Output of one daily alert-engine run."""

    as_of: date
    tickers_scanned: int
    new_alerts: List[Alert] = field(default_factory=list)
    continuing_signals: List[str] = field(default_factory=list)
    dropped_signals: List[str] = field(default_factory=list)


def _state_path(for_date: date) -> Path:
    return _STATE_DIR / f"{for_date.isoformat()}.json"


def _load_state(for_date: date) -> Dict[str, dict]:
    """Load persisted screener state for a given date. Returns {} if missing."""
    path = _state_path(for_date)
    if not path.exists():
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load state file %s: %s", path, exc)
        return {}


def _save_state(for_date: date, state: Dict[str, dict]) -> None:
    """Persist screener state for a given date."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(for_date)
    with open(path, "w") as fh:
        json.dump(state, fh, indent=2, default=str)


def _prev_trading_day(d: date) -> date:
    """Return the previous weekday (approximate -- does not account for holidays)."""
    import datetime
    dt = datetime.date(d.year, d.month, d.day)
    step = datetime.timedelta(days=1)
    dt -= step
    while dt.weekday() >= 5:   # 5=Saturday, 6=Sunday
        dt -= step
    return dt


class AlertEngine:
    """Detects new BUY signals and produces Alert objects.

    Persists screener state at data/screener_state/YYYY-MM-DD.json for
    day-over-day comparison.
    """

    def run_daily_alert_check(
        self,
        as_of_date: Optional[date] = None,
    ) -> AlertEngineResult:
        """Run the full daily alert check across the entire screener universe.

        Args:
            as_of_date:   Date to evaluate. Defaults to today.

        Returns:
            AlertEngineResult with new_alerts, continuing_signals,
            dropped_signals, tickers_scanned, and as_of date.
        """
        if as_of_date is None:
            as_of_date = date.today()

        # 1. Run today's screener (full 50-ticker universe)
        logger.info("Running screener for %s", as_of_date)
        result = run_screener(as_of_date=as_of_date)

        today_rows: Dict[str, ScreenerRow] = {
            r.ticker: r
            for r in result.full_ranking
        }

        # 2. Build today's state dict and persist it
        today_state: Dict[str, dict] = {
            ticker: {
                "signal":    row.signal,
                "composite": row.composite_score,
                "date":      as_of_date.isoformat(),
            }
            for ticker, row in today_rows.items()
        }
        _save_state(as_of_date, today_state)

        # 3. Load yesterday's state (first-run if missing)
        prev_date  = _prev_trading_day(as_of_date)
        prev_state = _load_state(prev_date)

        # 4. Classify tickers
        new_alerts:         List[Alert] = []
        continuing_signals: List[str]  = []
        dropped_signals:    List[str]  = []

        today_buy = {t for t, row in today_rows.items() if row.signal == "BUY"}
        prev_buy  = {t for t, s in prev_state.items() if s.get("signal") == "BUY"}

        for ticker in today_buy:
            was_buy_yesterday = ticker in prev_buy
            if was_buy_yesterday:
                continuing_signals.append(ticker)
            else:
                # NEW signal -- build and format alert
                row = today_rows[ticker]
                alert = self._build_alert(row, as_of_date)
                new_alerts.append(alert)

        for ticker in prev_buy:
            if ticker not in today_buy:
                dropped_signals.append(ticker)

        return AlertEngineResult(
            as_of            = as_of_date,
            tickers_scanned  = len(today_rows),
            new_alerts       = new_alerts,
            continuing_signals = sorted(continuing_signals),
            dropped_signals  = sorted(dropped_signals),
        )

    def portfolio_alert_check(
        self,
        portfolio: List[dict],
        as_of_date: Optional[date] = None,
    ) -> List[PortfolioAlert]:
        """Check price thresholds and recovery signals for portfolio holdings.

        Args:
            portfolio:  List of holding dicts with keys: ticker, entry_price,
                        alert_up_pct, alert_down_pct.
            as_of_date: Date to evaluate. Defaults to today.

        Returns:
            List of PortfolioAlert (price targets and signal-on-held alerts).
        """
        if as_of_date is None:
            as_of_date = date.today()
        if not portfolio:
            return []

        result   = run_screener(as_of_date=as_of_date)
        buy_set  = {r.ticker for r in result.full_ranking if r.signal == "BUY"}
        score_map: Dict[str, tuple] = {
            r.ticker: (r.composite_score or 0.0, r.drawdown_pct)
            for r in result.full_ranking
        }
        prices   = PriceData()
        from datetime import timedelta
        today_str    = as_of_date.isoformat()
        lookback_str = (as_of_date - timedelta(days=10)).isoformat()

        alerts: List[PortfolioAlert] = []
        for h in portfolio:
            ticker      = (h.get("ticker") or "").upper()
            entry_price = h.get("entry_price")
            alert_up    = float(h.get("alert_up_pct")  or 20.0)
            alert_down  = float(h.get("alert_down_pct") or 10.0)

            if not ticker or entry_price is None:
                continue

            # Fetch current price (prefer screener universe, fall back to PriceData)
            cur_price: Optional[float] = None
            screener_row = next((r for r in result.full_ranking if r.ticker == ticker), None)
            if screener_row is not None:
                cur_price = screener_row.current_price
            else:
                try:
                    ohlcv = prices.get_prices(ticker, lookback_str, today_str)
                    if ohlcv is not None and not ohlcv.empty:
                        cur_price = float(ohlcv["Close"].iloc[-1])
                except Exception:
                    pass

            if cur_price is None:
                continue

            ret_pct = (cur_price / float(entry_price) - 1) * 100

            if ret_pct >= alert_up:
                copy = format_price_alert(ticker, "UP", alert_up, ret_pct)
                alerts.append(PortfolioAlert(
                    ticker     = ticker,
                    alert_type = "PRICE_TARGET_UP",
                    headline   = copy["headline"],
                    body       = copy["body"] + "\n\n" + copy["disclaimer"],
                ))
            elif ret_pct <= -alert_down:
                copy = format_price_alert(ticker, "DOWN", alert_down, ret_pct)
                alerts.append(PortfolioAlert(
                    ticker     = ticker,
                    alert_type = "PRICE_TARGET_DOWN",
                    headline   = copy["headline"],
                    body       = copy["body"] + "\n\n" + copy["disclaimer"],
                ))

            if ticker in buy_set:
                comp, dd = score_map.get(ticker, (0.0, 0.0))
                copy = format_signal_on_held_ticker(ticker, comp, dd)
                alerts.append(PortfolioAlert(
                    ticker     = ticker,
                    alert_type = "SIGNAL_ON_HELD_TICKER",
                    headline   = copy["headline"],
                    body       = copy["body"] + "\n\n" + copy["disclaimer"],
                ))

        return alerts

    def _build_alert(self, row: ScreenerRow, run_date: date) -> Alert:
        """Build an Alert from a ScreenerRow."""
        copy = format_new_buy_alert(
            ticker          = row.ticker,
            drawdown_pct    = row.drawdown_pct,
            composite_score = row.composite_score or 0.0,
            current_price   = row.current_price,
        )
        full_msg = f"{copy['body']}\n\n{copy['disclaimer']}"

        return Alert(
            ticker           = row.ticker,
            entry_date       = run_date,
            entry_price      = row.current_price,
            drawdown_pct     = row.drawdown_pct,
            composite_score  = row.composite_score or 0.0,
            dip_score        = row.dip_score or 0.0,
            momentum_score   = row.momentum_score or 0.0,
            volume_score     = row.volume_score or 0.0,
            gate_passed      = row.gate is True,
            signal_type      = "NEW_BUY",
            formatted_message = full_msg,
        )
