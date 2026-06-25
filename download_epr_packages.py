#!/usr/bin/env python3
"""Download Fleet integration zips for offline staging (run on orchestrator with network)."""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from config_loader import EPR_PACKAGES, ROOT

EPR_DIR = ROOT / "packages" / "epr"
EPR_BASE = "https://epr.elastic.co/download"


def download_package(name: str, version: str) -> Path:
    EPR_DIR.mkdir(parents=True, exist_ok=True)
    zip_name = f"{name}-{version}.zip"
    dest = EPR_DIR / zip_name
    if dest.is_file() and dest.stat().st_size > 1024:
        print(f"OK {zip_name} (exists)")
        return dest

    url = f"{EPR_BASE}/{name}/{zip_name}"
    print(f"Downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "elastic-stack-hyperv/1.0"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = resp.read()
    dest.write_bytes(data)
    print(f"OK {zip_name} ({len(data)} bytes)")
    return dest


def main() -> int:
    missing = 0
    for name, version in EPR_PACKAGES.items():
        try:
            download_package(name, version)
        except Exception as exc:
            print(f"FAIL {name}-{version}: {exc}", file=sys.stderr)
            missing += 1
    if missing:
        print(f"\n{missing} package(s) failed. Copy zips manually to {EPR_DIR}", file=sys.stderr)
        return 1
    print(f"\nAll packages in {EPR_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())