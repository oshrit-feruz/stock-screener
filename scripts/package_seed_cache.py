#!/usr/bin/env python3
"""Package data/seed_cache/ into the tarball shipped as a GitHub Release asset.

Companion to build_full_cache.py (which populates data/seed_cache/) and
fetch_release_cache.py (which downloads + extracts this exact tarball at deploy
time). The archive's top-level entries match seed_cache/ subfolder names
directly (no wrapping directory), so extracting into data/seed_cache/ reproduces
the source tree exactly — required for fetch_release_cache.py's extractall.

Run after build_full_cache.py:
    python scripts/package_seed_cache.py
    python scripts/package_seed_cache.py --out /tmp/my_archive.tar.gz
"""
from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SEED = REPO / "data" / "seed_cache"
_DEFAULT_OUT = REPO / "seed_cache_2010_2026.tar.gz"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(_DEFAULT_OUT), help="Output archive path")
    args = ap.parse_args()
    out = Path(args.out)

    if not (_SEED / "manifest.json").exists():
        raise SystemExit(f"{_SEED} has no manifest.json — run build_full_cache.py first.")

    n = 0
    with tarfile.open(out, "w:gz") as tar:
        for item in sorted(_SEED.iterdir()):
            tar.add(item, arcname=item.name)
            n += sum(1 for _ in item.rglob("*") if _.is_file()) if item.is_dir() else 1

    mb = out.stat().st_size / 1e6
    print(f"Packaged {n} files from {_SEED} -> {out} ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
