# Combined validation — Score-Plus sizing × money-market cash sleeve

Two improvements, validated separately, combined into one variant. All four run
on **identical conditions** — 2018-01-02 → 2024-12-31, the same 50 tickers,
threshold 0.60, exit at day 252, no stop-loss, $100,000 start, max 10 concurrent
positions — differing only in two independent switches:

- **sizing**: `flat` (10% every signal) vs `score_plus` (comp ≥ 0.70 → 12%, else
  10%; the extra 2% comes from idle cash only, never by shrinking another
  position; skip if < 5% of the portfolio is free, else open with what's left).
- **cash**: `zero` (idle cash earns 0%) vs `money_market` (4.5%/yr, daily pro-rata).

| Variant | Sizing | Cash | Final $ | Total ret | CAGR | Sharpe | Max DD |
|---|---|---|--:|--:|--:|--:|--:|
| **A** · V1 baseline | flat | 0% | $293,354 | +193.4% | +16.6% | 0.86 | −32.2% |
| **B** · Score-Plus only | score_plus | 0% | $304,355 | +204.4% | +17.2% | 0.84 | −33.6% |
| **C** · Money market only | flat | 4.5% | $343,924 | +243.9% | +19.3% | 0.98 | −31.0% |
| **D** · Full combo (main) | score_plus | 4.5% | **$355,911** | **+255.9%** | **+19.9%** | 0.95 | −33.0% |
| *SPY buy & hold (ref)* | — | — | *$244,206* | *+144.2%* | *+13.6%* | *0.75* | *−33.7%* |

## Linear-expectation check — does the stack add up?

| | Final $ | Total ret |
|---|--:|--:|
| Baseline A | $293,354 | +193.4% |
| + Score-Plus improvement (B − A) | +$11,001 | +11.0% |
| + Money-market improvement (C − A) | +$50,571 | +50.6% |
| **= Expected D (linear sum)** | **$354,925** | **+254.9%** |
| Actual D (measured) | $355,911 | +255.9% |
| **Interaction (actual − expected)** | **+$986** | **+1.0%** |

**Verdict: negligible / essentially additive.** The interaction is only ~2% of
the combined lift, so the two improvements stack almost perfectly, with a tiny
*positive* cross-term.

Two forces nearly cancel:
- **Drag (negative):** Score-Plus deploys *more* idle cash into 12% positions,
  leaving less cash for the 4.5% sleeve to earn on.
- **Boost (positive):** each lever enlarges the portfolio base the other
  compounds on — a bigger base means both a bigger 4.5% accrual and bigger
  10–12% allocations.

The net here is a trivial +$986, so for planning purposes the gains can be
treated as additive: **money market does the heavy lifting (+$50.6k / +50.6 pts),
Score-Plus adds a modest +$11.0k / +11.0 pts, and combining them captures very
close to the full sum.** Note that, as in the standalone Score-Plus result, the
sizing lever slightly *raises* drawdown (D −33.0% vs C −31.0%) and trims Sharpe
(0.95 vs 0.98) — the combo's extra return comes with marginally more risk, almost
all of it from the Score-Plus side.

Reproduce: `python scripts/run_combined_validation.py`
