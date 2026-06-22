from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from core.data.fundamentals import FundamentalSnapshot
from validation.walk_forward import WalkForwardEngine

_SNAP = FundamentalSnapshot(
    statement_date=date(2022, 9, 30),
    revenue_growth_yoy=0.05,
    debt_to_equity=1.2,
    roe=0.18,
    net_margin=0.22,
)

_PRICE_RETURN = 0.15


def _make_engine(
    get_snapshot_side_effect=None,
    get_return_side_effect=None,
    tickers=("AAPL", "MSFT"),
    snapshot_dates=("2022-12-31",),
):
    mock_fundamentals = MagicMock()
    mock_prices = MagicMock()

    if get_snapshot_side_effect is not None:
        mock_fundamentals.get_snapshot.side_effect = get_snapshot_side_effect
    else:
        mock_fundamentals.get_snapshot.return_value = _SNAP

    if get_return_side_effect is not None:
        mock_prices.get_return.side_effect = get_return_side_effect
    else:
        mock_prices.get_return.return_value = _PRICE_RETURN

    return WalkForwardEngine(
        tickers=list(tickers),
        snapshot_dates=list(snapshot_dates),
        prices=mock_prices,
        fundamentals=mock_fundamentals,
    )


def test_build_snapshot_df_basic():
    engine = _make_engine()
    df = engine.build_snapshot_df()
    assert len(df) == 2
    assert set(df["ticker"]) == {"AAPL", "MSFT"}
    assert df["forward_return_12m"].iloc[0] == pytest.approx(_PRICE_RETURN)


def test_build_snapshot_df_skips_none_fundamentals():
    """Tickers where get_snapshot returns None must be excluded."""
    def snap_side(ticker, as_of):
        return None if ticker == "MSFT" else _SNAP

    engine = _make_engine(get_snapshot_side_effect=snap_side)
    df = engine.build_snapshot_df()
    assert len(df) == 1
    assert df["ticker"].iloc[0] == "AAPL"


def test_build_snapshot_df_skips_none_forward_return():
    """Rows where forward return is None must be excluded."""
    call_count = {"n": 0}

    def return_side(ticker, start, end):
        call_count["n"] += 1
        # First call (AAPL forward return) → None; rest → value
        if call_count["n"] == 1:
            return None
        return _PRICE_RETURN

    engine = _make_engine(get_return_side_effect=return_side)
    df = engine.build_snapshot_df()
    # AAPL is dropped (forward return None), MSFT survives
    assert len(df) == 1
    assert df["ticker"].iloc[0] == "MSFT"


def test_build_snapshot_df_includes_momentum():
    """momentum_12m and momentum_6m must be present and populated."""
    engine = _make_engine()
    df = engine.build_snapshot_df()
    assert "momentum_12m" in df.columns
    assert "momentum_6m" in df.columns
    # Both should be _PRICE_RETURN (all get_return calls return same mock value)
    assert df["momentum_12m"].iloc[0] == pytest.approx(_PRICE_RETURN)
    assert df["momentum_6m"].iloc[0] == pytest.approx(_PRICE_RETURN)


def test_forward_window_dates():
    """Forward return window should start 2 days after snapshot and end 365 days after."""
    mock_fundamentals = MagicMock()
    mock_fundamentals.get_snapshot.return_value = _SNAP
    mock_prices = MagicMock()
    mock_prices.get_return.return_value = 0.10

    engine = WalkForwardEngine(
        tickers=["AAPL"],
        snapshot_dates=["2022-12-31"],
        prices=mock_prices,
        fundamentals=mock_fundamentals,
    )
    engine.build_snapshot_df()

    snap_date = date(2022, 12, 31)
    expected_fwd_start = (snap_date + timedelta(days=2)).isoformat()
    expected_fwd_end   = (snap_date + timedelta(days=365)).isoformat()

    # The first get_return call is the forward return
    calls = mock_prices.get_return.call_args_list
    fwd_call = next(c for c in calls if c.args[1] == expected_fwd_start)
    assert fwd_call.args[2] == expected_fwd_end


def test_empty_df_on_all_none():
    """If every ticker returns None snapshot, result is an empty DataFrame."""
    engine = _make_engine(get_snapshot_side_effect=lambda t, d: None)
    df = engine.build_snapshot_df()
    assert df.empty


def test_multiple_snapshot_dates():
    engine = _make_engine(
        tickers=("AAPL",),
        snapshot_dates=("2022-12-31", "2023-12-31"),
    )
    df = engine.build_snapshot_df()
    assert len(df) == 2
    assert df["snapshot_date"].nunique() == 2
