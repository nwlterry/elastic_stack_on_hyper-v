#!/usr/bin/env python3
"""
1) Elevate + rename Hyper-V checkpoints to pre-upgrade-system-8.18.4-20260724
2) Delete all Elasticsearch snapshots in fs_nfs_snapshots
3) Create pre-upgrade-system-8.18.4-20260724 ES snapshot (system + features + global)
"""
from __future__ import annotations

import ctypes
import json
import shlex
import subprocess
import time
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run

ROOT = Path(__file__).resolve().parent
HV_PS1 = ROOT / "Rename-HyperVSnaps.ps1"
HV_LOG = ROOT / "logs" / "hv-rename-snap.log"
OLD_HV = "post-roles-data-content-20260724-1245"
SNAP_NAME = "pre-upgrade-system-8.18.4-20260724"
REPO = "fs_nfs_snapshots"


def es(c, auth, method, path, body=None, timeout=600):
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def parse_json_blob(out: str):
    start = out.find("{")
    if start < 0:
        start = out.find("[")
    if start < 0:
        return {}
    return json.loads(out[start:])


def rename_hyperv() -> bool:
    HV_LOG.parent.mkdir(parents=True, exist_ok=True)
    HV_LOG.write_text("", encoding="utf-8")
    arg = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{HV_PS1}" '
        f'-OldName "{OLD_HV}" -NewName "{SNAP_NAME}" -LogPath "{HV_LOG}"'
    )
    print(f"=== Hyper-V rename: {OLD_HV} -> {SNAP_NAME} ===", flush=True)
    print(f"arg={arg}", flush=True)

    # SYSTEM scheduled task (no UAC if allowed)
    task = "ISMELK-HV-Rename-Snap"
    ps = f"""
$ErrorActionPreference = 'Continue'
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -EA SilentlyContinue
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {json.dumps(arg)}
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName '{task}' -Action $a -Principal $p -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName '{task}' | Select-Object State | Format-List
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(r.stdout, flush=True)
    print(r.stderr, flush=True)

    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", arg, str(ROOT), 1
    )
    print(f"ShellExecuteW={rc}", flush=True)

    for i in range(60):
        if HV_LOG.is_file():
            text = HV_LOG.read_text(encoding="utf-8", errors="replace")
            if "DONE" in text:
                print(text, flush=True)
                return "DONE_WITH_ERRORS" not in text
            if i % 3 == 0 and text.strip():
                print(f"--- hv log @ {i*2}s ---\n{text[-600:]}", flush=True)
        time.sleep(2)
    print("TIMEOUT waiting for Hyper-V rename DONE", flush=True)
    if HV_LOG.is_file():
        print(HV_LOG.read_text(encoding="utf-8", errors="replace"), flush=True)
    return False


def connect_any():
    last = None
    for key in ("es02", "es01", "es03", "es04"):
        try:
            c = connect(NODES[key][0])
            auth = curl_elastic_auth(get_elastic_password(c))
            print(f"connected via {key}", flush=True)
            return c, auth
        except Exception as e:
            last = e
            print(f"{key}: {e}", flush=True)
    raise RuntimeError(f"no ES node reachable: {last}")


def delete_all_es_snapshots(c, auth) -> None:
    print(f"=== Delete all ES snapshots in {REPO} ===", flush=True)
    out = es(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty")
    print(out[:2000], flush=True)
    data = parse_json_blob(out)
    snaps = data.get("snapshots") or []
    if not snaps:
        # empty repo may return error or empty
        print("no snapshots found (or empty list)", flush=True)
        # try delete wildcard just in case
        print(es(c, auth, "DELETE", f"/_snapshot/{REPO}/*?pretty", timeout=600), flush=True)
        return
    for s in snaps:
        name = s.get("snapshot")
        if not name:
            continue
        print(f"  DELETE {name}", flush=True)
        print(
            es(c, auth, "DELETE", f"/_snapshot/{REPO}/{name}?pretty", timeout=600),
            flush=True,
        )
    time.sleep(2)
    after = es(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty")
    print("after delete:", after[:1200], flush=True)


def create_es_snapshot(c, auth) -> str:
    print(f"=== Create ES snapshot {SNAP_NAME} ===", flush=True)
    print(es(c, auth, "GET", "/_features?pretty")[:2000], flush=True)
    body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": ["*"],
        "metadata": {
            "taken_by": "rename_hv_and_es_preupgrade_snap",
            "taken_because": "pre-upgrade system 8.18.4: system indices + all feature states + cluster state",
            "es_version": "8.18.4",
        },
    }
    print(
        es(
            c,
            auth,
            "PUT",
            f"/_snapshot/{REPO}/{SNAP_NAME}?wait_for_completion=false&pretty",
            body,
            timeout=120,
        ),
        flush=True,
    )

    state = "IN_PROGRESS"
    for i in range(180):
        out = es(c, auth, "GET", f"/_snapshot/{REPO}/{SNAP_NAME}?pretty", timeout=120)
        data = parse_json_blob(out)
        snaps = data.get("snapshots") or []
        s = snaps[0] if snaps else data.get("snapshot") or data
        if isinstance(s, dict):
            state = s.get("state") or state
            shards = s.get("shards") or {}
            feats = [f.get("feature_name") for f in (s.get("feature_states") or [])]
            print(
                f"  poll {i}: state={state} shards={shards} features={feats}",
                flush=True,
            )
            if state in ("SUCCESS", "FAILED", "PARTIAL"):
                print(
                    json.dumps(
                        {
                            "snapshot": s.get("snapshot"),
                            "state": state,
                            "include_global_state": s.get("include_global_state"),
                            "indices_count": len(s.get("indices") or []),
                            "feature_states": feats,
                            "shards": shards,
                            "failures": s.get("failures"),
                            "duration_in_millis": s.get("duration_in_millis"),
                        },
                        indent=2,
                    ),
                    flush=True,
                )
                break
        else:
            print(f"  poll {i}: raw={out[:400]}", flush=True)
        time.sleep(10)
    else:
        print("ES snapshot poll timeout", flush=True)
        return "TIMEOUT"

    print(
        es(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty")[:2000],
        flush=True,
    )
    return state


def main() -> int:
    hv_ok = rename_hyperv()
    if not hv_ok:
        print("WARN Hyper-V rename may have failed; continuing with ES snapshot", flush=True)

    c, auth = connect_any()
    try:
        delete_all_es_snapshots(c, auth)
        state = create_es_snapshot(c, auth)
    finally:
        c.close()

    print(
        f"\n=== DONE ===\n"
        f"Hyper-V checkpoint: {SNAP_NAME} (ok={hv_ok})\n"
        f"ES repo {REPO} snapshot: {SNAP_NAME} state={state}\n",
        flush=True,
    )
    if state != "SUCCESS":
        return 2
    if not hv_ok:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
