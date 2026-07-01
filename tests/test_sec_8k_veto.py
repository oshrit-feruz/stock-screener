"""Unit tests for data/sec_8k_veto.py — the fail-closed 8-K veto layer.

All EDGAR access is mocked at the module level; no network calls. Covers the
point-in-time 90-day 8-K window, the veto item set (incl. the deliberate
exclusion of Item 5.02), the going-concern path, and the fact-only ("do not
block on missing data") posture.
"""
import json
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


# ── 90-day window boundary + multi-item / multi-filing ordering ─────────────

def test_8k_exactly_at_90_day_boundary_still_vetoes(monkeypatch):
    # window_start = as_of - 90 days; a filing dated exactly on window_start is
    # NOT < window_start, so it must still qualify (boundary is inclusive).
    _set_filings(monkeypatch, [_filing("8-K", "2023-01-01", ["1.03"])])
    blocked, _ = veto.is_vetoed("X", "2023-04-01", check_going_concern=False)
    assert blocked is True


def test_8k_one_day_past_90_day_boundary_does_not_veto(monkeypatch):
    _set_filings(monkeypatch, [_filing("8-K", "2022-12-31", ["1.03"])])
    blocked, _ = veto.is_vetoed("X", "2023-04-01", check_going_concern=False)
    assert blocked is False


def test_first_qualifying_item_in_filing_wins(monkeypatch):
    # Non-veto item listed before a veto item in the same filing: the scan
    # over `f["items"]` must still find the qualifying one.
    _set_filings(monkeypatch, [_filing("8-K", "2023-03-17", ["7.01", "1.03", "9.01"])])
    blocked, reason = veto.is_vetoed("X", "2023-03-20", check_going_concern=False)
    assert blocked is True
    assert "1.03" in reason


def test_most_recent_qualifying_8k_reported_when_multiple_present(monkeypatch):
    # Filings are sorted most-recent-first; the scan should report the newest
    # qualifying 8-K, not an older one further back in the list.
    _set_filings(monkeypatch, [
        _filing("8-K", "2023-03-01", ["4.01"]),
        _filing("8-K", "2023-01-10", ["1.03"]),
    ])
    blocked, reason = veto.is_vetoed("X", "2023-03-15", check_going_concern=False)
    assert blocked is True
    assert "4.01" in reason and "2023-03-01" in reason


def test_cybersecurity_item_105_vetoes(monkeypatch):
    _set_filings(monkeypatch, [_filing("8-K", "2024-01-19", ["1.05"])])
    blocked, reason = veto.is_vetoed("X", "2024-02-01", check_going_concern=False)
    assert blocked is True
    assert "1.05" in reason


def test_veto_8k_items_are_categorical_and_exclude_502():
    # Sanity-check the literature-derived, fitted-nothing item set itself.
    assert set(veto.VETO_8K_ITEMS) == {"1.03", "4.01", "4.02", "1.05"}
    assert all(isinstance(v, str) and v for v in veto.VETO_8K_ITEMS.values())


# ── going-concern: only the most-recent qualifying annual/quarterly filing ──

def test_going_concern_checks_only_first_qualifying_10k_10q(monkeypatch):
    # Two 10-Ks on file; the OLDER one is going-concern, the newer is clean.
    # is_vetoed must stop after the first (most recent) 10-K/10-Q as of the
    # date and not fall through to the older one.
    newer = _filing("10-K", "2023-02-01", [], accession="0001-newer")
    older = _filing("10-K", "2022-02-01", [], accession="0002-older")
    _set_filings(monkeypatch, [newer, older])

    def gc(cik, f):
        return f["accession"] == "0002-older"

    monkeypatch.setattr(veto, "_doc_has_going_concern", gc)
    blocked, _ = veto.is_vetoed("X", "2023-06-01", check_going_concern=True)
    assert blocked is False


def test_going_concern_skips_filings_dated_after_as_of(monkeypatch):
    # A 10-K filed after the as_of date must not be consulted (no look-ahead);
    # the older, clean 10-K should be used instead.
    future = _filing("10-K", "2023-08-01", [], accession="0003-future")
    past = _filing("10-K", "2022-02-01", [], accession="0004-past")
    _set_filings(monkeypatch, [future, past])
    monkeypatch.setattr(veto, "_doc_has_going_concern", lambda cik, f: True)
    blocked, reason = veto.is_vetoed("X", "2023-06-01", check_going_concern=True)
    assert blocked is True
    assert "0004-past" not in reason  # sanity: reason references the past filing's form/date
    assert "2022-02-01" in reason


def test_going_concern_not_checked_when_cik_unresolvable(monkeypatch):
    _set_filings(monkeypatch, [_filing("10-K", "2022-02-01", [])])
    monkeypatch.setattr(veto._edgar, "_get_cik", lambda ticker: None)
    monkeypatch.setattr(veto, "_doc_has_going_concern",
                        MagicMock(side_effect=AssertionError("should not be called")))
    blocked, _ = veto.is_vetoed("X", "2022-06-01", check_going_concern=True)
    assert blocked is False


# ── _normalise_recent: parallel-array parsing from the submissions API ──────

def test_normalise_recent_filters_forms_and_splits_items():
    block = {
        "form":           ["8-K", "10-K", "4", "10-Q"],
        "filingDate":      ["2023-01-01", "2023-02-01", "2023-03-01", "2023-04-01"],
        "items":           ["1.03,4.01", "", "", "2.02"],
        "accessionNumber": ["a1", "a2", "a3", "a4"],
        "primaryDocument": ["d1.htm", "d2.htm", "d3.htm", "d4.htm"],
    }
    out = veto._normalise_recent(block)
    # Form "4" (not 8-K/10-K/10-Q) must be dropped.
    assert [r["form"] for r in out] == ["8-K", "10-K", "10-Q"]
    assert out[0]["items"] == ["1.03", "4.01"]
    assert out[1]["items"] == []
    assert out[2]["accession"] == "a4"


def test_normalise_recent_handles_ragged_parallel_arrays():
    # Real submissions payloads can have arrays of differing length across
    # a merge of shards; indices beyond a short array must default safely.
    block = {
        "form": ["8-K", "8-K"],
        "filingDate": ["2023-01-01"],       # short by one
        "items": ["1.03"],                  # short by one
        "accessionNumber": [],               # empty
        "primaryDocument": ["d1.htm"],       # short by one
    }
    out = veto._normalise_recent(block)
    assert len(out) == 2
    assert out[0]["filingDate"] == "2023-01-01"
    assert out[0]["items"] == ["1.03"]
    assert out[1]["filingDate"] == ""
    assert out[1]["items"] == []
    assert out[1]["accession"] == ""
    assert out[1]["primaryDocument"] == ""


def test_normalise_recent_empty_block_returns_empty_list():
    assert veto._normalise_recent({}) == []


# ── _fetch_filings: CIK resolution + submissions merge ───────────────────────

def test_fetch_filings_returns_none_when_cik_unresolvable(monkeypatch):
    monkeypatch.setattr(veto._edgar, "_get_cik", lambda ticker: None)
    assert veto._fetch_filings("ZZZZ") is None


def test_fetch_filings_returns_none_when_submissions_unreachable(monkeypatch):
    monkeypatch.setattr(veto._edgar, "_get_cik", lambda ticker: 320193)
    monkeypatch.setattr(veto, "_fetch_json", lambda url: None)
    assert veto._fetch_filings("AAPL") is None


def test_fetch_filings_merges_recent_and_shards_sorted_desc(monkeypatch):
    monkeypatch.setattr(veto._edgar, "_get_cik", lambda ticker: 320193)

    recent_block = {
        "filings": {
            "recent": {
                "form": ["8-K"],
                "filingDate": ["2023-06-01"],
                "items": ["1.03"],
                "accessionNumber": ["a-recent"],
                "primaryDocument": ["r.htm"],
            },
            "files": [{"name": "CIK0000320193-submissions-001.json"}],
        }
    }
    shard_block = {
        "form": ["8-K"],
        "filingDate": ["2019-01-01"],
        "items": ["4.02"],
        "accessionNumber": ["a-old"],
        "primaryDocument": ["o.htm"],
    }

    def fake_fetch_json(url):
        if "submissions-001" in url:
            return shard_block
        return recent_block

    monkeypatch.setattr(veto, "_fetch_json", fake_fetch_json)
    records = veto._fetch_filings("AAPL")
    assert [r["accession"] for r in records] == ["a-recent", "a-old"]  # desc by date


def test_fetch_filings_skips_shard_with_missing_name(monkeypatch):
    monkeypatch.setattr(veto._edgar, "_get_cik", lambda ticker: 320193)
    recent_block = {
        "filings": {
            "recent": {"form": [], "filingDate": [], "items": [],
                       "accessionNumber": [], "primaryDocument": []},
            "files": [{}],   # no "name" key → must be skipped, not error
        }
    }
    monkeypatch.setattr(veto, "_fetch_json", lambda url: recent_block)
    records = veto._fetch_filings("AAPL")
    assert records == []


# ── _get_filings: in-memory memo → disk cache (TTL) → network ───────────────

def test_get_filings_uses_disk_cache_when_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    cache_file = tmp_path / f"{veto._safe_ticker('AAPL')}.json"
    cache_file.write_text(json.dumps([{"form": "8-K", "filingDate": "2023-01-01",
                                        "items": ["1.03"], "accession": "a1",
                                        "primaryDocument": "d.htm"}]))
    monkeypatch.setattr(veto, "_fetch_filings",
                        MagicMock(side_effect=AssertionError("must not hit network")))
    records = veto._get_filings("AAPL")
    assert records[0]["accession"] == "a1"


def test_get_filings_refetches_when_disk_cache_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    cache_file = tmp_path / f"{veto._safe_ticker('AAPL')}.json"
    cache_file.write_text(json.dumps([{"form": "8-K", "filingDate": "2020-01-01",
                                        "items": [], "accession": "old",
                                        "primaryDocument": "d.htm"}]))
    # Force the cache to look stale regardless of the real TTL constant.
    monkeypatch.setattr(veto, "_cache_fresh", lambda path: False)
    fresh_records = [{"form": "8-K", "filingDate": "2023-01-01", "items": ["1.03"],
                      "accession": "new", "primaryDocument": "d.htm"}]
    monkeypatch.setattr(veto, "_fetch_filings", lambda ticker: fresh_records)
    records = veto._get_filings("AAPL")
    assert records[0]["accession"] == "new"
    # And the disk cache must have been overwritten with the fresh data.
    assert json.loads(cache_file.read_text())[0]["accession"] == "new"


def test_get_filings_writes_disk_cache_on_miss(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    fresh_records = [{"form": "8-K", "filingDate": "2023-01-01", "items": ["1.03"],
                      "accession": "new", "primaryDocument": "d.htm"}]
    monkeypatch.setattr(veto, "_fetch_filings", lambda ticker: fresh_records)
    records = veto._get_filings("AAPL")
    assert records == fresh_records
    cache_file = tmp_path / f"{veto._safe_ticker('AAPL')}.json"
    assert cache_file.exists()


def test_get_filings_memoizes_in_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    calls = {"n": 0}

    def fetch(ticker):
        calls["n"] += 1
        return [{"form": "8-K", "filingDate": "2023-01-01", "items": [],
                 "accession": "x", "primaryDocument": "d.htm"}]

    monkeypatch.setattr(veto, "_fetch_filings", fetch)
    veto._get_filings("AAPL")
    veto._get_filings("AAPL")
    assert calls["n"] == 1


def test_get_filings_none_result_not_persisted_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(veto, "_fetch_filings", lambda ticker: None)
    records = veto._get_filings("ZZZZ")
    assert records is None
    cache_file = tmp_path / f"{veto._safe_ticker('ZZZZ')}.json"
    assert not cache_file.exists()


# ── prefetch_veto_cache: warms cache, tolerates per-ticker failures ──────────

def test_prefetch_veto_cache_calls_get_filings_for_each_ticker(monkeypatch):
    seen = []
    monkeypatch.setattr(veto, "_get_filings", lambda t: seen.append(t))
    veto.prefetch_veto_cache(["AAPL", "MSFT", "GOOG"], "2023-01-01")
    assert seen == ["AAPL", "MSFT", "GOOG"]


def test_prefetch_veto_cache_continues_past_exceptions(monkeypatch):
    seen = []

    def fake_get_filings(t):
        if t == "BAD":
            raise RuntimeError("network down")
        seen.append(t)

    monkeypatch.setattr(veto, "_get_filings", fake_get_filings)
    veto.prefetch_veto_cache(["AAPL", "BAD", "MSFT"], "2023-01-01")
    assert seen == ["AAPL", "MSFT"]


# ── _doc_has_going_concern: memo → disk cache → fetch + phrase matching ─────

def test_doc_has_going_concern_missing_accession_or_doc_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    assert veto._doc_has_going_concern(320193, {"accession": "", "primaryDocument": "d.htm"}) is False
    assert veto._doc_has_going_concern(320193, {"accession": "a1", "primaryDocument": ""}) is False


def test_doc_has_going_concern_requires_both_phrases(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    f = {"accession": "acc-both", "primaryDocument": "d.htm"}
    monkeypatch.setattr(veto, "_fetch_doc_text",
                        lambda cik, acc, doc: "There is substantial doubt about our ability "
                                              "to continue as a going concern.")
    assert veto._doc_has_going_concern(320193, f) is True


def test_doc_has_going_concern_boilerplate_only_does_not_trigger(monkeypatch, tmp_path):
    # Routine "prepared assuming going concern" boilerplate without "substantial
    # doubt" must NOT trigger — this is the false-positive guard.
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    f = {"accession": "acc-boiler", "primaryDocument": "d.htm"}
    monkeypatch.setattr(veto, "_fetch_doc_text",
                        lambda cik, acc, doc: "prepared assuming the Company will "
                                              "continue as a going concern.")
    assert veto._doc_has_going_concern(320193, f) is False


def test_doc_has_going_concern_fetch_failure_returns_false_and_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    f = {"accession": "acc-fail", "primaryDocument": "d.htm"}
    monkeypatch.setattr(veto, "_fetch_doc_text", lambda cik, acc, doc: None)
    assert veto._doc_has_going_concern(320193, f) is False
    gc_cache = tmp_path / f"gc_{veto._safe_ticker('acc-fail')}.json"
    assert json.loads(gc_cache.read_text())["going_concern"] is False


def test_doc_has_going_concern_uses_disk_cache_without_refetch(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    gc_cache = tmp_path / f"gc_{veto._safe_ticker('acc-cached')}.json"
    gc_cache.write_text(json.dumps({"going_concern": True}))
    monkeypatch.setattr(veto, "_fetch_doc_text",
                        MagicMock(side_effect=AssertionError("must not refetch")))
    f = {"accession": "acc-cached", "primaryDocument": "d.htm"}
    assert veto._doc_has_going_concern(320193, f) is True


def test_doc_has_going_concern_memoizes_in_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(veto, "_CACHE_DIR", tmp_path)
    calls = {"n": 0}

    def fetch(cik, acc, doc):
        calls["n"] += 1
        return "substantial doubt ... going concern"

    monkeypatch.setattr(veto, "_fetch_doc_text", fetch)
    f = {"accession": "acc-memo", "primaryDocument": "d.htm"}
    veto._doc_has_going_concern(320193, f)
    veto._doc_has_going_concern(320193, f)
    assert calls["n"] == 1


# ── _fetch_doc_text: network fetch with byte cap + failure handling ─────────

class _FakeResponse:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self._chunks


def test_fetch_doc_text_joins_chunks(monkeypatch):
    monkeypatch.setattr(veto.time, "sleep", lambda s: None)
    resp = _FakeResponse([b"hello ", b"world"])
    monkeypatch.setattr(veto.requests, "get", lambda *a, **k: resp)
    text = veto._fetch_doc_text(320193, "0000000000-00-000000", "d.htm")
    assert text == "hello world"


def test_fetch_doc_text_returns_none_on_exception(monkeypatch):
    monkeypatch.setattr(veto.time, "sleep", lambda s: None)

    def boom(*a, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr(veto.requests, "get", boom)
    assert veto._fetch_doc_text(320193, "0000000000-00-000000", "d.htm") is None


def test_fetch_doc_text_stops_at_byte_cap(monkeypatch):
    monkeypatch.setattr(veto.time, "sleep", lambda s: None)
    big_chunk = b"x" * (veto._DOC_BYTE_CAP // 2 + 1)
    # Three chunks would exceed the cap; the loop must break after the second.
    resp = _FakeResponse([big_chunk, big_chunk, big_chunk])
    monkeypatch.setattr(veto.requests, "get", lambda *a, **k: resp)
    text = veto._fetch_doc_text(320193, "0000000000-00-000000", "d.htm")
    assert len(text) == 2 * len(big_chunk)  # capped after 2 chunks, not 3
