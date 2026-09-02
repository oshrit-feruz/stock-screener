# What can be said about the bot externally — claims, evidence, caveats

Every claim below points at the report that supports it. Anything not on this
page is not a claim we make. All figures are from the clean framework after the
method audit (`validation/SUMMARY.md` stage 15): survivorship-free universe,
next-session fills, delisted names sold at their final print, 2004-2024.

## Claims we can defend

| Claim | Evidence | Where |
|---|---|---|
| The backtest is survivorship-free and look-ahead-audited: point-in-time S&P 500 membership incl. delisted names, ranked by trailing dollar-volume, signals filled at the next session | 5 audit checks, all clean (eligibility vs governing rebalance with a future-ranking control; dollar-volume recomputed from raw; per-trade fill/exit checks) | `scripts/audit_clean_pit.py`, `clean_pit_backtest_2004.md` |
| As a **standalone, always-on** strategy the signal does **not** reliably beat the index: 8/17 (1y hold) and 7/17 (2y hold) rolling 5-year windows, median excess ≈ −0.5 pp, best robust Sharpe 0.77 vs SPY 0.80 | rolling windows; release-config ranking | `clean_pit_rolling.md`, `release_config_selection.md` |
| Deployed as a **conditional overlay on an S&P 500 core** (gate: market ≥10% below its trailing 252-session high; fixed 2-year hold; 10%×10 sleeves) it beat SPY in **14 of 17** rolling 5-year windows, median excess **+3.1 pp/yr**, median Sharpe **0.94 vs 0.80**, with the **same max drawdown as the index** (−55%) | rolling windows on the overlay | `spy_overlay.md`, `overlay_curves_2004.png` |
| In calendar years: beat SPY in **12 of 21**; 5 years were identical to SPY (no dislocation, no sleeves); of the 16 traded years, 12 beat and 4 lost | calendar-year scorecard | `overlay_by_year.md` |
| The 10% gate is not a tuned spike: 5-12% all beat SPY in 12-15 of 17 windows, and a gate chosen on one decade beats SPY on the other (both directions) | sensitivity grid + train/test | `overlay_oos.md` |
| Every refinement tried on top made it worse or was a tie: take-profit, stop-loss, trailing/SMA/recovery exits, staged and confirmed entry, concentration | sweep, adaptive-exit, staged-entry | `clean_pit_sweep.md`, `adaptive_exit.md`, `staged_entry.md` |

## Claims we do NOT make

- "The bot beats the market." Unconditionally false: standalone it is a coin flip. Only the conditional overlay beats SPY, and only in a majority of windows, not all.
- Any single-window CAGR or final dollar value as *the* result (e.g. 17.5% CAGR or ~$3M from $100k, 2004-2024). Those are best-case, start-date-dependent numbers; the rolling-window medians are the expectation.
- "Low risk" or "protects in a crash". Max drawdown equals the index's (−55% in 2008-09) and the losing years are crisis years (2008, 2022, 2011).
- Any expected future return. Historical, hypothetical, backtested only.

## Mandatory caveats on every use

- Hypothetical backtest results, not actual client performance; no trades were executed.
- No transaction costs, taxes, slippage or execution gaps are modelled.
- Dollar-volume is a liquidity proxy for size (tilts toward high-turnover names), not market cap.
- Price coverage is 96.7% of ever-members; the ~36 missing series are mostly bankruptcies/acquisitions, whose absence may slightly flatter results.
- The edge is concentrated in a handful of dislocations (2008-09, 2020, 2022); rolling windows overlap, so the effective sample of regime events is small.
- With the gate and a 2-year hold, a client can receive no active recommendation for years at a time; the satellite budget then simply sits in the S&P 500 core.
- Any public performance claim needs compliance/legal review for the relevant jurisdiction before publication.
