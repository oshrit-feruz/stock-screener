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


@pytest.mark.parametrize("days_held", [378, 504])
def test_the_research_average_is_not_attributed_to_a_day_it_never_measured(days_held):
    """_interp_expected_return clamps past the 252-day anchor, so beyond it the
    number is the 12-month mean whatever the position's age. Printing it as
    "the average at day 504" would offer a 12-month result as evidence for a
    2-year hold — a claim the study does not make."""
    from product.alerts.alert_templates import format_position_update
    line = next(ln for ln in format_position_update("COIN", 210.0, 175.0, days_held, -0.168)
                ["body"].split("\n") if "Historical average" in ln)
    assert "at 12 months" in line
    assert f"at day {days_held}" not in line


@pytest.mark.parametrize("days_held,expected", [(63, "+6.4%"), (252, "+49.2%")])
def test_within_the_measured_range_the_day_is_named(days_held, expected):
    """Inside the study's range the figure really is the average at that day."""
    from product.alerts.alert_templates import format_position_update
    line = next(ln for ln in format_position_update("COIN", 210.0, 175.0, days_held, -0.168)
                ["body"].split("\n") if "Historical average" in ln)
    assert f"at day {days_held}" in line
    assert expected in line


def test_the_exit_alert_labels_its_average_with_the_measured_horizon():
    """The exit alert is headlined "2-Year Hold Complete"; an unlabelled +49.2%
    beneath that reads as the 2-year average."""
    from product.alerts.alert_templates import format_exit_alert
    body = format_exit_alert("COIN", 210.0, 260.0, HOLD_TRADING_DAYS, 0.238)["body"]
    assert "Average return for this signal at 12 months: +49.2%" in body


# ── endpoint wiring ───────────────────────────────────────────────────────────
# The tests above exercise a mirror of the engine's parameter handling, which is
# exactly why they missed the real bug: BacktestParams declared hold_days and
# the endpoint never forwarded it, so every Simulator run used the default and
# the new selector did nothing. This one drives the endpoint itself and asserts
# on the dict the engine actually receives.

def test_the_endpoint_forwards_the_chosen_hold_to_the_engine(monkeypatch):
    import product.api.main as main

    captured: dict = {}

    def fake_thread(target=None, args=(), **kwargs):
        # args = (job_id, params); capture without running the backtest.
        captured.update(args[1])

        class _Noop:
            def start(self):
                pass
        return _Noop()

    monkeypatch.setattr(main.threading, "Thread", fake_thread)

    for chosen in (252, 378, 504):
        captured.clear()
        body = main.BacktestParams(hold_days=chosen, start_date="2018-01-01",
                                   end_date="2020-01-01")
        main.backtest(body)
        # The real job releases the concurrency semaphore when it finishes; the
        # stand-in thread never runs, so release it here or the third call 429s.
        main._bt_semaphore.release()
        assert captured.get("hold_days") == chosen, (
            f"the engine received {captured.get('hold_days')!r}, not the chosen {chosen}"
        )


def test_the_endpoint_default_is_the_policy_hold(monkeypatch):
    import product.api.main as main

    captured: dict = {}

    def fake_thread(target=None, args=(), **kwargs):
        captured.update(args[1])

        class _Noop:
            def start(self):
                pass
        return _Noop()

    monkeypatch.setattr(main.threading, "Thread", fake_thread)
    main.backtest(main.BacktestParams(start_date="2018-01-01", end_date="2020-01-01"))
    main._bt_semaphore.release()
    assert captured.get("hold_days") == HOLD_TRADING_DAYS


# ── exit-mode dispatch ────────────────────────────────────────────────────────
# _ui_exit_reason is the branch run_backtest takes for a Simulator run. It was
# lifted out of the loop so these can drive the real decision rather than a
# mirror of it — the hold_days bug above got through precisely because the
# tests agreed with a copy instead of with the engine.

@pytest.mark.parametrize("chosen", [252, 378, 504])
def test_hold_only_exits_on_the_chosen_day(chosen):
    from product.backtest.engine import _ui_exit_reason
    assert _ui_exit_reason("hold_only", chosen - 1, chosen, None, 0.4) is None
    assert _ui_exit_reason("hold_only", chosen, chosen, None, 0.4) == f"{chosen}d"


def test_threshold_or_hold_prefers_the_hold():
    from product.backtest.engine import _ui_exit_reason
    # Both conditions true on the same bar: the hold is the one reported.
    assert _ui_exit_reason("threshold_or_hold", 504, 504, 0.1, 0.4) == "504d"
    assert _ui_exit_reason("threshold_or_hold", 10, 504, 0.1, 0.4) == "threshold"
    assert _ui_exit_reason("threshold_or_hold", 10, 504, 0.9, 0.4) is None


def test_threshold_only_respects_a_shorter_chosen_hold():
    """The cap used to be the policy constant unconditionally, so a 252-day
    selection still ran to 504 and the trade table said "504d_cap" — the
    result disagreed with the control the user had set."""
    from product.backtest.engine import _ui_exit_reason
    assert _ui_exit_reason("threshold_only", 252, 252, 0.9, 0.4) == "252d_cap"
    assert _ui_exit_reason("threshold_only", 251, 252, 0.9, 0.4) is None


def test_threshold_only_never_runs_past_the_research():
    """A selection longer than the policy hold does not extend the cap: the
    study covers 504 days and nothing beyond it."""
    from product.backtest.engine import _ui_exit_reason
    assert _ui_exit_reason("threshold_only", HOLD_TRADING_DAYS, 5000, 0.9, 0.4) \
        == f"{HOLD_TRADING_DAYS}d_cap"


def test_an_unknown_exit_mode_closes_nothing():
    """Documents why the API constrains exit_mode: reaching the engine with a
    spelling it does not match is not an error, it is a run with no exit rule."""
    from product.backtest.engine import _ui_exit_reason
    assert _ui_exit_reason("hold-only", 100000, 504, 0.0, 0.4) is None


# ── request validation ────────────────────────────────────────────────────────
# Without these the bad value is accepted, a job is created, 202 comes back, and
# the failure surfaces on a later poll as "Internal error" — a client mistake
# reported as a server fault, or in the exit_mode case not reported at all.

@pytest.mark.parametrize("bad", [0, -1, -504])
def test_the_api_rejects_a_nonsense_hold(bad):
    import pydantic

    from product.api.main import BacktestParams
    with pytest.raises(pydantic.ValidationError):
        BacktestParams(hold_days=bad)


def test_the_api_rejects_an_unknown_exit_mode():
    import pydantic

    from product.api.main import BacktestParams
    with pytest.raises(pydantic.ValidationError):
        BacktestParams(exit_mode="hold-only")


@pytest.mark.parametrize("mode", ["hold_only", "threshold_or_hold", "threshold_only",
                                  "252d_only", "threshold_or_252d"])
def test_the_api_still_accepts_every_supported_mode(mode):
    """Including the two legacy spellings — a cached page must not start
    failing validation on a rename the engine still understands."""
    from product.api.main import BacktestParams
    assert BacktestParams(exit_mode=mode).exit_mode == mode


@pytest.mark.parametrize("bad", [4157, 10000, 10**9])
def test_the_api_rejects_a_hold_no_window_could_complete(bad):
    """Past the simulator's widest window no run can close a single trade, so
    the result would be an empty table reading as "the strategy did nothing"."""
    import pydantic

    from product.api.main import BacktestParams
    with pytest.raises(pydantic.ValidationError):
        BacktestParams(hold_days=bad)


def test_the_ceiling_is_the_window_not_the_policy_hold():
    """Deliberately NOT capped at HOLD_TRADING_DAYS. The holding-period study
    compared 252/378/504, found the upper tail grows monotonically with holding
    time, and flagged that the 504 edge rests on 21 completed trades — so
    "does a longer hold help, or just run out of trades?" is exactly what this
    tool is for, and a cap at the policy value would forbid asking it."""
    from product.api.main import _MAX_HOLD_DAYS, BacktestParams
    assert _MAX_HOLD_DAYS > HOLD_TRADING_DAYS
    assert BacktestParams(hold_days=HOLD_TRADING_DAYS + 1).hold_days \
        == HOLD_TRADING_DAYS + 1
    assert BacktestParams(hold_days=_MAX_HOLD_DAYS).hold_days == _MAX_HOLD_DAYS
