"""Unit tests for the EODHD price adapter (network-free)."""
from unittest.mock import MagicMock, patch

import pandas as pd

from core.data.eodhd import fetch_eod, normalize_ticker


def test_normalize_ticker_plain():
    assert normalize_ticker("AAPL") == "AAPL.US"
    assert normalize_ticker("spy") == "SPY.US"


def test_normalize_ticker_class_suffix_uses_dash():
    # Dotted class suffixes must become dashes (the dot form 404s on EODHD).
    assert normalize_ticker("BRK.B") == "BRK-B.US"
    assert normalize_ticker("BF.B") == "BF-B.US"


def test_normalize_ticker_already_suffixed():
    assert normalize_ticker("AAPL.US") == "AAPL.US"
    assert normalize_ticker("BRK-B.US") == "BRK-B.US"


_SAMPLE = [
    # A 2:1 split → adjusted_close is half the raw close on the pre-split bar.
    {"date": "2022-01-03", "open": 100.0, "high": 104.0, "low": 98.0,
     "close": 100.0, "adjusted_close": 50.0, "volume": 1000},
    {"date": "2022-01-04", "open": 52.0, "high": 55.0, "low": 51.0,
     "close": 54.0, "adjusted_close": 54.0, "volume": 2000},
]


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else _SAMPLE
    r.text = ""
    return r


def test_fetch_eod_adjusted_maps_adjusted_close_to_close():
    with patch.dict("os.environ", {"EODHD_API_KEY": "k"}), \
         patch("core.data.eodhd.requests.get", return_value=_resp()):
        df = fetch_eod("AAPL", "2022-01-03", "2022-01-04", adjust=True)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    # Close == adjusted_close.
    assert df["Close"].iloc[0] == 50.0
    # OHL scaled by adjusted_close/close = 0.5 on the pre-split bar.
    assert df["Open"].iloc[0] == 50.0
    assert df["High"].iloc[0] == 52.0
    assert df["Volume"].iloc[0] == 1000
    # Index is a naive DatetimeIndex, ascending.
    assert df.index[0] == pd.Timestamp("2022-01-03")
    assert df.index.is_monotonic_increasing


def test_fetch_eod_raw_keeps_unadjusted():
    with patch.dict("os.environ", {"EODHD_API_KEY": "k"}), \
         patch("core.data.eodhd.requests.get", return_value=_resp()):
        df = fetch_eod("AAPL", "2022-01-03", "2022-01-04", adjust=False)
    # Raw close is untouched (no adjustment factor applied).
    assert df["Close"].iloc[0] == 100.0
    assert df["Open"].iloc[0] == 100.0


def test_fetch_eod_missing_key_returns_empty(monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    df = fetch_eod("AAPL", "2022-01-03", "2022-01-04")
    assert df.empty


def test_fetch_eod_http_error_returns_empty():
    with patch.dict("os.environ", {"EODHD_API_KEY": "k"}), \
         patch("core.data.eodhd.requests.get", return_value=_resp(status=404)):
        df = fetch_eod("BRK.B", "2022-01-03", "2022-01-04")
    assert df.empty


def test_fetch_eod_empty_payload_returns_empty():
    with patch.dict("os.environ", {"EODHD_API_KEY": "k"}), \
         patch("core.data.eodhd.requests.get", return_value=_resp(payload=[])):
        df = fetch_eod("AAPL", "2022-01-03", "2022-01-04")
    assert df.empty


def test_fetch_eod_network_exception_returns_empty():
    with patch.dict("os.environ", {"EODHD_API_KEY": "k"}), \
         patch("core.data.eodhd.requests.get", side_effect=Exception("tls")):
        df = fetch_eod("AAPL", "2022-01-03", "2022-01-04")
    assert df.empty
