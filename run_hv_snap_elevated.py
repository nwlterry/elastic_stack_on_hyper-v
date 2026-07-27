#!/usr/bin/env python3
"""Elevate Hyper-V snap reset via UAC ShellExecute or SYSTEM task; wait for DONE in log."""
from __future__ import annotations

import ctypes
import json
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PS1 = ROOT / "Remove-AllHyperVSnaps-And-Snapshot.ps1"
LOG = ROOT / "logs" / "hv-snap-reset.log"
SNAP = f"post-roles-data-content-{datetime.now().strftime('%Y%m%d-%H%M')}"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    arg = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{PS1}" '
        f'-SnapshotName "{SNAP}" -LogPath "{LOG}"'
    )
    print(f"SnapshotName={SNAP}", flush=True)
    print(f"arg={arg}", flush=True)

    # Prefer SYSTEM scheduled task (no UAC popup)
    task = "ISMELK-HV-Snap-Reset2"
    ps = f"""
$ErrorActionPreference = 'Continue'
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -EA SilentlyContinue
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {json.dumps(arg)}
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName '{task}' -Action $a -Principal $p -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName '{task}' | Select-Object State | Format-List
Get-ScheduledTaskInfo -TaskName '{task}' | Format-List
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(r.stdout, flush=True)
    print(r.stderr, flush=True)

    # Also try ShellExecute elevation as backup if log stays empty
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", arg, str(ROOT), 1
    )
    print(f"ShellExecuteW={rc}", flush=True)

    for i in range(120):
        if LOG.is_file():
            text = LOG.read_text(encoding="utf-8", errors="replace")
            if "DONE" in text:
                print(text[-3000:], flush=True)
                print(f"OK SnapshotName={SNAP}", flush=True)
                return 0
            if i % 6 == 0 and text.strip():
                print(f"--- log @ {i*5}s ---\n{text[-800:]}", flush=True)
        time.sleep(5)
    print("TIMEOUT waiting for hv log DONE", flush=True)
    if LOG.is_file():
        print(LOG.read_text(encoding="utf-8", errors="replace")[-2000:], flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
