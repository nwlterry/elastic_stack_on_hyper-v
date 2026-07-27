#!/usr/bin/env python3
"""
NFS snapshot repo + first system/state snapshot + es04 data_hot + master data_content.

Phases (use --phase to run subset):
  1 hyperv     - elevate: create es04 VM + Kibana NFS disk (Add-Es04AndNfsDisk.ps1)
  2 nfs        - Kibana NFS export + ES clients path.repo + rolling restart
  3 snapshot   - register fs repo, first system-indices + cluster-state snapshot
  4 es04       - wait flash, data disk, install ES 9.4.1, enroll data_hot
  5 roles      - es01–03 -> master + data_content (rolling)

Defaults: ES_UID=994 ES_GID=991, NFS on Kibana, mount /mnt/es-snapshots
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from deploy_ordered_stack import (
    CLUSTER,
    DOMAIN,
    ES_NODES,
    NODES,
    REMOTE,
    VERSION,
    connect,
    copy_scripts,
    curl_elastic_auth,
    get_elastic_password,
    run,
)

ROOT = Path(__file__).parent
ES_UID = 994
ES_GID = 991
NFS_SERVER = NODES["kibana"][1]
NFS_EXPORT = "/exports/elasticsearch-snapshots"
MOUNT_POINT = "/mnt/es-snapshots"
REPO_NAME = "fs_nfs_snapshots"
SNAPSHOT_NAME = "system-state-initial"
ES04_KEY = "es04"
ES04_IP = "10.44.40.34"
ES04_FQDN = f"ismelkesnode04.{DOMAIN}"
ES04_VM = "ISMELKESNODE04"

# remote_cluster_client required when monitoring.ui.ccs.enabled is true (or left default).
MASTER_ROLES = ["master", "data_content", "remote_cluster_client"]
HOT_ROLES = ["data_hot", "ingest", "transform", "remote_cluster_client"]


def _es04_in_nodes() -> bool:
    return ES04_KEY in NODES


def existing_es_targets() -> list[tuple[str, str]]:
    """es01–es03 only (before es04 joins)."""
    return [NODES[k] for k in ("es01", "es02", "es03") if k in NODES]


def all_es_targets() -> list[tuple[str, str]]:
    """All ES nodes currently defined in config (includes es04 when present)."""
    return list(ES_NODES)


def es_curl(c, auth: str, method: str, path: str, body: dict | None = None, timeout: int = 120) -> dict:
    data = ""
    if body is not None:
        data = f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    out = run(
        c,
        f"curl -sk -u {auth} -X {method} {data}'https://localhost:9200{path}'",
        check=False,
        timeout=timeout,
    )
    try:
        return json.loads(out.strip().splitlines()[-1] if out.strip() else "{}")
    except json.JSONDecodeError:
        return {"raw": out[:2000]}


def wait_cluster(c, auth: str, want: str = "green", timeout: int = 900) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = es_curl(c, auth, "GET", "/_cluster/health")
        status = h.get("status")
        print(f"  cluster status={status} nodes={h.get('number_of_nodes')}", flush=True)
        if status == want or (want == "yellow" and status in ("yellow", "green")):
            return True
        time.sleep(10)
    return False


def rolling_restart_es(auth: str, nodes: list[tuple[str, str]], reason: str) -> None:
    print(f"\n=== Rolling restart ES ({reason}) ===", flush=True)
    for ip, fqdn in nodes:
        print(f"--- {fqdn} ---", flush=True)
        c = connect(ip)
        run(c, "systemctl restart elasticsearch", timeout=120, check=False)
        # wait local
        for _ in range(60):
            out = run(
                c,
                f"curl -sk -u {auth} -o /dev/null -w '%{{http_code}}' https://localhost:9200",
                check=False,
            )
            if "200" in out or "401" in out:
                break
            time.sleep(5)
        c.close()
        es = connect(NODES["es01"][0])
        if not wait_cluster(es, auth, "green", timeout=600):
            print(f"WARN: cluster not green after {fqdn} restart", flush=True)
        es.close()


def phase_hyperv() -> int:
    print("=== Phase hyperv: es04 VM + Kibana NFS disk ===", flush=True)
    ps1 = ROOT / "Add-Es04AndNfsDisk.ps1"
    # Run via scheduled task as SYSTEM for elevation
    task = "ISMELK-AddEs04Nfs"
    out_log = r"C:\Windows\Temp\ismelk-add-es04-nfs.log"
    arg = (
        f"-NoProfile -ExecutionPolicy Bypass -File {shlex.quote(str(ps1))} "
        f"*> {shlex.quote(out_log)} 2>&1"
    )
    cmd = f"""
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {json.dumps(arg)}
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName '{task}' -Action $action -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
$deadline = (Get-Date).AddMinutes(45)
do {{
  Start-Sleep -Seconds 5
  $i = Get-ScheduledTaskInfo -TaskName '{task}'
}} while ($i.LastTaskResult -eq 267009 -and (Get-Date) -lt $deadline)
$info = Get-ScheduledTaskInfo -TaskName '{task}'
Write-Host ("LastTaskResult=" + $info.LastTaskResult)
Get-Content {json.dumps(out_log)} -ErrorAction SilentlyContinue | Select-Object -Last 80
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue
if ($info.LastTaskResult -ne 0) {{ exit 1 }}
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    print(r.stdout[-4000:] if r.stdout else "", flush=True)
    if r.stderr:
        print(r.stderr[-2000:], flush=True)
    if r.returncode != 0:
        print(
            "Hyper-V phase failed (need admin). Run elevated:\n"
            f"  powershell -ExecutionPolicy Bypass -File {ps1}",
            flush=True,
        )
        return 1
    return 0


def phase_nfs(auth: str) -> int:
    print("=== Phase nfs: export + clients + path.repo ===", flush=True)
    kb = connect(NODES["kibana"][0])
    copy_scripts(kb, roles=("kibana",))
    env = (
        f"ES_UID={ES_UID} ES_GID={ES_GID} "
        f"EXPORT_ROOT={shlex.quote(NFS_EXPORT)} "
        f"EXPORT_CLIENTS=10.44.40.0/24"
    )
    run(kb, f"{env} bash {REMOTE}/setup-nfs-snapshot-export.sh", timeout=600)
    kb.close()

    targets = existing_es_targets()
    for ip, fqdn in targets:
        print(f"--- NFS client {fqdn} ---", flush=True)
        c = connect(ip)
        copy_scripts(c, roles=("elasticsearch",))
        env = (
            f"ES_UID={ES_UID} ES_GID={ES_GID} "
            f"NFS_SERVER={shlex.quote(NFS_SERVER)} "
            f"NFS_EXPORT={shlex.quote(NFS_EXPORT)} "
            f"MOUNT_POINT={shlex.quote(MOUNT_POINT)}"
        )
        run(c, f"{env} bash {REMOTE}/setup-es-nfs-repo-client.sh", timeout=600)
        c.close()

    rolling_restart_es(auth, targets, "path.repo")
    return 0


def phase_snapshot(auth: str) -> int:
    print("=== Phase snapshot: register repo + first backup ===", flush=True)
    es = connect(NODES["es01"][0])
    # Verify all nodes see repo path
    verify = es_curl(
        es,
        auth,
        "POST",
        f"/_snapshot/{REPO_NAME}/_verify",
        {},
    )
    # create repo first
    body = {
        "type": "fs",
        "settings": {
            "location": MOUNT_POINT,
            "compress": True,
        },
    }
    put = es_curl(es, auth, "PUT", f"/_snapshot/{REPO_NAME}?verify=true", body)
    print(json.dumps(put, indent=2)[:1500], flush=True)
    if put.get("error") or put.get("status", 200) >= 400:
        # maybe already exists
        if "already" not in json.dumps(put).lower() and put.get("acknowledged") is not True:
            print("FAIL create repo", flush=True)
            es.close()
            return 1

    verify = es_curl(es, auth, "POST", f"/_snapshot/{REPO_NAME}/_verify", {})
    print("verify:", json.dumps(verify)[:800], flush=True)

    # System indices (.*) + cluster global state only
    snap_body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "metadata": {
            "taken_by": "setup_nfs_snapshot_roles.py",
            "purpose": "system indices + cluster state baseline",
        },
    }
    snap = es_curl(
        es,
        auth,
        "PUT",
        f"/_snapshot/{REPO_NAME}/{SNAPSHOT_NAME}?wait_for_completion=true",
        snap_body,
        timeout=1800,
    )
    print(json.dumps(snap, indent=2)[:3000], flush=True)
    state = (snap.get("snapshot") or {}).get("state") or snap.get("state")
    if state and state not in ("SUCCESS", "PARTIAL"):
        # nested format
        snaps = snap.get("snapshots") or []
        if snaps:
            state = snaps[0].get("state")
    if state not in ("SUCCESS", "PARTIAL"):
        # ES 8+ returns snapshot object differently
        if snap.get("accepted") is True:
            # wait
            for _ in range(120):
                st = es_curl(es, auth, "GET", f"/_snapshot/{REPO_NAME}/{SNAPSHOT_NAME}")
                s = (st.get("snapshots") or [{}])[0]
                print(f"  snapshot state={s.get('state')}", flush=True)
                if s.get("state") in ("SUCCESS", "PARTIAL", "FAILED"):
                    state = s.get("state")
                    snap = st
                    break
                time.sleep(10)
    if state not in ("SUCCESS", "PARTIAL"):
        print(f"FAIL snapshot state={state}", flush=True)
        es.close()
        return 1
    print(f"OK snapshot {SNAPSHOT_NAME} state={state}", flush=True)
    es.close()
    return 0


def wait_ssh(ip: str, timeout: int = 3600) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = connect(ip, attempts=2)
            out = run(c, "test -f /root/.flash-install-complete && echo OK || echo WAIT", check=False)
            c.close()
            if "OK" in out:
                return True
            print("  waiting flash install...", flush=True)
        except Exception as e:
            print(f"  waiting SSH {ip}: {e}", flush=True)
        time.sleep(30)
    return False


def set_node_roles_yml(c, roles: list[str]) -> None:
    roles_yaml = "[" + ", ".join(roles) + "]"
    run(
        c,
        f"""python3 - <<'PY'
from pathlib import Path
import re
path = Path("/etc/elasticsearch/elasticsearch.yml")
text = path.read_text()
lines = text.splitlines()
out = []
skip = False
for line in lines:
    if re.match(r"^node\\.roles\\s*:", line):
        if "[" in line and "]" in line:
            continue
        skip = True
        continue
    if skip:
        if re.match(r"^\\s*-\\s+", line):
            continue
        skip = False
    out.append(line)
text = "\\n".join(out).rstrip() + "\\n"
text += "node.roles: {roles_yaml}\\n"
path.write_text(text)
print("node.roles: {roles_yaml}")
PY""",
        check=True,
    )


def phase_es04(auth: str) -> int:
    print("=== Phase es04: install + enroll data_hot ===", flush=True)
    if not wait_ssh(ES04_IP, timeout=3600):
        print("FAIL es04 not ready on SSH/flash marker", flush=True)
        return 1

    # Attach Hyper-V data disk if present but not connected
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"""
$vm='{ES04_VM}'
$vhd=Join-Path 'D:\\Virtual Machines' '{ES04_VM}\\Virtual Hard Disks\\{ES04_VM}-Data.vhdx'
if ((Test-Path $vhd) -and (Get-VM $vm -EA SilentlyContinue)) {{
  $has = Get-VMHardDiskDrive -VMName $vm | Where-Object {{ $_.Path -eq $vhd }}
  if (-not $has) {{ Add-VMHardDiskDrive -VMName $vm -Path $vhd; Write-Host "attached $vhd" }}
}}
""",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    c = connect(ES04_IP)
    copy_scripts(c, roles=("elasticsearch",))
    run(c, f"bash {REMOTE}/prepare-data-disk.sh", timeout=300, check=False)
    run(
        c,
        f"bash {REMOTE}/install-elasticsearch.sh --version {VERSION} "
        f"--node {ES04_FQDN} --cluster {CLUSTER}",
        timeout=900,
    )
    set_node_roles_yml(c, HOT_ROLES)

    # Enrollment token from es01
    es = connect(NODES["es01"][0])
    token_out = run(
        es,
        "/usr/share/elasticsearch/bin/elasticsearch-create-enrollment-token -s node",
        timeout=120,
    )
    es.close()
    # last non-empty line is usually the token
    token = ""
    for line in reversed(token_out.strip().splitlines()):
        line = line.strip()
        if line and not line.startswith("$") and "enrollment" not in line.lower():
            token = line
            break
    if not token or len(token) < 20:
        print(f"FAIL enrollment token: {token_out[:500]}", flush=True)
        c.close()
        return 1
    print(f"  token len={len(token)}", flush=True)

    run(
        c,
        f"NODE_ENROLLMENT_TOKEN={shlex.quote(token)} "
        f"bash -c '/usr/share/elasticsearch/bin/elasticsearch-reconfigure-node "
        f"--enrollment-token \"$NODE_ENROLLMENT_TOKEN\" <<< y'",
        timeout=300,
        check=False,
    )
    # re-apply roles + path.repo after reconfigure
    set_node_roles_yml(c, HOT_ROLES)
    env = (
        f"ES_UID={ES_UID} ES_GID={ES_GID} "
        f"NFS_SERVER={shlex.quote(NFS_SERVER)} "
        f"NFS_EXPORT={shlex.quote(NFS_EXPORT)} "
        f"MOUNT_POINT={shlex.quote(MOUNT_POINT)}"
    )
    run(c, f"{env} bash {REMOTE}/setup-es-nfs-repo-client.sh", timeout=600)
    run(c, "systemctl enable --now elasticsearch", timeout=180)
    # wait join
    for _ in range(60):
        out = run(
            c,
            f"curl -sk -u {auth} https://localhost:9200/_cat/nodes?h=name",
            check=False,
        )
        if "ismelkesnode04" in out:
            break
        time.sleep(10)
    c.close()

    es = connect(NODES["es01"][0])
    if not wait_cluster(es, auth, "green", timeout=900):
        print("WARN: not green after es04 join", flush=True)
    nodes = es_curl(es, auth, "GET", "/_cat/nodes?v&h=name,node.role,master")
    print(nodes.get("raw", nodes), flush=True)
    es.close()
    return 0


def phase_roles(auth: str) -> int:
    print("=== Phase roles: masters -> master+data_content ===", flush=True)
    # Ensure es04 present
    es = connect(NODES["es01"][0])
    cat = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?h=name'",
        check=False,
    )
    es.close()
    if "ismelkesnode04" not in cat:
        print("FAIL: es04 not in cluster; run --phase es04 first", flush=True)
        return 1

    # Rolling: es03, es02, es01
    order = [NODES["es03"], NODES["es02"], NODES["es01"]]
    for ip, fqdn in order:
        print(f"--- set roles on {fqdn}: {MASTER_ROLES} ---", flush=True)
        c = connect(ip)
        set_node_roles_yml(c, MASTER_ROLES)
        run(c, "systemctl restart elasticsearch", timeout=180)
        c.close()
        es = connect(NODES["es01"][0] if fqdn != NODES["es01"][1] else NODES["es02"][0])
        # es01 restart: use es02
        if fqdn == NODES["es01"][1]:
            es.close()
            es = connect(NODES["es02"][0])
        if not wait_cluster(es, auth, "green", timeout=1200):
            print(f"WARN not green after {fqdn}", flush=True)
        es.close()

    es = connect(NODES["es02"][0])
    print(
        run(
            es,
            f"curl -sk -u {auth} "
            "'https://localhost:9200/_cat/nodes?v&h=name,ip,node.role,master'",
            check=False,
        ),
        flush=True,
    )
    es.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        choices=["all", "hyperv", "nfs", "snapshot", "es04", "roles"],
        default="all",
    )
    ap.add_argument("--skip-hyperv", action="store_true")
    args = ap.parse_args()

    phases = (
        ["hyperv", "nfs", "snapshot", "es04", "roles"]
        if args.phase == "all"
        else [args.phase]
    )
    if args.skip_hyperv and "hyperv" in phases:
        phases.remove("hyperv")

    auth = None
    if any(p in phases for p in ("nfs", "snapshot", "es04", "roles")):
        es = connect(NODES["es01"][0])
        auth = curl_elastic_auth(get_elastic_password(es))
        es.close()

    for p in phases:
        print(f"\n######## PHASE {p} ########\n", flush=True)
        if p == "hyperv":
            rc = phase_hyperv()
        elif p == "nfs":
            rc = phase_nfs(auth)
        elif p == "snapshot":
            rc = phase_snapshot(auth)
        elif p == "es04":
            rc = phase_es04(auth)
        elif p == "roles":
            rc = phase_roles(auth)
        else:
            rc = 1
        if rc != 0:
            print(f"PHASE {p} failed rc={rc}", flush=True)
            return rc
    print("\n=== ALL REQUESTED PHASES COMPLETE ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
