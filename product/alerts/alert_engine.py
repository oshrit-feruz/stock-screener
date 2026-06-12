"""Alert engine: detect new BUY signals and generate user-facing alert objects.

Compares today's screener output against the user's open-position ledger
and watchlist to identify tickers that newly crossed the BUY threshold.
Does NOT send notifications — that is the responsibility of the delivery
layer (push notification, email, in-app). This module only produces
structured Alert objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Set

from product.screener.daily_screener import ScreenerResult, ScreenerRow


@dataclass
class Alert:
    """A single actionable alert for one ticker."""

    ticker: str
    alert_date: date
    signal: str                      # always "BUY" for new-signal alerts
    composite_score: Optional[float]
    drawdown_pct: float              # e.g. 0.48 = 48% below 52w high
    current_price: float
    high_52w: float
    headline: str                    # short user-facing string
    body: str                        # full copy block (from alert_templates)
    alert_type: str = "NEW_BUY"      # "NEW_BUY" | "EXIT_DUE" | "POSITION_UPDATE"


@dataclass
class AlertEngineResult:
    """Output of one engine run."""

    run_date: date
    new_alerts: List[Alert] = field(default_factory=list)
    suppressed: List[str] = field(default_factory=list)   # tickers skipped (already open)


class AlertEngine:
    """Detects new BUY signals and produces Alert objects.

    The engine is stateless — it does not persist positions itself.
    Pass open_tickers to suppress repeat alerts for positions already held.
    """

    def detect_new_signals(
        self,
        screener_result: ScreenerResult,
        open_tickers: Optional[Set[str]] = None,
        watchlist: Optional[Set[str]] = None,
    ) -> AlertEngineResult:
        """Compare screener result against open positions and return new alerts.

        Args:
            screener_result: Output of run_screener() for today.
            open_tickers:    Set of tickers with an open position.
                             BUY alerts for these are suppressed (already invested).
            watchlist:       If provided, only alert on tickers in the watchlist.
                             If None, alert on all BUY signals in the universe.

        Returns:
            AlertEngineResult with new_alerts and suppressed lists.
        """
        # TODO: implement
        raise NotImplementedError

    def _build_alert(self, row: ScreenerRow, run_date: date) -> Alert:
        """Build an Alert from a ScreenerRow using copy_framework templates.

        Args:
            row:       ScreenerRow with signal == "BUY".
            run_date:  Date of the screener run.

        Returns:
            Alert with headline and body populated from alert_templates.
        """
        # TODO: import from alert_templates and format
        raise NotImplementedError
