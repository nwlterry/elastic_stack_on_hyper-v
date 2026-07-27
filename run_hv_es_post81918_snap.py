#!/usr/bin/env python3
"""Elevate Checkpoint-EsNodes.ps1 (second post-upgrade 8.19.18 HV snap on ES only)."""
from __future__ import annotations

import ctypes
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PS1 = ROOT / "Checkpoint-EsNodes.ps1"
LOG = ROOT / "logs" / "hv-es-post-81918-snap.log"
SNAP = "post-upgrade-system-8.19.18-20260724"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    arg = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{PS1}" '
        f'-SnapshotName "{SNAP}" -LogPath "{LOG}"'
    )
    print(f"SnapshotName={SNAP}", flush=True)
    print(f"arg={arg}", flush=True)

    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", arg, str(ROOT), 1
    )
    print(f"ShellExecuteW={rc}", flush=True)
    if rc <= 32:
        print("ERROR elevation failed", flush=True)
        return 1

    for i in range(180):
        if LOG.is_file():
            text = LOG.read_text(encoding="utf-8", errors="replace")
            if "DONE" in text:
                print(text, flush=True)
                print(f"OK SnapshotName={SNAP}", flush=True)
                return 0 if "DONE_WITH_ERRORS" not in text else 2
            if i % 6 == 0 and text.strip():
                print(f"--- log @ {i*5}s ---\n{text[-800:]}", flush=True)
        time.sleep(5)
    print("TIMEOUT waiting for DONE", flush=True)
    if LOG.is_file():
        print(LOG.read_text(encoding="utf-8", errors="replace"), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
