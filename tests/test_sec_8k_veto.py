"""Unit tests for data/sec_8k_veto.py — the fail-closed 8-K veto layer.

All EDGAR access is mocked at the module level; no network calls. Covers the
point-in-time 90-day 8-K window, the veto item set (incl. the deliberate
exclusion of Item 5.02), the going-concern path, and the fact-only ("do not
block on missing data") posture.
"""
from unittest.mock import MagicMock

import pytest

import data.sec_8k_veto as veto


def _filing(form, filing_date, items, accession="0000000000-00-000000", doc="d.htm"):
    return {"form": form, "filingDate": filing_date, "items": items,
            "accession": accession, "primaryDocument": doc}


@pytest.fixture(autouse=True)
def _clear_memo():
    veto._filings_mem.clear()
    veto._gc_mem.clear()
    yield
    veto._filings_mem.clear()
    veto._gc_mem.clear()


def _set_filings(monkeypatch, records):
    monkeypatch.setattr(veto, "_get_filings", lambda ticker: list(records))
    # Give every ticker a resolvable CIK so the going-concern branch can run.
    monkeypatch.setattr(veto._edgar, "_get_cik", lambda ticker: 320193)


# ── 8-K item detection + 90-day point-in-time window ────────────────────────

def test_bankruptcy_8k_within_window_vetoes(monkeypatch):
    _set_filings(monkeypatch, [_filing("8-K", "2023-03-17", ["1.03"])])
    blocked, reason = veto.is_vetoed("X", "2023-03-20", check_going_concern=False)
    assert blocked is True
    assert "1.03" in reason and "2023-03-17" in reason


def test_8k_before_window_does_not_veto(monkeypatch):
    # Signal 95 days after the filing → outside the 90-day lookback.
    _set_filings(monkeypatch, [_filing("8-K", "2023-03-17", ["1.03"])])
    blocked, _ = veto.is_vetoed("X", "2023-06-20", check_going_concern=False)
    assert blocked is False


def test_future_8k_not_visible_point_in_time(monkeypatch):
    # A filing dated after the as_of date must not be used (no look-ahead).
    _set_filings(monkeypatch, [_filing("8-K", "2023-03-17", ["1.03"])])
    blocked, _ = veto.is_vetoed("X", "2023-03-01", check_going_concern=False)
    assert blocked is False


def test_restatement_and_auditor_change_veto(monkeypatch):
    _set_filings(monkeypatch, [_filing("8-K", "2019-05-06", ["2.02", "4.02"])])
    assert veto.is_vetoed("X", "2019-05-10", check_going_concern=False)[0] is True
    _set_filings(monkeypatch, [_filing("8-K", "2020-06-22", ["4.01", "9.01"])])
    assert veto.is_vetoed("X", "2020-07-01", check_going_concern=False)[0] is True


def test_non_veto_items_do_not_fire(monkeypatch):
    # Routine earnings / other items must never trigger the veto.
    _set_filings(monkeypatch, [_filing("8-K", "2023-03-17", ["2.02", "7.01", "9.01"])])
    assert veto.is_vetoed("X", "2023-03-20", check_going_concern=False)[0] is False


def test_item_502_ceo_turnover_is_excluded(monkeypatch):
    # 5.02 is deliberately NOT in the veto set (mixed evidence in the literature).
    assert "5.02" not in veto.VETO_8K_ITEMS
    _set_filings(monkeypatch, [_filing("8-K", "2023-03-17", ["5.02"])])
    assert veto.is_vetoed("X", "2023-03-20", check_going_concern=False)[0] is False


# ── going-concern path ──────────────────────────────────────────────────────

def test_going_concern_vetoes(monkeypatch):
    _set_filings(monkeypatch, [_filing("10-K", "2022-02-01", [])])
    monkeypatch.setattr(veto, "_doc_has_going_concern", lambda cik, f: True)
    blocked, reason = veto.is_vetoed("X", "2022-06-01", check_going_concern=True)
    assert blocked is True
    assert "going" in reason.lower()


def test_going_concern_clean_does_not_veto(monkeypatch):
    _set_filings(monkeypatch, [_filing("10-K", "2022-02-01", [])])
    monkeypatch.setattr(veto, "_doc_has_going_concern", lambda cik, f: False)
    assert veto.is_vetoed("X", "2022-06-01", check_going_concern=True)[0] is False


def test_going_concern_skipped_when_flag_false(monkeypatch):
    _set_filings(monkeypatch, [_filing("10-K", "2022-02-01", [])])
    # Would veto if consulted — but the flag is off, so it must not be called.
    monkeypatch.setattr(veto, "_doc_has_going_concern",
                        MagicMock(side_effect=AssertionError("should not be called")))
    assert veto.is_vetoed("X", "2022-06-01", check_going_concern=False)[0] is False


# ── fact-only posture (never block on missing data) ─────────────────────────

def test_unresolvable_ticker_is_unverifiable_not_blocked(monkeypatch):
    monkeypatch.setattr(veto, "_get_filings", lambda ticker: None)
    blocked, reason = veto.is_vetoed("ZZZZ", "2023-03-20")
    assert blocked is False
    assert "unverifiable" in reason


def test_clean_company_returns_empty_reason(monkeypatch):
    _set_filings(monkeypatch, [_filing("8-K", "2023-01-01", ["2.02"]),
                               _filing("10-K", "2022-02-01", [])])
    monkeypatch.setattr(veto, "_doc_has_going_concern", lambda cik, f: False)
    assert veto.is_vetoed("X", "2023-06-01") == (False, "")
