# Architecture: what computes, and what only reads

**Decided 2026-08-23. Read this before adding any scheduled job, cache, or
"compute it on demand" endpoint.**

## The rule

> **GitHub Actions computes. Render reads.**
>
> Anything expensive — universe ranking, cache building — runs in GitHub Actions
> and is written down. The Render web service reads what Actions produced.

**Where the rule holds today, precisely** — stated exactly rather than
aspirationally, because a doc that overstates the invariant is the same hazard
as a health check that watches the wrong directory:

| Work | Runs on Render? |
|---|---|
| Universe ranking (~500-member PIT market cap) | **No.** Actions only. Render reads `data/universe/current.json`. |
| Prebuilt PIT grid / price cache build | **No.** Manual `build_full_cache.py`, shipped as a Release asset. |
| Daily signal scan of the 100 listed tickers | **Yes, still** — `main.py`'s `_get_screener_data()` calls `run_screener()` on a cache miss, and the startup warm can too. |

So the rule is fully enforced for the expensive part (the ~500-ticker ranking,
the +188MB path that caused the OOM restarts) and **not yet** for the 100-ticker
signal scan. That scan is roughly a fifth of the work and no longer does any
market-cap computation, but it is not zero and it is not precomputed.

**Follow-up to close the gap:** Actions already computes the daily screen and
persists it to `automation/daily-state`, which Render does not read. Making
Render serve that artifact instead of calling `run_screener()` would make the
rule absolute. Until then, do not read the heading as "Render computes nothing".

Two corollaries, both load-bearing:

1. **There is exactly one producer per artifact.** If two places can compute the
   same thing, they will disagree, and the disagreement will be silent.
2. **A missing input is an error, never a fallback.** Never a *substitute*
   universe, never an empty list, never an unbounded stale default. Precisely:
   a late monthly list is served only while it is within the 62-day cutoff
   (`universe_list.MAX_AGE_DAYS`) **and** only with a `is_late` WARNING naming
   the problem; past the cutoff, and for anything missing/malformed/empty, the
   caller raises. Degradation is allowed to be bounded and noisy. It is never
   allowed to be silent.

## Why (the incident this rule comes from)

Between **2026-07-01 and 2026-08-23** the live screener returned **zero signals
every single day** — 30+ consecutive CI runs logging `scanning 0 tickers`, and
`/api/screener` serving `{"buy_signals": [], "full_ranking": []}` with HTTP 200.
Nobody noticed for ~7.5 weeks.

The chain:

- `2026-06-29` point-in-time market cap introduced; ranking now needs
  `data/cache/prices_raw`.
- `2026-07-01` the screener switched from a static committed ticker list to
  `get_universe_top_n()` — so it now depended on `prices_raw`.
- `2026-07-07` the seed-cache mechanism shipped, listing five subdirectories to
  package. `prices_raw` was **not** among them, and never has been in any commit.

On Render, `_raw_close()` found no files → every market cap was `None` → the
ranking returned `[]` → the screener scanned nothing → and it reported that as a
**successful run**.

Three separate things kept it invisible:

- **The health check watched the wrong directory.** It counted `data/cache/prices`
  (230 files ✓) and PIT grid months (198 ✓) — both healthy — and never mentioned
  `prices_raw`, the dependency that was actually absent.
- **The failure was caught at WARNING and swallowed.** `universe = []` on
  exception, then business as usual.
- **The backtest kept working**, because it only queries grid dates served from
  the prebuilt grid and never calls `_raw_close()`. So the system looked fine
  from the one angle anyone was checking.

## What this means in practice

| Artifact | Produced by | Consumed by | Transport |
|---|---|---|---|
| Monthly Top-100 universe | `scripts/build_universe_list.py` via `.github/workflows/monthly-universe.yml` | daily screener, `/api/screener` | `data/universe/current.json`, **committed to main** |
| Daily screening state, positions, alerts | `.github/workflows/daily-screener.yml` | — | `automation/daily-state` branch |
| Prebuilt PIT grid + price cache | `scripts/build_full_cache.py` (manual) | Simulator/backtest | GitHub Release asset → `scripts/fetch_release_cache.py` |

The universe list is the one artifact committed to **main**, deliberately: it
must reach the Render web service, and a Render redeploy once a month is the
correct cost for a universe change. The daily workflow still avoids main on
purpose — a daily redeploy would be pure churn.

## Cadence is not a free parameter

The universe rebuilds **monthly** because `product/backtest/engine.py` rebuilds
it monthly (`_UNIVERSE_N`, "rebuilt monthly") and the research harness does too.
Live and backtest must use the same cadence, or **live is no longer running the
strategy whose PSR/DSR statistics were measured** — and those statistics are the
product's core claim.

Quarterly was considered and rejected for exactly this reason. It would have cut
rankings from 12/year to 4/year — a negligible saving, since the daily job's cost
is unchanged either way — in exchange for making the live strategy unvalidated.
If you ever want to change this cadence, re-run backtest validation first.

## Universe transitions

The universe list gates **entry only** (`engine.py`, inside the BUY-signal
block). Exits are time-based (252 trading days) or threshold-based and never
consult membership. Therefore:

- A new monthly list takes effect **immediately** for new entries.
- **Open positions are untouched** — they run to their 252-day exit even if the
  ticker drops off the list.
- No grace period is needed, and none should be added: this is already exactly
  how the backtest behaves, and divergence here would break parity.

## Known follow-up (not yet addressed)

`_pit_entry_valid()` reuses a cached PIT market cap if it is <30 days old **or**
its as-of date is >120 days old. Dates in the **30–120 day gap** expire and get
recomputed — which on Render silently yields `None`. The backtest's most recent
months can therefore drop members as the seed ages, with no error. Fixing this
needs a judgment call on the right thresholds; tracked separately.
