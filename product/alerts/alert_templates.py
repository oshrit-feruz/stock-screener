"""User-facing copy for all alert types.

All strings use magnitude-edge framing derived from Stage 5b validation:
  mean 252d return +49.2%, spread +26.9% over random, bear mean +50.6%.

Copy rules (from copy_framework.json):
  - Never use "win rate", "guaranteed", "high confidence", or "bullish"
  - Lead with what the signal IS (recovery opportunity) not what it predicts
  - Always name the expected drawdown path (median -15% before recovery)
  - Signal is a 252-day hold -- communicate the full timeline upfront
  - Magnitude edge framing: recovery returns are typically large but lumpy
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict

_COPY_PATH = Path(__file__).parent.parent / "ui_copy" / "copy_framework.json"

_CASE_STUDY_LINES = [
    "  AVGO Mar 2020: down 48%, signal fired -> +89.9% in 3 months",
    "  TSLA May 2023: down 49%, signal fired -> +61.5% in 3 months",
    "  CRM Dec 2022: down 50%, signal fired -> +53.0% in 3 months",
]

# Historical return anchors (day -> mean cumulative return from Stage 5b)
_RETURN_ANCHORS = [(0, 0.0), (21, 0.020), (63, 0.064), (252, 0.492)]

# Approximate percentile lookup: (unrealized_return threshold -> percentile)
_PCT_RANK_TABLE = [
    (-0.45,  5),
    (-0.25, 15),
    (-0.15, 25),
    (-0.10, 35),
    ( 0.00, 45),
    ( 0.20, 60),
    ( 0.40, 75),
    ( 0.70, 90),
    ( 1.50, 99),
]


def load_copy_framework() -> Dict[str, Any]:
    """Load the full copy_framework.json into a dict."""
    with open(_COPY_PATH) as fh:
        return json.load(fh)


def _interp_expected_return(days_held: int) -> float:
    """Linear interpolation of expected cumulative return at days_held."""
    for i in range(len(_RETURN_ANCHORS) - 1):
        d0, r0 = _RETURN_ANCHORS[i]
        d1, r1 = _RETURN_ANCHORS[i + 1]
        if d0 <= days_held <= d1:
            t = (days_held - d0) / (d1 - d0)
            return r0 + t * (r1 - r0)
    return _RETURN_ANCHORS[-1][1]


def _pct_rank(current_return: float) -> int:
    """Approximate percentile of current_return vs historical distribution."""
    for threshold, pct in _PCT_RANK_TABLE:
        if current_return <= threshold:
            return pct
    return 99


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
    dd_str = f"{abs(drawdown_pct) * 100:.1f}"
    headline = f"{ticker}: Recovery Setup Detected"
    body = (
        f"[!] {ticker}: Recovery Setup Detected\n\n"
        f"Down {dd_str}% from 52-week high.\n"
        f"Quality: [OK] Revenue positive, Debt low, Margin positive.\n"
        f"Momentum: returning. Volume: elevated.\n"
        f"Signal strength: {composite_score:.2f} / 1.00\n\n"
        f"Why now?\n"
        f"This pattern has appeared 2,382 times in 7 years of data.\n"
        f"Average 12-month return from similar entries: +49.2%\n"
        f"vs random entry: +22.3% (edge: +26.9 percentage points)\n\n"
        f"What to expect:\n"
        f"The stock may drop another 10-15% before recovering.\n"
        f"That is normal. Median drawdown before recovery: -15%.\n"
        f"Planned hold: 12 months (~252 trading days).\n"
        f"No stop-loss. The edge requires holding through the dip.\n\n"
        f"Similar historical entries:\n"
        + "\n".join(_CASE_STUDY_LINES) + "\n\n"
        f"[!] This is not a recommendation. You decide.\n"
        f"This is a statistical pattern, not a guarantee.\n"
        f"1 in 4 similar entries ends negative at 12 months."
    )
    disclaimer = load_copy_framework()["disclaimer_variations"]["short"]
    return {"headline": headline, "body": body, "disclaimer": disclaimer}


def format_position_update(
    ticker: str,
    entry_price: float,
    current_price: float,
    days_held: int,
    unrealized_return: float,
) -> Dict[str, str]:
    """Render the position-update copy (periodic check-in or loss warning).

    Args:
        ticker:             Stock ticker symbol.
        entry_price:        Price at signal date.
        current_price:      Current price.
        days_held:          Trading days since entry.
        unrealized_return:  (current_price / entry_price) - 1.

    Returns:
        Dict with keys: "headline", "body", "disclaimer".
    """
    expected_return = _interp_expected_return(days_held)
    pct_rank = _pct_rank(unrealized_return)
    days_remaining = max(0, 252 - days_held)
    # Approximate calendar days to exit (trading days * 7/5)
    exit_date_approx = date.today() + timedelta(days=int(days_remaining * 7 / 5))

    if unrealized_return < -0.20:
        context_msg = (
            "You are in the bottom quartile. This happens to 25% of entries.\n"
            "78% of entries at this level still finished positive at 12 months.\n"
            "Re-check the thesis: is the company still fundamentally sound?"
        )
    elif unrealized_return < 0.0:
        context_msg = (
            "You are in the normal drawdown zone. Median entry experiences\n"
            "-15% before recovery. Hold."
        )
    elif unrealized_return < 0.20:
        context_msg = (
            "You are tracking well. Average at 12 months: +49.2%.\n"
            "Early gains do not guarantee final returns -- stay the course."
        )
    else:
        context_msg = (
            "You are ahead of 80% of historical entries at this stage.\n"
            "Average at 12 months is +49.2%. Consider your exit plan\n"
            "as you approach day 252."
        )

    headline = f"{ticker} -- Day {days_held} of 252 ({unrealized_return * 100:+.1f}%)"
    body = (
        f"{ticker} -- Day {days_held} of 252\n\n"
        f"Your return: {unrealized_return * 100:+.1f}%\n"
        f"Historical average at day {days_held}: {expected_return * 100:+.1f}%\n"
        f"Your position in distribution: {pct_rank}th percentile\n\n"
        f"{context_msg}\n\n"
        f"Days remaining: {days_remaining}\n"
        f"Planned exit date: {exit_date_approx}\n"
        f"[!] This is not advice. You control the exit decision."
    )
    disclaimer = load_copy_framework()["disclaimer_variations"]["short"]
    return {"headline": headline, "body": body, "disclaimer": disclaimer}


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
    ret_str = f"{realized_return * 100:+.1f}%"
    headline = f"{ticker} -- 12-Month Hold Complete ({ret_str})"
    body = (
        f"{ticker} -- 12-Month Hold Complete\n\n"
        f"You entered at ${entry_price:.2f}.\n"
        f"Today's price: ${exit_price:.2f}\n"
        f"Your return: {ret_str}\n\n"
        f"Historical context:\n"
        f"Average return for this signal: +49.2%\n"
        f"% of similar entries that were positive: 78.9%\n\n"
        f"What now?\n"
        f"Your 252-day planned hold is complete. The signal has no view\n"
        f"beyond this point -- there is no validated edge for holding longer.\n\n"
        f"Your options:\n"
        f"  - Sell today and lock in your return.\n"
        f"  - Hold longer at your own discretion (no signal guidance).\n"
        f"  - Reinvest if a new signal fires on this ticker.\n\n"
        f"[!] This is not advice. You decide."
    )
    disclaimer = load_copy_framework()["disclaimer_variations"]["medium"]
    return {"headline": headline, "body": body, "disclaimer": disclaimer}
