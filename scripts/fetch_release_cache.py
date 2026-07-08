#!/usr/bin/env python3
"""Download + extract the prebuilt Simulator cache from a GitHub Release asset.

The 2010-2026 seed cache is too large to commit to the repo (unlike the 2018-2024
cache in PR #30, which was ~55MB), so it ships as a release asset instead and is
fetched at deploy time — BEFORE scripts/seed_cache.py copies data/seed_cache/ into
data/cache/. That copy step is unchanged: this script's only job is to make sure
data/seed_cache/ exists and is populated before it runs.

Configuration (env vars, all with defaults matching this repo):
    SEED_CACHE_RELEASE_REPO   "owner/repo"                  (default: oshrit-feruz/stock-screener)
    SEED_CACHE_RELEASE_TAG    release tag holding the asset  (default: cache-v1)
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
_DEFAULT_TAG = "cache-v1"
_DEFAULT_ASSET = "seed_cache_2010_2026.tar.gz"
_TIMEOUT = 300  # the archive can be a few hundred MB; allow a slow connection


def _auth_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _find_asset(assets: list, asset_name: str):
    """Match `asset_name` exactly first; if absent, tolerate GitHub's own
    disambiguation of a duplicate upload (re-uploading the same filename to a
    release yields "seed_cache_2010_2026.1.tar.gz", ".2", ...). Without this
    fallback a renamed asset makes fetch_and_extract() silently return False
    and the app boots with a cold cache — exactly the failure mode this
    function exists to prevent. Returns (asset_or_None, used_fuzzy_match).
    """
    exact = next((a for a in assets if a.get("name") == asset_name), None)
    if exact is not None:
        return exact, False
    stem = asset_name[: -len(".tar.gz")] if asset_name.endswith(".tar.gz") else asset_name
    candidates = sorted(
        (a for a in assets if a.get("name", "").startswith(stem) and a["name"].endswith(".tar.gz")),
        key=lambda a: a["name"],
    )
    return (candidates[0], True) if candidates else (None, False)


def fetch_and_extract() -> bool:
    """Return True if the seed cache is present after this call (already there,
    or freshly downloaded); False if it's missing and the download failed/was
    skipped. Never raises. Logs every stage at WARNING (not INFO): this runs
    both at build time (own process; __main__ calls logging.basicConfig) and
    at app-startup time (imported into product/api/main.py's lifespan, where
    nothing configures logging) — WARNING is what Python's logging "handler
    of last resort" actually prints in the latter case, so anything logged at
    INFO here would be invisible in the Render runtime logs even though it
    prints fine locally when this script is run directly.
    """
    repo = os.environ.get("SEED_CACHE_RELEASE_REPO", _DEFAULT_REPO)
    tag = os.environ.get("SEED_CACHE_RELEASE_TAG", _DEFAULT_TAG)
    asset_name = os.environ.get("SEED_CACHE_RELEASE_ASSET", _DEFAULT_ASSET)

    if (_SEED / "manifest.json").exists():
        log.warning("RELEASE_CACHE: %s already present, skipping download (repo=%s tag=%s asset=%s)",
                    _SEED, repo, tag, asset_name)
        return True

    log.warning("RELEASE_CACHE: fetch attempted — repo=%s tag=%s asset=%s", repo, tag, asset_name)
    headers = {**_auth_headers(), "Accept": "application/vnd.github+json"}

    try:
        rel_resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
            headers=headers, timeout=30,
        )
    except Exception as exc:
        log.warning("RELEASE_CACHE: download FAILED — could not reach GitHub API: %r", exc)
        return False
    if rel_resp.status_code != 200:
        log.warning("RELEASE_CACHE: download FAILED — release %s/%s not found (HTTP %s); "
                    "the seed cache has not been published yet, the app will run cold "
                    "(fallback universe) until it is.",
                    repo, tag, rel_resp.status_code)
        return False

    assets = rel_resp.json().get("assets", [])
    asset, fuzzy = _find_asset(assets, asset_name)
    if asset is None:
        log.warning("RELEASE_CACHE: download FAILED — asset %s not found on release %s "
                    "(available: %s)", asset_name, tag, [a.get("name") for a in assets])
        return False
    if fuzzy:
        log.warning("RELEASE_CACHE: exact asset %s not found; using %s instead "
                    "(GitHub renames a duplicate upload with a .N suffix)",
                    asset_name, asset["name"])

    try:
        dl_resp = requests.get(
            asset["url"],
            headers={**headers, "Accept": "application/octet-stream"},
            timeout=_TIMEOUT, stream=True,
        )
        dl_resp.raise_for_status()
    except Exception as exc:
        log.warning("RELEASE_CACHE: download FAILED — %s: %r", asset["name"], exc)
        return False
    log.warning("RELEASE_CACHE: download OK — %s (%s bytes)", asset["name"], asset.get("size", "?"))

    try:
        _SEED.mkdir(parents=True, exist_ok=True)
        seed_resolved = _SEED.resolve()
        n_extracted = 0
        with tarfile.open(fileobj=dl_resp.raw, mode="r|gz") as tar:
            # Guard against path traversal in a (trusted, but still validated)
            # archive — refuse any member that would land outside data/seed_cache/.
            for member in tar:
                target = (_SEED / member.name).resolve()
                # Use proper containment check: target must be seed_cache or a descendant
                if seed_resolved not in target.parents and target != seed_resolved:
                    raise ValueError(f"unsafe path in archive: {member.name}")
                tar.extract(member, _SEED)
                if member.isfile():
                    n_extracted += 1
    except Exception as exc:
        log.warning("RELEASE_CACHE: extraction FAILED — %r", exc)
        return False

    ok = (_SEED / "manifest.json").exists()
    log.warning("RELEASE_CACHE: extracted %d file(s) -> %s (manifest present: %s)",
               n_extracted, _SEED, ok)
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fetch_and_extract()
    sys.exit(0)
