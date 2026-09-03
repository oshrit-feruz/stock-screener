#!/usr/bin/env python3
"""Download + extract the prebuilt Simulator cache from a GitHub Release asset.

The 2010-2026 seed cache is too large to commit to the repo (unlike the 2018-2024
cache in PR #30, which was ~55MB), so it ships as a release asset instead and is
fetched at deploy time — BEFORE scripts/seed_cache.py copies data/seed_cache/ into
data/cache/. That copy step is unchanged: this script's only job is to make sure
data/seed_cache/ exists and is populated before it runs.

The asset is fetched from the release's DIRECT download URL first
(github.com/<repo>/releases/download/<tag>/<asset>, trying GitHub's ".1"/".2"
renames of a duplicate upload as well); the api.github.com lookup is only the
fallback, because that endpoint is rate-limited per source IP and a hosting
provider's shared egress exhausts it — the live service booted cold on an
HTTP 403 from it while the asset itself was perfectly downloadable.

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
import shutil
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
_TGZ = ".tar.gz"
_TIMEOUT = 300  # the archive can be a few hundred MB; allow a slow connection


def _auth_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _is_regular_file(path: Path) -> bool:
    """A real file at `path` — not a directory, and not a symlink to one.
    is_file() follows links, so a manifest.json symlink would read as a
    present seed and skip every fetch; a link is never a manifest."""
    return not path.is_symlink() and path.is_file()


def _asset_candidates(asset_name: str) -> list[str]:
    """The names an asset may carry on the release: the configured one first,
    then GitHub's own renames of a duplicate upload (".1", ".2", ".3")."""
    if not asset_name.endswith(_TGZ):
        return [asset_name]
    stem = asset_name[: -len(_TGZ)]
    return [asset_name] + [f"{stem}.{n}{_TGZ}" for n in (1, 2, 3)]


def _open_direct(repo: str, tag: str, asset_name: str, headers: dict):
    """Stream the asset from the release's direct download URL, trying each
    candidate name in turn. Returns (response, name) on a 200, else None.

    Direct first, the API second: the per-tag API lookup below is one
    unauthenticated call to api.github.com, and that endpoint is rate-limited
    PER SOURCE IP (60/hour) — an IP shared by every service on the same
    hosting egress. The live service booted cold with "HTTP 403" on exactly
    that call. The download URL goes through github.com and its CDN instead,
    is not metered that way, and needs no token on a public repo.
    """
    for name in _asset_candidates(asset_name):
        url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT, stream=True,
                                allow_redirects=True)
        except Exception as exc:
            log.warning("RELEASE_CACHE: direct download of %s failed: %r", name, exc)
            continue
        if resp.status_code == 200:
            return resp, name
        log.warning("RELEASE_CACHE: no asset at %s (HTTP %s)", url, resp.status_code)
        resp.close()
    return None


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
    stem = asset_name[: -len(_TGZ)] if asset_name.endswith(_TGZ) else asset_name
    candidates = sorted(
        (a for a in assets if a.get("name", "").startswith(stem) and a["name"].endswith(_TGZ)),
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

    if _is_regular_file(_SEED / "manifest.json"):
        log.warning("RELEASE_CACHE: %s already present, skipping download "
                    "(repo=%s tag=%s asset=%s)", _SEED, repo, tag, asset_name)
        return True

    log.warning("RELEASE_CACHE: fetch attempted — repo=%s tag=%s asset=%s", repo, tag, asset_name)
    headers = {**_auth_headers(), "Accept": "application/vnd.github+json"}

    direct = _open_direct(repo, tag, asset_name, _auth_headers())
    if direct is not None:
        dl_resp, name = direct
        log.warning("RELEASE_CACHE: download OK (direct) — %s (%s bytes)",
                    name, dl_resp.headers.get("Content-Length", "?"))
        return _extract(dl_resp)

    # Direct URL gave nothing: fall back to asking the API which assets exist.
    log.warning("RELEASE_CACHE: direct download found no asset; asking the GitHub API")
    try:
        rel_resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
            headers=headers, timeout=30,
        )
    except Exception as exc:
        log.warning("RELEASE_CACHE: download FAILED — could not reach GitHub API: %r", exc)
        return False
    if rel_resp.status_code != 200:
        hint = (" — a 403 here is usually the unauthenticated GitHub API rate limit "
                "(60 requests/hour per source IP, shared across a hosting provider's "
                "egress), not a missing release" if rel_resp.status_code == 403 else "")
        log.warning("RELEASE_CACHE: download FAILED — release %s/%s not found (HTTP %s)%s; "
                    "the app will run cold (fallback universe) until the seed lands.",
                    repo, tag, rel_resp.status_code, hint)
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
    return _extract(dl_resp)


def _extract(dl_resp) -> bool:
    """Unpack a streamed tar.gz response into data/seed_cache/, atomically.

    The archive is unpacked into a STAGING directory beside the seed and only
    moved into place once it is complete and carries manifest.json. Unpacking
    straight into data/seed_cache/ would let a download that died halfway —
    after manifest.json, before the price pickles — leave a "present" seed
    behind, and every later fetch would skip on that manifest while the cache
    stayed empty. A failed or incomplete archive leaves nothing behind.
    True when the seed is in place afterwards. Never raises.
    """
    staging = _SEED.parent / (_SEED.name + ".partial")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        staging_resolved = staging.resolve()
        n_extracted = 0
        with tarfile.open(fileobj=dl_resp.raw, mode="r|gz") as tar:
            # Guard against path traversal in a (trusted, but still validated)
            # archive — refuse any member that would land outside the staging dir.
            for member in tar:
                # Links are never part of a seed. Python 3.11's default
                # extraction follows them, so an archive could plant a
                # manifest.json symlink pointing anywhere and pass the
                # checks below; the containment check alone cannot see
                # where a link points.
                if member.issym() or member.islnk():
                    raise ValueError(f"link entry in archive: {member.name}")
                target = (staging / member.name).resolve()
                if staging_resolved not in target.parents and target != staging_resolved:
                    raise ValueError(f"unsafe path in archive: {member.name}")
                tar.extract(member, staging)
                if member.isfile():
                    n_extracted += 1
        if not _is_regular_file(staging / "manifest.json"):
            raise ValueError("archive carries no manifest.json")
        # Complete and validated: swap it in. rmtree of a stale seed is only
        # reached when no manifest was there (fetch_and_extract returns early
        # otherwise), so nothing usable is ever discarded.
        shutil.rmtree(_SEED, ignore_errors=True)
        os.replace(staging, _SEED)
    except Exception as exc:
        log.warning("RELEASE_CACHE: extraction FAILED — %r; nothing was installed", exc)
        shutil.rmtree(staging, ignore_errors=True)
        return False

    log.warning("RELEASE_CACHE: extracted %d file(s) -> %s (manifest present: True)",
                n_extracted, _SEED)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    fetch_and_extract()
    sys.exit(0)
