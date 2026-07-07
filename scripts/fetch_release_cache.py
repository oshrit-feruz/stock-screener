#!/usr/bin/env python3
"""Download + extract the prebuilt Simulator cache from a GitHub Release asset.

The 2010-2026 seed cache is too large to commit to the repo (unlike the 2018-2024
cache in PR #30, which was ~55MB), so it ships as a release asset instead and is
fetched at deploy time — BEFORE scripts/seed_cache.py copies data/seed_cache/ into
data/cache/. That copy step is unchanged: this script's only job is to make sure
data/seed_cache/ exists and is populated before it runs.

Configuration (env vars, all with defaults matching this repo):
    SEED_CACHE_RELEASE_REPO   "owner/repo"                  (default: oshrit-feruz/stock-screener)
    SEED_CACHE_RELEASE_TAG    release tag holding the asset  (default: seed-cache-2010-2026)
    SEED_CACHE_RELEASE_ASSET  asset file name                (default: seed_cache_2010_2026.tar.gz)
    GITHUB_TOKEN              optional; required only if the repo is private
                              (a public repo's release assets download without one)

Idempotent: if data/seed_cache/manifest.json already exists, the download is
skipped entirely (mirrors seed_cache.seed()'s own skip-if-present behavior, so
re-running this on every boot costs nothing once the cache has landed once).

Fails OPEN: any error (network, missing token, 404) is logged and the function
returns without raising — the caller (build step or app startup) continues
regardless, exactly like the rest of this codebase's cache-layer fallbacks. A
missing seed cache means a cold-cache backtest (slower / fallback universe),
not a crash.
"""
from __future__ import annotations

import io
import logging
import os
import sys
import tarfile
from pathlib import Path

import requests

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
_SEED = REPO / "data" / "seed_cache"

_DEFAULT_REPO = "oshrit-feruz/stock-screener"
_DEFAULT_TAG = "seed-cache-2010-2026"
_DEFAULT_ASSET = "seed_cache_2010_2026.tar.gz"
_TIMEOUT = 300  # the archive can be a few hundred MB; allow a slow connection


def _auth_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_and_extract() -> bool:
    """Return True if the seed cache is present after this call (already there,
    or freshly downloaded); False if it's missing and the download failed/was
    skipped. Never raises.
    """
    if (_SEED / "manifest.json").exists():
        log.info("fetch_release_cache: %s already present, skipping download", _SEED)
        return True

    repo = os.environ.get("SEED_CACHE_RELEASE_REPO", _DEFAULT_REPO)
    tag = os.environ.get("SEED_CACHE_RELEASE_TAG", _DEFAULT_TAG)
    asset_name = os.environ.get("SEED_CACHE_RELEASE_ASSET", _DEFAULT_ASSET)
    headers = {**_auth_headers(), "Accept": "application/vnd.github+json"}

    try:
        rel_resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
            headers=headers, timeout=30,
        )
    except Exception as exc:
        log.warning("fetch_release_cache: could not reach GitHub API: %r", exc)
        return False
    if rel_resp.status_code != 200:
        log.warning("fetch_release_cache: release %s/%s not found (HTTP %s) — "
                    "the seed cache has not been published yet; the app will "
                    "run cold (fallback universe) until it is.",
                    repo, tag, rel_resp.status_code)
        return False

    assets = rel_resp.json().get("assets", [])
    asset = next((a for a in assets if a.get("name") == asset_name), None)
    if asset is None:
        log.warning("fetch_release_cache: asset %s not found on release %s "
                    "(available: %s)", asset_name, tag, [a.get("name") for a in assets])
        return False

    try:
        dl_resp = requests.get(
            asset["url"],
            headers={**headers, "Accept": "application/octet-stream"},
            timeout=_TIMEOUT, stream=True,
        )
        dl_resp.raise_for_status()
        archive_bytes = dl_resp.content
    except Exception as exc:
        log.warning("fetch_release_cache: download of %s failed: %r", asset_name, exc)
        return False

    try:
        _SEED.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            # Guard against path traversal in a (trusted, but still validated)
            # archive — refuse any member that would land outside data/seed_cache/.
            for member in tar.getmembers():
                target = (_SEED / member.name).resolve()
                if not str(target).startswith(str(_SEED.resolve())):
                    raise ValueError(f"unsafe path in archive: {member.name}")
            tar.extractall(_SEED)
    except Exception as exc:
        log.warning("fetch_release_cache: extraction failed: %r", exc)
        return False

    ok = (_SEED / "manifest.json").exists()
    log.warning("fetch_release_cache: downloaded + extracted %s -> %s (manifest present: %s)",
               asset_name, _SEED, ok)
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fetch_and_extract()
    sys.exit(0)
