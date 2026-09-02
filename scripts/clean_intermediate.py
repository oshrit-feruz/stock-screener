#!/usr/bin/env python3
"""Stages 1-2 of the clean point-in-time research framework: the
survivorship-free signal universe shared by every `run_clean_pit_*` script.

Universe (no lookahead, no survivorship):
  * Membership: PIT S&P 500 constituents on each quarterly rebalance date (free
    fja05680 history, includes removed / delisted names).
  * Size rank: at each rebalance, rank the CURRENT members by trailing 63-day
    MEDIAN dollar-volume (RAW close x volume) and keep the top 100. Dollar
    volume needs only prices, so it is computable for the whole window and for
    delisted names — the clean substitute for market cap, which is unavailable
    free that far back.
  * A signal is eligible only if its ticker is in the top 100 as of the most
    recent rebalance on or before the signal date.

Signal: the frozen recovery composite >= BUY_THRESHOLD. NO fundamental quality
gate — the gate needs EDGAR data that only exists ~2010+ and mostly for
survivors, which would re-introduce bias; this is a PURE price-signal test.

The expensive part (scoring every ticker, dollar-volume at every rebalance) is
cached once in `data/cache/clean_pit_intermediate.pkl`; it is window-independent.
"""
from __future__ import annotations

import bisect
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.signals.recovery_score import BUY_THRESHOLD, compute_recovery_signals  # noqa: E402
from data.sp500_universe import get_universe  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────
FETCH_START = "1998-06-01"           # warmup so 252d windows are valid from 2000
FETCH_END = "2024-12-31"
SIM_END = pd.Timestamp("2024-12-31")
TOP_N = 100
DV_WINDOW = 63                       # trailing trading days for median dollar-volume
CROSSING_FLOOR = pd.Timestamp("2000-01-01")   # fixed floor: intermediate is window-independent

ADJ_DIR = ROOT / "data" / "cache" / "prices"
RAW_DIR = ROOT / "data" / "cache" / "prices_raw"
INTERMED = ROOT / "data" / "cache" / "clean_pit_intermediate.pkl"


def safe_name(ticker: str) -> str:
    """Filesystem-safe ticker (matches the cache writers: BRK.B -> BRKB)."""
    return "".join(c for c in ticker if c.isalnum() or c in "-_")


def load_frame(dir_: Path, ticker: str) -> pd.DataFrame | None:
    """Cached OHLCV frame for a ticker, or None if absent / unreadable / empty."""
    p = dir_ / f"{safe_name(ticker)}_{FETCH_START}_{FETCH_END}.pkl"
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None
    return df if df is not None and not df.empty else None


def load_close(ticker: str) -> pd.Series | None:
    """Adjusted close series for a ticker (what trades are marked on), or None."""
    adj = load_frame(ADJ_DIR, ticker)
    return adj["Close"].astype(float) if adj is not None else None


def rebalance_dates() -> list[pd.Timestamp]:
    ds = [pd.Timestamp(f"{y}-{m:02d}-15") for y in range(1999, 2025) for m in (3, 6, 9, 12)]
    return [d for d in ds if d <= SIM_END]


# ── Stage 1: per-ticker crossings + dollar-volume at rebalance dates ──────────

def _dollar_volume_at(raw: pd.DataFrame, rebals: list[pd.Timestamp]) -> dict:
    """Trailing median raw dollar-volume as of each rebalance (only data <= date)."""
    med = (raw["Close"] * raw["Volume"]).astype(float).rolling(DV_WINDOW).median()
    per = {}
    for d in rebals:
        sub = med[med.index <= d]
        if not sub.empty and np.isfinite(sub.iloc[-1]):
            per[d] = float(sub.iloc[-1])
    return per


def _buy_crossings(adj: pd.DataFrame) -> list[tuple]:
    """(timestamp, composite, close) at every upward crossing of BUY_THRESHOLD.

    A NaN composite resets the state, so a BUY right after a data gap counts as a
    fresh crossing (same semantics as the product's signal detection).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        scored = compute_recovery_signals(adj)
    comp = scored["composite_score"]
    inbuy = (comp >= BUY_THRESHOLD).fillna(False).astype(bool)
    cross = inbuy & ~inbuy.shift(1, fill_value=False)
    cross &= (scored.index >= CROSSING_FLOOR) & (scored.index <= SIM_END)
    hit = scored[cross]
    return [(ts, float(c), float(px))
            for ts, c, px in zip(hit.index, hit["composite_score"], hit["Close"])]


def _load_cached_intermediate() -> dict | None:
    if not INTERMED.exists():
        return None
    try:
        with open(INTERMED, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_intermediate(data: dict) -> None:
    try:
        tmp = INTERMED.with_suffix(".pkl.tmp")
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
        tmp.replace(INTERMED)
    except Exception:
        pass


def build_intermediate() -> dict:
    """Return {crossings: {tkr: [(ts, comp, close)]}, dv: {tkr: {rebal_ts: dv}},
    members_by_rebal: {rebal_iso: [tkr]}, rebals: [rebal_iso]} (cached on disk)."""
    cached = _load_cached_intermediate()
    if cached is not None:
        print("  loaded cached intermediate", flush=True)
        return cached

    rebals = rebalance_dates()
    members_by_rebal = {d: get_universe(d.date().isoformat()) for d in rebals}
    universe = sorted({t for m in members_by_rebal.values() for t in m})
    print(f"  universe {len(universe)} tickers, {len(rebals)} rebalances", flush=True)

    crossings: dict[str, list] = {}
    dv: dict[str, dict] = {}
    for i, t in enumerate(universe, 1):
        raw = load_frame(RAW_DIR, t)
        if raw is not None and "Volume" in raw.columns:
            per = _dollar_volume_at(raw, rebals)
            if per:
                dv[t] = per
        adj = load_frame(ADJ_DIR, t)
        if adj is not None and len(adj) >= 252:
            cross = _buy_crossings(adj)
            if cross:
                crossings[t] = cross
        if i % 100 == 0:
            print(f"    processed {i}/{len(universe)}", flush=True)

    data = {"crossings": crossings, "dv": dv,
            "members_by_rebal": {d.isoformat(): m for d, m in members_by_rebal.items()},
            "rebals": [d.isoformat() for d in rebals]}
    _save_intermediate(data)
    return data


# ── Stage 2: top-N per rebalance, eligible crossings ─────────────────────────

def ranked_by_rebal(data: dict) -> dict[pd.Timestamp, list[tuple[str, float]]]:
    """Every ranked member per rebalance as (ticker, dollar-volume), best first."""
    dv = data["dv"]
    out = {}
    for d_iso in data["rebals"]:
        d = pd.Timestamp(d_iso)
        members = data["members_by_rebal"][d_iso]
        caps = [(t, dv[t][d]) for t in members if t in dv and d in dv[t]]
        caps.sort(key=lambda x: -x[1])
        out[d] = caps
    return out


def top_n_by_rebal(data: dict, n: int = TOP_N) -> dict[pd.Timestamp, list[str]]:
    return {d: [t for t, _ in caps[:n]] for d, caps in ranked_by_rebal(data).items()}


def governing_rebalance(keys: list[pd.Timestamp], ts: pd.Timestamp) -> pd.Timestamp | None:
    """The most recent rebalance strictly on/before `ts` (None if none yet)."""
    idx = bisect.bisect_right(keys, ts) - 1
    return keys[idx] if idx >= 0 else None


def eligible_crossings(data: dict, topn: dict) -> dict[str, list[tuple]]:
    """Keep only crossings whose ticker is in the top-N as of the governing
    rebalance (the most recent one on or before the crossing date)."""
    keys = sorted(topn)
    top_sets = {d: set(v) for d, v in topn.items()}
    out: dict[str, list] = {}
    for t, cross in data["crossings"].items():
        kept = []
        for ts, comp, close in cross:
            rebal = governing_rebalance(keys, ts)
            if rebal is not None and t in top_sets[rebal]:
                kept.append((ts, comp, close))
        if kept:
            out[t] = kept
    return out
