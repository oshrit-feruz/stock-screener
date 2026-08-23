"""The screener endpoint must never wedge, and must never scan on demand.

Two production failures are pinned here:

  1. `_sc_warming` is cleared in a `finally`, which does not run when the thread
     holding it dies without unwinding — a worker timeout, or the OOM killer
     taking the process mid-scan. The flag then stays True with no computation
     behind it, and every later request answers {"warming": true} forever. That
     is the observed outage, not a hypothetical.

  2. A 100-ticker scan inside a request is what exhausted the 512MB instance.
     docs/ARCHITECTURE.md: Actions computes, Render reads. With no precomputed
     state the honest answer is 503 — never a scan, and never a 200 carrying an
     empty ranking.
"""
from __future__ import annotations

import time

import pytest

import product.api.main as m


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(m, "_sc_data", None)
    monkeypatch.setattr(m, "_sc_ts", 0.0)
    monkeypatch.setattr(m, "_sc_warming", False)
    monkeypatch.setattr(m, "_sc_warm_started", 0.0)
    monkeypatch.setattr(m, "_sc_universe_fp", None)
    # A valid universe, so these tests exercise the scan/cache path and not the
    # separate universe-validation 503.
    monkeypatch.setattr(m, "load_universe_list", lambda *a, **k: object())
    monkeypatch.setattr(m, "_universe_fingerprint", lambda _u: "fp-test")


def _no_scan(*_a, **_k):
    raise AssertionError("run_screener must not be called")


def test_fresh_flag_reports_warming(monkeypatch):
    monkeypatch.setattr(m, "_sc_warming", True)
    monkeypatch.setattr(m, "_sc_warm_started", time.time())
    monkeypatch.setattr(m, "run_screener", _no_scan)
    assert m._get_screener_data().get("warming") is True


def test_wedged_flag_is_reclaimed_instead_of_warming_forever(monkeypatch):
    """The outage: flag set, owning thread long dead, nothing computing."""
    monkeypatch.setattr(m, "_sc_warming", True)
    monkeypatch.setattr(m, "_sc_warm_started", time.time() - m._SC_WARM_TIMEOUT - 1)
    monkeypatch.setattr(m, "_load_disk_cache", lambda *a, **k: None)
    monkeypatch.setattr(m, "_ALLOW_ONDEMAND_SCAN", False)
    monkeypatch.setattr(m, "run_screener", _no_scan)

    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()      # reclaimed, not {"warming": True}


def test_no_precomputed_state_refuses_to_scan(monkeypatch):
    monkeypatch.setattr(m, "_load_disk_cache", lambda *a, **k: None)
    monkeypatch.setattr(m, "_ALLOW_ONDEMAND_SCAN", False)
    monkeypatch.setattr(m, "run_screener", _no_scan)

    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()


def test_refusal_releases_the_flag_so_it_cannot_wedge(monkeypatch):
    """A refusal must not leave the endpoint permanently 'warming'."""
    monkeypatch.setattr(m, "_load_disk_cache", lambda *a, **k: None)
    monkeypatch.setattr(m, "_ALLOW_ONDEMAND_SCAN", False)
    monkeypatch.setattr(m, "run_screener", _no_scan)

    with pytest.raises(m.ScreenerStateUnavailable):
        m._get_screener_data()
    assert m._sc_warming is False


def test_precomputed_state_is_served_without_scanning(monkeypatch):
    from datetime import date

    from product.screener.daily_screener import ScreenerResult, ScreenerRow

    row = ScreenerRow(
        ticker="AAPL", current_price=1.0, high_52w=2.0, drawdown_pct=0.5,
        dip_score=0.8, momentum_score=0.7, volume_score=0.6,
        composite_score=0.75, gate=True, signal="BUY",
    )
    result = ScreenerResult(as_of_date=date(2026, 8, 3), buy_signals=[row], full_ranking=[row])
    monkeypatch.setattr(m, "_load_disk_cache", lambda *a, **k: result)
    monkeypatch.setattr(m, "run_screener", _no_scan)

    data = m._get_screener_data()
    assert data["as_of"] == "2026-08-03"
    assert len(data["full_ranking"]) == 1
    assert m._sc_warming is False
