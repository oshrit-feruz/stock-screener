"""Manual overrides of a signal's `active` flag.

What `active` means, and why an override is a separate thing
------------------------------------------------------------
`active` is DERIVED, not stored: product/satellite_policy.is_active() computes
it from the signal and the market regime (a BUY while SPY is >= gate_dd below
its trailing high). That derivation is the validated policy and stays the
single source of truth -- nothing here changes it.

An override sits BESIDE the computed value rather than replacing it. The
screener payload keeps publishing what the policy said (`active_policy`) and
says where the served value came from (`active_source`), so a consumer can
always tell a human decision from the gate's. Overriding the computed field in
place would have made the two indistinguishable one day later.

An override is deliberately NOT scoped to a scan date: it persists until it is
cleared, which is what "override" has to mean to be useful across the daily
rerun. `set_at` is published with it so a stale one is visible as stale.

Backends
--------
Supabase when SUPABASE_URL and SUPABASE_SERVICE_KEY are both set; otherwise a
JSON file, exactly as product/storage/positions.py does and for the same
reason: the web service runs on Render, whose container filesystem is discarded
on restart, so a file-only store would silently lose every override and would
not be shared between machines at all. The file backend is what keeps tests and
local development working with no credentials and no network.

As in positions.py the fallback is not an error handler: if Supabase is
configured but unreachable these functions raise, rather than quietly writing
to a file nobody reads.

The Supabase primitives (config reading, the https guard, the request wrapper)
and the ticker validation are imported from positions.py on purpose. They carry
security-relevant rules -- refusing to send the service-role key over http, and
rejecting a ticker that could steer a PostgREST filter -- and those rules must
have exactly one implementation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from product.storage.positions import (
    StorageError,
    _clean_ticker,
    _config,
    _request,
    backend,
)

logger = logging.getLogger(__name__)

__all__ = ["StorageError", "backend", "load", "set_override", "clear_override"]

_TABLE = "bot_active_overrides"

_ROOT = Path(__file__).parent.parent.parent
_FILE = _ROOT / "data" / "screener_overrides" / "active_overrides.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sound_row(row: Any) -> Optional[dict]:
    """A stored override, or None if it is not one.

    The file is ordinary JSON on disk and the table is writable by anything
    holding the service key, so what comes back is checked rather than trusted.
    `active` must be a real bool: a truthy string here would flip a signal to
    actionable on the strength of a typo.
    """
    if not isinstance(row, dict):
        return None
    if not isinstance(row.get("active"), bool):
        return None
    try:
        ticker = _clean_ticker(row.get("ticker", ""))
    except (ValueError, TypeError):
        return None
    set_at = row.get("set_at")
    return {
        "ticker": ticker,
        "active": row["active"],
        "set_at": set_at if isinstance(set_at, str) else None,
    }


def _read_file() -> Dict[str, dict]:
    if not _FILE.exists():
        return {}
    try:
        data = json.loads(_FILE.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", _FILE, exc)
        return {}
    if not isinstance(data, list):
        return {}
    rows = [r for r in (_sound_row(r) for r in data) if r is not None]
    if len(rows) != len(data):
        logger.warning("Dropped %d unreadable override(s) from %s",
                       len(data) - len(rows), _FILE)
    return {r["ticker"]: r for r in rows}


def _write_file(rows: Dict[str, dict]) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FILE.with_suffix(_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(rows.values(), key=lambda r: r["ticker"]),
                              indent=2))
    tmp.replace(_FILE)     # atomic: a crash mid-write cannot truncate the store


def load() -> Dict[str, dict]:
    """Every override, keyed by ticker: {ticker: {ticker, active, set_at}}.

    Returns {} when nothing is stored. Unreadable rows are dropped rather than
    raised on, so one bad row cannot take the screener endpoint down with it.
    """
    if _config():
        rows = _request("GET", f"{_TABLE}?select=ticker,active,set_at") or []
        sound = [r for r in (_sound_row(r) for r in rows) if r is not None]
        if len(sound) != len(rows):
            logger.warning("Dropped %d unreadable override(s) from %s",
                           len(rows) - len(sound), _TABLE)
        return {r["ticker"]: r for r in sound}
    return _read_file()


def set_override(ticker: str, active: bool) -> dict:
    """Store (or replace) the override for one ticker. Returns the stored row."""
    ticker = _clean_ticker(ticker)
    if not isinstance(active, bool):
        raise ValueError(f"active must be a bool, got {type(active).__name__}")
    row = {"ticker": ticker, "active": active, "set_at": _now()}

    if _config():
        # merge-duplicates: one row per ticker, so re-flagging a name replaces
        # the previous decision instead of accumulating a history of them.
        _request("POST", _TABLE, json=row,
                 headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    else:
        rows = _read_file()
        rows[ticker] = row
        _write_file(rows)
    logger.info("active override: %s -> %s", ticker, active)
    return row


def clear_override(ticker: str) -> bool:
    """Drop the override for one ticker. False when there was nothing to drop."""
    ticker = _clean_ticker(ticker)

    if _config():
        # return=representation so the caller learns whether a row actually went,
        # rather than reporting a cleared override that never existed.
        deleted = _request("DELETE", f"{_TABLE}?ticker=eq.{ticker}",
                           headers={"Prefer": "return=representation"}) or []
        gone = bool(deleted)
    else:
        rows = _read_file()
        gone = rows.pop(ticker, None) is not None
        if gone:
            _write_file(rows)
    if gone:
        logger.info("active override cleared: %s", ticker)
    return gone
