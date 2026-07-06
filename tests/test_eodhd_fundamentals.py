"""Unit tests for the EODHD fundamentals fallback adapter (network-free)."""
from datetime import date
from unittest.mock import patch

from core.data.eodhd_fundamentals import EODHDFundamentals

# Minimal EODHD-shaped payload: two fiscal years so growth is computable.
_RAW = {
    "Financials": {
        "Income_Statement": {"yearly": {
            "2020-09-30": {"date": "2020-09-30", "filing_date": "2020-10-30",
                           "totalRevenue": "100.0", "netIncome": "20.0"},
            "2021-09-30": {"date": "2021-09-30", "filing_date": "2021-10-29",
                           "totalRevenue": "133.0", "netIncome": "34.0"},
        }},
        "Balance_Sheet": {"yearly": {
            "2020-09-30": {"date": "2020-09-30", "filing_date": "2020-10-30",
                           "totalStockholderEquity": "50.0", "longTermDebt": "80.0"},
            "2021-09-30": {"date": "2021-09-30", "filing_date": "2021-10-29",
                           "totalStockholderEquity": "63.0", "longTermDebt": "109.0"},
        }},
    }
}


def _adapter(tmp_path, raw):
    ef = EODHDFundamentals(cache_dir=tmp_path)
    return patch.object(ef, "_fetch", return_value=raw), ef


def test_process_field_mapping():
    snaps = EODHDFundamentals._process(_RAW)
    assert len(snaps) == 2
    fy21 = snaps[-1]
    assert fy21["statement_date"] == "2021-09-30"
    assert fy21["filed_date"] == "2021-10-29"
    assert round(fy21["revenue_growth_yoy"], 3) == 0.33   # (133-100)/100
    assert round(fy21["debt_to_equity"], 3) == round(109 / 63, 3)
    assert round(fy21["net_margin"], 3) == round(34 / 133, 3)
    assert round(fy21["roe"], 3) == round(34 / 63, 3)


def test_point_in_time_no_lookahead(tmp_path):
    p, ef = _adapter(tmp_path, _RAW)
    with p:
        # Before FY2021 was filed → only FY2020 is visible.
        snap = ef.get_snapshot("AAPL", date(2021, 6, 30))
    assert snap.statement_date == date(2020, 9, 30)
    assert snap.filed_date == date(2020, 10, 30)


def test_point_in_time_latest_filed(tmp_path):
    p, ef = _adapter(tmp_path, _RAW)
    with p:
        snap = ef.get_snapshot("AAPL", date(2022, 1, 1))
    assert snap.statement_date == date(2021, 9, 30)  # FY2021 now filed


def test_403_returns_none_failclosed(tmp_path):
    ef = EODHDFundamentals(cache_dir=tmp_path)
    with patch.object(ef, "_fetch", return_value=None):
        assert ef.get_snapshot("ZZZ", date(2022, 1, 1)) is None


def test_negative_result_memoized(tmp_path):
    ef = EODHDFundamentals(cache_dir=tmp_path)
    with patch.object(ef, "_fetch", return_value=None) as m:
        ef.get_snapshot("ZZZ", date(2022, 1, 1))
        ef.get_snapshot("ZZZ", date(2023, 1, 1))
    assert m.call_count == 1  # fetched once, memoized None thereafter


def test_missing_key_no_request(tmp_path, monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    ef = EODHDFundamentals(cache_dir=tmp_path)
    with patch("core.data.eodhd_fundamentals.requests.get") as g:
        assert ef.get_snapshot("AAPL", date(2022, 1, 1)) is None
        g.assert_not_called()
