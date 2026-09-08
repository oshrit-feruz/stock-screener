"""The position book must survive a restart and be the same book on both sides.

The bug these cover: open_positions.json lived on whichever machine wrote it.
The daily run (GitHub Actions) and the web service (Render) each had their own
copy, so a position opened in the app was invisible to the exit tracker and
was gone at the next restart. Both halves now read one Postgres table.

The Supabase tests drive the REST layer with a fake transport rather than a
live project: the service key is a production secret that must not be in a test
environment, and asserting on the request we would have sent is what actually
pins the contract (URL, filters, headers, how failures map).
"""
from __future__ import annotations

import json
from datetime import date

import pytest
import requests

from product.storage import positions as store

_ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Never touch the real book, and never inherit a developer's credentials."""
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(store, "_OPEN_FILE", tmp_path / "open_positions.json")
    monkeypatch.setattr(store, "_CLOSED_FILE", tmp_path / "closed_positions.json")


def _configure(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")


class _Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    @property
    def content(self):
        return b"x" if self._payload is not None else b""

    def json(self):
        return self._payload


# ── which backend is live ───────────────────────────────────────────────────

def test_without_credentials_the_book_is_the_local_files():
    assert store.backend() == "files"


def test_with_credentials_the_book_is_supabase(monkeypatch):
    _configure(monkeypatch)
    assert store.backend() == "supabase"


def test_a_half_configured_environment_is_not_treated_as_supabase(monkeypatch):
    """URL but no key (or the reverse) must not be read as 'Supabase is on'.

    Otherwise a partial deploy would send every request unauthenticated and the
    failures would look like a Supabase outage rather than a missing variable.
    """
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    assert store.backend() == "files"
    monkeypatch.delenv("SUPABASE_URL")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")
    assert store.backend() == "files"


# ── the file backend, which is what tests and local development run on ──────

def test_a_position_opens_and_reads_back():
    assert store.open_position("AAPL", date(2026, 1, 5), 100.0) is True
    rows = store.load_open()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["entry_date"] == "2026-01-05"
    assert rows[0]["reminder_sent"] is False


def test_opening_the_same_position_twice_is_a_no_op():
    """The daily run can fire the same signal twice (a rerun, a retry). The
    second must not create a second position in the book."""
    store.open_position("AAPL", date(2026, 1, 5), 100.0)
    assert store.open_position("AAPL", date(2026, 1, 5), 999.0) is False
    rows = store.load_open()
    assert len(rows) == 1
    assert rows[0]["entry_price"] == 100.0, "the first open wins; it is not overwritten"


def test_the_same_ticker_on_a_different_date_is_a_separate_position():
    store.open_position("AAPL", date(2026, 1, 5), 100.0)
    store.open_position("AAPL", date(2026, 6, 1), 120.0)
    assert len(store.load_open()) == 2


def test_closing_moves_a_position_out_of_the_open_book():
    store.open_position("AAPL", date(2026, 1, 5), 100.0)
    assert store.close_position("AAPL", date(2026, 1, 5), date(2026, 2, 1),
                                120.0, 0.2, 19) is True
    assert store.load_open() == []
    closed = store.load_closed()
    assert len(closed) == 1
    assert closed[0]["exit_price"] == 120.0
    assert closed[0]["realized_return"] == 0.2
    assert closed[0]["days_held"] == 19


def test_closing_something_that_is_not_open_reports_that():
    assert store.close_position("NOPE", date(2026, 1, 5), date(2026, 2, 1),
                                1.0, 0.0, 1) is False


def test_the_reminder_flag_persists():
    """It is written down rather than recomputed so a skipped run -- weekend,
    holiday, outage -- cannot cause the 30-day notice to be missed or repeated."""
    store.open_position("AAPL", date(2026, 1, 5), 100.0)
    store.mark_reminder_sent("AAPL", date(2026, 1, 5))
    assert store.load_open()[0]["reminder_sent"] is True


def test_count_open_matches_the_book():
    assert store.count_open() == 0
    store.open_position("AAPL", date(2026, 1, 5), 100.0)
    store.open_position("MSFT", date(2026, 1, 6), 200.0)
    store.close_position("AAPL", date(2026, 1, 5), date(2026, 2, 1), 120.0, 0.2, 19)
    assert store.count_open() == 1


def test_a_corrupt_file_reads_as_empty_rather_than_crashing(monkeypatch, tmp_path):
    """A truncated file must not take the whole web service down; the panel
    shows nothing and the run continues."""
    bad = tmp_path / "open_positions.json"
    bad.write_text("{not json")
    monkeypatch.setattr(store, "_OPEN_FILE", bad)
    assert store.load_open() == []


def test_a_write_cannot_truncate_the_book(monkeypatch):
    """Writes go through a temp file and a rename, so a crash mid-write leaves
    the previous book intact instead of a half-written one."""
    from pathlib import Path

    store.open_position("AAPL", date(2026, 1, 5), 100.0)
    original = Path.replace

    def boom(self, target):
        raise OSError("disk died mid-rename")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        store.open_position("MSFT", date(2026, 1, 6), 200.0)
    monkeypatch.setattr(Path, "replace", original)

    rows = store.load_open()
    assert [r["ticker"] for r in rows] == ["AAPL"], "the existing book survived"


# ── the Supabase backend ────────────────────────────────────────────────────

def test_reads_ask_supabase_for_the_open_rows(monkeypatch):
    seen = {}

    def fake(method, url, **kw):
        seen["method"], seen["url"], seen["headers"] = method, url, kw["headers"]
        return _Resp(payload=[{"ticker": "AAPL", "entry_date": "2026-01-05",
                               "entry_price": 100.0, "signal_composite": None,
                               "signal_drawdown": None, "reminder_sent": False}])

    _configure(monkeypatch)
    monkeypatch.setattr(requests, "request", fake)

    rows = store.load_open()
    assert rows[0]["ticker"] == "AAPL"
    assert seen["method"] == "GET"
    assert "bot_positions" in seen["url"]
    assert "status=eq.open" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer service-key"
    assert seen["headers"]["apikey"] == "service-key"


def test_a_duplicate_open_is_the_database_saying_no_not_an_error(monkeypatch):
    """The unique constraint is what makes opening idempotent under a race.
    Postgres reports 23505; that means 'already recorded', not 'failed'."""
    _configure(monkeypatch)
    monkeypatch.setattr(requests, "request",
                        lambda *a, **k: _Resp(409, {"code": "23505"}))
    assert store.open_position("AAPL", date(2026, 1, 5), 100.0) is False


def test_closing_filters_on_the_row_being_open(monkeypatch):
    """Scoped to status=eq.open so a re-sent close cannot rewrite the exit price
    of a position that already closed."""
    seen = {}

    def fake(method, url, **kw):
        seen["method"], seen["url"] = method, url
        return _Resp(payload=[{"ticker": "AAPL"}])

    _configure(monkeypatch)
    monkeypatch.setattr(requests, "request", fake)

    assert store.close_position("AAPL", date(2026, 1, 5), date(2026, 2, 1),
                                120.0, 0.2, 19) is True
    assert seen["method"] == "PATCH"
    assert "status=eq.open" in seen["url"]
    assert "ticker=eq.AAPL" in seen["url"]
    assert "entry_date=eq.2026-01-05" in seen["url"]


def test_closing_a_row_that_was_already_closed_reports_false(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(payload=[]))
    assert store.close_position("AAPL", date(2026, 1, 5), date(2026, 2, 1),
                                120.0, 0.2, 19) is False


# ── the property that matters most ──────────────────────────────────────────

def _kill_the_network(monkeypatch):
    _configure(monkeypatch)

    def dead(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "request", dead)


def test_a_read_against_an_unreachable_supabase_raises(monkeypatch):
    _kill_the_network(monkeypatch)
    with pytest.raises(store.StorageError):
        store.load_open()


def test_a_write_against_an_unreachable_supabase_raises(monkeypatch):
    _kill_the_network(monkeypatch)
    with pytest.raises(store.StorageError):
        store.open_position("AAPL", date(2026, 1, 5), 100.0)


def test_an_outage_leaves_nothing_in_the_fallback_file(monkeypatch):
    """This is the whole point of the change.

    Falling back to the local file when the database is unreachable would
    recreate the split book silently -- the app would report success, the row
    would sit on a container disk nobody else reads, and it would be gone at the
    next restart. A loud failure can be retried; a silent one cannot be noticed.
    """
    _kill_the_network(monkeypatch)
    with pytest.raises(store.StorageError):
        store.open_position("AAPL", date(2026, 1, 5), 100.0)
    assert not store._OPEN_FILE.exists(), \
        "an outage must not leave a position written to the local fallback file"


# ── input validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "AAPL&limit=1&or=(status.eq.closed)",  # extra PostgREST params
    "AAPL&status=eq.closed",               # redirect the filter at other rows
    "AAPL\nJan 01 forged log line",        # forge a line in the run log
    "../../etc/passwd",
    "*",
    "",
    "A" * 20,
])
def test_a_ticker_that_is_not_a_symbol_is_refused(bad):
    """`ticker=eq.<value>` is a URL query and the ticker also reaches the log,
    so a free-form string could append filter parameters of its own choosing --
    widening an UPDATE past the row it was meant to touch -- or forge log lines.
    Rejected at the store, not left to whatever validation a caller happens to
    have."""
    with pytest.raises(ValueError):
        store.open_position(bad, date(2026, 1, 5), 100.0)


@pytest.mark.parametrize("raw,expected", [("aapl", "AAPL"), ("  msft  ", "MSFT"),
                                          ("BRK.B", "BRK.B"), ("RDS-A", "RDS-A")])
def test_real_symbols_survive_normalisation(raw, expected):
    """Class shares and preferreds carry a dot or hyphen; the guard must not
    reject them, and case is normalised rather than refused."""
    store.open_position(raw, date(2026, 1, 5), 100.0)
    assert store.load_open()[0]["ticker"] == expected


def test_closing_also_validates_the_ticker():
    with pytest.raises(ValueError):
        store.close_position("AAPL&status=eq.closed", date(2026, 1, 5),
                             date(2026, 2, 1), 1.0, 0.0, 1)


def test_count_open_reads_the_total_from_the_range_header(monkeypatch):
    """Asks for no rows and reads the count out of Content-Range, so the daily
    summary line does not pull the whole book across the wire."""
    seen = {}

    def fake_get(url, **kw):
        seen["url"], seen["headers"] = url, kw["headers"]
        return _Resp(206, payload=[], headers={"Content-Range": "0-0/7"})

    _configure(monkeypatch)
    monkeypatch.setattr(requests, "get", fake_get)

    assert store.count_open() == 7
    assert seen["headers"]["Prefer"] == "count=exact"
    assert seen["headers"]["Range"] == "0-0"
    assert "status=eq.open" in seen["url"]


def test_count_open_of_an_empty_book_is_zero(monkeypatch):
    """PostgREST answers '*/0' when nothing matches; that must not parse as a
    crash or as a wrong non-zero count."""
    _configure(monkeypatch)
    monkeypatch.setattr(requests, "get",
                        lambda url, **kw: _Resp(200, payload=[],
                                                headers={"Content-Range": "*/0"}))
    assert store.count_open() == 0


def test_an_http_error_from_supabase_raises_with_the_reason(monkeypatch):
    """PostgREST puts the cause in the body (constraint name, RLS denial).
    Losing it would turn every storage problem into an unexplained 500."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        requests, "request",
        lambda *a, **k: _Resp(401, {"message": "invalid authentication credentials"}))
    with pytest.raises(store.StorageError, match="invalid authentication"):
        store.load_open()
