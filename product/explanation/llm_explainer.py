"""LLM explainer: build prompts that explain a BUY signal to a user.

Generates structured prompts for an LLM (e.g. claude-sonnet-4-6) that
produce plain-language explanations of why a ticker fired a BUY signal.
The explainer is prompt-only — it does not call any LLM API directly.
Callers are responsible for sending the prompt and handling the response.

Framing constraints (from Stage 5b, copy_framework.json):
  - Explain the signal as a recovery opportunity, not a buy recommendation
  - Include the expected drawdown path (median -15% before recovery)
  - Communicate the 252-day hold requirement
  - Cite historical base rates, not predictions
  - Never use "guaranteed", "certain", "high confidence", or "will"
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from product.screener.daily_screener import ScreenerRow


@dataclass
class ExplainerPrompt:
    """A fully rendered prompt ready to send to an LLM."""

    system: str          # system-level framing and constraints
    user: str            # user-turn prompt with ticker-specific data
    suggested_model: str = "claude-sonnet-4-6"
    max_tokens: int = 400


def build_signal_explanation_prompt(
    row: ScreenerRow,
    as_of_date: date,
    user_risk_label: Optional[str] = None,
) -> ExplainerPrompt:
    """Build an LLM prompt that explains why ticker fired BUY.

    The prompt instructs the LLM to describe:
      1. What the signal detected (drawdown from 52w high)
      2. What the historical base rate implies (magnitude edge, not direction)
      3. What the user should expect (median -15% MAE before recovery)
      4. The hold plan (252 trading days)

    Args:
        row:             ScreenerRow with signal == "BUY".
        as_of_date:      Date of the screener run.
        user_risk_label: Optional user risk label ("conservative" | "growth").
                         Affects framing emphasis only — NOT the signal.

    Returns:
        ExplainerPrompt ready to send to an LLM API.
    """
    # TODO: implement — build system + user prompt strings from row data
    raise NotImplementedError


def build_drawdown_explanation_prompt(
    ticker: str,
    entry_price: float,
    current_price: float,
    days_held: int,
    drawdown_from_entry: float,
) -> ExplainerPrompt:
    """Build an LLM prompt that explains a current unrealized loss.

    Used when user opens the app and sees a position at -15% or worse.
    The prompt should contextualize the drawdown as expected behavior,
    cite the median MAE stat, and reinforce the hold plan.

    Args:
        ticker:               Stock ticker.
        entry_price:          Price at signal date.
        current_price:        Current price.
        days_held:            Trading days since entry.
        drawdown_from_entry:  (current_price / entry_price) - 1  (negative).

    Returns:
        ExplainerPrompt ready to send to an LLM API.
    """
    # TODO: implement
    raise NotImplementedError
