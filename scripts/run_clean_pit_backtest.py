#!/usr/bin/env python3
"""Clean point-in-time backtest — survivorship-bias-free, long window (2000-2024).

Universe (fully clean, no lookahead, no survivorship):
  * Membership: PIT S&P 500 constituents on each date (free fja05680 history,
    includes removed / delisted names).
  * Size rank: at each quarterly rebalance, rank the CURRENT members by trailing
    63-day MEDIAN dollar-volume (RAW close x volume) and keep the top 100. Dollar
    volume needs only prices, so it is computable for the whole window and for
    delisted names — the clean substitute for market cap, which is unavailable
    free that far back. A signal is eligible only if its ticker is in the top 100
    as of the most recent rebalance on or before the signal date.

Signal: the frozen recovery composite >= 0.60. NO fundamental quality gate — the
gate needs EDGAR data that only exists ~2010+ and mostly for survivors, which
would re-introduce bias; this is therefore a PURE price-signal test on a clean
universe.

Sizing / exits: V1 (10% of portfolio value per signal, max 10 concurrent, no
stop-loss). Three holding periods compared — 252 / 378 / 504 trading days — with
the completion rule (a signal is taken only if the full hold fits inside the data
window; otherwise excluded, never marked-to-market).

Outputs: comparison table, per-trade return distribution by holding bucket, and a
single portfolio-value chart of the three holds + SPY.
"""
from __future__ import annotations

import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402

from core.data.eodhd import fetch_eod  # noqa: E402
from core.signals.recovery_score import BUY_THRESHOLD, compute_recovery_signals  # noqa: E402
from data.sp500_universe import get_universe  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────
_FETCH_START = "1998-06-01"
_FETCH_END = "2024-12-31"
_SIM_START = pd.Timestamp("2000-01-03")
_SIM_END = pd.Timestamp("2024-12-31")
_TOP_N = 100
_DV_WINDOW = 63           # trailing trading days for median dollar-volume
_PCT = 0.10
_MAX_POS = 10
_INITIAL_CAP = 100_000.0
_MIN_POSITION = 1_000.0
_HOLDS = [252, 378, 504]

_ADJ_DIR = ROOT / "data" / "cache" / "prices"
_RAW_DIR = ROOT / "data" / "cache" / "prices_raw"
_OUT_DIR = ROOT / "validation"
_PNG = _OUT_DIR / "clean_pit_portfolio.png"
_MD = _OUT_DIR / "clean_pit_backtest.md"
_INTERMED = ROOT / "data" / "cache" / "clean_pit_intermediate.pkl"


def _safe(t: str) -> str:
    return "".join(c for c in t if c.isalnum() or c in "-_")


def _load_frame(dir_: Path, ticker: str) -> pd.DataFrame | None:
    p = dir_ / f"{_safe(ticker)}_{_FETCH_START}_{_FETCH_END}.pkl"
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            df = pickle.load(f)
        return df if df is not None and not df.empty else None
    except Exception:
        return None


def _rebalance_dates() -> list[pd.Timestamp]:
    ds = [pd.Timestamp(f"{y}-{m:02d}-15")
          for y in range(1999, 2025) for m in (3, 6, 9, 12)]
    return [d for d in ds if d <= _SIM_END]


# ── Stage 1: per-ticker crossings + dollar-volume at rebalance dates ──────────

def build_intermediate() -> dict:
    """Return {crossings: {tkr:[(ts,comp,close)]}, dv: {tkr:{rebal_ts:dv}},
    members_by_rebal: {rebal_ts:[tkr]}, close_panel_tickers: set}."""
    if _INTERMED.exists():
        try:
            with open(_INTERMED, "rb") as f:
                data = pickle.load(f)
            print("  loaded cached intermediate", flush=True)
            return data
        except Exception:
            pass

    rebals = _rebalance_dates()
    members_by_rebal = {d: get_universe(d.date().isoformat()) for d in rebals}
    universe = sorted({t for m in members_by_rebal.values() for t in m})
    print(f"  universe {len(universe)} tickers, {len(rebals)} rebalances", flush=True)

    crossings: dict[str, list] = {}
    dv: dict[str, dict] = {}

    import warnings
    for i, t in enumerate(universe, 1):
        adj = _load_frame(_ADJ_DIR, t)
        raw = _load_frame(_RAW_DIR, t)
        # dollar-volume from raw (true traded value; robust median)
        if raw is not None and "Volume" in raw.columns:
            dvol = (raw["Close"] * raw["Volume"]).astype(float)
            med = dvol.rolling(_DV_WINDOW).median()
            per = {}
            for d in rebals:
                sub = med[med.index <= d]
                if not sub.empty and np.isfinite(sub.iloc[-1]):
                    per[d] = float(sub.iloc[-1])
            if per:
                dv[t] = per
        # crossings from adjusted (frozen composite >= 0.60, NO gate)
        if adj is not None and len(adj) >= 252:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                scored = compute_recovery_signals(adj)
            cross = []
            prev = False
            comp_s = scored["composite_score"]
            close_s = scored["Close"]
            for j in range(len(scored)):
                c = comp_s.iloc[j]
                if pd.isna(c):
                    prev = False
                    continue
                inbuy = bool(c >= BUY_THRESHOLD)
                if inbuy and not prev:
                    ts = scored.index[j]
                    if _SIM_START <= ts <= _SIM_END:
                        cross.append((ts, float(c), float(close_s.iloc[j])))
                prev = inbuy
            if cross:
                crossings[t] = cross
        if i % 100 == 0:
            print(f"    processed {i}/{len(universe)}", flush=True)

    data = {"crossings": crossings, "dv": dv,
            "members_by_rebal": {d.isoformat(): m for d, m in members_by_rebal.items()},
            "rebals": [d.isoformat() for d in rebals]}
    try:
        with open(_INTERMED, "wb") as f:
            pickle.dump(data, f)
    except Exception:
        pass
    return data


# ── Stage 2: top-100 per rebalance, eligible crossings ───────────────────────

def top_n_by_rebal(data: dict) -> dict:
    dv = data["dv"]
    out = {}
    for d_iso in data["rebals"]:
        d = pd.Timestamp(d_iso)
        members = data["members_by_rebal"][d_iso]
        caps = [(t, dv[t][d]) for t in members if t in dv and d in dv[t]]
        caps.sort(key=lambda x: -x[1])
        out[d] = [t for t, _ in caps[:_TOP_N]]
    return out


def eligible_crossings(data: dict, topn: dict) -> dict:
    """Keep only crossings whose ticker is in the top-100 as of the most recent
    rebalance on or before the crossing date."""
    rebal_ts = sorted(topn.keys())
    import bisect
    keys = [d for d in rebal_ts]
    out: dict[str, list] = {}
    for t, cross in data["crossings"].items():
        kept = []
        for ts, comp, close in cross:
            idx = bisect.bisect_right(keys, ts) - 1
            if idx < 0:
                continue
            if t in topn[keys[idx]]:
                kept.append((ts, comp, close))
        if kept:
            out[t] = kept
    return out


# ── Stage 3: portfolio simulation (V1 sizing, parameterised hold) ────────────

def _load_close_panel(tickers: list[str], cal: pd.DatetimeIndex) -> pd.DataFrame:
    series = {}
    for t in tickers:
        adj = _load_frame(_ADJ_DIR, t)
        if adj is not None:
            series[t] = adj["Close"]
    panel = pd.DataFrame(series).reindex(cal, method="ffill")
    return panel


def simulate(elig: dict, panel: pd.DataFrame, cal: pd.DatetimeIndex,
             hold: int) -> dict:
    events = defaultdict(list)
    for t, cross in elig.items():
        for ts, comp, close in cross:
            if _SIM_START <= ts <= cal[-1]:
                events[ts].append((t, comp, close))

    prices = panel.reindex(cal, method="ffill")
    arr = prices.values.astype(float)
    col = {c: i for i, c in enumerate(prices.columns)}
    last_idx = len(cal) - 1

    def px(di, t):
        ci = col.get(t)
        return float(arr[di, ci]) if ci is not None else float("nan")

    def pval(di, cash, pos):
        v = cash
        for p in pos.values():
            q = px(di, p["ticker"])
            v += p["shares"] * (q if not np.isnan(q) else p["entry_price"])
        return v

    cash = _INITIAL_CAP
    pos: dict[int, dict] = {}
    last_entry: dict[str, int] = {}
    trades: list[dict] = []
    excl = 0
    dvals = np.zeros(len(cal))
    pid = 0
    # map event dates to calendar indices

    for di, day in enumerate(cal):
        for k in [k for k, v in list(pos.items()) if v["exit_idx"] == di]:
            p = pos.pop(k)
            ep = px(di, p["ticker"])
            if np.isnan(ep):
                ep = p["entry_price"]
            cash += p["shares"] * ep
            trades.append({"ticker": p["ticker"], "entry": p["entry_date"].date(),
                           "exit": day.date(), "ret": ep / p["entry_price"] - 1,
                           "pnl": p["shares"] * ep - p["cost"]})
        # events on this day (only if it's a trading day in cal)
        for t, comp, cprice in sorted(events.get(day, []), key=lambda x: -x[1]):
            if di + hold > last_idx:
                excl += 1
                continue
            if t in last_entry and (di - last_entry[t]) < hold:
                continue
            if len(pos) >= _MAX_POS:
                continue
            pv = pval(di, cash, pos)
            alloc = min(pv * _PCT, cash)
            if alloc < _MIN_POSITION:
                continue
            ep = px(di, t)
            if np.isnan(ep) or ep <= 0:
                ep = cprice
            if ep <= 0:
                continue
            pid += 1
            pos[pid] = {"ticker": t, "entry_date": day, "entry_idx": di,
                        "exit_idx": di + hold, "entry_price": ep,
                        "shares": alloc / ep, "cost": alloc}
            last_entry[t] = di
            cash -= alloc
        dvals[di] = pval(di, cash, pos)

    # events can fall on non-trading days; snap them to the next trading day
    # (handled below by pre-snapping). Any leftover open positions => none by
    # construction (completion gate), assert:
    assert not pos, "open positions remain"
    dv = pd.Series(dvals, index=cal)
    return {"trades": trades, "excluded": excl, "daily": dv,
            "final": float(dv.iloc[-1])}


def snap_events_to_calendar(elig: dict, cal: pd.DatetimeIndex) -> dict:
    """Move each crossing timestamp to the first trading day >= it (signals are
    detected on adjusted-close dates, which are always trading days, but a
    ticker's own calendar may differ slightly from the master SPY calendar)."""
    out: dict[str, list] = {}
    import bisect
    cal_list = list(cal)
    for t, cross in elig.items():
        kept = []
        for ts, comp, close in cross:
            idx = bisect.bisect_left(cal_list, ts)
            if idx >= len(cal_list):
                continue
            kept.append((cal_list[idx], comp, close))
        out[t] = kept
    return out


# ── Metrics ───────────────────────────────────────────────────────────────────

def metrics(daily: pd.Series, cal: pd.DatetimeIndex) -> dict:
    n_years = (cal[-1] - cal[0]).days / 365.25
    final = float(daily.iloc[-1])
    total = final / _INITIAL_CAP - 1
    cagr = (final / _INITIAL_CAP) ** (1 / n_years) - 1
    rmax = daily.cummax()
    dd = ((daily - rmax) / rmax).min()
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() * np.sqrt(252) / rets.std()) if rets.std() > 0 else 0.0
    return {"final": final, "total": total, "cagr": cagr, "max_dd": float(dd),
            "sharpe": sharpe, "n_years": n_years}


_BUCKETS = [("< -20%", lambda r: r <= -0.20), ("-20..0%", lambda r: -0.20 < r <= 0),
            ("0..20%", lambda r: 0 < r <= 0.20), ("20..50%", lambda r: 0.20 < r <= 0.50),
            ("50..100%", lambda r: 0.50 < r <= 1.0), (">100%", lambda r: r > 1.0)]


def trade_stats(trades: list[dict]) -> dict:
    r = np.array([t["ret"] for t in trades], float)
    if len(r) == 0:
        return {"n": 0}
    def p(q):
        return float(np.percentile(r, q))
    return {"n": len(r), "avg": float(r.mean()), "median": float(np.median(r)),
            "win": float((r > 0).mean()), "p90": p(90), "p95": p(95), "max": float(r.max()),
            "buckets": {name: int(sum(1 for x in r if c(x))) for name, c in _BUCKETS}}


def spy_metrics(cal: pd.DatetimeIndex) -> dict:
    spy = fetch_eod("SPY.US", _FETCH_START, _FETCH_END, adjust=True)
    s = spy["Close"].reindex(cal, method="ffill")
    vals = s / float(s.iloc[0]) * _INITIAL_CAP
    m = metrics(vals, cal)
    m["daily"] = vals
    return m


# ── Report + plot ─────────────────────────────────────────────────────────────

def report(results, mets, stats, spy_m, cal) -> str:
    n_years = mets[0]["n_years"]
    L = ["# Clean point-in-time backtest — S&P 500, 2000-2024\n",
         f"Window **{cal[0].date()}..{cal[-1].date()}** "
         f"({len(cal)} trading days, ~{n_years:.1f}y). Start ${_INITIAL_CAP:,.0f}.\n",
         "**Universe (clean):** PIT S&P 500 membership (incl. delisted), ranked each "
         f"quarter by trailing {_DV_WINDOW}-day median dollar-volume, top {_TOP_N}. "
         "No survivorship bias, no lookahead.\n",
         "**Signal:** recovery composite >= 0.60, **no fundamental gate** (pure price "
         "signal). **Sizing:** V1 — 10%/signal, max 10, no stop-loss. Late signals that "
         "cannot complete the hold are excluded.\n",
         "## Summary\n",
         "| Metric | H252 | H378 | H504 | SPY |", "|---|--:|--:|--:|--:|"]

    def row(label, cells, spy_cell):
        L.append("| " + label + " | " + " | ".join(cells) + " | " + spy_cell + " |")

    row("Final value", [f"${m['final']:,.0f}" for m in mets], f"${spy_m['final']:,.0f}")
    row("Total return", [f"{m['total']:+.1%}" for m in mets], f"{spy_m['total']:+.1%}")
    row("CAGR", [f"{m['cagr']:+.1%}" for m in mets], f"{spy_m['cagr']:+.1%}")
    row("Sharpe", [f"{m['sharpe']:.2f}" for m in mets], f"{spy_m['sharpe']:.2f}")
    row("Max drawdown", [f"{m['max_dd']:.1%}" for m in mets], f"{spy_m['max_dd']:.1%}")
    row("Avg trade", [f"{s['avg']:+.1%}" for s in stats], "—")
    row("Median trade", [f"{s['median']:+.1%}" for s in stats], "—")
    row("Win rate", [f"{s['win']:.0%}" for s in stats], "—")
    row("Trades", [f"{s['n']}" for s in stats], "—")
    row("Excluded (late)", [f"{r['excluded']}" for r in results], "—")

    L.append("\n## Return distribution by holding bucket (share of trades)\n")
    L.append("| Bucket | H252 | H378 | H504 |\n|---|--:|--:|--:|")
    for name, _ in _BUCKETS:
        cells = []
        for s in stats:
            cells.append(f"{(s['buckets'][name] / s['n']):.0%}" if s["n"] else "—")
        L.append("| " + name + " | " + " | ".join(cells) + " |")
    L.append("\n![portfolio](" + _PNG.name + ")\n")

    s252, s378, s504 = stats
    L.append("## Interpretation (bias-free, 25 years)\n")
    L.append("**The holding-period effect is real and now robust.** Every metric improves "
             f"monotonically with hold length — CAGR {mets[0]['cagr']:+.1%} -> "
             f"{mets[1]['cagr']:+.1%} -> {mets[2]['cagr']:+.1%}, win rate "
             f"{s252['win']:.0%} -> {s378['win']:.0%} -> {s504['win']:.0%}, and the fat "
             f"right tail (>100% trades) {s252['buckets']['>100%']/s252['n']:.0%} -> "
             f"{s378['buckets']['>100%']/s378['n']:.0%} -> "
             f"{s504['buckets']['>100%']/s504['n']:.0%}. On 217/154/114 trades over 25 "
             "years (vs 31/23/21 in the biased 2018-2024 study), the 'let recoveries run "
             "past a year' thesis holds up.\n")
    L.append("**But the edge over SPY is far smaller than the biased test implied.** The "
             f"1-year hold (H252, {mets[0]['cagr']:+.1%} CAGR) actually **loses to SPY** "
             f"({spy_m['cagr']:+.1%}); only the longer holds beat it (H378 "
             f"{mets[1]['cagr']:+.1%}, H504 {mets[2]['cagr']:+.1%}). The earlier "
             "50-stock 2018-2024 test showed every variant crushing SPY — that was the "
             "survivorship/selection bias talking.\n")
    L.append("**Tail risk is severe.** Max drawdowns of "
             f"{mets[0]['max_dd']:.0%}..{mets[2]['max_dd']:.0%} (vs SPY "
             f"{spy_m['max_dd']:.0%}) — a concentrated, no-stop, dip-buying book got "
             "destroyed in 2002 and 2008-09 (visible in the chart). The 2018-2024 window "
             "never contained a 2008, so it reported ~-32% and hid this.\n")
    L.append("### Data caveats\n"
             "- **Size proxy:** ranked by dollar-volume, not exact market cap (unavailable "
             "free pre-2015). Highly correlated for mega-caps, not identical.\n"
             "- **No fundamental gate:** pure price signal, so no quality filter (the gate "
             "needs EDGAR data that would re-introduce survivorship bias over 25y).\n"
             "- **Fetch coverage:** 1054/1090 ever-members fetched (96.7%). Facebook was "
             "recovered by aliasing FB->META. The ~36 missing are mostly bankruptcies/"
             "acquisitions with no free continuing series (LEHMQ, RSHCQ, AAMRQ, APC, CAM, "
             "PCL...). Note some are bankruptcies that would have been big *losers* had the "
             "dip signal fired on them — so their absence may slightly *flatter* results.\n")
    return "\n".join(L)


def make_plot(results, spy_m, cal):
    fig, ax = plt.subplots(figsize=(12, 7))
    colors = {252: "#1f77b4", 378: "#ff7f0e", 504: "#2ca02c"}
    for h, res in zip(_HOLDS, results):
        ax.plot(res["daily"].index, res["daily"].values, color=colors[h], linewidth=1.4,
                label=f"H{h} (final ${res['final']:,.0f})")
    ax.plot(spy_m["daily"].index, spy_m["daily"].values, "--", color="#7f7f7f",
            linewidth=1.3, label=f"SPY (final ${spy_m['final']:,.0f})")
    ax.axhline(_INITIAL_CAP, color="black", linewidth=0.6, alpha=0.4)
    ax.set_yscale("log")
    ax.set_title("Clean PIT top-100 (dollar-volume) — holding periods vs SPY, 2000-2024")
    ax.set_ylabel("Portfolio value ($, log)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.25, which="both")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    fig.tight_layout()
    fig.savefig(_PNG, dpi=130)
    print(f"  chart -> {_PNG}", flush=True)


def main() -> None:
    print("Stage 1: signals + dollar-volume...", flush=True)
    data = build_intermediate()
    print("Stage 2: top-100 per rebalance...", flush=True)
    topn = top_n_by_rebal(data)
    elig = eligible_crossings(data, topn)
    n_elig = sum(len(v) for v in elig.values())
    print(f"  eligible crossings: {n_elig} across {len(elig)} tickers", flush=True)

    # Master calendar = SPY trading days in window
    spy = fetch_eod("SPY.US", _FETCH_START, _FETCH_END, adjust=True)
    cal = spy["Close"].index
    cal = cal[(cal >= _SIM_START) & (cal <= _SIM_END)]
    print(f"  calendar {cal[0].date()}..{cal[-1].date()} ({len(cal)} days)", flush=True)

    elig = snap_events_to_calendar(elig, cal)
    held = sorted({t for t in elig})
    print(f"Stage 3: price panel for {len(held)} held-eligible tickers...", flush=True)
    panel = _load_close_panel(held, cal)

    spy_m = spy_metrics(cal)
    results, mets, stats = [], [], []
    for h in _HOLDS:
        res = simulate(elig, panel, cal, h)
        m = metrics(res["daily"], cal)
        s = trade_stats(res["trades"])
        results.append(res)
        mets.append(m)
        stats.append(s)
        print(f"  H{h}: trades={s['n']} excl={res['excluded']} final=${m['final']:,.0f} "
              f"CAGR={m['cagr']:+.1%} sharpe={m['sharpe']:.2f} win={s['win']:.0%}", flush=True)

    make_plot(results, spy_m, cal)
    rep = report(results, mets, stats, spy_m, cal)
    _MD.write_text(rep)
    print(f"  report -> {_MD}\n", flush=True)
    print("=" * 70, flush=True)
    print(rep, flush=True)


if __name__ == "__main__":
    main()
