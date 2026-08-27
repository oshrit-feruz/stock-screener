"""/api/stock/{ticker}/fundamentals: real filed figures or an honest
"unavailable" — never an estimated number, never heavy compute.

Display-only and deliberately NOT point-in-time (newest filing, no 90-day
lag); the PIT interface for signals remains get_snapshot.
"""
from __future__ import annotations

import time

from datetime import date

import pytest

import product.api.main as m
from core.data.edgar import EdgarFundamentals


def _facts(vals):  # [(end, filed, val), ...]
    return {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"end": e, "filed": f, "val": v, "form": "10-K", "fp": "FY"}
        for e, f, v in vals
    ]}}}}}


@pytest.fixture
def ef(tmp_path):
    return EdgarFundamentals(cache_dir=tmp_path)


def test_report_returns_latest_annual_with_yoy(ef):
    ef._facts_mem["FAKE"] = (time.time(), _facts([
        ("2024-12-31", "2025-02-15", 1100.0),
        ("2023-12-31", "2024-02-15", 1000.0),
    ]))
    r = ef.get_revenue_report("FAKE")
    assert r == {"revenue": 1100.0, "period_end": "2024-12-31",
                 "filed": "2025-02-15", "form": "10-K", "yoy_pct": 10.0}


def test_no_lag_newest_filing_is_visible(ef):
    """A filing from last week must appear — the 90-day PIT lag is deliberately
    NOT applied on the display path."""
    recent = (date.today().replace(day=1)).isoformat()
    ef._facts_mem["FAKE"] = (time.time(), _facts([("2025-12-31", recent, 2000.0)]))
    assert ef.get_revenue_report("FAKE")["revenue"] == 2000.0


def test_yoy_is_none_not_estimated_when_prior_year_missing(ef):
    ef._facts_mem["FAKE"] = (time.time(), _facts([("2024-12-31", "2025-02-15", 1100.0)]))
    assert ef.get_revenue_report("FAKE")["yoy_pct"] is None


def test_no_facts_returns_none(ef):
    ef._facts_mem["FAKE"] = (time.time(), None)
    assert ef.get_revenue_report("FAKE") is None


# ── endpoint contract ─────────────────────────────────────────────────────────

def _with_report(monkeypatch, report):
    class _Stub:
        def get_revenue_report(self, t):
            return report
    monkeypatch.setattr(m, "_get_edgar_report_client", lambda: _Stub())


def test_endpoint_ok_shape(monkeypatch):
    _with_report(monkeypatch, {"revenue": 1100.0, "period_end": "2024-12-31",
                               "filed": "2025-02-15", "form": "10-K", "yoy_pct": 10.0})
    out = m.stock_fundamentals("aapl")
    assert out == {
        "ticker": "AAPL", "status": "ok",
        "revenue": {"value": 1100.0, "period_end": "2024-12-31", "yoy_pct": 10.0},
        "filing": {"filed": "2025-02-15", "form": "10-K"},
        "source": "SEC EDGAR companyfacts",
    }


def test_endpoint_unavailable_when_no_data(monkeypatch):
    _with_report(monkeypatch, None)
    out = m.stock_fundamentals("ZZZZ")
    assert out["status"] == "unavailable" and "reason" in out
    assert "revenue" not in out, "unavailable must carry no fabricated figures"


def test_endpoint_rejects_garbage_ticker(monkeypatch):
    _with_report(monkeypatch, {"revenue": 1.0})  # must never be reached
    for bad in ("../etc", "AAPL; DROP", "", "TOOLONGTICKER"):
        assert m.stock_fundamentals(bad)["status"] == "unavailable"


def test_endpoint_survives_client_exception(monkeypatch):
    class _Boom:
        def get_revenue_report(self, t):
            raise RuntimeError("edgar down")
    monkeypatch.setattr(m, "_get_edgar_report_client", lambda: _Boom())
    assert m.stock_fundamentals("AAPL")["status"] == "unavailable"


def test_yoy_none_when_prior_revenue_is_negative(ef):
    """CodeRabbit: revenue 100 vs prior -100 must not yield a fabricated-looking
    -200%. A non-positive prior denominator makes the ratio meaningless."""
    ef._facts_mem["FAKE"] = (time.time(), _facts([
        ("2024-12-31", "2025-02-15", 100.0),
        ("2023-12-31", "2024-02-15", -100.0),
    ]))
    assert ef.get_revenue_report("FAKE")["yoy_pct"] is None


def test_yoy_none_when_prior_revenue_is_zero(ef):
    ef._facts_mem["FAKE"] = (time.time(), _facts([
        ("2024-12-31", "2025-02-15", 100.0),
        ("2023-12-31", "2024-02-15", 0.0),
    ]))
    assert ef.get_revenue_report("FAKE")["yoy_pct"] is None


def test_memo_hit_within_ttl_is_served_without_reparsing(ef, monkeypatch):
    import core.data.edgar as ed
    ef._facts_mem["FAKE"] = (time.time(), _facts([("2024-12-31", "2025-02-15", 100.0)]))
    monkeypatch.setattr(ed, "_fetch_json", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not refetch — memo is fresh")))
    assert ef.get_revenue_report("FAKE")["revenue"] == 100.0


def test_memo_entry_past_ttl_is_not_served(ef, monkeypatch):
    """CodeRabbit: a long-lived client (the API's process-wide report client)
    must not serve a stale memory hit forever — a re-filed annual report has
    to become visible once the disk TTL has elapsed."""
    import core.data.edgar as ed
    stale_ts = time.time() - ed._CACHE_TTL_SECONDS - 1
    ef._facts_mem["FAKE"] = (stale_ts, _facts([("2023-12-31", "2024-02-15", 100.0)]))
    monkeypatch.setattr(ef, "_get_cik", lambda t: None)  # short-circuit to a clean miss
    assert ef.get_revenue_report("FAKE") is None      # fell through, did NOT use the stale memo
    refreshed_ts, refreshed_data = ef._facts_mem["FAKE"]
    assert refreshed_data is None and refreshed_ts > stale_ts, \
        "the stale entry must be replaced by a fresh lookup, not served as-is"
