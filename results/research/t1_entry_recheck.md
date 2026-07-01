# Entry look-ahead fix (T+1 open) + splits & capital audit

## Part 1 — Entry look-ahead bias: FIXED

The composite score on day T uses day-T's close, but entries previously also
filled at day-T's close (same bar) — look-ahead. Entries now fill at **day T+1's
open** (first realistically executable price). The 252-day time-based exit is
unchanged. Research/backtest engines keep T+1 **opt-in** (legacy default → prior
studies reproduce exactly); the product Simulator (`engine.py`) defaults to T+1.

### Before/after (2018-2024, $100k, 252d exit)

| Config | Fill | Final $ | Total ret | CAGR | Sharpe | Max DD | Trades |
|---|---|--:|--:|--:|--:|--:|--:|
| A · Best (Top-100 PIT + thr0.60 + money market) | same-bar close | $296,397 | +196.4% | +16.8% | 0.82 | −31.4% | 41 |
| A · Best | **T+1 open** | $289,564 | +189.6% | +16.4% | 0.81 | −31.3% | 41 |
| B · Plain V1 (50 survivors, flat 10%) | same-bar close | $294,709 | +194.7% | +16.7% | 0.86 | −32.2% | 33 |
| B · Plain V1 | **T+1 open** | $292,093 | +192.1% | +16.5% | 0.86 | −32.0% | 33 |

**Delta (T+1 − legacy):** Best −0.4 pts CAGR / −0.01 Sharpe / −$6,833;
V1 −0.1 pts CAGR / −0.01 Sharpe / −$2,615. Trade counts unchanged.

The "same-bar close" rows reproduce the previously documented numbers exactly
(0.82 / 0.86), confirming the change is backward-compatible.

**Interpretation — the bias was real but its impact is small.** Earlier I
estimated up to a few CAGR points; the measured haircut is only ~0.4 pt on the
best variant. Reason: although the per-entry gap to next open averaged +1.5% in
the sample, the *median* was ~0 and many entries gap **down** (you fill cheaper) —
e.g. AMD 2018-10-25 close $19.27 vs 2018-10-26 open **$18.49**, a *better* fill.
The large +8–10% gaps clustered on a handful of Mar-2020 entries, so the
portfolio-level effect is diluted. The fix does **not** overturn the prior
results; combined with the Deflated-Sharpe finding (edge over SPY only ~56%
significant), the conclusion is unchanged: the edge over SPY remains marginal/
unproven — the look-ahead was not materially inflating it.

Product-engine spot-check (`engine.py`, 50-survivor, 2018-2021): runs cleanly both
ways; `next_open` AMD entry = 2018-10-26 @ $18.49 (T+1 open) vs `close` = 2018-10-25
@ $19.27 (signal-day close). Entry date and price move to the fill bar as intended.

## Part 2 — Stock splits during open positions: NOT A BUG

The backtest uses **split+dividend-adjusted** close everywhere (`auto_adjust=True`).
Entry and exit prices both come from the same back-adjusted series, and share
count (`alloc/entry_price`) is never re-derived mid-hold, so `ret = exit/entry` is
already split-correct. The raw (unadjusted) cache is used **only** for market-cap
ranking, never for backtest pricing — no adjusted/unadjusted mixing. Tickers that
split while held in 2018-2024 (AAPL 4:1, TSLA 5:1 & 3:1, NVDA 4:1, AMZN 20:1,
GOOGL 20:1, etc.) therefore compute correct returns. **No fix needed.**

## Part 3 — Capital allocation ("open at 100% deployed"): INVESTIGATED

- `product/backtest/engine.py`: **fixed** (commit caf429f). Hard guard
  `if cash < desired_alloc: record missed_capital; continue` — no partial fills,
  never over-deploys.
- `scripts/run_portfolio_sim.py`: the original over-deployment bug is **not
  present** — `alloc = min(port_val*pct, cash)` keeps `alloc ≤ cash`, so cash never
  goes negative and a fully-deployed book is skipped via `alloc < _MIN_POSITION`.
  It does allow **partial fills** when `_MIN_POSITION < cash < desired` — a
  behavioral *inconsistency* with the product engine (which skips). Not the
  over-deployment bug; **left as-is** (aligning the rule would change prior study
  numbers — deferred, out of scope for this PR).

## Status
| Part | Status |
|---|---|
| 1 · Entry look-ahead (T+1 open) | **FIXED** (3 engines; product default = next_open) |
| 2 · Splits during open positions | **NOT A BUG** (adjusted-series returns are split-correct) |
| 3 · Capital allocation | **INVESTIGATED** (product already fixed; harness partial-fill inconsistency noted, deferred) |

Reproduce: `python research/run_t1_entry_recheck.py`
