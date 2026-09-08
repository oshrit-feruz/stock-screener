"""Raw-price freshness contract for the monthly universe build.

Presence is not freshness. The Actions cache carries data/cache between runs, so
last month's raw-price file still exists this month — and data.sp500_universe.
_raw_close() would happily return last month's close, drifting the ranking
further out of date every month with no error.

Two behaviours are pinned here:
  1. a cached frame that does not reach the as-of date is treated as stale;
  2. a refresh REPLACES the ticker's file rather than adding a second one —
     load-bearing, because data.sp500_universe._raw_frame() reads the
     deepest-START candidate (under either the safe `BRKB_*` or legacy
     `BRK.B_*` spelling), so an accumulated old file would keep winning and
     the refresh would silently have no effect.
"""
from __future__ import annotations

import json
import pickle
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import scripts.build_universe_list as bul
from scripts.build_universe_list import (
    _DELIST_GRACE_DAYS,
    _atomic_write_pickle,
    _classify_stale,
    _covers,
    _ensure_raw_prices,
    _frame_reaches,
    _refresh_start,
    _ticker_is_current,
)

_AS_OF = date(2026, 9, 1)


def _frame(last_day: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(["2026-07-01", last_day]))
    return pd.DataFrame({"Close": [100.0, 110.0]}, index=idx)


def _pickle(tmp_path: Path, name: str, frame) -> Path:
    p = tmp_path / name
    with open(p, "wb") as f:
        pickle.dump(frame, f)
    return p


# ── _covers ───────────────────────────────────────────────────────────────────

def test_frame_reaching_as_of_is_current(tmp_path):
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", _frame("2026-09-01")), _AS_OF) is True


def test_frame_past_as_of_is_current(tmp_path):
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", _frame("2026-09-04")), _AS_OF) is True


def test_last_months_frame_is_stale(tmp_path):
    """The regression: a file fetched last month must NOT count as cached."""
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", _frame("2026-08-03")), _AS_OF) is False


def test_empty_frame_is_stale(tmp_path):
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    assert _covers(_pickle(tmp_path, "AAPL_x.pkl", empty), _AS_OF) is False


def test_unreadable_file_is_stale(tmp_path):
    p = tmp_path / "AAPL_x.pkl"
    p.write_bytes(b"not a pickle")
    assert _covers(p, _AS_OF) is False


def test_missing_file_is_stale(tmp_path):
    assert _covers(tmp_path / "nope.pkl", _AS_OF) is False


# ── two-month scenario ────────────────────────────────────────────────────────

def test_second_month_sees_last_months_file_as_stale(tmp_path):
    """Month 1 writes a file; month 2 must classify it stale and refresh it."""
    m1 = _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    assert _covers(m1, date(2026, 8, 3)) is True     # current in month 1
    assert _covers(m1, date(2026, 9, 1)) is False    # stale in month 2


def test_replacement_leaves_exactly_one_file_so_raw_close_reads_the_fresh_one(tmp_path):
    """_raw_close picks sorted(glob(...))[0] — the earliest start. If a refresh
    added a file instead of replacing, that stale earliest file would still win."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))

    # What _ensure_raw_prices does on refresh: unlink existing, then write.
    for old in tmp_path.glob("AAPL_*.pkl"):
        old.unlink()
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))

    files = sorted(tmp_path.glob("AAPL_*.pkl"))
    assert len(files) == 1, "refresh must replace, not accumulate"
    assert _covers(files[0], _AS_OF) is True


# ── _ticker_is_current: judged on the file _raw_close actually reads ──────────

def test_ticker_with_no_files_is_not_current(tmp_path):
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_ticker_with_single_fresh_file_is_current(tmp_path):
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is True


def test_ticker_with_single_stale_file_is_not_current(tmp_path):
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_ticker_with_stale_AND_fresh_file_is_not_current(tmp_path):
    """The regression an `any(covers)` test would miss: a fresh file exists, but
    _raw_close reads the earliest-start one, which is stale. Must refresh."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))   # earliest -> chosen
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))   # fresh but ignored
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_other_tickers_do_not_leak_into_the_decision(tmp_path):
    _pickle(tmp_path, "MSFT_2025-09-01.pkl", _frame("2026-09-01"))
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is False


def test_dotted_ticker_deep_safe_file_beats_shallow_legacy_file(tmp_path):
    """Both naming contracts coexist for a dotted ticker (BRKB_* from the
    clean-universe fetch, BRK.B_* from this builder). The chosen file must be
    the deepest-START one regardless of spelling — never lexical path order,
    which would let a shallow legacy file shadow a deep safe-name one."""
    _pickle(tmp_path, "BRK.B_2025-09-01.pkl", _frame("2026-09-01"))     # shallow, fresh
    _pickle(tmp_path, "BRKB_1998-06-01_2024-12-31.pkl", _frame("2026-08-03"))  # deep, stale
    chosen = bul.u._raw_candidates("BRK.B", tmp_path)[0]
    assert chosen.name == "BRKB_1998-06-01_2024-12-31.pkl"
    assert _ticker_is_current(tmp_path, "BRK.B", _AS_OF) is False   # judged on the chosen file
    assert _refresh_start(tmp_path, "BRK.B", "2025-09-01") == "1998-06-01"


def test_accumulating_would_have_regressed(tmp_path):
    """Pins WHY replacement matters: with both files present, the earliest-start
    file _raw_close would choose is the stale one."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-09-01"))

    chosen = sorted(tmp_path.glob("AAPL_*.pkl"))[0]   # _raw_close's rule
    assert chosen.name == "AAPL_2025-08-01.pkl"
    assert _covers(chosen, _AS_OF) is False           # ...and it is stale


# ── refresh must never truncate existing history ──────────────────────────────

def test_refresh_start_keeps_deep_history(tmp_path):
    """build_full_cache.py writes deep files (2009-) that the PIT grid rebuild
    needs. A monthly refresh replaces the ticker's file, so it must refetch from
    at least as far back — otherwise history is silently truncated and
    build_full_cache's 'already have a file?' check skips restoring it."""
    (tmp_path / "AAPL_2009-01-01.pkl").write_bytes(b"x")
    assert _refresh_start(tmp_path, "AAPL", "2025-09-01") == "2009-01-01"


def test_refresh_start_uses_default_when_no_deeper_history(tmp_path):
    (tmp_path / "AAPL_2025-09-01.pkl").write_bytes(b"x")
    assert _refresh_start(tmp_path, "AAPL", "2025-08-01") == "2025-08-01"


def test_refresh_start_with_no_existing_files(tmp_path):
    assert _refresh_start(tmp_path, "AAPL", "2025-09-01") == "2025-09-01"


def test_refresh_start_ignores_unparsable_filenames(tmp_path):
    (tmp_path / "AAPL_notadate.pkl").write_bytes(b"x")
    assert _refresh_start(tmp_path, "AAPL", "2025-09-01") == "2025-09-01"


# ── a member that stopped trading is excluded, a provider hiccup still aborts ──

def _ended(last_day: str) -> pd.DataFrame:
    """Two-bar raw frame whose final print is ``last_day``."""
    idx = pd.DatetimeIndex(pd.to_datetime(["2026-06-01", last_day]))
    return pd.DataFrame({"Close": [100.0, 90.0], "Volume": [1, 1]}, index=idx)


def test_frame_reaching_as_of_is_current_without_probing():
    """A frame that already covers the as-of date never touches the provider."""
    calls = []
    probe = lambda *a: calls.append(a)  # noqa: E731
    assert _classify_stale("AAPL", _frame("2026-09-01"), _AS_OF, probe=probe) == "current"
    assert calls == []


def test_final_print_weeks_ago_plus_provider_confirmation_is_delisted():
    """EA / AVB / EQR on 2026-09-01: last bar mid-August, membership snapshot
    from June still lists them, provider answers 200-empty for the tail."""
    probed = []

    def probe(t, start, end):
        probed.append((t, start, end))
        return False

    assert _classify_stale("EA", _ended("2026-08-10"), _AS_OF, probe=probe) == "delisted"
    assert probed == [("EA", "2026-08-11", "2026-09-01")]


def test_recent_last_bar_is_a_failure_not_a_delisting():
    """A gap shorter than the grace period is provider lag — never treated as
    the name having stopped trading, even if the provider says no bars yet."""
    recent = (_AS_OF - pd.Timedelta(days=_DELIST_GRACE_DAYS - 1)).isoformat()
    assert _classify_stale("AAPL", _ended(recent), _AS_OF, probe=lambda *a: False) == "failed"


def test_provider_error_on_the_tail_is_a_failure():
    """A probe that cannot get an answer (None) proves nothing; the build must
    not shrink the pool on a timeout."""
    assert _classify_stale("EA", _ended("2026-08-10"), _AS_OF, probe=lambda *a: None) == "failed"


def test_bars_found_in_the_tail_is_a_failure_to_investigate():
    """The frame stopped early but the provider has later bars: partial
    response, not a delisting."""
    assert _classify_stale("EA", _ended("2026-08-10"), _AS_OF, probe=lambda *a: True) == "failed"


def test_empty_frame_is_a_failure():
    """No bars at all (or no frame) is a fetch failure, never a delisting."""
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    assert _classify_stale("EA", empty, _AS_OF, probe=lambda *a: False) == "failed"
    assert _classify_stale("EA", None, _AS_OF, probe=lambda *a: False) == "failed"


def test_ensure_raw_prices_separates_delisted_from_failed(tmp_path, monkeypatch):
    """The refresh loop reports current, delisted and failed names apart."""
    frames = {
        "AAPL": _frame("2026-09-01"),                    # current after refresh
        "EA": _ended("2026-08-10"),                      # stopped trading
        "XYZ": pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([])),  # provider failure
    }
    monkeypatch.setattr(bul, "_RAW", tmp_path)
    monkeypatch.setattr(bul, "fetch_eod", lambda t, s, e, adjust: frames[t])
    monkeypatch.setattr(bul, "probe_bars", lambda t, s, e: False)
    monkeypatch.setattr(bul.time, "sleep", lambda *_: None)
    got, failed, delisted = _ensure_raw_prices(["AAPL", "EA", "XYZ"], _AS_OF)
    assert got == 1
    assert delisted == ["EA"]
    assert failed == ["XYZ"]
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is True
    assert list(tmp_path.glob("EA_*.pkl")) == []       # nothing written for a dead name


def test_ranking_drops_delisted_names_before_ranking(monkeypatch):
    """A dead name with a cached pre-delisting raw file still has a trailing
    dollar-volume, so filtering the fetch pool alone is not enough: the ranking
    re-reads the membership and must be told what to leave out, or the dead
    name takes a slot from a live one."""
    monkeypatch.setattr(bul.u, "get_universe", lambda d: ["EA", "AAPL", "MSFT"])
    dv = {"EA": 9e9, "AAPL": 5e9, "MSFT": 4e9}
    monkeypatch.setattr(bul.u, "pit_dollar_volume", lambda t, d: dv[t])
    assert bul.u.get_universe_top_n("2026-09-01", 2) == ["EA", "AAPL"]
    assert bul.u.get_universe_top_n("2026-09-01", 2, exclude={"EA"}) == ["AAPL", "MSFT"]


# ── fetched frames must be validated before they are trusted or stored ────────

def test_frame_reaching_as_of_is_accepted():
    assert _frame_reaches(_frame("2026-09-01"), _AS_OF) is True


def test_nonempty_frame_ending_before_as_of_is_rejected():
    """fetch_eod can return a populated frame whose last bar predates as_of
    (thin coverage, a halt, a partial response). Storing it would rank the very
    same build on a stale close, and the file would look perfectly healthy."""
    assert _frame_reaches(_frame("2026-08-03"), _AS_OF) is False


def test_empty_frame_is_rejected():
    empty = pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([]))
    assert _frame_reaches(empty, _AS_OF) is False


def test_none_frame_is_rejected():
    assert _frame_reaches(None, _AS_OF) is False


# ── atomic replace: a failed write must not destroy good data ─────────────────

def test_atomic_write_creates_target_and_leaves_no_temp(tmp_path):
    target = tmp_path / "AAPL_2025-09-01.pkl"
    _atomic_write_pickle(target, _frame("2026-09-01"))
    assert target.exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert _covers(target, _AS_OF) is True


def test_atomic_write_overwrites_existing_target(tmp_path):
    target = _pickle(tmp_path, "AAPL_2025-09-01.pkl", _frame("2026-08-03"))
    assert _covers(target, _AS_OF) is False
    _atomic_write_pickle(target, _frame("2026-09-01"))
    assert _covers(target, _AS_OF) is True


def test_failed_write_preserves_existing_file_and_cleans_temp(tmp_path, monkeypatch):
    """The reason atomicity matters: a mid-write failure must leave the previous
    good data intact rather than a truncated file where history used to be."""
    target = _pickle(tmp_path, "AAPL_2009-01-01.pkl", _frame("2026-08-03"))
    before = target.read_bytes()

    def _boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(bul.pickle, "dump", _boom)
    with pytest.raises(OSError):
        _atomic_write_pickle(target, _frame("2026-09-01"))

    assert target.read_bytes() == before, "existing data must survive a failed write"
    assert list(tmp_path.glob("*.tmp")) == [], "temp file must be cleaned up"


def test_temp_file_is_invisible_to_the_ticker_glob(tmp_path):
    """A stranded temp must never be picked up as a cache file by _raw_close's
    glob, which matches {ticker}_*.pkl."""
    (tmp_path / "AAPL_2025-09-01.pkl.tmp").write_bytes(b"partial")
    assert list(tmp_path.glob("AAPL_*.pkl")) == []


def test_duplicates_removed_after_replace_keeping_target(tmp_path):
    """Atomic replace alone is not enough: an older earliest-start duplicate
    would still win _raw_close's sort, so duplicates go after the replace."""
    _pickle(tmp_path, "AAPL_2025-08-01.pkl", _frame("2026-08-03"))
    target = tmp_path / "AAPL_2025-09-01.pkl"
    _atomic_write_pickle(target, _frame("2026-09-01"))

    for old in tmp_path.glob("AAPL_*.pkl"):          # the loop's cleanup step
        if old != target:
            old.unlink(missing_ok=True)

    assert sorted(p.name for p in tmp_path.glob("AAPL_*.pkl")) == ["AAPL_2025-09-01.pkl"]
    assert _ticker_is_current(tmp_path, "AAPL", _AS_OF) is True


# ── write-once: the retry window is for failures, not re-ranks ───────────────
#
# The workflow runs on days 1-5 of each month. Those days are retries for a run
# that FAILED, not four extra chances to re-rank. The guard used to also require
# the freshly computed tickers to match the committed ones, which inverted the
# intent: any difference — including one caused by a transient data problem —
# read as "not current" and rewrote the file. September 2026 was rewritten four
# times for the same as-of date, and since a universe rewrite changes the
# fingerprint /api/screener validates against, each rewrite invalidated every
# result published before it.

def _publish(tmp_path, monkeypatch, as_of: str, tickers: list[str]) -> Path:
    out = tmp_path / "current.json"
    out.write_text(json.dumps({"as_of": as_of, "n": len(tickers), "tickers": tickers}))
    monkeypatch.setattr(bul, "_OUT", out)
    return out


def test_a_published_month_is_not_republished(tmp_path, monkeypatch):
    _publish(tmp_path, monkeypatch, "2026-09-01", ["AAPL", "MSFT"])
    assert bul._already_published(_AS_OF) is True


def test_the_decision_cannot_depend_on_the_ranking():
    """The regression, pinned where it cannot be faked. The guard used to take
    the freshly computed tickers and require them to match; that is precisely
    how a transient data problem reopened a published month and churned the
    fingerprint. Taking only the as-of date makes that impossible by
    construction, so the signature is the assertion."""
    import inspect
    assert list(inspect.signature(bul._already_published).parameters) == ["as_of"]


def test_a_different_month_is_not_yet_published(tmp_path, monkeypatch):
    _publish(tmp_path, monkeypatch, "2026-08-03", ["AAPL", "MSFT"])
    assert bul._already_published(_AS_OF) is False


def test_no_file_means_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(bul, "_OUT", tmp_path / "nothing.json")
    assert bul._already_published(_AS_OF) is False


def test_an_unreadable_file_means_not_published(tmp_path, monkeypatch):
    """A corrupt list must not wedge the month — the retry has to be able to
    rebuild it."""
    out = tmp_path / "current.json"
    out.write_text("{ not json")
    monkeypatch.setattr(bul, "_OUT", out)
    assert bul._already_published(_AS_OF) is False


# ── unrankable members: the silent substitution ─────────────────────────────
#
# Reaching the as-of date is not enough to rank: the ranking is a trailing
# median over _DV_WINDOW sessions ENDING there. A member that is current but
# shallow yields no dollar-volume and vanishes — while rank N+1 slides up, so
# the list still comes out at exactly N and the length guard never fires.
#
# This is what happened on 2026-09-04: the published Top-100 lost HON from rank
# 97 and gained ON at 100, then reverted the next day. Rank 97 is not a boundary
# name.

def test_a_member_with_no_dollar_volume_is_reported(monkeypatch):
    monkeypatch.setattr(bul.u, "pit_dollar_volume",
                        lambda t, d: None if t == "HON" else 1.0)
    assert bul._unrankable(["AAPL", "HON", "MSFT"], _AS_OF) == ["HON"]


def test_a_fully_rankable_pool_reports_nothing(monkeypatch):
    monkeypatch.setattr(bul.u, "pit_dollar_volume", lambda t, d: 1.0)
    assert bul._unrankable(["AAPL", "HON", "MSFT"], _AS_OF) == []


def test_a_zero_dollar_volume_is_rankable_and_not_reported(monkeypatch):
    """Only None means "cannot be computed". A real zero is the ranking's own
    business — get_universe_top_n drops it, and that is a market fact, not a
    data failure this guard should abort on."""
    monkeypatch.setattr(bul.u, "pit_dollar_volume",
                        lambda t, d: 0.0 if t == "HON" else 1.0)
    assert bul._unrankable(["AAPL", "HON", "MSFT"], _AS_OF) == []


def test_the_check_asks_the_ranking_its_own_question(monkeypatch):
    """Pins that it goes through pit_dollar_volume rather than re-deriving
    rankability: the two must not be able to drift apart, and the cached values
    are what makes the check free."""
    seen = []
    monkeypatch.setattr(bul.u, "pit_dollar_volume",
                        lambda t, d: seen.append((t, d)) or 1.0)
    bul._unrankable(["AAPL", "MSFT"], _AS_OF)
    assert seen == [("AAPL", "2026-09-01"), ("MSFT", "2026-09-01")]


# ── the abort decision ──────────────────────────────────────────────────────

def test_no_error_when_every_member_ranks(monkeypatch):
    monkeypatch.setattr(bul.u, "pit_dollar_volume", lambda t, d: 1.0)
    assert bul._unrankable_error(["AAPL", "HON"], _AS_OF, set(), 100) is None


def test_the_error_names_the_offender(monkeypatch):
    monkeypatch.setattr(bul.u, "pit_dollar_volume",
                        lambda t, d: None if t == "HON" else 1.0)
    msg = bul._unrankable_error(["AAPL", "HON"], _AS_OF, set(), 100)
    assert msg is not None
    assert "HON" in msg
    assert "101" in msg, "the message must say which rank would silently be promoted"


def test_an_allowed_name_does_not_abort(monkeypatch):
    """--allow-unrankable is the escape hatch for a genuine recent spin-off, so
    a real one cannot wedge the build for the whole month."""
    monkeypatch.setattr(bul.u, "pit_dollar_volume",
                        lambda t, d: None if t == "SPIN" else 1.0)
    assert bul._unrankable_error(["AAPL", "SPIN"], _AS_OF, {"SPIN"}, 100) is None


def test_allowing_one_name_does_not_excuse_another(monkeypatch):
    monkeypatch.setattr(bul.u, "pit_dollar_volume",
                        lambda t, d: None if t in ("SPIN", "HON") else 1.0)
    msg = bul._unrankable_error(["AAPL", "SPIN", "HON"], _AS_OF, {"SPIN"}, 100)
    assert msg is not None and "HON" in msg and "SPIN" not in msg
