#!/usr/bin/env python3
"""Run Checkpoint-AllStackVMs.ps1 as SYSTEM (no UAC). Keeps existing checkpoints."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PS1 = ROOT / "Checkpoint-AllStackVMs.ps1"
LOG = ROOT / "logs" / "hv-checkpoint-elevated.log"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)
    args = p.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    ps_arg = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{PS1}" '
        f'-SnapshotName "{args.name}" -LogPath "{LOG}"'
    )
    task = "ISMELK-HV-Checkpoint"
    ps = f"""
$ErrorActionPreference = 'Continue'
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -EA SilentlyContinue
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {json.dumps(ps_arg)}
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName '{task}' -Action $a -Principal $p -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(r.stdout, r.stderr, flush=True)
    if r.returncode != 0 or "Access is denied" in (r.stdout + r.stderr):
        import ctypes

        print("SYSTEM task denied; requesting UAC ShellExecute", flush=True)
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell.exe", ps_arg, str(ROOT), 1
        )
        print(f"ShellExecuteW={rc}", flush=True)
        if rc <= 32:
            return 1
    for _ in range(180):
        text = LOG.read_text(encoding="utf-8", errors="replace") if LOG.is_file() else ""
        if "=== VERIFY ===" in text or "ERROR" in text:
            # wait for script end
            time.sleep(5)
            text = LOG.read_text(encoding="utf-8", errors="replace")
            print(text[-8000:], flush=True)
            if "ERROR" in text and "OK " not in text:
                return 1
            return 0
        time.sleep(5)
    print(LOG.read_text(encoding="utf-8", errors="replace")[-4000:] if LOG.is_file() else "no log", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
