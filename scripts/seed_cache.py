#!/usr/bin/env python3
"""Seed the runtime cache from the committed prebuilt cache on a cold deploy.

`data/cache/` is gitignored and therefore empty on a fresh clone (Render, a fresh
CI runner). This copies the committed `data/seed_cache/` tree into `data/cache/`
so the Simulator's first request is already warm — the true Top-100 universe
builds and the backtest runs without live price/EDGAR fetches.

Idempotent and non-destructive: only files that do NOT already exist in
`data/cache/` are copied, so a warm cache (or newer daily-run data) is never
overwritten. Safe to run on every boot.

    python scripts/seed_cache.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SEED = REPO / "data" / "seed_cache"
_CACHE = REPO / "data" / "cache"


def seed() -> int:
    if not _SEED.is_dir():
        print(f"seed_cache: nothing to seed ({_SEED} not found)")
        return 0
    copied = skipped = errors = 0
    for src in _SEED.rglob("*"):
        if not src.is_file() or src.name == "manifest.json":
            continue
        rel = src.relative_to(_SEED)
        dst = _CACHE / rel
        if dst.exists():
            skipped += 1
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except Exception as e:
            print(f"seed_cache: ERROR copying {rel}: {e}")
            errors += 1
    status = f"seed_cache: copied {copied} file(s), skipped {skipped} already present"
    if errors:
        status += f", {errors} error(s)"
    print(f"{status} -> {_CACHE}")
    return copied


if __name__ == "__main__":
    seed()
    sys.exit(0)
