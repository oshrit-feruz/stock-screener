# Recovery-signal validation — full summary

**Goal (two parts):**
1. Does this recovery/dip signal *reliably* beat a passive index (SPY) for a real user — or were the flattering results an artifact of bias?
2. If we build on the signal regardless, what is the **best configuration to release**?

---

## The journey — and why each step was needed

| Stage | What we ran | Headline result |
|---|---|---|
| **0. Biased baseline** | 50 hand-picked large caps, 2018-2024, quality gate | Signal "crushes" SPY (V1 +195% vs SPY +145%) |
| **1. Holding-period test** | 252 / 378 / 504 days on that biased set | Longer hold → fatter tail, all variants beat SPY |
| **2. Diagnose bias** | Audited the universe | The 50-name list + EDGAR market-cap ranking dropped delisted names → **survivorship bias** |
| **3. Clean rebuild** | PIT S&P 500 top-100 by **dollar-volume** (incl. delisted), 2000/2004-2024, pure price signal | Look-ahead-free (audited). Edge **shrinks**: 1yr hold *loses* to SPY; only 2y+ beats; drawdowns −62%…−70% |
| **4. Fix the product** | Switched the simulator + live screener to dollar-volume ranking + fail-open gate | Product now survivorship-free; trades delisted names (AABA, AGN, CELG) it used to drop |
| **5. Simulator vs research** | Both engines, same window | Agree where it matters (2y beats, 1y loses, ~−70% DD); diverge at 378/1008 → method-sensitive |
| **6. Real investor from 2004** | $100k, bot-managed vs SPY | Default 1y hold **loses to SPY** ($431k vs $785k); tuned 2y wins ($1.03M) but after −64% DD |
| **7. Parameter sweep** | hold × stop-loss × take-profit × sizing | Take-profit always hurts; stop-loss marginal; longer hold + concentration best *in-sample* |
| **8. Robustness (pivotal)** | Rolling 5y/7y windows | Edge is a **coin flip** (beats SPY ~47-53% of windows, median excess ≈ 0). Outperformance is **regime-dependent** (crisis-recovery only) |
| **9. Release selection** | Candidate configs ranked by **rolling Sharpe** | Winner: **2y hold / 5%×20 diversified / no SL / no TP**. In-sample "concentration" was overfitting |

---

## Answer to Goal 1 — does it reliably beat the index?

**No.** On the clean, survivorship-free, look-ahead-free framework:
- Across rolling 5-year windows the strategy beats SPY in only **8/17** windows (1y hold) and **8/17** (2y) — a coin flip, median excess return **near zero or negative**.
- Even the *best* robust config has a **median rolling Sharpe of 0.71 vs SPY's 0.80** — risk-adjusted-inferior to just holding the index.
- The outperformance is **crisis alpha**: it wins big only when the holding window contains a major dislocation to dip-buy (2004-2008 +20.7%, 2009-2013 +12.2%) and lags through steady bull markets (2011-2017 windows lose 6-9pp). The full-period "beats SPY" was an artifact of the window starting just before 2008.
- Drawdowns are severe in every configuration (−32% median rolling; −64%…−70% peak), worse than SPY in 2008 (−47% vs −36%).

**Implication:** whether a user beats the index depends on whether their holding window happens to contain a recoverable crash — timing no one controls. This is not a reliable index-beating machine.

## Answer to Goal 2 — best config to release (if building anyway)

Chosen criterion: **robust risk-adjusted return** (median Sharpe across rolling windows, low dispersion).

**→ 2-year hold, 5% per position, max 20 concurrent (diversified), no stop-loss, no take-profit.**

- Median rolling Sharpe **0.71** (highest of all candidates), lowest dispersion, shallowest median drawdown (−32%).
- Beats the in-sample favourite (20%×5 concentrated, Sharpe 0.60, most fragile) — chosen on rolling windows precisely to avoid that overfitting trap.
- Firm findings that shaped it: **no take-profit** (it caps the recovery winners that carry the whole edge), **stop-loss adds little** (only a wide −40% barely helps), **diversification beats concentration** out-of-sample, **~2-year hold** is the sweet spot.

**Honesty caveat:** this is the most stable config *if* shipping on this signal — not a claim that it beats SPY. It does not, on a robust basis.

---

## What changed in the codebase
- `data/sp500_universe.py` — survivorship-free dollar-volume PIT top-100 ranking (market-cap variant kept for reference).
- `product/backtest/engine.py`, `product/screener/daily_screener.py` — dollar-volume universe + **fail-open** quality gate (whole product).
- Build pipeline warms/seeds a dollar-volume grid. **Deploy note:** rebuild the `cache-v1` release asset with the updated build scripts so production ships the grid (else it cold-falls-back to the fixed universe).

## Analysis artifacts (in `validation/`)
- `clean_pit_backtest_2000.md/.png`, `clean_pit_backtest_2004.md/.png` — clean holding-period backtests
- `audit_clean_pit.py` output — look-ahead / survivorship audit (VERDICT: clean)
- `edgar_coverage.md` — why market-cap ranking was biased pre-2018
- `clean_pit_sweep.md` — hold × SL × TP × sizing sweep
- `clean_pit_rolling.md/.png` — rolling-window robustness (the pivotal finding)
- `investor_2004.md/.png` — real-investor-from-2004 narrative
- `release_config_selection.md` — release config ranked by robust Sharpe
