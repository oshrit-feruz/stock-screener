"""User-facing copy for all alert types.

All strings use magnitude-edge framing derived from Stage 5b validation:
  mean 252d return +49.2%, spread +26.9% over random, bear mean +50.6%.

Copy rules (from copy_framework.json):
  - Never use "win rate", "guaranteed", "high confidence", or "bullish"
  - Lead with what the signal IS (recovery opportunity) not what it predicts
  - Always name the expected drawdown path (median -15% before recovery)
  - Signal is a 252-day hold — communicate the full timeline upfront
  - Magnitude edge framing: recovery returns are typically large but lumpy
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


_COPY_PATH = Path(__file__).parent.parent / "ui_copy" / "copy_framework.json"


def load_copy_framework() -> Dict[str, Any]:
    """Load the full copy_framework.json into a dict.

    Returns:
        Parsed JSON dict. Raises FileNotFoundError if copy file is missing.
    """
    # TODO: implement
    raise NotImplementedError


def format_new_buy_alert(
    ticker: str,
    drawdown_pct: float,
    composite_score: float,
    current_price: float,
) -> Dict[str, str]:
    """Render the new-BUY-signal alert copy for a ticker.

    Args:
        ticker:          Stock ticker symbol.
        drawdown_pct:    Current drawdown from 52w high as a fraction (e.g. 0.48).
        composite_score: Recovery composite score (0-1).
        current_price:   Latest close price.

    Returns:
        Dict with keys: "headline", "body", "disclaimer".
    """
    # TODO: load copy_framework, select smart_alert_fired context, format
    raise NotImplementedError


def format_position_update(
    ticker: str,
    entry_price: float,
    current_price: float,
    days_held: int,
    unrealized_return: float,
) -> Dict[str, str]:
    """Render the position-update copy (periodic check-in or -15% warning).

    Args:
        ticker:             Stock ticker symbol.
        entry_price:        Price at signal date.
        current_price:      Current price.
        days_held:          Trading days since entry.
        unrealized_return:  (current_price / entry_price) - 1.

    Returns:
        Dict with keys: "headline", "body", "disclaimer".
    """
    # TODO: select context from copy_framework based on unrealized_return level
    raise NotImplementedError


def format_exit_alert(
    ticker: str,
    entry_price: float,
    exit_price: float,
    days_held: int,
    realized_return: float,
) -> Dict[str, str]:
    """Render the 252-day exit alert copy.

    Args:
        ticker:           Stock ticker symbol.
        entry_price:      Price at signal date.
        exit_price:       Current price at day 252.
        days_held:        Should be 252 (or close to it).
        realized_return:  (exit_price / entry_price) - 1.

    Returns:
        Dict with keys: "headline", "body", "disclaimer".
    """
    # TODO: implement
    raise NotImplementedError
