#!/usr/bin/env python3
"""Rank the monthly Top-N large-cap universe and write data/universe/current.json.

ARCHITECTURE (docs/ARCHITECTURE.md): **GitHub Actions computes, Render reads.**
This is the only place the point-in-time ranking runs. The rank is by trailing
median DOLLAR-VOLUME (raw close x raw volume; data.sp500_universe.get_universe_top_n),
which is survivorship-free — it needs only prices, so it never depends on SEC's
active-tickers list the way the former market-cap ranking did. It
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
import logging
import os
import pickle
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import data.sp500_universe as u  # noqa: E402
from core.data.eodhd import fetch_eod, probe_bars  # noqa: E402
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


def _atomic_write_pickle(target: Path, obj) -> None:
    """Write `obj` to `target` atomically: temp file, fsync, os.replace.

    A plain `open(target, "wb")` truncates the destination the instant it is
    called, so a failure mid-write (disk full, the job's 45-minute timeout
    killing the process) leaves a truncated file where good data was. os.replace
    is atomic within a filesystem, so `target` is either the old bytes or the
    complete new ones — never half of either. The temp file is removed on
    failure so a crashed run leaves no litter.
    """
    tmp = target.with_name(target.name + ".tmp")   # not *.pkl -> invisible to the globs
    try:
        with open(tmp, "wb") as f:
            pickle.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


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
    """True if the raw file `_raw_frame()` will actually read reaches `as_of`.

    Freshness must be judged on the CHOSEN file — data.sp500_universe picks the
    deepest-history candidate under either naming contract (safe `BRKB_*` or
    legacy `BRK.B_*`, see `_raw_candidates`) — not on "some file for this
    ticker is fresh". With an old file and a fresh one side by side the latter
    is true while the ranking still reads the stale one, so an `any()` test
    silently passes a ticker whose price is months out of date. Judging the
    chosen file collapses that case into "stale"; the refresh then unlinks the
    duplicates under both spellings.
    """
    existing = u._raw_candidates(ticker, raw_dir)
    if not existing:
        return False
    return _covers(existing[0], as_of)


def _refresh_start(raw_dir: Path, ticker: str, default_start: str) -> str:
    """Earliest start to refetch from: never later than history already held.

    Returns the earliest start date encoded in this ticker's existing filenames
    (either naming contract) if it predates `default_start`, else
    `default_start`. Keeps a refresh from truncating the deep history
    build_full_cache.py relies on.
    """
    starts = [u._raw_start_date(p, ticker) for p in u._raw_candidates(ticker, raw_dir)]
    return min([s for s in starts if s] + [default_start])


# A name whose last bar is at least this many calendar days before the as-of
# date, and for which the provider explicitly answers "no bars" for the tail,
# has stopped trading (acquired, taken private, delisted). Shorter gaps are
# treated as the provider not having caught up yet, and stay a hard failure:
# the job runs on days 1-5 of the month, so a day or two of provider lag is
# real, while a fortnight of nothing after a final print is not.
_DELIST_GRACE_DAYS = 7


def _classify_stale(ticker: str, df, as_of: date, probe=None) -> str:
    """Why a fetched frame does not reach `as_of`: 'current', 'delisted' or 'failed'.

    'delisted' needs BOTH a real last bar well before as_of AND the provider's
    own confirmation (`probe` -> False, i.e. HTTP 200 with an empty list for the
    window after that bar). An empty frame, a probe error or a recent last bar
    is 'failed' — the job must not rank on it, and must not drop the name
    either, because a transient provider problem must never quietly shrink the
    pool. That is the same silent-degradation class the whole guard exists for.

    Why this is needed at all: the membership list (fja05680) is a periodic
    snapshot, so a member acquired or delisted since the last snapshot still
    appears in the pool while the price provider has correctly stopped printing
    it. Without this the monthly build cannot complete until the snapshot is
    refreshed, which can take months.
    """
    if _frame_reaches(df, as_of):
        return "current"
    if df is None or getattr(df, "empty", True):
        return "failed"
    last_bar = df.index.max().date()
    if (as_of - last_bar).days < _DELIST_GRACE_DAYS:
        return "failed"
    tail_start = (pd.Timestamp(last_bar) + pd.Timedelta(days=1)).date().isoformat()
    # Resolved at call time (not as a default argument) so tests can stub the
    # module's probe_bars and the classification actually sees the stub.
    has_more = (probe or probe_bars)(ticker, tail_start, as_of.isoformat())
    return "delisted" if has_more is False else "failed"


def _ensure_raw_prices(pool: list[str], as_of: date) -> tuple[int, list[str], list[str]]:
    """Fetch raw (unadjusted) price series that are missing OR stale for `as_of`.

    Raw prices are required because dollar-volume must be raw_close x raw volume —
    split-adjusted prices and volumes would distort every future-splitter and
    corrupt the cross-sectional ranking.

    Writes exactly ONE file per ticker (canonical filesystem-safe name, the same
    contract as the clean-universe fetch), replacing any earlier one under
    either spelling. This is load-bearing, not tidiness:
    data.sp500_universe._raw_frame() reads the deepest-history candidate across
    BOTH naming contracts. Leaving last month's file alongside a fresh one would
    mean the ranking keeps reading the stale file and the refresh silently has
    no effect.

    Returns (refreshed, failed, delisted). `failed` names must abort the build;
    `delisted` names (see `_classify_stale`) have stopped trading since the
    membership snapshot and are excluded from the pool by the caller.
    """
    _RAW.mkdir(parents=True, exist_ok=True)
    default_start = (pd.Timestamp(as_of) - pd.Timedelta(days=_RAW_LOOKBACK_DAYS)).date().isoformat()

    stale = [t for t in pool if not _ticker_is_current(_RAW, t, as_of)]

    print(f"Raw prices: {len(pool) - len(stale)} current, fetching/refreshing {len(stale)}…")
    got = 0
    failed: list[str] = []
    delisted: list[str] = []
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
            verdict = _classify_stale(t, df, as_of)
            if verdict == "failed":
                failed.append(t)
                continue
            if verdict == "delisted":
                last_bar = df.index.max().date()
                print(f"  {t}: no bars after {last_bar} and the provider confirms none — "
                      f"stopped trading; excluded from this month's pool")
                delisted.append(t)
                continue
            # Order matters twice over:
            #   1. write the replacement atomically FIRST, so nothing on disk is
            #      destroyed unless the new file is complete (round 4's rule:
            #      never mutate before the replacement is known good);
            #   2. only THEN drop the ticker's other files, because
            #      _ticker_is_current()/_raw_close() depend on there being
            #      exactly one — an atomic replace alone would leave an older
            #      earliest-start duplicate winning the sort.
            target = _RAW / f"{u._safe_ticker(t)}_{start}.pkl"
            _atomic_write_pickle(target, df)
            for old in u._raw_candidates(t, _RAW):  # both spellings
                if old != target:
                    old.unlink(missing_ok=True)
            got += 1
        except Exception as exc:
            failed.append(t)
            print(f"  raw {t}: {exc!r}"[:120])
        finally:
            time.sleep(0.1)
    print(f"  refreshed {got}/{len(stale)}"
          + (f", {len(delisted)} stopped trading" if delisted else "")
          + (f", {len(failed)} FAILED" if failed else ""))
    return got, failed, delisted


def _diagnose_empty_ranking(pool: list[str], as_of: date, sample: int = 5) -> str:
    """Explain WHICH dollar-volume input is missing, for the abort message.

    "ranking produced 0/100" on its own is not actionable — the trailing median
    dollar-volume needs a raw close AND at least `_DV_WINDOW` sessions of raw
    volume on/before the as-of date. Naming the failing side turns a debugging
    session into a glance at the log.
    """
    no_price, no_dv, ok = [], [], []
    for t in pool[:sample]:
        px = u._raw_close(t, as_of.isoformat())
        if not px or px <= 0:
            no_price.append(t)
            continue
        dv = u.pit_dollar_volume(t, as_of.isoformat())
        (ok if (dv and dv > 0) else no_dv).append(t)
    parts = [f"sampled {len(pool[:sample])} pool tickers"]
    if no_price:
        parts.append(f"{len(no_price)} missing raw close ({', '.join(no_price)})")
    if no_dv:
        parts.append(
            f"{len(no_dv)} missing trailing dollar-volume ({', '.join(no_dv)}) — "
            f"fewer than {u._DV_WINDOW} raw sessions with volume on/before {as_of}"
        )
    if ok:
        parts.append(f"{len(ok)} had both")
    return "; ".join(parts)


def main() -> int:
    # Without this the module loggers have no handler configured and the EDGAR /
    # EODHD diagnostics fall back to Python's bare lastResort output. This job's
    # whole value when it fails is its log.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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

    _got, failed, delisted = _ensure_raw_prices(pool, as_of)
    if delisted:
        # Still members on the last membership snapshot, but the provider
        # confirms they have stopped trading since. They cannot be ranked (no
        # current price) and must not block the build; say so, loudly.
        print(
            f"WARNING: {len(delisted)} member(s) stopped trading after the membership "
            f"snapshot and are excluded from this month's pool: {', '.join(sorted(delisted))}",
            file=sys.stderr,
        )
        pool = [t for t in pool if t not in set(delisted)]
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

    # Refuse to publish a degraded list. A short list means dollar-volumes could
    # not be computed for much of the pool — writing it would quietly narrow the
    # strategy's opportunity set, which is exactly the silent-degradation class
    # this whole design is meant to prevent.
    if len(tickers) < args.top_n:
        print(
            f"ERROR: ranking produced {len(tickers)}/{args.top_n} tickers — the pool's "
            f"dollar-volumes are incomplete (missing raw prices / volume history). "
            f"Refusing to publish a partial universe. "
            f"Diagnostic: {_diagnose_empty_ranking(pool, as_of)}",
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
