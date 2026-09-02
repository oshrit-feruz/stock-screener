"""Monthly Top-N universe list — the single source of truth for what the daily
screener scans.

ARCHITECTURE (see docs/ARCHITECTURE.md): **GitHub Actions computes, Render reads.**
This module only ever READS the list produced by scripts/build_universe_list.py
and committed to main. It never ranks, never substitutes a different universe,
and never returns an empty list quietly: a missing, malformed, or too-old list
raises UniverseListError so the caller fails loudly and visibly.

That rule exists because the opposite behaviour shipped once and cost ~7.5 weeks
of silence — the screener caught a universe-lookup failure at WARNING, set
`universe = []`, and then reported "0 signals" as a *successful* run every day
from 2026-07-01. A screener that scans nothing must look like a failure, not
like a quiet day in the market.

Deliberately stdlib-only (no pandas/numpy) so the load-and-validate path is
cheap to import and directly unit-testable without the scientific stack.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import NamedTuple, Optional

# data/universe/current.json, committed to main by the monthly workflow.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = _REPO_ROOT / "data" / "universe" / "current.json"

# A list older than this is refused outright.
#
# Sizing: the list is stamped with the FIRST trading day of its month, so in
# normal operation its age drifts 0..~30 days. If one monthly rebuild is missed
# the list ages 31..~61 days before the next attempt. 62 therefore tolerates
# exactly ONE missed rebuild (surfaced as is_late -> a WARNING, never silent)
# and refuses TWO — the point at which "the monthly job is broken" stops being
# a blip and starts being an outage that must not scan on regardless.
MAX_AGE_DAYS = 62


class UniverseListError(RuntimeError):
    """The monthly universe list is missing, malformed, or too old to use.

    Raised instead of degrading to a partial or substitute universe. Callers
    must surface this, not swallow it.
    """


class UniverseList(NamedTuple):
    tickers: list[str]
    as_of: date
    age_days: int
    # True when the list is usable but not from the current month — i.e. the
    # monthly rebuild is late. Caller should log a WARNING; the run continues.
    is_late: bool


def load_universe_list(
    path: Optional[Path] = None,
    today: Optional[date] = None,
) -> UniverseList:
    """Load and validate the monthly universe list.

    Raises UniverseListError if the file is absent, unparsable, structurally
    invalid, empty, or older than MAX_AGE_DAYS. Never returns an empty list.
    """
    path = Path(path) if path is not None else DEFAULT_PATH
    today = today or date.today()

    if not path.exists():
        raise UniverseListError(
            f"Universe list not found at {path}. It is produced monthly by "
            f"scripts/build_universe_list.py and committed to main; the daily "
            f"screener cannot run without it. Refusing to scan a substitute universe."
        )

    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        raise UniverseListError(f"Universe list at {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise UniverseListError(f"Universe list at {path} must be a JSON object, got {type(raw).__name__}.")

    as_of_raw = raw.get("as_of")
    if not isinstance(as_of_raw, str):
        raise UniverseListError(f"Universe list at {path} has no string 'as_of' field.")
    try:
        as_of = date.fromisoformat(as_of_raw)
    except ValueError as exc:
        raise UniverseListError(f"Universe list at {path} has an unparsable 'as_of' ({as_of_raw!r}): {exc}") from exc

    tickers = raw.get("tickers")
    if not isinstance(tickers, list) or not all(isinstance(t, str) and t for t in tickers):
        raise UniverseListError(f"Universe list at {path} 'tickers' must be a non-empty list of strings.")
    if not tickers:
        raise UniverseListError(
            f"Universe list at {path} is EMPTY (as_of {as_of}). An empty universe means the "
            f"monthly ranking failed; refusing to report a zero-signal run as success."
        )

    age_days = (today - as_of).days
    if age_days > MAX_AGE_DAYS:
        raise UniverseListError(
            f"Universe list at {path} is stale: as_of {as_of} is {age_days} days old "
            f"(max {MAX_AGE_DAYS}). The monthly rebuild has not run. Refusing to scan "
            f"an out-of-date universe."
        )
    if age_days < 0:
        raise UniverseListError(
            f"Universe list at {path} is dated in the future (as_of {as_of}, today {today})."
        )

    is_late = (as_of.year, as_of.month) != (today.year, today.month)
    return UniverseList(tickers=list(tickers), as_of=as_of, age_days=age_days, is_late=is_late)
