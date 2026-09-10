"""Manual `active` overrides: the store, and the merge onto a screener payload.

Two properties carry the design and are what these tests pin:

  * the policy value survives an override. `active_policy` keeps reporting what
    satellite_policy.is_active() computed, and `active_source` says which of the
    two the served `active` came from. Overwriting the computed field in place
    would make a human decision indistinguishable from the gate's a day later.
  * an unreadable or unreachable store degrades to the policy value rather than
    taking the screener endpoint down. Serving the gate's own answer is the safe
    direction — it is what the validated strategy says.

The file backend is forced throughout: these tests must never reach a real
Supabase project, even on a developer machine that has credentials exported.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The override store, backed by a file under tmp_path."""
    import product.storage.active_overrides as overrides
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setattr(overrides, "_FILE", tmp_path / "active_overrides.json")
    return overrides


# ── the store ───────────────────────────────────────────────────────────────

def test_backend_is_files_without_credentials(store):
    assert store.backend() == "files"


def test_set_then_load_round_trips(store):
    row = store.set_override("nvda", True)
    assert row["ticker"] == "NVDA", "tickers are normalised to upper case"
    assert row["active"] is True
    assert store.load() == {"NVDA": row}


def test_load_is_empty_when_nothing_is_stored(store):
    assert store.load() == {}


def test_setting_the_same_ticker_replaces_rather_than_accumulates(store):
    store.set_override("NVDA", True)
    store.set_override("NVDA", False)
    loaded = store.load()
    assert list(loaded) == ["NVDA"]
    assert loaded["NVDA"]["active"] is False


def test_clear_reports_whether_there_was_anything_to_clear(store):
    store.set_override("NVDA", True)
    assert store.clear_override("NVDA") is True
    assert store.clear_override("NVDA") is False, \
        "clearing nothing must not report a cleared override"
    assert store.load() == {}


def test_clearing_one_ticker_leaves_the_others(store):
    store.set_override("NVDA", True)
    store.set_override("AAPL", False)
    store.clear_override("NVDA")
    assert list(store.load()) == ["AAPL"]


@pytest.mark.parametrize("bad", ["", "bad ticker!", "A" * 20, "NVDA;DROP", "a&b=c"])
def test_a_ticker_that_is_not_a_symbol_is_refused(store, bad):
    """The ticker reaches a PostgREST filter on the Supabase backend, so a value
    that could steer one is rejected before it is ever stored."""
    with pytest.raises(ValueError):
        store.set_override(bad, True)
    with pytest.raises(ValueError):
        store.clear_override(bad)


@pytest.mark.parametrize("bad", ["true", 1, 0, None, "", [], {}])
def test_active_must_be_a_real_bool(store, bad):
    """A truthy string would flip a signal to actionable on the strength of a
    typo, so the type is checked rather than coerced."""
    with pytest.raises(ValueError):
        store.set_override("NVDA", bad)


def test_unreadable_rows_are_dropped_not_raised_on(store):
    """One hand-edited or half-written row must not take the endpoint down."""
    store._FILE.parent.mkdir(parents=True, exist_ok=True)
    store._FILE.write_text(json.dumps([
        {"ticker": "NVDA", "active": True, "set_at": "2026-09-10T00:00:00+00:00"},
        {"ticker": "AAPL", "active": "yes"},      # not a bool
        {"ticker": "not a ticker", "active": True},
        "a string, not a row",
        {"active": True},                          # no ticker
    ]))
    assert list(store.load()) == ["NVDA"]


def test_a_corrupt_file_reads_as_empty(store):
    store._FILE.parent.mkdir(parents=True, exist_ok=True)
    store._FILE.write_text("{ not json")
    assert store.load() == {}


# ── the merge onto a payload ────────────────────────────────────────────────

def _payload():
    return {
        "as_of": "2026-09-10",
        "buy_signals":  [{"ticker": "NVDA", "active": False},
                         {"ticker": "AAPL", "active": True}],
        "full_ranking": [{"ticker": "NVDA", "active": False},
                         {"ticker": "AAPL", "active": True},
                         {"ticker": "MSFT", "active": None}],
    }


@pytest.fixture
def api(store, monkeypatch):
    """product.api.main with its override store pointed at the test file."""
    import product.api.main as main
    monkeypatch.setattr(main, "active_store", store)
    return main


def test_every_row_carries_its_provenance_even_with_no_overrides(api):
    out = api._apply_active_overrides(_payload())
    for row in out["buy_signals"] + out["full_ranking"]:
        assert row["active_source"] == "policy"
        assert row["active_policy"] == row["active"]
        assert "active_set_at" not in row


def test_an_override_replaces_active_but_never_active_policy(api, store):
    store.set_override("NVDA", True)
    row = api._apply_active_overrides(_payload())["buy_signals"][0]
    assert row["ticker"] == "NVDA"
    assert row["active"] is True,          "the served value is the override"
    assert row["active_policy"] is False,  "the gate's answer is still published"
    assert row["active_source"] == "override"
    assert row["active_set_at"], "an override says when it was made, so a stale one shows"


def test_an_override_applies_to_both_lists(api, store):
    store.set_override("NVDA", True)
    out = api._apply_active_overrides(_payload())
    assert out["buy_signals"][0]["active"] is True
    assert out["full_ranking"][0]["active"] is True


def test_rows_without_an_override_are_untouched(api, store):
    store.set_override("NVDA", True)
    aapl = api._apply_active_overrides(_payload())["buy_signals"][1]
    assert aapl["ticker"] == "AAPL"
    assert aapl["active"] is True
    assert aapl["active_source"] == "policy"


def test_an_override_can_set_a_null_regime_row(api, store):
    """`active` is null when the regime is unknown — a third state, not a false.
    An override is still allowed to resolve it."""
    store.set_override("MSFT", True)
    msft = api._apply_active_overrides(_payload())["full_ranking"][2]
    assert msft["active"] is True
    assert msft["active_policy"] is None


def test_the_rest_of_the_payload_passes_through(api, store):
    store.set_override("NVDA", True)
    out = api._apply_active_overrides(_payload())
    assert out["as_of"] == "2026-09-10"


def test_a_warming_payload_is_returned_unchanged(api):
    """It carries no rows; touching it would invent a shape the client does not
    expect while the scan is still running."""
    assert api._apply_active_overrides({"warming": True}) == {"warming": True}


def test_a_store_failure_serves_the_policy_values(api, monkeypatch):
    """The screener must not go down because the override store is unreachable,
    and the value it falls back to is the gate's own."""
    def boom():
        raise RuntimeError("Supabase is unreachable")
    monkeypatch.setattr(api.active_store, "load", boom)

    out = api._apply_active_overrides(_payload())
    assert out["buy_signals"][0]["active"] is False
    assert out["buy_signals"][0]["active_source"] == "policy"
