"""Exit tracker: manage open positions and fire the 252-day exit alert.

Tracks the entry date and price for each open position. On each daily run,
checks whether any position has reached 252 trading days and generates an
exit alert. Does not execute trades -- only produces ExitAlert objects.

Exit rule (FROZEN -- do not modify):
  Hold 252 trading days (~12 months). No stop-loss. No profit target.
  Exit alert fires at day 252. User has 5 trading days to confirm or defer.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from product.alerts.alert_templates import (
    format_exit_alert,
    format_position_update,
    _interp_expected_return,
    _pct_rank,
)

_EXIT_HOLD_DAYS  = 252
_EXIT_GRACE_DAYS = 5
_REMINDER_DAYS   = 30  # advance-notice window before exit

_POSITIONS_DIR = Path(__file__).parent.parent.parent / "data" / "positions"
_OPEN_FILE     = _POSITIONS_DIR / "open_positions.json"
_CLOSED_FILE   = _POSITIONS_DIR / "closed_positions.json"

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """An open position created on a BUY signal."""

    ticker: str
    entry_date: date
    entry_price: float
    signal_composite: Optional[float] = None
    signal_drawdown:  Optional[float] = None


@dataclass
class ExitAlert:
    """Alert generated when a position reaches the 252-day exit date."""

    ticker: str
    entry_date: date
    exit_date: date
    entry_price: float
    current_price: float
    realized_return: float     # (current_price / entry_price) - 1
    days_held: int
    is_win: bool               # realized_return > 0
    alert_type: str = "EXIT"   # "EXIT" or "REMINDER"
    formatted_message: str = ""


@dataclass
class ExitTrackerResult:
    """Output of one daily tracker check."""

    check_date: date
    exit_alerts: List[ExitAlert] = field(default_factory=list)
    open_positions: List[Position] = field(default_factory=list)


def _load_json_list(path: Path) -> List[dict]:
    if not path.exists():
        return []
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load %s: %s", path, exc)
        return []


def _save_json_list(path: Path, data: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2, default=str)


def _pos_to_dict(p: Position) -> dict:
    return {
        "ticker":           p.ticker,
        "entry_date":       p.entry_date.isoformat(),
        "entry_price":      p.entry_price,
        "signal_composite": p.signal_composite,
        "signal_drawdown":  p.signal_drawdown,
    }


def _dict_to_pos(d: dict) -> Position:
    return Position(
        ticker           = d["ticker"],
        entry_date       = date.fromisoformat(d["entry_date"]),
        entry_price      = float(d["entry_price"]),
        signal_composite = d.get("signal_composite"),
        signal_drawdown  = d.get("signal_drawdown"),
    )


class ExitTracker:
    """Tracks open positions and fires exit alerts at 252 trading days.

    Positions are persisted at:
      data/positions/open_positions.json
      data/positions/closed_positions.json
    """

    def open_position(
        self,
        ticker: str,
        entry_date: date,
        entry_price: float,
        signal_composite: Optional[float] = None,
        signal_drawdown: Optional[float] = None,
    ) -> None:
        """Record a new open position (idempotent -- won't duplicate same ticker+date).

        Args:
            ticker:           Stock ticker symbol.
            entry_date:       Date the signal fired and position was entered.
            entry_price:      Closing price on entry_date.
            signal_composite: composite_score at entry (for reference).
            signal_drawdown:  drawdown_pct at entry (for reference).
        """
        positions = [_dict_to_pos(d) for d in _load_json_list(_OPEN_FILE)]

        # Idempotent: skip if same ticker+entry_date already recorded
        for p in positions:
            if p.ticker == ticker and p.entry_date == entry_date:
                logger.info("Position %s %s already recorded", ticker, entry_date)
                return

        positions.append(Position(
            ticker           = ticker,
            entry_date       = entry_date,
            entry_price      = entry_price,
            signal_composite = signal_composite,
            signal_drawdown  = signal_drawdown,
        ))
        _save_json_list(_OPEN_FILE, [_pos_to_dict(p) for p in positions])
        logger.info("Opened position: %s at %.2f on %s", ticker, entry_price, entry_date)

    def check_exits(
        self,
        today: date,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> List[ExitAlert]:
        """Check all open positions for exit eligibility.

        A position at >= _EXIT_HOLD_DAYS trading days generates an EXIT alert
        and is moved to closed_positions.json. A position at exactly
        (_EXIT_HOLD_DAYS - _REMINDER_DAYS) generates a REMINDER alert.

        Args:
            today:          Date to evaluate against.
            current_prices: Optional {ticker: price} lookup. Falls back to
                            entry price if ticker is missing (conservative).

        Returns:
            List of ExitAlert objects (EXIT and REMINDER types).
        """
        if current_prices is None:
            current_prices = {}

        positions  = [_dict_to_pos(d) for d in _load_json_list(_OPEN_FILE)]
        closed     = _load_json_list(_CLOSED_FILE)

        alerts:     List[ExitAlert]  = []
        still_open: List[Position]   = []

        for pos in positions:
            days_held = self._count_trading_days(pos.entry_date, today)
            cur_price = current_prices.get(pos.ticker, pos.entry_price)
            ret       = cur_price / pos.entry_price - 1

            if days_held >= _EXIT_HOLD_DAYS:
                copy = format_exit_alert(
                    ticker          = pos.ticker,
                    entry_price     = pos.entry_price,
                    exit_price      = cur_price,
                    days_held       = days_held,
                    realized_return = ret,
                )
                alerts.append(ExitAlert(
                    ticker            = pos.ticker,
                    entry_date        = pos.entry_date,
                    exit_date         = today,
                    entry_price       = pos.entry_price,
                    current_price     = cur_price,
                    realized_return   = ret,
                    days_held         = days_held,
                    is_win            = ret > 0,
                    alert_type        = "EXIT",
                    formatted_message = f"{copy['body']}\n\n{copy['disclaimer']}",
                ))
                closed.append({
                    "ticker":          pos.ticker,
                    "entry_date":      pos.entry_date.isoformat(),
                    "entry_price":     pos.entry_price,
                    "exit_date":       today.isoformat(),
                    "exit_price":      cur_price,
                    "realized_return": ret,
                    "days_held":       days_held,
                })
            else:
                still_open.append(pos)
                if days_held == (_EXIT_HOLD_DAYS - _REMINDER_DAYS):
                    copy = format_position_update(
                        ticker            = pos.ticker,
                        entry_price       = pos.entry_price,
                        current_price     = cur_price,
                        days_held         = days_held,
                        unrealized_return = ret,
                    )
                    alerts.append(ExitAlert(
                        ticker            = pos.ticker,
                        entry_date        = pos.entry_date,
                        exit_date         = today,
                        entry_price       = pos.entry_price,
                        current_price     = cur_price,
                        realized_return   = ret,
                        days_held         = days_held,
                        is_win            = ret > 0,
                        alert_type        = "REMINDER",
                        formatted_message = (
                            f"Your {pos.ticker} position exits in {_REMINDER_DAYS} "
                            f"trading days. Plan your exit.\n\n"
                            f"{copy['body']}\n\n{copy['disclaimer']}"
                        ),
                    ))

        _save_json_list(_OPEN_FILE,   [_pos_to_dict(p) for p in still_open])
        _save_json_list(_CLOSED_FILE, closed)

        return alerts

    def get_position_update(
        self,
        ticker: str,
        current_price: float,
    ) -> Optional[dict]:
        """Return current return, days held, percentile rank, and expected return.

        Args:
            ticker:        Ticker to look up.
            current_price: Current market price.

        Returns:
            Dict with ticker, entry_date, entry_price, current_price, days_held,
            unrealized_return, expected_return, pct_rank, context_copy.
            Returns None if ticker is not in open positions.
        """
        open_raw = _load_json_list(_OPEN_FILE)
        for d in open_raw:
            pos = _dict_to_pos(d)
            if pos.ticker.upper() == ticker.upper():
                days_held         = self._count_trading_days(pos.entry_date, date.today())
                unrealized_return = current_price / pos.entry_price - 1
                expected_return   = _interp_expected_return(days_held)
                percentile        = _pct_rank(unrealized_return)
                copy = format_position_update(
                    ticker            = pos.ticker,
                    entry_price       = pos.entry_price,
                    current_price     = current_price,
                    days_held         = days_held,
                    unrealized_return = unrealized_return,
                )
                return {
                    "ticker":            pos.ticker,
                    "entry_date":        pos.entry_date.isoformat(),
                    "entry_price":       pos.entry_price,
                    "current_price":     current_price,
                    "days_held":         days_held,
                    "unrealized_return": unrealized_return,
                    "expected_return":   expected_return,
                    "pct_rank":          percentile,
                    "context_copy":      copy["body"],
                }
        return None

    def _count_trading_days(self, start: date, end: date) -> int:
        """Count trading days between start (exclusive) and end (inclusive).

        Uses numpy.busday_count with the default US weekday schedule.
        Does not account for market holidays (sufficient for 252d approximation).
        """
        return int(np.busday_count(start.isoformat(), end.isoformat()))
