"""The in-memory EDGAR facts memo must hold slices, not whole documents.

A parsed companyfacts JSON is several MB; the LRU held 32 of them, measured as
~+175MB RSS mid-backtest — half of the 512MB OOM. The memo now stores only the
(taxonomy, concept) slices this module reads. Pinned:

  * pruning preserves every value get_snapshot / get_shares_outstanding read
    (same nesting, same entries);
  * unneeded concepts are dropped and the pruned copy is small;
  * a malformed document passes through pruning untouched rather than erroring;
  * the DISK cache keeps the full document (pruning is memory-only).
"""
from __future__ import annotations

import json
from datetime import date

from core.data.edgar import EdgarFundamentals, _prune_facts


def _fake_facts() -> dict:
    entry = lambda v: {"units": {"USD": [
        {"end": "2021-12-31", "filed": "2022-02-15", "val": v, "form": "10-K", "fp": "FY"},
        {"end": "2020-12-31", "filed": "2021-02-15", "val": v * 0.9, "form": "10-K", "fp": "FY"},
    ]}}
    return {"facts": {
        "us-gaap": {
            "Revenues":             entry(1000.0),
            "NetIncomeLoss":        entry(100.0),
            "StockholdersEquity":   entry(500.0),
            "LongTermDebt":         entry(200.0),
            # ballast: the kind of thing that makes real documents huge
            **{f"IrrelevantConcept{i}": entry(float(i)) for i in range(200)},
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2021-12-31", "filed": "2022-02-15", "val": 1_000_000, "form": "10-K"},
            ]}},
            "EntityRegistrantName": {"units": {"pure": [{"val": "FAKE"}]}},
        },
    }}


def test_pruned_document_keeps_every_needed_slice():
    pruned = _prune_facts(_fake_facts())
    assert pruned["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]["val"] == 1000.0
    assert pruned["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]


def test_pruned_document_drops_ballast_and_shrinks():
    full, pruned = _fake_facts(), _prune_facts(_fake_facts())
    assert "IrrelevantConcept0" not in pruned["facts"]["us-gaap"]
    assert "EntityRegistrantName" not in pruned["facts"]["dei"]
    assert len(json.dumps(pruned)) < len(json.dumps(full)) / 10


def test_malformed_document_passes_through():
    assert _prune_facts(None) is None
    assert _prune_facts({"not_facts": 1}) == {"not_facts": 1}


def test_snapshot_and_shares_identical_on_pruned_vs_full(tmp_path):
    """The consumers must not notice pruning: same snapshot, same shares."""
    ef = EdgarFundamentals(cache_dir=tmp_path)
    as_of = date(2022, 8, 1)   # cutoff 2022-05-03 -> the 2022-02-15 filings are in

    full = _fake_facts()
    ef._facts_mem["FAKE"] = full
    snap_full = ef.get_snapshot("FAKE", as_of)
    shares_full = ef.get_shares_outstanding("FAKE", as_of)

    ef._facts_mem.clear()
    ef._facts_mem["FAKE"] = _prune_facts(_fake_facts())
    snap_pruned = ef.get_snapshot("FAKE", as_of)
    shares_pruned = ef.get_shares_outstanding("FAKE", as_of)

    assert shares_full == shares_pruned == 1_000_000
    assert (snap_full is None) == (snap_pruned is None)
    if snap_full is not None:
        assert snap_full == snap_pruned


def test_disk_cache_keeps_full_document_memo_holds_pruned(tmp_path, monkeypatch):
    import core.data.edgar as ed
    ef = EdgarFundamentals(cache_dir=tmp_path)
    monkeypatch.setattr(ed, "_fetch_json", lambda url: _fake_facts())
    monkeypatch.setattr(ef, "_get_cik", lambda t: 123)

    ef._get_facts("FAKE")
    on_disk = json.loads((tmp_path / "FAKE.json").read_text())
    assert "IrrelevantConcept0" in on_disk["facts"]["us-gaap"]      # disk: full
    assert "IrrelevantConcept0" not in ef._facts_mem["FAKE"]["facts"]["us-gaap"]  # memo: pruned


def test_release_pit_cache_drops_grid_and_reloads_lazily(tmp_path, monkeypatch):
    import data.sp500_universe as u
    monkeypatch.setattr(u, "_PIT_MCAP_DIR", tmp_path)
    monkeypatch.setattr(u, "_PIT_MCAP_FILE", tmp_path / "pit_market_caps.json")
    monkeypatch.setattr(u, "_pit_cache", {"AAPL|2026-08-03": 1.0})
    monkeypatch.setattr(u, "_pit_dirty", True)

    u.release_pit_cache()
    assert u._pit_cache is None, "grid must be dropped from memory"
    assert json.loads((tmp_path / "pit_market_caps.json").read_text()), \
        "pending entries must be flushed to disk before dropping"
    assert u._load_pit_cache() == {"AAPL|2026-08-03": 1.0}, "reload is lazy and lossless"
