#!/usr/bin/env python3
"""Shared simulation core for the clean point-in-time research scripts
(`run_clean_pit_*`, `run_spy_overlay`, `run_overlay_oos`, `run_adaptive_exit`,
`run_staged_entry`, `plot_overlay_curves`).

Every script inherits the same bias controls, so a fix here fixes them all:

  * Next-session fills. A signal is computed on bar t's close, so it fills at the
    close of the NEXT master-calendar session (no same-bar look-ahead). The
    completion rule and the hold clock both run from the fill.
  * No phantom prices. A ticker's series is forward-filled only inside its own
    quote range. A name that stops trading inside the window (acquired,
    bankrupt, delisted) is force-exited at its final print (exit kind "delist")
    and never carried at a stale quote. A name with no print on its fill day is
    not bought.
  * Completion rule. An entry is taken only if its full hold fits inside the
    window; otherwise it is counted as `excluded`, never marked-to-market.
  * One master calendar (SPY sessions) for every book, so all curves align.

Two idle-money regimes: `core="cash"` (standalone: idle money earns nothing)
and `core="spy"` (overlay: idle money is always in SPY, sleeves are funded by
selling SPY and proceeds rotate back). An optional `gate_dd` deploys a sleeve
only when the market's drawdown from its trailing 252-session high, measured
on the SIGNAL day, is at least the gate.
"""
from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(ROOT / ".env"))

import clean_intermediate as ci  # noqa: E402

from core.data.eodhd import fetch_eod  # noqa: E402

INITIAL_CAP = 100_000.0
MIN_POSITION = 1_000.0
GATE_LOOKBACK = 252          # sessions for the market's trailing high
DEFAULT_START = "2004-01-02"  # hermetic window: ranking coverage >= 85%

ExitRule = Callable[[dict, int, float, "Market"], Optional[str]]
OpenHook = Callable[[dict, int, "Market"], None]


# ── Market data ───────────────────────────────────────────────────────────────

def load_spy() -> pd.DataFrame:
    """Adjusted SPY bars for the full fetch window; fails fast when missing."""
    spy = fetch_eod("SPY.US", ci.FETCH_START, ci.FETCH_END, adjust=True)
    if spy is None or spy.empty or "Close" not in spy.columns:
        raise RuntimeError(
            "No SPY price history from EODHD — the master calendar cannot be built. "
            "Check EODHD_API_KEY in .env and network access."
        )
    return spy


def market_drawdown(spy_close: pd.Series, lookback: int = GATE_LOOKBACK) -> pd.Series:
    """Drawdown from the trailing `lookback`-session high, on the series' own
    calendar (computed on the FULL history so early windows have a real high)."""
    s = spy_close.astype(float)
    high = s.rolling(lookback, min_periods=1).max()
    return (high - s) / high


class PricePanel:
    """Adjusted closes for a set of tickers on a master calendar.

    Each series is forward-filled only between its first and last real quote;
    after the last quote it is NaN. `last_quote[t]` is that final print date.
    """

    def __init__(self, tickers: list[str], cal: pd.DatetimeIndex,
                 loader: Callable[[str], Optional[pd.Series]] = ci.load_close):
        cols: dict[str, pd.Series] = {}
        self.last_quote: dict[str, pd.Timestamp] = {}
        for t in tickers:
            s = loader(t)
            if s is None:
                continue
            s = s[~s.index.duplicated(keep="last")].sort_index().reindex(cal)
            lq = s.last_valid_index()
            if lq is None:
                continue
            s.loc[:lq] = s.loc[:lq].ffill()
            cols[t] = s
            self.last_quote[t] = lq
        self.df = pd.DataFrame(cols, index=cal)
        self.cal = cal


class Market:
    """A PricePanel sliced to one simulation window, as fast arrays, plus the
    per-ticker forced-exit day and (optionally) SPY level and market drawdown."""

    def __init__(self, panel: PricePanel, cal: pd.DatetimeIndex,
                 spy_close: Optional[pd.Series] = None):
        self.cal = cal
        self.n = len(cal)
        frame = panel.df.reindex(cal)
        self.columns = list(frame.columns)
        self.arr = frame.to_numpy(dtype=float)
        self.col = {c: i for i, c in enumerate(self.columns)}
        # Final-print day inside this window => forced exit that day.
        self.delist_idx: dict[str, int] = {}
        for t, lq in panel.last_quote.items():
            if lq < cal[-1]:
                i = int(cal.searchsorted(lq, side="right")) - 1
                if i >= 0:
                    self.delist_idx[t] = i
        if spy_close is not None:
            self.spy = spy_close.reindex(cal, method="ffill").to_numpy(dtype=float)
            self.dd = market_drawdown(spy_close).reindex(cal, method="ffill").to_numpy(dtype=float)
        else:
            self.spy = None
            self.dd = None

    def px(self, di: int, ticker: str) -> float:
        ci_ = self.col.get(ticker)
        return float(self.arr[di, ci_]) if ci_ is not None else float("nan")

    def delists_on(self, di: int, ticker: str) -> bool:
        return self.delist_idx.get(ticker) == di

    def aux(self, frame: pd.DataFrame) -> np.ndarray:
        """Align an auxiliary panel (same columns as the PricePanel) to this window."""
        return frame.reindex(index=self.cal, columns=self.columns).to_numpy(dtype=float)


# ── Events ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Event:
    ticker: str
    comp: float
    sig_idx: int    # last session on/before the signal bar (regime is read here)
    fill_idx: int   # first session strictly after the signal bar (entry here)


def schedule_events(elig: dict, cal: pd.DatetimeIndex) -> dict[int, list[Event]]:
    """Map eligible crossings to fill sessions: signal on bar t -> fill at the
    next master-calendar session. Signals with no next session in the window
    (or outside it) are dropped. Same-day events are ordered by composite."""
    n = len(cal)
    out: dict[int, list[Event]] = defaultdict(list)
    for t, cross in elig.items():
        for ts, comp, _close in cross:
            if ts < cal[0] or ts > cal[-1]:
                continue
            fill = int(cal.searchsorted(ts, side="right"))
            if fill >= n:
                continue
            out[fill].append(Event(t, float(comp), fill - 1, fill))
    for lst in out.values():
        lst.sort(key=lambda e: -e.comp)
    return dict(out)


# ── Portfolio ─────────────────────────────────────────────────────────────────

@dataclass
class SimConfig:
    hold: int = 504
    stop: float = 0.0            # daily-close hard stop (0 = none)
    tp: float = 0.0              # daily-close take-profit (0 = none)
    pct: float = 0.10            # sleeve = pct of total portfolio value
    max_pos: int = 10
    core: str = "cash"           # "cash" | "spy"
    gate_dd: Optional[float] = None   # min market DD on the signal day to deploy
    initial: float = INITIAL_CAP
    min_pos: float = MIN_POSITION


class Portfolio:
    """Positions + idle money (cash or an SPY core) on a Market."""

    def __init__(self, mkt: Market, core: str = "cash", initial: float = INITIAL_CAP):
        if core == "spy" and mkt.spy is None:
            raise ValueError("core='spy' needs a Market built with spy_close")
        self.mkt = mkt
        self.core = core
        self.cash = initial if core == "cash" else 0.0
        self.spy_shares = initial / mkt.spy[0] if core == "spy" else 0.0
        self.pos: dict[int, dict] = {}
        self.last_entry: dict[str, int] = {}
        self.trades: list[dict] = []
        self.excluded = 0
        self._pid = 0

    # idle money
    def idle(self, di: int) -> float:
        return self.cash if self.core == "cash" else self.spy_shares * self.mkt.spy[di]

    def _spend(self, di: int, dollars: float) -> None:
        if self.core == "cash":
            self.cash -= dollars
        else:
            self.spy_shares -= dollars / self.mkt.spy[di]

    def _receive(self, di: int, dollars: float) -> None:
        if self.core == "cash":
            self.cash += dollars
        else:
            self.spy_shares += dollars / self.mkt.spy[di]

    def value(self, di: int) -> float:
        v = self.idle(di)
        for p in self.pos.values():
            q = self.mkt.px(di, p["ticker"])
            v += p["shares"] * (p["entry_price"] if math.isnan(q) else q)
        return v

    # positions
    def open(self, di: int, ticker: str, dollars: float, price: float, hold: int) -> dict:
        self._spend(di, dollars)
        self._pid += 1
        p = {"pid": self._pid, "ticker": ticker, "entry_idx": di, "exit_idx": di + hold,
             "entry_price": price, "shares": dollars / price, "cost": dollars}
        self.pos[self._pid] = p
        self.last_entry[ticker] = di
        return p

    def add_to(self, pid: int, di: int, dollars: float, price: float) -> None:
        """Add a tranche to an open position (entry_price becomes the average)."""
        p = self.pos[pid]
        sh = dollars / price
        p["entry_price"] = (p["entry_price"] * p["shares"] + price * sh) / (p["shares"] + sh)
        p["shares"] += sh
        p["cost"] += dollars
        self._spend(di, dollars)

    def close(self, di: int, pid: int, price: float, kind: str) -> dict:
        p = self.pos.pop(pid)
        proceeds = p["shares"] * price
        self._receive(di, proceeds)
        tr = {"ticker": p["ticker"], "entry": self.mkt.cal[p["entry_idx"]].date(),
              "exit": self.mkt.cal[di].date(), "entry_price": p["entry_price"],
              "exit_price": price, "ret": price / p["entry_price"] - 1,
              "pnl": proceeds - p["cost"], "kind": kind, "days": di - p["entry_idx"]}
        self.trades.append(tr)
        return tr

    def can_complete(self, di: int, hold: int) -> bool:
        """Completion rule; counts the rejection so reports can show it."""
        if di + hold > self.mkt.n - 1:
            self.excluded += 1
            return False
        return True

    def locked(self, ticker: str, di: int, hold: int) -> bool:
        """One live sleeve per name: no re-entry within a hold of the last entry."""
        return ticker in self.last_entry and (di - self.last_entry[ticker]) < hold

    def try_enter(self, di: int, ticker: str, cfg: SimConfig) -> Optional[dict]:
        """Full-sleeve entry at this session's close under the config's rules."""
        if not self.can_complete(di, cfg.hold):
            return None
        if self.locked(ticker, di, cfg.hold) or len(self.pos) >= cfg.max_pos:
            return None
        price = self.mkt.px(di, ticker)
        if not price > 0:                      # NaN or non-positive: no print, no fill
            return None
        alloc = min(self.value(di) * cfg.pct, self.idle(di))
        if alloc < cfg.min_pos:
            return None
        return self.open(di, ticker, alloc, price, cfg.hold)

    def _exit_kind(self, p: dict, di: int, price: float, cfg: SimConfig,
                   rule: Optional[ExitRule]) -> Optional[str]:
        if di >= p["exit_idx"]:
            return "hold"
        if math.isnan(price):
            return None
        if cfg.stop > 0 and price <= p["entry_price"] * (1 - cfg.stop):
            return "stop"
        if cfg.tp > 0 and price >= p["entry_price"] * (1 + cfg.tp):
            return "tp"
        return rule(p, di, price, self.mkt) if rule is not None else None

    def process_exits(self, di: int, cfg: SimConfig, rule: Optional[ExitRule] = None) -> None:
        """Close every position whose hold / stop / tp / rule fires today, and
        force-exit names printing for the last time today at that final print."""
        for pid in list(self.pos):
            p = self.pos[pid]
            price = self.mkt.px(di, p["ticker"])
            kind = self._exit_kind(p, di, price, cfg, rule)
            if kind is None and self.mkt.delists_on(di, p["ticker"]):
                kind = "delist"
            if kind is not None:
                self.close(di, pid, p["entry_price"] if math.isnan(price) else price, kind)


# ── One simulation ────────────────────────────────────────────────────────────

@dataclass
class SimResult:
    daily: pd.Series
    trades: list[dict]
    excluded: int
    exits: Counter = field(default_factory=Counter)

    @property
    def final(self) -> float:
        return float(self.daily.iloc[-1])


def run(mkt: Market, events: dict[int, list[Event]], cfg: SimConfig,
        exit_rule: Optional[ExitRule] = None, on_open: Optional[OpenHook] = None) -> SimResult:
    """Simulate one book over `mkt.cal`.

    Per session: exits first (so freed money can fund today's fills), then the
    fills scheduled for today (gated on the signal-day regime if configured),
    then mark-to-market.
    """
    if cfg.gate_dd is not None and mkt.dd is None:
        raise ValueError("gate_dd needs a Market built with spy_close")
    port = Portfolio(mkt, cfg.core, cfg.initial)
    daily = np.zeros(mkt.n)
    for di in range(mkt.n):
        port.process_exits(di, cfg, exit_rule)
        for ev in events.get(di, ()):
            if cfg.gate_dd is not None and not mkt.dd[ev.sig_idx] >= cfg.gate_dd:
                continue
            p = port.try_enter(di, ev.ticker, cfg)
            if p is not None and on_open is not None:
                on_open(p, di, mkt)
        daily[di] = port.value(di)
    assert not port.pos, "open positions remain — completion rule violated"
    return SimResult(daily=pd.Series(daily, index=mkt.cal), trades=port.trades,
                     excluded=port.excluded, exits=Counter(t["kind"] for t in port.trades))


# ── Metrics ───────────────────────────────────────────────────────────────────

def metrics(daily: pd.Series, cal: pd.DatetimeIndex, initial: float = INITIAL_CAP) -> dict:
    n_years = (cal[-1] - cal[0]).days / 365.25
    final = float(daily.iloc[-1])
    rmax = daily.cummax()
    dd = float(((daily - rmax) / rmax).min())
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() * np.sqrt(252) / rets.std()) if rets.std() > 0 else 0.0
    return {"final": final, "total": final / initial - 1,
            "cagr": (final / initial) ** (1 / n_years) - 1, "max_dd": dd,
            "sharpe": sharpe, "n_years": n_years}


BUCKETS = [("< -20%", lambda r: r <= -0.20), ("-20..0%", lambda r: -0.20 < r <= 0),
           ("0..20%", lambda r: 0 < r <= 0.20), ("20..50%", lambda r: 0.20 < r <= 0.50),
           ("50..100%", lambda r: 0.50 < r <= 1.0), (">100%", lambda r: r > 1.0)]


def trade_stats(trades: list[dict]) -> dict:
    r = np.array([t["ret"] for t in trades], float)
    if len(r) == 0:
        return {"n": 0}
    return {"n": len(r), "avg": float(r.mean()), "median": float(np.median(r)),
            "win": float((r > 0).mean()), "p90": float(np.percentile(r, 90)),
            "p95": float(np.percentile(r, 95)), "max": float(r.max()), "min": float(r.min()),
            "buckets": {name: int(sum(1 for x in r if c(x))) for name, c in BUCKETS},
            "kinds": dict(Counter(t.get("kind", "hold") for t in trades))}


def spy_curve(spy_close: pd.Series, cal: pd.DatetimeIndex,
              initial: float = INITIAL_CAP) -> pd.Series:
    s = spy_close.reindex(cal, method="ffill")
    return s / float(s.iloc[0]) * initial


def spy_metrics(spy_close: pd.Series, cal: pd.DatetimeIndex) -> dict:
    vals = spy_curve(spy_close, cal)
    m = metrics(vals, cal)
    m["daily"] = vals
    return m


# ── Research inputs + rolling windows ─────────────────────────────────────────

@dataclass
class Inputs:
    elig: dict                    # eligible crossings {ticker: [(ts, comp, close)]}
    panel: PricePanel             # closes on the full research calendar
    cal: pd.DatetimeIndex         # full research calendar (SPY sessions in window)
    spy_close: pd.Series          # full SPY history (for regime + benchmark)


def load_inputs(sim_start: str = DEFAULT_START, verbose: bool = True) -> Inputs:
    """Everything a research script needs, built once: eligible crossings from the
    cached intermediate, the SPY master calendar and the price panel."""
    data = ci.build_intermediate()
    elig = ci.eligible_crossings(data, ci.top_n_by_rebal(data))
    spy = load_spy()
    cal = spy.index
    cal = cal[(cal >= pd.Timestamp(sim_start)) & (cal <= ci.SIM_END)]
    panel = PricePanel(sorted(elig), cal)
    if verbose:
        n_elig = sum(len(v) for v in elig.values())
        print(f"  eligible crossings: {n_elig} across {len(elig)} tickers; calendar "
              f"{cal[0].date()}..{cal[-1].date()} ({len(cal)} sessions); panel "
              f"{panel.df.shape[1]} tickers", flush=True)
    return Inputs(elig=elig, panel=panel, cal=cal, spy_close=spy["Close"].astype(float))


@dataclass
class Window:
    name: str
    cal: pd.DatetimeIndex
    mkt: Market
    events: dict[int, list[Event]]
    spy: dict                     # spy_metrics over this window


def make_window(inp: Inputs, cal: pd.DatetimeIndex, name: str) -> Window:
    return Window(name=name, cal=cal, mkt=Market(inp.panel, cal, inp.spy_close),
                  events=schedule_events(inp.elig, cal), spy=spy_metrics(inp.spy_close, cal))


def full_window(inp: Inputs) -> Window:
    return make_window(inp, inp.cal, f"{inp.cal[0].year}-{inp.cal[-1].year}")


def rolling_windows(inp: Inputs, win_len: int, first_year: Optional[int] = None,
                    last_year: Optional[int] = None) -> list[Window]:
    """Fixed-length calendar-year windows stepped one year at a time."""
    y0 = first_year if first_year is not None else inp.cal[0].year
    y1 = last_year if last_year is not None else inp.cal[-1].year
    out = []
    for sy in range(y0, y1 - win_len + 2):
        w0, w1 = pd.Timestamp(f"{sy}-01-01"), pd.Timestamp(f"{sy + win_len - 1}-12-31")
        cw = inp.cal[(inp.cal >= w0) & (inp.cal <= w1)]
        if len(cw) >= 252:
            out.append(make_window(inp, cw, f"{sy}-{sy + win_len - 1}"))
    return out


def evaluate_rolling(windows: list[Window],
                     sim_fn: Callable[[Window], pd.Series]) -> dict:
    """Run `sim_fn(window) -> daily value series` on every window and summarise
    it against SPY over the same window: beat-rate on CAGR, median excess CAGR,
    median Sharpe, median max drawdown (plus the per-window lists)."""
    rows = []
    for w in windows:
        m = metrics(sim_fn(w), w.cal)
        rows.append({"win": w.name, "cagr": m["cagr"], "sharpe": m["sharpe"],
                     "max_dd": m["max_dd"], "spy_cagr": w.spy["cagr"],
                     "spy_sharpe": w.spy["sharpe"], "excess": m["cagr"] - w.spy["cagr"],
                     "beat": m["cagr"] > w.spy["cagr"],
                     "beat_sharpe": m["sharpe"] > w.spy["sharpe"]})
    exc = [r["excess"] for r in rows]
    shp = [r["sharpe"] for r in rows]
    return {"rows": rows, "n": len(rows), "beats": sum(r["beat"] for r in rows),
            "beats_sharpe": sum(r["beat_sharpe"] for r in rows),
            "med_excess": float(np.median(exc)) if rows else 0.0,
            "med_sharpe": float(np.median(shp)) if rows else 0.0,
            "std_sharpe": float(np.std(shp)) if rows else 0.0,
            "min_sharpe": float(np.min(shp)) if rows else 0.0,
            "med_dd": float(np.median([r["max_dd"] for r in rows])) if rows else 0.0}


def spy_median_sharpe(windows: list[Window]) -> float:
    return float(np.median([w.spy["sharpe"] for w in windows]))


def overlay_series(w: Window, gate: Optional[float], hold: int = 504, pct: float = 0.10,
                   max_pos: int = 10) -> pd.Series:
    """SPY-core conditional overlay: idle money in SPY, sleeves only when the
    signal-day market drawdown >= gate (None = always deploy)."""
    cfg = SimConfig(hold=hold, pct=pct, max_pos=max_pos, core="spy", gate_dd=gate)
    return run(w.mkt, w.events, cfg).daily
