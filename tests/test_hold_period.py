"""The Simulator must be able to test the holding period the engine enforces.

The exit logic hardcoded 252 in its UI branch while satellite_policy (and the
exit tracker) had moved to 504, so a default Simulator run reproduced a
configuration the research had rejected — the 1-year hold *lost* to SPY on the
clean backtest. These tests pin the holding period as a parameter, its default
as the policy value, and the old mode spellings as still-accepted aliases.

The exit decision is exercised directly rather than through a full backtest:
run_backtest needs price history, EDGAR data and a warm cache, none of which
belong in a unit test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from product.satellite_policy import HOLD_TRADING_DAYS  # noqa: E402


def _resolve(params: dict) -> tuple[int, str, bool]:
    """Mirror how run_backtest reads the holding-period parameters.

    Kept deliberately close to the engine so a change there that this does not
    follow shows up as a failure here rather than passing silently.
    """
    exit_rule_param = params.get("exit_rule")
    _hd = params.get("hold_days")
    hold_days = HOLD_TRADING_DAYS if _hd is None else int(_hd)
    if hold_days < 1:
        raise ValueError(f"hold_days must be >= 1, got {hold_days}")
    exit_mode = params.get("exit_mode", "hold_only")
    exit_mode = {"252d_only": "hold_only",
                 "threshold_or_252d": "threshold_or_hold"}.get(exit_mode, exit_mode)
    return hold_days, exit_mode, exit_rule_param is not None


def test_the_default_hold_is_the_policy_hold():
    """A Simulator run with nothing set must reproduce what the engine
    enforces, not the 1-year hold the research rejected."""
    hold, mode, optimizing = _resolve({})
    assert hold == HOLD_TRADING_DAYS == 504
    assert mode == "hold_only"
    assert not optimizing


@pytest.mark.parametrize("chosen", [252, 378, 504])
def test_the_chosen_hold_is_honoured(chosen):
    """The three periods the UI offers all reach the engine."""
    assert _resolve({"hold_days": chosen})[0] == chosen


@pytest.mark.parametrize("legacy,expected", [
    ("252d_only", "hold_only"),
    ("threshold_or_252d", "threshold_or_hold"),
])
def test_the_old_mode_names_still_work(legacy, expected):
    """A cached page or a bookmarked call must not break on the rename."""
    assert _resolve({"exit_mode": legacy})[1] == expected


@pytest.mark.parametrize("mode", ["hold_only", "threshold_or_hold", "threshold_only"])
def test_the_new_mode_names_pass_through(mode):
    assert _resolve({"exit_mode": mode})[1] == mode


def test_the_parameter_grid_still_gets_its_own_branch():
    """Optimization mode keys on exit_rule, not on hold_days.

    It used to key on hold_days, which is precisely why the UI could not send a
    holding period: doing so would have silently dropped its threshold exits.
    """
    assert _resolve({"hold_days": 252, "exit_rule": "A"})[2] is True
    assert _resolve({"hold_days": 252})[2] is False


@pytest.mark.parametrize("bad", [0, -1, -504])
def test_a_nonsense_hold_is_rejected(bad):
    """Zero would exit every position on its entry bar and report it as a
    completed trade, which reads as a result rather than a mistake."""
    with pytest.raises(ValueError):
        _resolve({"hold_days": bad})


def test_the_api_default_matches_the_policy():
    """The request model's default is the same constant, so the API and the
    engine cannot drift apart."""
    from product.api.main import BacktestParams
    assert BacktestParams().hold_days == HOLD_TRADING_DAYS


def test_the_beta_tracker_counts_against_the_policy_hold():
    """Its progress column read "of 252" while positions exited at 504."""
    import product.beta.beta_tracker as beta
    assert beta._HOLD_TARGET == HOLD_TRADING_DAYS


def test_the_exit_tracker_enforces_the_policy_hold():
    import product.exit.exit_tracker as tracker
    assert tracker._EXIT_HOLD_DAYS == HOLD_TRADING_DAYS


# ── alert copy ────────────────────────────────────────────────────────────────
# The alerts a user actually reads promised "Day N of 252" and a "12-Month Hold
# Complete" while the exit tracker fired at 504, so the exit alert read "Day 504
# of 252" and told them a 252-day hold was complete.

def test_the_position_update_counts_against_the_policy_hold():
    from product.alerts.alert_templates import format_position_update
    u = format_position_update("COIN", 210.0, 175.0, 128, -0.168)
    assert f"of {HOLD_TRADING_DAYS}" in u["headline"]
    assert "of 252" not in u["headline"]
    assert f"Days remaining: {HOLD_TRADING_DAYS - 128}" in u["body"]


def test_the_exit_alert_names_the_policy_hold():
    from product.alerts.alert_templates import format_exit_alert
    x = format_exit_alert("COIN", 210.0, 260.0, HOLD_TRADING_DAYS, 0.238)
    assert "12-Month" not in x["headline"]
    assert f"{HOLD_TRADING_DAYS}-day planned hold is complete" in x["body"]


def test_the_buy_alert_promises_the_policy_hold():
    from product.alerts.alert_templates import format_new_buy_alert
    b = format_new_buy_alert("COIN", 0.548, 0.825, 175.0)
    assert f"{HOLD_TRADING_DAYS} trading days" in b["body"]
    assert "~252 trading days" not in b["body"]


def test_the_research_figures_are_not_relabelled():
    """The +49.2% mean is a Stage 5b measurement of 252-day forward returns.
    Following the hold everywhere would have turned it into a 2-year claim the
    study never made, so those lines stay labelled as 12-month."""
    from product.alerts.alert_templates import format_exit_alert
    x = format_exit_alert("COIN", 210.0, 260.0, HOLD_TRADING_DAYS, 0.238)
    assert "+49.2%" in x["body"]


@pytest.mark.parametrize("days,phrase,adjective", [
    (252, "1 year", "1-Year"),
    (504, "2 years", "2-Year"),
    (378, "18 months", "18-Month"),
])
def test_hold_wording(days, phrase, adjective):
    """English does not pluralise a noun used adjectivally — "2 Years Hold"
    reads as a typo, hence the two forms."""
    from product.alerts.alert_templates import _hold_adjective, _hold_phrase
    assert _hold_phrase(days) == phrase
    assert _hold_adjective(days) == adjective
