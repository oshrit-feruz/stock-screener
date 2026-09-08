"""The position book: one store that every part of the system reads and writes.

Why this module exists
----------------------
The book used to live in two JSON files under data/positions/. That could not
work, because the two halves of the system run on different machines:

  * the daily run executes in GitHub Actions and committed those files to the
    automation/daily-state branch;
  * the web service runs on Render, read the same paths from its *own* checkout
    of main (which holds `[]`), wrote back to a container filesystem that is
    discarded on restart, and fetched only data/screener_cache from that branch.

So a position opened from the app was invisible to the exit tracker -- nothing
would ever fire its exit -- and vanished at the next restart. Positions the
daily run held were invisible in the app. Neither side was wrong on its own;
they were simply never looking at the same bytes.

Both now talk to one Postgres table (Supabase, `public.bot_positions`), so
"where is the book?" has a single answer.

Backends
--------
Supabase when SUPABASE_URL and SUPABASE_SERVICE_KEY are both set; otherwise the
original JSON files. The fallback is what keeps the test suite and local
development working with no credentials and no network, and it is why deploying
this change before the environment variables exist is safe: the system behaves
exactly as it did before until they are set.

The fallback is deliberately *not* an error handler. If Supabase is configured
but unreachable, these functions raise rather than quietly writing to a file
nobody reads -- a loud failure is recoverable, a silent split book is not.

Access is service-role only. RLS is enabled on the table with no policies, so
the anon key (which is public, it ships in the browser) can neither read nor
write it; the service role bypasses RLS. Never put SUPABASE_SERVICE_KEY
anywhere a browser can see.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

_TABLE = "bot_positions"
_TIMEOUT = 15  # seconds; the daily run must not hang forever on a hung socket

_ROOT = Path(__file__).parent.parent.parent
_POSITIONS_DIR = _ROOT / "data" / "positions"
_OPEN_FILE = _POSITIONS_DIR / "open_positions.json"
_CLOSED_FILE = _POSITIONS_DIR / "closed_positions.json"

# The columns the JSON files carried, so callers see the same dict shape from
# either backend and did not need rewriting when the store changed.
_OPEN_FIELDS = ("ticker", "entry_date", "entry_price",
                "signal_composite", "signal_drawdown", "reminder_sent")
_CLOSED_FIELDS = ("ticker", "entry_date", "entry_price",
                  "exit_date", "exit_price", "realized_return", "days_held")


class StorageError(RuntimeError):
    """Supabase is configured but the request failed.

    Raised instead of falling back to files: see the module docstring.
    """


# A listed symbol: letters and digits, optionally with a dot or hyphen for
# class shares and preferreds (BRK.B, RDS-A). Deliberately strict, and enforced
# here rather than trusting the API models, because a ticker reaches two places
# where a free-form string is dangerous:
#
#   * a PostgREST filter -- `ticker=eq.<value>` is a URL query, so an
#     unencoded `&` or `?` in the value appends parameters of the attacker's
#     choosing and can widen an UPDATE past the row it was meant to touch;
#   * the run log -- a newline forges log lines.
#
# Values are URL-encoded on top of this. Validation is what makes the encoding
# unnecessary rather than load-bearing; both are cheap.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def _clean_ticker(ticker: str) -> str:
    """Normalise to upper case and reject anything that is not a symbol."""
    candidate = (ticker or "").strip().upper()
    if not _TICKER_RE.match(candidate):
        raise ValueError(f"Not a valid ticker: {ticker!r}")
    return candidate


# ── backend selection ───────────────────────────────────────────────────────

def _config() -> Optional[tuple[str, str]]:
    """(url, service_key) when Supabase is configured, else None.

    Read at call time rather than import time so tests can set or clear the
    variables with monkeypatch, and so a redeploy that adds them takes effect
    without a code change.

    The URL must be https with a host. Every request carries the service-role
    key in an Authorization header, and that key bypasses RLS -- it is the only
    way into bot_positions. Over http it would cross the network in clear text,
    so a typo in one environment variable would leak the credential that owns
    the book. Refuse rather than send it.
    """
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not (url and key):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise StorageError(
            "SUPABASE_URL must be an https:// URL with a host; refusing to send "
            f"the service-role key to {url!r}"
        )
    return (url, key)


def backend() -> str:
    """'supabase' or 'files' -- which store is live. For logs and /api/health."""
    return "supabase" if _config() else "files"


def _request(method: str, path: str, **kw) -> Any:
    cfg = _config()
    if cfg is None:                     # pragma: no cover - guarded by callers
        raise StorageError("Supabase is not configured")
    url, key = cfg
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        **kw.pop("headers", {}),
    }
    try:
        resp = requests.request(method, f"{url}/rest/v1/{path}",
                                headers=headers, timeout=_TIMEOUT, **kw)
    except requests.RequestException as exc:
        raise StorageError(f"{method} {path} failed: {exc}") from exc
    if resp.status_code >= 400:
        # The body carries PostgREST's reason (constraint name, RLS denial).
        raise StorageError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        return None
    return resp.json()


# ── file backend ────────────────────────────────────────────────────────────

def _book_path(which: str) -> Path:
    """Resolve one of the two fixed book files.

    Callers pass a literal, never data. Taking a name out of a closed set
    rather than an arbitrary Path means no request value can steer a read or a
    write at a path of its choosing, whatever a future caller does.
    """
    if which == "open":
        return _OPEN_FILE
    if which == "closed":
        return _CLOSED_FILE
    raise ValueError(f"Unknown book: {which!r}")


def _sound_row(row: Any) -> Optional[dict]:
    """A row from the file, or None if it is not one.

    The file is ordinary JSON on disk: it can be hand-edited, half-written by an
    older build, or restored from a stale copy. Whatever comes back flows on
    into the API responses and, once the migration is live, into Supabase
    writes -- so it is checked here rather than trusted for having been ours
    once. Only the two fields every caller depends on are required; the rest
    are optional and pass through.
    """
    if not isinstance(row, dict):
        return None
    try:
        ticker = _clean_ticker(row.get("ticker", ""))
        date.fromisoformat(str(row.get("entry_date", "")))
    except (ValueError, TypeError):
        return None
    return {**row, "ticker": ticker}


def _read_file(which: str) -> List[dict]:
    path = _book_path(which)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        return []
    rows = [r for r in (_sound_row(r) for r in data) if r is not None]
    if len(rows) != len(data):
        logger.warning("Dropped %d unreadable row(s) from the %s book",
                       len(data) - len(rows), which)
    return rows


def _write_file(which: str, rows: List[dict]) -> None:
    path = _book_path(which)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2, default=str))
    tmp.replace(path)          # atomic: a crash mid-write cannot truncate the book


def _project(row: dict, fields: tuple) -> dict:
    """Keep only the fields a caller expects, so both backends look alike."""
    return {k: row.get(k) for k in fields}


# ── reads ───────────────────────────────────────────────────────────────────

def _key(row: dict) -> tuple:
    return (row.get("ticker"), row.get("entry_date"))


def load_open() -> List[dict]:
    """Every open position, oldest entry first.

    On Supabase this is one row set: status is a column, so a position cannot be
    in two states at once.

    The file backend has no transaction across its two files. Closing writes the
    closed book first and the open book second, deliberately: interrupted that
    way a position is briefly in both, which this read resolves, whereas the
    other order would lose it outright. Anything already closed is therefore not
    open, whatever the open file still says.
    """
    if _config():
        rows = _request("GET", f"{_TABLE}?status=eq.open&order=entry_date.asc"
                               f"&select={','.join(_OPEN_FIELDS)}")
        return [_project(r, _OPEN_FIELDS) for r in (rows or [])]

    closed = {_key(r) for r in _read_file("closed")}
    rows = [r for r in _read_file("open") if _key(r) not in closed]
    return [_project(r, _OPEN_FIELDS) for r in rows]


def load_closed() -> List[dict]:
    """Every closed position, oldest exit first."""
    if _config():
        rows = _request("GET", f"{_TABLE}?status=eq.closed&order=exit_date.asc"
                               f"&select={','.join(_CLOSED_FIELDS)}")
        return [_project(r, _CLOSED_FIELDS) for r in (rows or [])]
    return [_project(r, _CLOSED_FIELDS) for r in _read_file("closed")]


# ── writes ──────────────────────────────────────────────────────────────────

def open_position(
    ticker: str,
    entry_date: date,
    entry_price: float,
    signal_composite: Optional[float] = None,
    signal_drawdown: Optional[float] = None,
) -> bool:
    """Record a new open position. Returns False if it was already recorded.

    Idempotent on (ticker, entry_date). On Supabase that is a unique constraint,
    so two writers racing on the same signal cannot both insert -- the loser is
    rejected by the database rather than by a check that read stale state.
    """
    ticker = _clean_ticker(ticker)
    row = {
        "ticker": ticker,
        "entry_date": entry_date.isoformat(),
        "entry_price": float(entry_price),
        "signal_composite": signal_composite,
        "signal_drawdown": signal_drawdown,
        "reminder_sent": False,
        "status": "open",
    }

    if _config():
        try:
            _request("POST", _TABLE, json=row,
                     headers={"Prefer": "return=minimal"})
        except StorageError as exc:
            if "23505" in str(exc):        # unique_violation: already recorded
                logger.info("Position %s %s already recorded", ticker, entry_date)
                return False
            raise
        logger.info("Opened position: %s at %.2f on %s", ticker, entry_price, entry_date)
        return True

    rows = _read_file("open")
    if any(r.get("ticker") == row["ticker"]
           and r.get("entry_date") == row["entry_date"] for r in rows):
        logger.info("Position %s %s already recorded", ticker, entry_date)
        return False
    rows.append(_project(row, _OPEN_FIELDS))
    _write_file("open", rows)
    logger.info("Opened position: %s at %.2f on %s", ticker, entry_price, entry_date)
    return True


def close_position(
    ticker: str,
    entry_date: date,
    exit_date: date,
    exit_price: float,
    realized_return: float,
    days_held: int,
) -> bool:
    """Close one open position. Returns False if it was not open.

    On Supabase this is a single UPDATE of the row in place, so a position
    cannot end up in neither list (or both) the way a delete-then-insert across
    two tables can when the second half fails.
    """
    ticker = _clean_ticker(ticker)
    patch = {
        "status": "closed",
        "exit_date": exit_date.isoformat(),
        "exit_price": float(exit_price),
        "realized_return": float(realized_return),
        "days_held": int(days_held),
    }

    if _config():
        updated = _request(
            "PATCH",
            f"{_TABLE}?ticker=eq.{quote(ticker, safe='')}"
            f"&entry_date=eq.{quote(entry_date.isoformat(), safe='')}"
            f"&status=eq.open",
            json=patch, headers={"Prefer": "return=representation"},
        )
        return bool(updated)

    rows = _read_file("open")
    match = next((r for r in rows
                  if r.get("ticker") == ticker
                  and r.get("entry_date") == entry_date.isoformat()), None)
    if match is None:
        return False
    closed = _read_file("closed")
    closed.append(_project({**match, **patch}, _CLOSED_FIELDS))
    _write_file("closed", closed)
    _write_file("open", [r for r in rows if r is not match])
    return True


def mark_reminder_sent(ticker: str, entry_date: date) -> bool:
    """Claim the 30-day advance notice for this position.

    Returns True for the caller that actually flipped the flag, False if it was
    already set. The claim, not just the record: two runs overlapping -- the
    daily job and a manual re-run, say -- would both read reminder_sent=False
    and both send the notice. Filtering the UPDATE on reminder_sent=false makes
    the database the arbiter, so exactly one caller is told to send it.

    Persisted rather than recomputed so a skipped run -- weekend, holiday,
    outage -- cannot cause the reminder to be missed either.

    The file fallback claims by read-modify-write, which is not atomic across
    processes. That is left as it is deliberately: the only caller is
    check_exits, whose only caller is the daily run, which GitHub serializes
    with a concurrency group -- so nothing in this system issues two concurrent
    claims. A flock would not close the gap that matters either, being
    per-filesystem while the two halves run on different hosts. That is the
    reason the book is in Postgres rather than the reason to lock a file.
    """
    ticker = _clean_ticker(ticker)
    if _config():
        updated = _request(
            "PATCH",
            f"{_TABLE}?ticker=eq.{quote(ticker, safe='')}"
            f"&entry_date=eq.{quote(entry_date.isoformat(), safe='')}"
            f"&status=eq.open&reminder_sent=is.false",
            json={"reminder_sent": True},
            headers={"Prefer": "return=representation"},
        )
        return bool(updated)

    rows = _read_file("open")
    claimed = False
    for r in rows:
        if (r.get("ticker") == ticker
                and r.get("entry_date") == entry_date.isoformat()
                and not r.get("reminder_sent")):
            r["reminder_sent"] = True
            claimed = True
    if claimed:
        _write_file("open", rows)
    return claimed


def count_open() -> int:
    """How many positions are open. Cheap on Supabase: no rows come back."""
    if _config():
        cfg = _config()
        assert cfg is not None
        url, key = cfg
        try:
            resp = requests.get(
                f"{url}/rest/v1/{_TABLE}?status=eq.open&select=id",
                headers={"apikey": key, "Authorization": f"Bearer {key}",
                         "Prefer": "count=exact", "Range": "0-0"},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise StorageError(f"count_open failed: {exc}") from exc
        if resp.status_code >= 400:
            raise StorageError(f"count_open -> {resp.status_code}: {resp.text[:200]}")
        # Content-Range is "0-0/<total>" (or "*/0" when empty).
        total = resp.headers.get("Content-Range", "*/0").split("/")[-1]
        return int(total) if total.isdigit() else 0
    # Counts what load_open() would return rather than the raw file, so the
    # summary line cannot disagree with the page about how many are open.
    return len(load_open())
