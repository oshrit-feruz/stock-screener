#!/usr/bin/env python3
"""Rank the monthly Top-N large-cap universe and write data/universe/current.json.

ARCHITECTURE (docs/ARCHITECTURE.md): **GitHub Actions computes, Render reads.**
This is the only place the expensive point-in-time market-cap ranking runs. It
executes once a month on a GitHub Actions runner (~16GB RAM, no time pressure),
and its output — a plain JSON list of tickers — is committed to main. Render's
web service never ranks; product/screener/universe_list.py only reads this file.

Cadence is MONTHLY, matching product/backtest/engine.py's `_UNIVERSE_N`
("rebuilt monthly") and the research harness. Do not change it to quarterly or
anything else without re-running the backtest validation: the live strategy must
be the strategy whose PSR/DSR statistics were measured, or live results stop
being attributable to the backtest.

The as-of date is the FIRST TRADING DAY of the target month — the same key the
backtest uses (`fmonths.setdefault((ts.year, ts.month), ts)`), so a live month's
universe is computed identically to that month's backtest universe.

    EODHD_API_KEY=... python scripts/build_universe_list.py
    EODHD_API_KEY=... python scripts/build_universe_list.py --month 2026-09
    EODHD_API_KEY=... python scripts/build_universe_list.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import data.sp500_universe as u  # noqa: E402
from core.data.eodhd import fetch_eod  # noqa: E402
from core.data.prices import PriceData  # noqa: E402

_OUT = REPO / "data" / "universe" / "current.json"
_RAW = REPO / "data" / "cache" / "prices_raw"
TOP_N = 100

# Raw-price history needed per ticker. The ranking only ever reads the LAST bar
# on or before the as-of date (data.sp500_universe._raw_close), so deep history
# is dead weight here — historical ranking comes from the prebuilt PIT grid, not
# from this job. One year comfortably covers a stale/thin ticker.
_RAW_LOOKBACK_DAYS = 365


def _first_trading_day(year: int, month: int) -> date | None:
    """First trading day of the month, from SPY's actual calendar.

    Mirrors the backtest, which derives month keys from SPY's price index rather
    than from a synthetic calendar, so the two agree on month boundaries.

    Returns None when the month has no SPY bar yet — the normal case when this
    runs on the 1st of a month that begins on a weekend or holiday. The caller
    treats that as "nothing to do yet", not as a failure.
    """
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    spy = PriceData().get_prices("SPY", start.isoformat(), end.isoformat())
    if spy is None or spy.empty:
        return None
    return spy.index.min().date()


def _already_current(as_of: date, tickers: list[str]) -> bool:
    """True if the committed list already matches this ranking.

    Keeps the job idempotent: the workflow runs on several days at the start of
    each month so a single failure self-heals, and without this check every one
    of those runs would rewrite `generated_at` and push a no-op commit (each of
    which redeploys Render).
    """
    if not _OUT.exists():
        return False
    try:
        cur = json.loads(_OUT.read_text())
    except Exception:
        return False
    return cur.get("as_of") == as_of.isoformat() and cur.get("tickers") == tickers


def _frame_reaches(df, as_of: date) -> bool:
    """True if an in-memory raw-price frame actually reaches `as_of`.

    fetch_eod() returns an EMPTY frame on any provider failure, but it can also
    return a NON-empty frame whose last bar predates as_of (thin coverage, a
    halt, a partial response). Accepting the latter stores a stale close that
    the very same build then ranks on — silently, since the file exists and
    looks populated.
    """
    if df is None or getattr(df, "empty", True):
        return False
    return df.index.max().date() >= as_of


def _covers(path: Path, as_of: date) -> bool:
    """True if a cached raw-price frame actually reaches `as_of`.

    Presence is NOT freshness. The Actions cache carries data/cache between runs,
    so a file fetched last month still exists this month — and _raw_close() takes
    the last bar on or before the as-of date, which would silently be last
    month's close. Left unchecked the ranking drifts further out of date every
    month, with no error: exactly the silent-degradation class this whole design
    exists to prevent.
    """
    try:
        with open(path, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return False
    return _frame_reaches(df, as_of)


def _ticker_is_current(raw_dir: Path, ticker: str, as_of: date) -> bool:
    """True if the raw file `_raw_close()` will actually read reaches `as_of`.

    Freshness must be judged on the CHOSEN file — data.sp500_universe._raw_close
    resolves a ticker with `sorted(glob(f"{ticker}_*.pkl"))[0]`, the earliest
    start date — not on "some file for this ticker is fresh". With an old file
    and a fresh one side by side the latter is true while the ranking still
    reads the stale one, so an `any()` test silently passes a ticker whose price
    is months out of date. Judging the chosen file collapses that case into
    "stale"; the refresh then unlinks the duplicates.
    """
    existing = sorted(raw_dir.glob(f"{ticker}_*.pkl"))
    if not existing:
        return False
    return _covers(existing[0], as_of)


def _refresh_start(raw_dir: Path, ticker: str, default_start: str) -> str:
    """Earliest start to refetch from: never later than history already held.

    Returns the earliest start date encoded in this ticker's existing filenames
    if it predates `default_start`, else `default_start`. Keeps a refresh from
    truncating the deep history build_full_cache.py relies on.
    """
    starts = []
    for p in raw_dir.glob(f"{ticker}_*.pkl"):
        stem = p.stem.rsplit("_", 1)
        if len(stem) == 2:
            try:
                date.fromisoformat(stem[1])
                starts.append(stem[1])
            except ValueError:
                continue
    return min(starts + [default_start])


def _ensure_raw_prices(pool: list[str], as_of: date) -> tuple[int, list[str]]:
    """Fetch raw (unadjusted) price series that are missing OR stale for `as_of`.

    Raw prices are required because market cap must be raw_close x raw shares —
    split-adjusted prices would deflate every future-splitter and corrupt the
    cross-sectional ranking.

    Writes exactly ONE file per ticker, replacing any earlier one. This is
    load-bearing, not tidiness: data.sp500_universe._raw_close() resolves a
    ticker with `sorted(glob(f"{ticker}_*.pkl"))[0]` — the EARLIEST start date.
    Leaving last month's file alongside a fresh one would mean the ranking keeps
    reading the stale file and the refresh silently has no effect.
    """
    _RAW.mkdir(parents=True, exist_ok=True)
    default_start = (pd.Timestamp(as_of) - pd.Timedelta(days=_RAW_LOOKBACK_DAYS)).date().isoformat()

    stale = [t for t in pool if not _ticker_is_current(_RAW, t, as_of)]

    print(f"Raw prices: {len(pool) - len(stale)} current, fetching/refreshing {len(stale)}…")
    got = 0
    failed: list[str] = []
    for t in stale:
        # Never shrink a ticker's history. scripts/build_full_cache.py writes
        # deep raw files (2009-onward) that the PIT grid rebuild depends on, and
        # this job replaces the ticker's file. Refetching only our 1-year window
        # would silently truncate that history, and build_full_cache's own
        # "already have a file?" check would then skip re-fetching it — quietly
        # breaking the next grid build. So extend from the earliest start we
        # already hold.
        start = _refresh_start(_RAW, t, default_start)
        try:
            df = fetch_eod(t, start, as_of.isoformat(), adjust=False)
            # Validate BEFORE touching what is on disk. Unlinking first would
            # destroy good (possibly deep-history) data and replace it with a
            # short or empty response — turning a transient provider hiccup into
            # permanent cache damage.
            if not _frame_reaches(df, as_of):
                failed.append(t)
                continue
            # Replace, never accumulate — see _ticker_is_current().
            for old in _RAW.glob(f"{t}_*.pkl"):
                old.unlink(missing_ok=True)
            with open(_RAW / f"{t}_{start}.pkl", "wb") as f:
                pickle.dump(df, f)
            got += 1
        except Exception as exc:
            failed.append(t)
            print(f"  raw {t}: {exc!r}"[:120])
        finally:
            time.sleep(0.1)
    print(f"  refreshed {got}/{len(stale)}" + (f", {len(failed)} FAILED" if failed else ""))
    return got, failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", help="Target month YYYY-MM (default: current month)")
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--dry-run", action="store_true", help="Rank but do not write the file")
    args = ap.parse_args()

    if args.month:
        year, month = (int(x) for x in args.month.split("-"))
    else:
        today = date.today()
        year, month = today.year, today.month

    as_of = _first_trading_day(year, month)
    if as_of is None:
        today = date.today()
        if (year, month) == (today.year, today.month):
            print(f"{year}-{month:02d} has no trading day yet — nothing to do.")
            return 0
        print(f"ERROR: no SPY data for {year}-{month:02d}; cannot determine its "
              f"first trading day.", file=sys.stderr)
        return 1
    print(f"Target month {year}-{month:02d} → as_of (first trading day) {as_of}")

    pool = sorted(u.get_universe(as_of.isoformat()))
    print(f"Ranking pool: {len(pool)} S&P 500 members")
    if not pool:
        print("ERROR: membership lookup returned 0 tickers.", file=sys.stderr)
        return 1

    _got, failed = _ensure_raw_prices(pool, as_of)
    if failed:
        # Abort BEFORE ranking. A ticker with no current raw price gets mcap
        # None and is silently dropped from the pool — and because the Top-N is
        # drawn from ~500 members, the list still comes out at exactly N, so the
        # len(tickers) < top_n guard below does NOT catch it. The published
        # universe would just quietly omit whichever large-caps failed to fetch.
        print(
            f"ERROR: {len(failed)} ticker(s) have no raw price covering {as_of} after "
            f"refresh: {', '.join(sorted(failed)[:20])}"
            f"{' …' if len(failed) > 20 else ''}. Refusing to rank on an incomplete "
            f"pool — re-run once the provider recovers.",
            file=sys.stderr,
        )
        return 1

    tickers = u.get_universe_top_n(as_of.isoformat(), args.top_n)
    print(f"Ranked Top-{args.top_n}: {len(tickers)} tickers")

    # Refuse to publish a degraded list. A short list means market caps could not
    # be computed for much of the pool — writing it would quietly narrow the
    # strategy's opportunity set, which is exactly the silent-degradation class
    # this whole design is meant to prevent.
    if len(tickers) < args.top_n:
        print(
            f"ERROR: ranking produced {len(tickers)}/{args.top_n} tickers — the pool's "
            f"market caps are incomplete (missing raw prices or EDGAR shares). "
            f"Refusing to publish a partial universe.",
            file=sys.stderr,
        )
        return 1

    if _already_current(as_of, tickers):
        print(f"Universe list already current for {as_of} — nothing to write.")
        return 0

    payload = {
        "as_of": as_of.isoformat(),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "n": args.top_n,
        "pool_size": len(pool),
        "tickers": tickers,
    }

    if args.dry_run:
        print(json.dumps(payload, indent=2)[:600])
        print("(--dry-run: not written)")
        return 0

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {_OUT} ({len(tickers)} tickers, as_of {as_of})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
