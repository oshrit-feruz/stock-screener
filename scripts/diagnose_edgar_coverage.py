#!/usr/bin/env python3
"""EDGAR coverage diagnostic for the point-in-time S&P 500 universe.

For each year-end 2009..2024 it takes the PIT S&P 500 membership and measures,
across those members, how much EDGAR actually covers — because the top-100
ranking depends on EDGAR shares outstanding:

  cik_ok    — ticker maps to a CIK in SEC company_tickers.json at all.
              (SEC lists only CURRENTLY-active tickers, so delisted/acquired
              names fail here → this column exposes the survivorship gap in the
              ranking step, independent of PIT membership being correct.)
  shares_ok — get_shares_outstanding() returns a value as of that date
              (captures the XBRL-depth issue: sparse before ~2010).
  gate_ok   — get_snapshot() yields a usable quality-gate verdict (rev growth,
              D/E, net margin all present).

No EODHD, no prices — pure EDGAR (free). Writes a markdown table.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.data.edgar import EdgarFundamentals
from core.signals.recovery_score import passes_quality_gate
from data.sp500_universe import get_universe

_OUT = Path(__file__).parent.parent / "validation" / "edgar_coverage.md"
YEARS = range(2009, 2025)
DATES = {y: f"{y}-12-31" for y in YEARS}


def _membership_years() -> tuple[dict[str, list[int]], dict[int, int]]:
    """{ticker: [years it is a member]} and {year: member count}."""
    members_by_year = {y: get_universe(DATES[y]) for y in YEARS}
    membership: dict[str, list[int]] = {}
    for y in YEARS:
        for t in members_by_year[y]:
            membership.setdefault(t, []).append(y)
    return membership, {y: len(members_by_year[y]) for y in YEARS}


def _evaluate_ticker(edgar: EdgarFundamentals, t: str, years: list[int], counts: dict) -> None:
    """Score one ticker for every year it is a member (companyfacts fetched once)."""
    if edgar._get_cik(t) is None:
        return
    for y in years:
        counts[y]["cik"] += 1
        d = DATES[y]
        if edgar.get_shares_outstanding(t, d) is not None:
            counts[y]["sh"] += 1
        snap = edgar.get_snapshot(t, d)
        if snap is not None and passes_quality_gate(snap) is not None:
            counts[y]["gate"] += 1


def _render(counts: dict) -> str:
    print(f"\n{'year':<6}{'members':>8}{'cik_ok':>8}{'shares':>8}{'gate':>7}  (cik% / sh% / gate%)")
    lines = ["# EDGAR coverage of the PIT S&P 500 membership\n",
             "How much of each year's point-in-time membership the top-100 ranking "
             "can actually see. `cik_ok` = maps to a SEC CIK at all (delisted names "
             "fail → survivorship gap in the ranking step). `shares_ok` = has "
             "shares-outstanding as of the date (XBRL depth). `gate_ok` = has a "
             "usable fundamental quality-gate verdict.\n",
             "| Year | Members | CIK ok | Shares ok | Gate ok | Shares % | Gate % |",
             "|---|--:|--:|--:|--:|--:|--:|"]
    for y in YEARS:
        c = counts[y]
        n = c["n"]
        print(f"{y:<6}{n:>8}{c['cik']:>8}{c['sh']:>8}{c['gate']:>7}  "
              f"({c['cik'] / n:.0%} / {c['sh'] / n:.0%} / {c['gate'] / n:.0%})")
        lines.append(f"| {y} | {n} | {c['cik']} | {c['sh']} | {c['gate']} | "
                     f"{c['sh'] / n:.0%} | {c['gate'] / n:.0%} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    edgar = EdgarFundamentals(fallback=None)  # pure EDGAR; no EODHD fallback
    membership, n_by_year = _membership_years()
    counts = {y: {"n": n_by_year[y], "cik": 0, "sh": 0, "gate": 0} for y in YEARS}

    # Ticker-outer: fetch each unique ticker's companyfacts ONCE, evaluate it for
    # every year it is a member, then evict it from the in-memory memo so only
    # one multi-MB companyfacts JSON is resident at a time (avoids OOM).
    total = len(membership)
    for i, (t, yrs) in enumerate(sorted(membership.items()), 1):
        _evaluate_ticker(edgar, t, yrs, counts)
        edgar._facts_mem.pop(t, None)
        if i % 100 == 0:
            print(f"  ...{i}/{total} tickers processed")

    _OUT.write_text(_render(counts))
    print(f"\nsaved -> {_OUT}")


if __name__ == "__main__":
    main()
