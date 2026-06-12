"""Exit tracker: manage open positions and fire the 252-day exit alert.

Tracks the entry date and price for each open position. On each daily run,
checks whether any position has reached 252 trading days and generates an
exit alert. Does not execute trades — only produces ExitAlert objects.

Exit rule (FROZEN — do not modify):
  Hold 252 trading days (~12 months). No stop-loss. No profit target.
  Exit alert fires at day 252. User has 5 trading days to confirm or defer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional


_EXIT_HOLD_DAYS = 252
_EXIT_GRACE_DAYS = 5   # user may defer up to 5 trading days past day 252


@dataclass
class Position:
    """An open position created on a BUY signal."""

    ticker: str
    entry_date: date
    entry_price: float
    signal_composite: Optional[float] = None   # composite_score at entry
    signal_drawdown: Optional[float] = None    # drawdown_pct at entry


@dataclass
class ExitAlert:
    """Alert generated when a position reaches the 252-day exit date."""

    ticker: str
    entry_date: date
    exit_date: date
    entry_price: float
    current_price: float
    realized_return: float          # (current_price / entry_price) - 1
    days_held: int
    is_win: bool                    # realized_return > 0


@dataclass
class ExitTrackerResult:
    """Output of one daily tracker check."""

    check_date: date
    exit_alerts: List[ExitAlert] = field(default_factory=list)
    open_positions: List[Position] = field(default_factory=list)


class ExitTracker:
    """Tracks open positions and fires exit alerts at 252 trading days.

    The tracker is stateless — it does not persist positions itself.
    The caller is responsible for persisting the position ledger
    (e.g. a database or JSON file) and passing it back on each run.
    """

    def check_exits(
        self,
        positions: List[Position],
        current_prices: Dict[str, float],
        check_date: Optional[date] = None,
    ) -> ExitTrackerResult:
        """Check all open positions for exit eligibility.

        A position is eligible for exit if it has been held for >=252
        trading-calendar days (approximated as calendar days * 5/7).
        For simplicity, the tracker uses trading-day count by looking
        up the number of business days between entry_date and check_date.

        Args:
            positions:       List of open Position objects.
            current_prices:  Dict of {ticker: latest_close} from screener.
            check_date:      Date to evaluate. Defaults to today.

        Returns:
            ExitTrackerResult with exit_alerts (positions at >=252d)
            and open_positions (positions not yet at 252d).
        """
        # TODO: implement — count trading days, build ExitAlert for eligible positions
        raise NotImplementedError

    def _count_trading_days(self, start: date, end: date) -> int:
        """Count trading days between start (exclusive) and end (inclusive).

        Uses numpy.busday_count with default US weekday schedule.
        Does not account for market holidays (sufficient for 252d approximation).

        Args:
            start: Entry date (not counted).
            end:   Check date (counted if a weekday).

        Returns:
            Integer count of weekdays in (start, end].
        """
        # TODO: implement using numpy.busday_count
        raise NotImplementedError
