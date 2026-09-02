# Recovery-signal validation — full summary

**Goal (two parts):**
1. Does this recovery/dip signal *reliably* beat a passive index (SPY) for a real user — or were the flattering results an artifact of bias?
2. If we build on the signal regardless, what is the **best configuration to release**?

**Short answer:** as a standalone, always-on strategy — **no**, it's a coin flip (crisis alpha only). Deployed as a **conditional overlay on an SPY core** — **yes**: it beats SPY in 14/17 rolling windows with a higher Sharpe, and the result survives out-of-sample tests. The best config is simple, and every refinement tried on top of it failed.

---

## The journey — and why each step was needed

| Stage | What we ran | Headline result |
|---|---|---|
| **0. Biased baseline** | 50 hand-picked large caps, 2018-2024, quality gate | Signal "crushes" SPY (V1 +195% vs SPY +145%) |
| **1. Holding-period test** | 252 / 378 / 504 days on that biased set | Longer hold → fatter tail, all variants beat SPY |
| **2. Diagnose bias** | Audited the universe | 50-name list + EDGAR market-cap ranking dropped delisted names → **survivorship bias** |
| **3. Clean rebuild** | PIT S&P 500 top-100 by **dollar-volume** (incl. delisted), 2000/2004-2024, pure price signal | Look-ahead-free (audited). Edge **shrinks**: 1yr hold *loses* to SPY; only 2y+ beats; drawdowns −62%…−70% |
| **4. Fix the product** | Simulator + live screener switched to dollar-volume ranking + fail-open gate | Product now survivorship-free; trades delisted names (AABA, AGN, CELG) it used to drop |
| **5. Simulator vs research** | Both engines, same window | Agree where it matters (2y beats, 1y loses, ~−70% DD); diverge at 378/1008 → method-sensitive |
| **6. Real investor from 2004** | $100k, bot-managed vs SPY | Default 1y hold **loses to SPY** ($431k vs $785k); tuned 2y wins ($1.03M) but after −64% DD |
| **7. Parameter sweep** | hold × stop-loss × take-profit × sizing | Take-profit always hurts; stop-loss marginal; longer hold + concentration best *in-sample* |
| **8. Robustness (pivotal)** | Rolling 5y/7y windows | Standalone edge is a **coin flip** (beats SPY ~47-53% of windows, median excess ≈ 0). Outperformance is **regime-dependent** (crisis-recovery only) |
| **9. Release selection** | Candidates ranked by **rolling Sharpe** | Standalone winner: 2y / 5%×20 diversified — Sharpe 0.71, still **below SPY's 0.80**. In-sample "concentration" was overfitting |
| **10. Diagnosis** | Where is the failure? | The alpha is real but **sparse** (crisis-recovery only); running it always-on dilutes it under lag + ~25% idle-cash drag. **The failure is the deployment, not the signal** |
| **11. SPY-core overlay (breakthrough)** | Always in SPY; signal fires only when market DD ≥ 10%, rotating out of SPY and back | **14/17 windows beat SPY, median excess +3.2%, median Sharpe 0.90 vs SPY 0.80.** Full period CAGR +16.1%, DD −55% (= SPY's own) |
| **12. Out-of-sample validation** | Threshold plateau + train/test split | Gate is a **plateau 5-12%** (all 12-14/17), not a lone spike; train-chosen D beats SPY on the untouched test half **in both directions** |
| **13. Adaptive exit** | Trailing 15-30%, SMA50 break, recovery-target vs fixed 504 | **Fixed hold wins every metric.** Price-reactive exits clip the volatile recovery winners (trail-15 fires 209×; SMA50 → −67% DD). Time-based exit is a *feature* |
| **14. Staged / confirmed entry** | Tranches (3-5), bottom-confirmation waits (5-20d) | **No help.** Staging lowers sleeve return (+65% → +57%: buying a rising recovery at worse prices); confirmation is worse (−57..−60% DD). Drawdown is market-driven, not timing-driven |

---

## Answer to Goal 1 — does it reliably beat the index?

**As a standalone strategy: no.** On the clean framework it beats SPY in only ~8/17 rolling windows with median excess ≈ 0, and even its best robust config has a median rolling Sharpe (0.71) below SPY's (0.80). Its outperformance is **crisis alpha**: it wins big only when the window contains a major dislocation it can dip-buy (2004-08 +20.7%, 2009-13 +12.2%) and lags through steady bull markets (2011-17 windows lose 6-9pp).

**As a conditional overlay on an SPY core: yes, and it holds up.** Keeping the portfolio in SPY and letting the signal fire only during dislocations (market DD ≥ 10%) beats SPY in **14/17** rolling windows, median excess **+3.2%**, median Sharpe **0.90 vs 0.80**, with the same peak drawdown as the index itself (−55%). The gate sits on a broad plateau (5-12%) and a threshold chosen on one decade beats SPY on the other, so it is a robust regime signal, not a fit.

## Answer to Goal 2 — best config to release

**→ SPY core + dislocation gate at 10% + fixed 2-year hold + 10% per sleeve, max 10 + full sleeve on the signal day. No stop-loss, no take-profit, no adaptive exit, no staging.**

Every refinement tried on top of it made things worse or was noise — which is the *good* outcome: the config is simple, with no over-tuned parameters. What the evidence settled:
- **No take-profit / trailing stop / adaptive exit** — recovery winners are volatile on the way up; anything price-reactive gets shaken out before the move completes. The fixed clock's virtue is that it is deaf to the noise.
- **No staging / confirmation** — recoveries rise, so later tranches buy worse prices; confirmation waits skip the winners that run straight up.
- **Diversification over concentration** — the in-sample 20%×5 was the least stable out-of-sample.
- **~2-year hold** is the sweet spot both engines agree on.

## The failure scorecard (what we diagnosed, what we could fix)

| Failure | Status |
|---|---|
| 1. Sparse edge run always-on | ✅ **Fixed** — 10% gate (validated OOS) |
| 2. ~25% idle-cash drag | ✅ **Fixed** — SPY core |
| 3. Time-based exit | ❌ **Misdiagnosis** — it's a feature; adaptive exits all lose |
| 4. −55% drawdown | ❌ **Not fixable by timing** — it's the market (2008 on the SPY core) plus a few names going to zero |

**The one lever left is entry *quality*, not timing.** The −99% worst sleeve and ~14% heavy losers are names heading to bankruptcy. A solvency/quality screen would address them — but that is exactly the fundamental gate we dropped because it cannot be measured without survivorship bias on free data. Blocked by data, not by the idea.

---

## What changed in the codebase
- `data/sp500_universe.py` — survivorship-free dollar-volume PIT top-100 ranking (market-cap variant kept for reference).
- `product/backtest/engine.py`, `product/screener/daily_screener.py` — dollar-volume universe + **fail-open** quality gate (whole product).
- Build pipeline warms/seeds a dollar-volume grid. **Deploy note:** rebuild the `cache-v1` release asset with the updated build scripts so production ships the grid (else it cold-falls-back to the fixed universe).
- **Not yet implemented in the product:** the SPY-core overlay (stage 11) — the research-validated config is the natural next engineering step.

## Analysis artifacts (in `validation/`)
- `clean_pit_backtest_2000/2004.md/.png` — clean holding-period backtests
- `audit_clean_pit.py` — look-ahead / survivorship audit (VERDICT: clean)
- `edgar_coverage.md` — why market-cap ranking was biased pre-2018
- `clean_pit_sweep.md` — hold × SL × TP × sizing sweep
- `clean_pit_rolling.md/.png` — rolling-window robustness (standalone = coin flip)
- `investor_2004.md/.png` — real-investor-from-2004 narrative
- `release_config_selection.md` — standalone config ranked by robust Sharpe
- `spy_overlay.md` — SPY-core conditional overlay (the breakthrough)
- `overlay_oos.md` — out-of-sample validation of the gate
- `adaptive_exit.md` — adaptive exit rules vs fixed hold
- `staged_entry.md` — staged / confirmed entry
