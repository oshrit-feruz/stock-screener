"""/api/screener must serve the newest PUBLISHED daily result, with provenance.

The daily scan runs in GitHub Actions and is published to the
automation/daily-state branch; Render fetches and serves it (docs/
ARCHITECTURE.md — Actions computes, Render reads). Pinned here:

  * the lookup walks back up to _DAILY_STATE_LOOKBACK_DAYS and labels the
    result with computed_on — Friday's scan on Sunday is served EXPLICITLY,
    never silently;
  * beyond the window it refuses (503), naming the producer;
  * a published result computed under a superseded universe is discarded by
    the fingerprint check, not served as "slightly stale";
  * the fetch path never triggers a scan.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta

import pytest

import product.api.main as m
import product.screener.daily_screener as ds
from product.screener.daily_screener import ScreenerResult, ScreenerRow


def _row():
    return ScreenerRow(
        ticker="AAPL", current_price=1.0, high_52w=2.0, drawdown_pct=0.5,
        dip_score=0.8, momentum_score=0.7, volume_score=0.6,
        composite_score=0.75, gate=True, signal="BUY",
    )


def _payload(as_of: date, fp: str) -> dict:
    r = _row()
    return {
        "as_of_date": as_of.isoformat(),
        "universe_fingerprint": fp,
        "buy_signals": [asdict(r)],
        "full_ranking": [asdict(r)],
    }


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(m, "_sc_data", None)
    monkeypatch.setattr(m, "_sc_ts", 0.0)
    monkeypatch.setattr(m, "_sc_warming", False)
    monkeypatch.setattr(m, "_sc_warm_started", 0.0)
    monkeypatch.setattr(m, "_sc_universe_fp", None)
    monkeypatch.setattr(m, "_ALLOW_ONDEMAND_SCAN", False)
    monkeypatch.setattr(m, "load_universe_list", lambda *a, **k: object())
    monkeypatch.setattr(m, "_universe_fingerprint", lambda _u: "fp-live")
    monkeypatch.setattr(
        m, "run_screener",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    yield


def _write_local(tmp_path, as_of: date, fp: str = "fp-live"):
    (tmp_path / f"{as_of.isoformat()}.json").write_text(json.dumps(_payload(as_of, fp)))


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload
    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def test_todays_local_result_served_with_computed_on(tmp_path, monkeypatch):
    _write_local(tmp_path, date.today())
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    data = m._get_screener_data()
    assert data["computed_on"] == date.today().isoformat()
    assert len(data["full_ranking"]) == 1


def test_weekend_gap_serves_fridays_result_labeled(tmp_path, monkeypatch):
    friday = date.today() - timedelta(days=2)
    _write_local(tmp_path, friday)
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    data = m._get_screener_data()
    assert data["computed_on"] == friday.isoformat()   # explicit, not silent


def test_missing_locally_is_fetched_from_daily_state(tmp_path, monkeypatch):
    yesterday = date.today() - timedelta(days=1)
    fetched = {}
    def fake_get(url, timeout):
        d = url.rsplit("/", 1)[1].removesuffix(".json")
        if d == yesterday.isoformat():
            fetched["url"] = url
            return _Resp(200, _payload(yesterday, "fp-live"))
        return _Resp(404)
    monkeypatch.setattr(m.requests, "get", fake_get)
    data = m._get_screener_data()
    assert data["computed_on"] == yesterday.isoformat()
    assert "data/screener_cache" in fetched["url"]


def test_nothing_within_window_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()
    assert m._sc_warming is False


def test_result_beyond_window_is_not_served(tmp_path, monkeypatch):
    _write_local(tmp_path, date.today() - timedelta(days=m._DAILY_STATE_LOOKBACK_DAYS + 1))
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()


def test_stale_universe_fingerprint_is_discarded_not_served(tmp_path, monkeypatch):
    """A published result from a superseded universe is wrong, not stale."""
    _write_local(tmp_path, date.today(), fp="fp-last-month")
    monkeypatch.setattr(m, "_fetch_published_daily_result", lambda d: False)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()


def test_fetch_failure_falls_through_to_refusal_not_scan(tmp_path, monkeypatch):
    def boom(url, timeout):
        raise OSError("network down")
    monkeypatch.setattr(m.requests, "get", boom)
    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()   # run_screener autouse-mock would raise if scanned
