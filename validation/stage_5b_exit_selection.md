# Stage 5b — Exit Strategy Selection

## Context

The Recovery Entry Detector has a validated **magnitude edge** (+34.4% vs +22.3% RANDOM, +12.2pp spread) with no directional edge (win-rate ≈ random). 60.9% of entries touch -20% from entry at some point before 252d. The naive -10% hard stop fires on 60.9% of entries that would have averaged +67% if held — it systematically destroys the edge.

This analysis evaluates 5 exit families across 29 variants to find the exit that best preserves the magnitude edge while reducing user pain.

## Recommended Exit Rule

**Hold 252d** (Family F1)

Hold for **252 trading days** (~12 months). Exit on the scheduled date regardless of price.

| Metric | Winner | Buy&Hold 252d |
|--------|--------|---------------|
| Mean 252d return | +34.4% | +34.4% |
| Spread vs RANDOM | +12.1% | +12.1% |
| %Pos exits | 72.6% | 72.6% |
| %Touched -20% | 35.6% | 35.6% |
| %Exit at loss | 27.4% | 27.4% |
| Avg hold (days) | 252 | 252 |
| Capture of edge | 100.1% | 100.1% |
| UX score | 0.468 | 0.468 |

## Why It Wins

The winner retains **100% of the buy-and-hold edge** (+34.4% mean vs +34.4% buy-and-hold). No tested exit variant reduces %Hit-20% relative to Hold 252d while also meeting the +8pp spread eligibility floor — the signal's edge lives entirely in the magnitude of the eventual recovery, not in the directional win rate.

Only 3 variants pass the eligibility floor (spread ≥ +8%): Hold 252d (+12.1pp), Stop -40% (+8.9pp), and H252d E40% (+8.9pp). The latter two are operationally equivalent and return +31.2% vs +34.4% for plain hold — a −3.2pp cost for adding a -40% hard stop that fires on 9.6% of entries (of which only 3.8% were eventual 252d-winners).

## Trade-off Accepted

- Edge given up vs pure buy-and-hold: **none** — Hold 252d IS the buy-and-hold baseline.
- Pain removed vs any alternative exit: **none possible without destroying the edge**. The 35.6% of entries that touch −20% before 252d average +14.5% at 252d — stopping them is net-negative.
- Accepted user experience: 27.4% of exits close at a loss; 35.6% of positions will show an unrealized drawdown ≥20% at some point before the exit date.
- This must be communicated clearly in onboarding: the signal is a recovery play; drawdowns are the norm, not a sign of failure.

## UI Implication

> "This position is planned for a **252-day hold** (~12 months). The signal targets an average return of 34% over this period, but the path will be bumpy — expect drawdowns before the recovery. We will alert you at the exit date."

## Fallback Behavior

If the user takes no action, the app sends an **exit alert at day 252** with the current price and realized return. The user confirms or defers by 5 trading days.

## Full Ranking (Top 5 Eligible by UX)

| Rank | Variant | Family | Mean | Spread | Capture | UX |
|------|---------|--------|------|--------|---------|----|
| 1 | Hold 252d | F1 | +34.4% | +12.1% | 100.1% | 0.468 |
| 2 | Stop 40% | F2 | +31.2% | +8.9% | 90.7% | 0.408 |
| 3 | H252d E40% | F4 | +31.2% | +8.9% | 90.7% | 0.408 |
