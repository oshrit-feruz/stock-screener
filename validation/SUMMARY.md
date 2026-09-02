# Recovery-signal validation — full summary

**Goal (two parts):**
1. Does this recovery/dip signal *reliably* beat a passive index (SPY) for a real user — or were the flattering results an artifact of bias?
2. If we build on the signal regardless, what is the **best configuration to release**?

**Short answer:** as a standalone, always-on strategy — **no**, it's a coin flip (crisis alpha only). Deployed as a **conditional overlay on an SPY core** — **yes**: it beats SPY in 14/17 rolling 5-year windows with a higher Sharpe, and the result survives out-of-sample tests. The best config is simple, and every refinement tried on top of it failed.

All numbers below are from the clean framework after the method audit (stage 15): survivorship-free universe, **next-session fills** (no same-bar look-ahead), **delisted names sold at their final print** (never carried at a stale quote), completion rule.

---

## The journey — and why each step was needed

| Stage | What we ran | Headline result |
|---|---|---|
| **0. Biased baseline** | 50 hand-picked large caps, 2018-2024, quality gate | Signal "crushes" SPY (V1 +195% vs SPY +145%) |
| **1. Holding-period test** | 252 / 378 / 504 days on that biased set | Longer hold → fatter tail, all variants beat SPY |
| **2. Diagnose bias** | Audited the universe | 50-name list + EDGAR market-cap ranking dropped delisted names → **survivorship bias** |
| **3. Clean rebuild** | PIT S&P 500 top-100 by **dollar-volume** (incl. delisted), 2000/2004-2024, pure price signal | Look-ahead-free (audited, 5 checks). Edge **shrinks**: 1y hold *loses* to SPY (+9.0% vs +10.3% CAGR, 2004-24); only the 2y hold beats it (+12.0%); drawdowns −66%…−70% |
| **4. Fix the product** | Simulator + live screener switched to dollar-volume ranking + fail-open gate | Product now survivorship-free; trades delisted names (AABA, AGN, CELG) it used to drop |
| **5. Simulator vs research** | Both engines, same window | Agree where it matters (2y beats, 1y loses, ~−70% DD); diverge at 378/1008 → method-sensitive |
| **6. Real investor from 2004** | $100k, bot-managed vs SPY (product engine, next-open fills) | Default 1y hold **loses to SPY** ($431k vs $785k); tuned 2y wins ($1.03M) but after −64% DD |
| **7. Parameter sweep** | hold × stop-loss × take-profit × sizing (96 cells) | Take-profit at +50%/+100% lowers CAGR at every hold (+200% ≈ no TP); stop-loss is noise (helps some holds, hurts others); only 15/96 cells beat SPY; best pure hold = 504 days |
| **8. Robustness (pivotal)** | Rolling 5y/7y windows | Standalone edge is a **coin flip** (beats SPY 8/17 windows at 1y, 7/17 at 2y; median excess ≈ −0.5pp). Outperformance is **regime-dependent** (crisis-recovery only) |
| **9. Release selection** | Candidates ranked by **rolling Sharpe** | Standalone winner: 2y / SL−40% / 20%×5 — median Sharpe 0.77, still **below SPY's 0.80**, with the widest dispersion (±0.39) |
| **10. Diagnosis** | Where is the failure? | The alpha is real but **sparse** (crisis-recovery only); running it always-on dilutes it under lag + ~25% idle-cash drag. **The failure is the deployment, not the signal** |
| **11. SPY-core overlay (breakthrough)** | Always in SPY; signal fires only when market DD ≥ 10%, rotating out of SPY and back | **14/17 windows beat SPY, median excess +3.1%, median Sharpe 0.94 vs SPY 0.80.** Full period CAGR +17.5%, DD −55% (= SPY's own) |
| **12. Out-of-sample validation** | Threshold plateau + train/test split | Gate is a **plateau 5-12%** (12-15/17 windows each), not a lone spike; a gate chosen on one decade beats SPY on the other **in both directions**. Supporting evidence, not proof: windows overlap and there are only two splits |
| **13. Adaptive exit** | Trailing 15-30%, SMA50 break, recovery-target vs fixed 504 | **Fixed hold wins every metric.** Price-reactive exits clip the volatile recovery winners (trail-15 fires 220×; SMA50 → −69% DD). Time-based exit is a *feature* |
| **14. Staged / confirmed entry** | Tranches (3-5), bottom-confirmation waits (5-20d) | **No help.** Staging ties on Sharpe (0.95 vs 0.94, noise) but lowers sleeve return (+74% → +64…68%: buying a rising recovery at worse prices) and raises the >30%-loser share; confirmation is worse (−58…−60% DD, 26-28 skipped winners). Drawdown is market-driven, not timing-driven |
| **15. Method audit (review fixes)** | Same-bar entry → next-session fill; stale delisted quotes → forced exit at final print; shared engine (`clean_sim.py`) | Standalone numbers fall (2y hold 13.4% → 12.0% CAGR: the same-bar fill was flattering); the overlay is unchanged in substance (14/17, +3.1%). **Every conclusion above survives** |

---

## Answer to Goal 1 — does it reliably beat the index?

**As a standalone strategy: no.** On the clean framework it beats SPY in only 8/17 (1y hold) and 7/17 (2y hold) rolling 5-year windows with median excess ≈ −0.5pp, and even its best robust config has a median rolling Sharpe (0.77) below SPY's (0.80). Its outperformance is **crisis alpha**: it wins big only when the window contains a major dislocation it can dip-buy (2004-08 +12.8pp at the 2y hold, 2009-13 +11.3pp at the 1y hold) and lags through steady bull markets (2011-15 −9.2pp at 1y; 2005-09 −13.1pp at 2y).

**As a conditional overlay on an SPY core: yes, and it holds up.** Keeping the portfolio in SPY and letting the signal fire only during dislocations (market DD ≥ 10%) beats SPY in **14/17** rolling windows, median excess **+3.1pp**, median Sharpe **0.94 vs 0.80**, with the same peak drawdown as the index itself (−55%). The gate sits on a broad plateau (5-12%) and a threshold chosen on one decade beats SPY on the other. That is evidence of a robust regime signal rather than a fit — with the caveats that the rolling windows overlap and the edge is concentrated in a handful of dislocations (2008-09, 2020, 2022), so the effective sample of regime events is small.

## Answer to Goal 2 — best config to release

**→ SPY core + dislocation gate at 10% + fixed 2-year hold + 10% per sleeve, max 10 + full sleeve at the first session after the signal. No stop-loss, no take-profit, no adaptive exit, no staging.**

Every refinement tried on top of it made things worse or was noise — which is the *good* outcome: the config is simple, with no over-tuned parameters. What the evidence settled:
- **No take-profit / trailing stop / adaptive exit** — recovery winners are volatile on the way up; anything price-reactive gets shaken out before the move completes. The fixed clock's virtue is that it is deaf to the noise.
- **No staging / confirmation** — recoveries rise, so later tranches buy worse prices; confirmation waits skip the winners that run straight up.
- **Diversification over concentration** — standalone, the concentrated 20%×5 tops the median-Sharpe table only with the widest dispersion and a −0.23 worst window; on the overlay 10%×10 is what was validated.
- **~2-year hold** is the sweet spot both engines agree on.

## The failure scorecard (what we diagnosed, what we could fix)

| Failure | Status |
|---|---|
| 1. Sparse edge run always-on | ✅ **Fixed** — 10% gate (validated OOS) |
| 2. ~25% idle-cash drag | ✅ **Fixed** — SPY core |
| 3. Time-based exit | ❌ **Misdiagnosis** — it's a feature; adaptive exits all lose |
| 4. −55% drawdown | ❌ **Not fixable by timing** — it's the market (2008 on the SPY core) plus a few names going to zero |

**The one lever left is entry *quality*, not timing.** The −99% worst sleeve and ~12% heavy losers are names heading to bankruptcy. A solvency/quality screen would address them — but that is exactly the fundamental gate we dropped because it cannot be measured without survivorship bias on free data. Blocked by data, not by the idea.

---

## What changed in the codebase
- `data/sp500_universe.py` — survivorship-free dollar-volume PIT top-100 ranking (market-cap variant kept for reference).
- `product/backtest/engine.py`, `product/screener/daily_screener.py` — dollar-volume universe + **fail-open** quality gate (whole product).
- `product/satellite_policy.py` — the validated overlay published from the engine: `market_regime` (SPY drawdown vs the 10% gate), per-row `active` (BUY *and* in dislocation), `target_exit_date` (504 sessions, shared with the exit tracker), `satellite_policy` block, `schema_version: 2`. Additive to the payload; shift-app's reader keeps working unchanged.
- `scripts/clean_intermediate.py` + `scripts/clean_sim.py` — the single research engine every `run_clean_pit_*` / overlay / exit / entry script now uses (next-session fills, forced exit at a final print, completion rule, cash or SPY core, optional regime gate).
- Build pipeline warms/seeds a dollar-volume grid. **Deploy note:** rebuild the `cache-v1` release asset with the updated build scripts so production ships the grid (else it cold-falls-back to the fixed universe).

## Analysis artifacts (in `validation/`)
- `clean_pit_backtest_2000/2004.md/.png` — clean holding-period backtests
- `scripts/audit_clean_pit.py` — look-ahead / survivorship / simulation audit (VERDICT: clean, 5 checks)
- `edgar_coverage.md` — why market-cap ranking was biased pre-2018
- `clean_pit_sweep.md` — hold × SL × TP × sizing sweep
- `clean_pit_rolling.md/.png` — rolling-window robustness (standalone = coin flip)
- `investor_2004.md/.png` — real-investor-from-2004 narrative (product engine)
- `release_config_selection.md` — standalone config ranked by robust Sharpe
- `spy_overlay.md` — SPY-core conditional overlay (the breakthrough)
- `overlay_oos.md` — out-of-sample validation of the gate
- `adaptive_exit.md` — adaptive exit rules vs fixed hold
- `staged_entry.md` — staged / confirmed entry
- `overlay_curves_2004/2008.png` — full-period equity + drawdown curves of the deployment variants vs SPY
