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
| Universe ranking (~500-member PIT dollar-volume; survivorship-free, replaced the EDGAR market-cap rank) | **No.** Actions only. Render reads `data/universe/current.json`. |
| Prebuilt PIT grid / price cache build | **No.** Manual `build_full_cache.py`, shipped as a Release asset. |
| Daily signal scan of the 100 listed tickers | **Not by default.** `main.py`'s `_get_screener_data()` serves the newest published daily-state result and answers **503** on a cache miss; it calls `run_screener()` in-process only when `SCREENER_ONDEMAND_SCAN=1` is set (the startup warm, when it runs, can too). |

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
| Daily screening state, alerts | `.github/workflows/daily-screener.yml` | — | `automation/daily-state` branch |
| The position book | `/api/positions/*` (Render) **and** the daily exit tracker (Actions) | both of those, plus the beta report | Supabase `public.bot_positions` |
| Daily screener result (`data/screener_cache/<date>.json`) | `.github/workflows/daily-screener.yml` | `/api/screener` (raw-file fetch, 4-**trading**-day lookback, `computed_on` provenance) | `automation/daily-state` branch |
| Prebuilt PIT grid + price cache | `scripts/build_full_cache.py` (manual) | Simulator/backtest | GitHub Release asset → `scripts/fetch_release_cache.py` |

### A month's universe is written once

The Top-100 list is the fingerprint every published screener result is
validated against, so rewriting it is not a refresh — it retroactively
invalidates every result computed under the old one. `/api/screener` then
discards them as wrong rather than stale, which is correct and also exactly how
a rewrite turns into a 503.

The monthly workflow runs on days 1-5 so a failed run self-heals. Those extra
days are retries for a *failure*; a run that published is final for that month.
The guard used to require the freshly computed tickers to match as well, which
inverted the intent — any difference, including one caused by a transient data
problem, read as "not yet current" and rewrote the file.

September 2026 rewrote the same as-of date four times. On the 4th the list lost
HON from rank 97 and gained ON at 100, then reverted on the 5th. Rank 97 is not
a boundary name: it dropped out because it produced no dollar-volume at all, and
the ranking drops such a member silently while rank 101 slides up — so the list
still came out at exactly 100 and the length guard never fired. Two of the four
days in `/api/screener`'s lookback window were spent on results the service then
refused to serve.

Both halves are now guarded: the build exits before any provider work once the
month is published, and refuses to rank at all while any pool member cannot
produce a dollar-volume, naming it.

### The staleness window is counted in trading days

`/api/screener` serves the newest published result within a bounded window, and
that window is measured in **trading days**, not calendar days — because the
producer only emits on days the NYSE is open. A consumer counting calendar days
spends its budget on days that could never have carried a result.

That is not hypothetical. After Labor Day 2026 the four-calendar-day window from
Tuesday reached back only to the Friday; Saturday, Sunday and the holiday
consumed the rest. The one weekday left inside it had been computed under a
universe that was since rebuilt, so the fingerprint check correctly discarded
it — and `/api/screener` answered 503 with two perfectly good results sitting
one day outside the window. Both halves now share one definition of "the market
was open", in `product/market_calendar.py`.

These two are the same outage seen from both ends, and neither fix subsumes the
other. The window was too narrow because it counted in the wrong unit; it was
empty because the universe rewrote itself under it. Widening a window that keeps
being invalidated only postpones the next 503, and a stable universe still needs
a window long enough to reach the last day the market was open.

### The position book is the one thing both halves write

Everything else in the table has a single writer. The position book has two —
the app, when you press Open or Close, and the daily run, when a position
reaches its exit — and they run on different machines. That is why it is the
one artifact in Postgres rather than in git.

It used to be `data/positions/*.json`, and that could not work. Actions
committed those files to `automation/daily-state`; Render read the same paths
from its own checkout of main, which holds `[]`, wrote back to a container
filesystem that is discarded on restart, and fetched only `data/screener_cache`
from that branch. So a position opened in the app was invisible to the exit
tracker — nothing would ever fire its exit — and was gone at the next restart.
Neither side was wrong on its own; they were never looking at the same bytes.

All reads and writes now go through `product/storage/positions.py`. It talks to
Supabase when `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are both set, and falls
back to the JSON files otherwise, which is what lets the test suite and local
development run with no credentials and no network. The fallback is not an
error handler: if Supabase is configured but unreachable the module raises,
because silently writing to a file nobody reads is the failure it exists to
prevent.

Access is service-role only. RLS is enabled on `bot_positions` with **no
policies**, so the anon key — which is public and ships in the browser — can
neither read nor write it; the service role bypasses RLS. `SUPABASE_SERVICE_KEY`
is server-side only: never in `product/web/`, never in a URL.

The universe list is the one artifact committed to **main**, deliberately: it
must reach the Render web service, and a Render redeploy once a month is the
correct cost for a universe change. The daily workflow still avoids main on
purpose — a daily redeploy would be pure churn.

A new universe list also **invalidates every published daily result**: each
result carries the fingerprint of the universe it was computed under, and
`/api/screener` refuses one computed under a superseded list (wrong universe,
not merely stale). So the monthly workflow, right after it pushes a changed
list, dispatches `daily-screener.yml` to publish a result under the new
fingerprint. Without that hand-off the screener answered 503 from the monthly
commit until the next scheduled daily run — up to a day, longer over a weekend.

## Publish steps must positively verify what they published

Every producer in the table above ends in a publish step — a commit, a push, a
file landing somewhere a consumer reads. The rule for those steps:

> **A publish step must positively confirm the artifact it published — its
> presence, and the property that makes it usable — not merely finish without
> error. "No error" and "published nothing" are indistinguishable unless you
> make them distinguishable.**

This is not hypothetical. The same bug class shipped **three times in one day**
(2026-08-23), each through a different door, each producing a green run that
published nothing:

1. **`git diff` on an untracked file** (monthly universe, PR #49). The guard
   `git diff --quiet -- current.json` was meant to skip no-op commits — but
   `git diff` compares *tracked* paths only, so on the very first build the
   brand-new file registered as "no change" and the commit was skipped. Green
   run, no artifact, live endpoint 503.
2. **`return None` on every failure** (EDGAR client). A total SEC outage — 503
   identical HTTP 403s — presented as "0 tickers ranked" with no indication
   the provider was the cause, because every failure mode collapsed into the
   same silent `None` a legitimate miss produces.
3. **`git add` on a gitignored path** (daily screener publish, PR #51 —
   caught in review, not production). `data/screener_cache/*.json` is
   gitignored, and plain `git add` skips ignored files *silently*; the
   empty-commit guard would then pass and the run would go green having
   published nothing. `git add -f` plus a comment is the fix.

The shared shape: a tool whose "nothing to do" answer is identical to its
"failed to do it" answer, wrapped in a step that only checks for errors.

What a publish step therefore does, concretely:

- **Assert the artifact exists** before the skip-guard runs, and treat absence
  as its own explicit outcome (see the missing-file check in
  `monthly-universe.yml` — absence must never stage a deletion either).
- **Diff the index, not the worktree** (`git add` then `git diff --cached`)
  when deciding "did anything change", so untracked files count.
- **Force-add published paths that are gitignored**, with a comment saying
  why — an unexplained `-f` will be "cleaned up" by the next reader.
- **Echo what was published** (date, ticker count, target ref) so the log of a
  healthy run states its output, and a run that published nothing reads wrong
  on sight.
- **Consumers stay loud**: a consumer finding nothing serves an explicit
  error naming the producer (`/api/screener`'s 503 does this), never a
  plausible empty result. The 7.5-week outage happened because an empty
  universe was served as a normal "0 signals" day.

When adding a new producer, assume its publish step has this bug until shown
otherwise — the third instance was written *while consciously hunting the
first two*.

The same shape exists one layer down, in tests: a broad `except` around a
fail-soft call converts a **signature error in a test double** into a
plausible "unavailable" (`_FakePrices.get_prices` missing a new keyword
surfaced as `current_price=None`, not as a `TypeError`). When a fake stands in
for a fail-soft interface, keep its signature exactly in step with the real
one — the except block will otherwise absorb the drift silently.

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
