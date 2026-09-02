"""Satellite-sleeve policy — the single source of truth for what the engine
recommends AND enforces, so the published `satellite_policy` can never drift
from the exit tracker's behaviour.

Validated configuration (see validation/SUMMARY.md, stages 11-15; clean
survivorship-free universe, next-session fills, delisted names sold at their
final print):
  * The recovery signal's alpha is sparse — it exists during market
    dislocations and is diluted to a coin flip when run always-on. Deployed
    as a conditional overlay on an S&P 500 core it beat SPY in 14/17 rolling
    5-year windows (median excess +3.1%, median Sharpe 0.94 vs 0.80) and the
    gate held out-of-sample (a 5-12% plateau; a threshold chosen on one decade
    beats SPY on the other, both directions).
  * Fixed 2-year hold (504 trading days), no stop-loss, no take-profit, no
    adaptive exit: every price-reactive exit clipped the volatile recovery
    winners that carry the edge. Staged / confirmed entry did not help.
  * Sleeves sized at 10% of the satellite budget, max 10 concurrent, so the
    whole budget is deployed only in a deep dislocation.

Everything here is additive to the screener payload: a consumer that does not
know these fields keeps working unchanged (shift-app's reader picks known keys
and ignores extras).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Policy constants (FROZEN together — change only with research sign-off) ──
HOLD_TRADING_DAYS: int = 504          # ~2 years; enforced by product/exit/exit_tracker.py
GATE_DD: float = 0.10                 # market DD from trailing 252d high that activates sleeves
GATE_DD_LOOKBACK_DAYS: int = 252      # trailing window for the market high
SLEEVE_PCT_OF_BUDGET: int = 10        # each sleeve = 10% of the client's satellite budget
MAX_SLEEVES: int = 10                 # so 100% of the budget deploys only in a deep dislocation
EXIT_RULE: str = "hold_2y_no_tp_no_sl"
SCHEMA_VERSION: int = 2

_MARKET_TICKER = "SPY"


def policy_dict() -> dict:
    """The `satellite_policy` block published with every screener payload."""
    return {
        "hold_trading_days": HOLD_TRADING_DAYS,
        "exit_rule": EXIT_RULE,
        "gate_dd": GATE_DD,
        "gate_lookback_days": GATE_DD_LOOKBACK_DAYS,
        "sleeve_pct_of_budget": SLEEVE_PCT_OF_BUDGET,
        "max_sleeves": MAX_SLEEVES,
    }


def regime_from_series(close: pd.Series, as_of: date) -> Optional[dict]:
    """Market regime from a close series: drawdown from the trailing
    `GATE_DD_LOOKBACK_DAYS` high as of `as_of`, and whether that clears the gate.

    Uses only bars on or before `as_of` (no look-ahead). Returns None when the
    series is empty or has no bar on/before the date — the caller must publish
    `market_regime: null` rather than invent a regime.
    """
    if close is None or len(close) == 0:
        return None
    s = close.dropna()
    s = s[s.index <= pd.Timestamp(as_of)]
    if s.empty:
        return None
    window = s.iloc[-GATE_DD_LOOKBACK_DAYS:]
    high = float(window.max())
    last = float(s.iloc[-1])
    if not (high > 0) or not np.isfinite(last):
        return None
    dd = max(0.0, (high - last) / high)
    return {
        "as_of": pd.Timestamp(s.index[-1]).date().isoformat(),
        "market_ticker": _MARKET_TICKER,
        "spy_dd_from_high": round(dd, 4),
        "in_dislocation": bool(dd >= GATE_DD),
        "gate_dd": GATE_DD,
        "lookback_days": GATE_DD_LOOKBACK_DAYS,
        # How many bars fed the trailing high; below the lookback the high is a
        # partial-window figure, which the consumer can choose to distrust.
        "bars_in_window": int(len(window)),
    }


def market_regime(prices, as_of: date, warmup_start: str) -> Optional[dict]:
    """Fetch SPY through the given PriceData-like object and compute the regime.

    Any failure (no data, exception) yields None — never a fabricated regime.
    """
    try:
        ohlcv = prices.get_prices(_MARKET_TICKER, warmup_start, as_of.isoformat())
    except Exception as exc:  # network / cache / adapter failure
        logger.warning("satellite_policy: %s fetch failed for %s — %s", _MARKET_TICKER, as_of, exc)
        return None
    if ohlcv is None or getattr(ohlcv, "empty", True) or "Close" not in ohlcv.columns:
        logger.warning("satellite_policy: no %s data on/before %s; market_regime is null",
                       _MARKET_TICKER, as_of)
        return None
    return regime_from_series(ohlcv["Close"], as_of)


def fill_date(signal_date: date) -> date:
    """The first weekday strictly after the signal bar — the earliest session a
    signal computed on that bar's close can actually be filled (the validated
    strategy fills at the next session, never on the signal bar)."""
    # roll="backward": a weekend signal date is first pulled back to Friday, so
    # +1 lands on Monday (strictly after); a weekday simply advances one day.
    out = np.busday_offset(np.datetime64(signal_date.isoformat()), 1, roll="backward")
    return pd.Timestamp(out).date()


def target_exit_date(entry: date, hold_days: int = HOLD_TRADING_DAYS) -> date:
    """Entry (fill) date + `hold_days` weekdays.

    Deliberately the same weekday arithmetic (no holiday calendar) that
    exit_tracker._count_trading_days uses, so the date the screener publishes
    is the date the tracker will actually fire on. Callers must pass the FILL
    date (`fill_date(signal_date)`), which is what the tracker records as the
    position's entry date.
    """
    out = np.busday_offset(np.datetime64(entry.isoformat()), hold_days, roll="forward")
    return pd.Timestamp(out).date()


def is_active(signal: Optional[str], regime: Optional[dict]) -> Optional[bool]:
    """Whether a candidate is actionable NOW under the overlay policy.

    True  — a BUY while the market is in a dislocation (deploy a sleeve).
    False — a BUY in a calm market (watch; keep the budget parked in the S&P
            core), or any non-BUY verdict.
    None  — the regime is unknown, so actionability cannot be stated. The
            signal itself is unchanged; only the regime is missing.
    """
    if signal != "BUY":
        return False
    if regime is None:
        return None
    return bool(regime.get("in_dislocation"))
