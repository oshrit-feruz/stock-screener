from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import requests

from config.tickers import VALIDATION_UNIVERSE

_log = logging.getLogger(__name__)

_RAW_DIR = Path(__file__).parent.parent.parent / "data" / "sp500_raw"
_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "universe_cache"
_CSV_PATH = _RAW_DIR / "constituents.csv"
_SOURCE_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)
_TIMEOUT = 15


def _download_constituents() -> bool:
    """Download the S&P 500 constituents CSV from GitHub. Returns True on success."""
    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(_SOURCE_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        _CSV_PATH.write_bytes(resp.content)
        return True
    except Exception as exc:
        _log.warning("Failed to download S&P 500 constituents: %s", exc)
        return False


def _load_constituents() -> list[dict] | None:
    """Return raw rows from CSV, downloading if necessary. None on failure."""
    if not _CSV_PATH.exists():
        if not _download_constituents():
            return None
    try:
        import pandas as pd  # noqa: PLC0415
        df = pd.read_csv(_CSV_PATH)
        if "Symbol" not in df.columns or "Date added" not in df.columns:
            _log.warning("Unexpected columns in constituents CSV: %s", list(df.columns))
            return None
        return df[["Symbol", "Date added"]].dropna(subset=["Symbol"]).to_dict("records")
    except Exception as exc:
        _log.warning("Failed to parse constituents CSV: %s", exc)
        return None


def _cache_path(as_of: date) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{as_of.isoformat()}.json"


def get_universe_on_date(as_of: date | str) -> list[str]:
    """
    Return S&P 500 tickers that were members on `as_of` date.

    Uses the "Date added" column from the current constituents list as a proxy:
    tickers added after `as_of` are excluded. This is a partial point-in-time
    filter — it does NOT account for historical removals (survivorship bias
    from delisted stocks remains). Falls back to VALIDATION_UNIVERSE if the
    data source is unavailable.
    """
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)

    cp = _cache_path(as_of)
    if cp.exists():
        try:
            return json.loads(cp.read_text())
        except Exception:
            pass

    rows = _load_constituents()
    if rows is None:
        _log.warning("Falling back to VALIDATION_UNIVERSE (50 tickers) for %s", as_of)
        return list(VALIDATION_UNIVERSE)

    result: list[str] = []
    for row in rows:
        symbol = str(row["Symbol"]).strip()
        date_added_raw = row.get("Date added", "")
        if not date_added_raw or str(date_added_raw).strip() in ("", "nan"):
            # Unknown add date — include conservatively
            result.append(symbol)
            continue
        try:
            date_added = date.fromisoformat(str(date_added_raw).strip()[:10])
            if date_added <= as_of:
                result.append(symbol)
        except ValueError:
            # Unparseable date — include conservatively
            result.append(symbol)

    result = sorted(set(result))
    _log.info("PIT universe on %s: %d tickers", as_of, len(result))

    try:
        cp.write_text(json.dumps(result))
    except Exception:
        pass

    return result


def refresh_constituents() -> bool:
    """Force re-download the constituents CSV, clearing the disk cache."""
    try:
        _CSV_PATH.unlink(missing_ok=True)
        for f in _CACHE_DIR.glob("*.json"):
            f.unlink(missing_ok=True)
    except Exception:
        pass
    return _download_constituents()
