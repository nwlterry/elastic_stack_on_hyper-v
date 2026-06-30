#!/usr/bin/env python3
"""Download Elastic Stack RPMs and agent archives for offline upgrade."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PKG_DIR = ROOT / "packages"
VERSIONS = {
    "intermediate": "8.19.9",
    "target": "9.4.1",
}
ARTIFACTS = [
    ("elasticsearch", "rpm"),
    ("kibana", "rpm"),
]
AGENT_ARCHIVE = "elastic-agent-{version}-linux-x86_64.tar.gz"
AGENT_VERIFY_SUFFIXES = (".sha512", ".asc")
AGENT_GPG_KEY = "GPG-KEY-elastic-agent"


def download(url: str, dest: Path) -> None:
    if dest.is_file() and dest.stat().st_size > 100:
        print(f"  OK exists {dest.name} ({dest.stat().st_size // 1024}KB)", flush=True)
        return
    print(f"  GET {url}", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    proc = subprocess.run(
        ["curl.exe", "-fSL", "-o", str(tmp), url],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"download failed {url}: {proc.stderr[:300]}")
    tmp.replace(dest)
    print(f"  OK saved {dest.name}", flush=True)


def main() -> int:
    PKG_DIR.mkdir(parents=True, exist_ok=True)
    gpg = PKG_DIR / "GPG-KEY-elasticsearch"
    if not gpg.is_file():
        download(
            "https://artifacts.elastic.co/GPG-KEY-elasticsearch",
            gpg,
        )

    agent_gpg = PKG_DIR / AGENT_GPG_KEY
    if not agent_gpg.is_file():
        download(
            f"https://artifacts.elastic.co/{AGENT_GPG_KEY}",
            agent_gpg,
        )

    for label, version in VERSIONS.items():
        print(f"\n=== {label} {version} ===", flush=True)
        for pkg, ext in ARTIFACTS:
            name = f"{pkg}-{version}-x86_64.{ext}"
            url = f"https://artifacts.elastic.co/downloads/{pkg}/{name}"
            download(url, PKG_DIR / name)
        archive = AGENT_ARCHIVE.format(version=version)
        base = f"https://artifacts.elastic.co/downloads/beats/elastic-agent/{archive}"
        download(base, PKG_DIR / archive)
        for suffix in AGENT_VERIFY_SUFFIXES:
            download(f"{base}{suffix}", PKG_DIR / f"{archive}{suffix}")

    print(f"\nPackages ready in {PKG_DIR}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())