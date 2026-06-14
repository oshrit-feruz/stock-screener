from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from core.data.edgar import EdgarFundamentals, _annual_entries, _best_entry
from core.data.fundamentals import FundamentalSnapshot


def _make_facts(concept: str, entries: list[dict]) -> dict:
    return {"facts": {"us-gaap": {concept: {"units": {"USD": entries}}}}}


def _fy_entry(fy: int, val: float, end: str, filed: str) -> dict:
    return {"fy": fy, "val": val, "end": end, "filed": filed, "form": "10-K", "fp": "FY"}


# ── _annual_entries / _best_entry helpers ─────────────────────────────────────

def test_annual_entries_deduplicates_by_end_date():
    """When two 10-K entries share the same period-end date, keep the latest filed."""
    facts = _make_facts("Revenues", [
        _fy_entry(2022, 100.0, "2022-09-24", "2022-10-28"),
        _fy_entry(2022, 101.0, "2022-09-24", "2023-01-05"),  # amended — filed later, same end
    ])
    entries = _annual_entries(facts, "Revenues")
    assert len(entries) == 1
    assert entries[0]["val"] == 101.0


def test_best_entry_respects_cutoff():
    """Entry filed after cutoff must be excluded."""
    entries = [
        _fy_entry(2022, 200.0, "2022-09-24", "2023-01-15"),  # filed after cutoff
        _fy_entry(2021, 180.0, "2021-09-25", "2021-11-05"),  # filed before cutoff
    ]
    cutoff = date(2022, 10, 2)  # 2022-12-31 - 90 days
    result = _best_entry(entries, cutoff)
    assert result is not None
    assert result["fy"] == 2021


# ── EdgarFundamentals.get_snapshot ───────────────────────────────────────────

def test_point_in_time_uses_filed_date(tmp_path):
    """A filing filed after (as_of - 90d) must be excluded even if the period ended earlier."""
    # as_of = 2022-12-31 → cutoff = 2022-10-02
    # Only filing has filed = 2022-12-01, which is AFTER cutoff
    facts = _make_facts("Revenues", [
        _fy_entry(2022, 300e9, "2022-09-24", "2022-12-01"),
    ])

    edgar = EdgarFundamentals(cache_dir=tmp_path, fallback=None)
    with patch.object(edgar, "_get_facts", return_value=facts):
        result = edgar.get_snapshot("AAPL", date(2022, 12, 31))

    assert result is None


def test_revenue_growth_computed_correctly(tmp_path):
    """Two consecutive FY filings → correct YoY growth.

    Use as_of=2023-12-31 (cutoff=2023-10-02) so that both FY2022 (filed 2022-11-05)
    and FY2021 (filed 2021-11-04) are available before the cutoff.
    """
    facts = _make_facts("Revenues", [
        _fy_entry(2022, 120.0, "2022-09-24", "2022-11-05"),
        _fy_entry(2021, 100.0, "2021-09-25", "2021-11-04"),
    ])

    edgar = EdgarFundamentals(cache_dir=tmp_path, fallback=None)
    with patch.object(edgar, "_get_facts", return_value=facts):
        snap = edgar.get_snapshot("AAPL", date(2023, 12, 31))

    assert snap is not None
    assert snap.revenue_growth_yoy == pytest.approx(0.20, rel=1e-6)


def test_fallback_to_yfinance(tmp_path):
    """When EDGAR has no facts, fallback's get_snapshot is called."""
    fallback_snap = FundamentalSnapshot(
        statement_date=date(2022, 9, 30),
        revenue_growth_yoy=0.05,
        debt_to_equity=1.2,
        roe=0.18,
        net_margin=0.22,
    )
    mock_fallback = MagicMock()
    mock_fallback.get_snapshot.return_value = fallback_snap

    edgar = EdgarFundamentals(cache_dir=tmp_path, fallback=mock_fallback)
    with patch.object(edgar, "_get_facts", return_value=None):
        result = edgar.get_snapshot("XYZ", date(2022, 12, 31))

    assert result is fallback_snap
    mock_fallback.get_snapshot.assert_called_once_with("XYZ", date(2022, 12, 31))


def test_rate_limit_sleep(tmp_path):
    """_fetch_json sleeps 0.12s before every HTTP request."""
    minimal_response = MagicMock()
    minimal_response.json.return_value = {}
    minimal_response.raise_for_status.return_value = None

    with patch("core.data.edgar.time.sleep") as mock_sleep, \
         patch("core.data.edgar.requests.get", return_value=minimal_response):
        from core.data.edgar import _fetch_json
        _fetch_json("https://example.com/a")
        _fetch_json("https://example.com/b")

    assert mock_sleep.call_count >= 2
    for call in mock_sleep.call_args_list:
        assert call.args[0] == pytest.approx(0.12)
