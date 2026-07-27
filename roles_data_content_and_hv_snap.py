#!/usr/bin/env python3
"""
1) es01–es03: drop all data_* roles except data_content
   keep master + data_content + remote_cluster_client
2) Delete ALL Hyper-V checkpoints on ELK VMs, then take a fresh checkpoint
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import (
    NODES,
    connect,
    curl_elastic_auth,
    get_elastic_password,
    run,
)

ROOT = Path(__file__).resolve().parent
MASTER_ROLES = ["master", "data_content", "remote_cluster_client"]
ORDER = ("es03", "es02", "es01")  # non-masters first if any; all are masters
SNAP_NAME = f"post-roles-data-content-{datetime.now().strftime('%Y%m%d-%H%M')}"


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
# show path.repo still present
for line in path.read_text().splitlines():
    if "path.repo" in line or line.startswith("node.roles"):
        print(line)
PY""",
        check=False,
    )


def force_restart_es(c) -> None:
    """Restart with force-kill if stuck deactivating (seen on these nodes)."""
    run(
        c,
        r"""
set -e
systemctl stop elasticsearch 2>/dev/null || true
# if still deactivating after brief wait, SIGKILL
for i in 1 2 3 4 5 6 7 8 9 10; do
  st=$(systemctl is-active elasticsearch 2>/dev/null || true)
  if [ "$st" = "inactive" ] || [ "$st" = "failed" ] || [ "$st" = "dead" ]; then
    break
  fi
  if [ "$st" = "deactivating" ] || [ "$st" = "activating" ]; then
    echo force_kill_state=$st
    pkill -9 -u elasticsearch 2>/dev/null || true
    pid=$(systemctl show elasticsearch -p MainPID --value 2>/dev/null || true)
    [ -n "$pid" ] && [ "$pid" != "0" ] && kill -9 "$pid" 2>/dev/null || true
    sleep 2
    systemctl reset-failed elasticsearch 2>/dev/null || true
    break
  fi
  sleep 3
done
systemctl reset-failed elasticsearch 2>/dev/null || true
systemctl start elasticsearch
""",
        check=False,
        timeout=180,
    )


def wait_api(ip: str, auth: str, timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = connect(ip, attempts=2)
            code = run(
                c,
                f"curl -sk -u {auth} -o /dev/null -w '%{{http_code}}' https://localhost:9200",
                check=False,
            )
            c.close()
            if "200" in code or "401" in code:
                return True
        except Exception as e:
            print(f"  wait_api {ip}: {e}", flush=True)
        time.sleep(8)
    return False


def wait_green(auth: str, probe_ip: str, timeout: int = 900) -> bool:
    """Wait for green, or yellow with no unassigned primaries (replica-only yellow is OK mid-role-change)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = connect(probe_ip, attempts=2)
        out = run(
            c,
            f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'",
            check=False,
        )
        c.close()
        print(out[:400], flush=True)
        if '"status" : "green"' in out or '"status":"green"' in out:
            return True
        # accept yellow if no primary unassigned and cluster has expected nodes
        if ('"status" : "yellow"' in out or '"status":"yellow"' in out) and (
            '"unassigned_primary_shards" : 0' in out or '"unassigned_primary_shards":0' in out
        ):
            # require at least 3 nodes
            if '"number_of_nodes" : 4' in out or '"number_of_nodes":4' in out or (
                '"number_of_nodes" : 3' in out
            ):
                print("  accepting yellow (no unassigned primaries)", flush=True)
                return True
        time.sleep(12)
    return False


def reduce_hot_replicas_if_needed(auth: str, elastic_pwd: str) -> None:
    """With a single data_hot node, set replicas=0 for pure hot-tier indices."""
    print("=== Adjust replicas for hot-tier indices if needed ===", flush=True)
    c = connect(NODES["es01"][0])
    print(
        run(
            c,
            f"curl -sk -u {auth} "
            f"'https://localhost:9200/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason' "
            f"| awk 'NR==1 || /UNASSIGNED/' | head -60",
            check=False,
        ),
        flush=True,
    )
    # auth for remote python: user:pass without shell quotes
    userpass = f"elastic:{elastic_pwd}"
    print(
        run(
            c,
            f"""
python3 - <<'PY'
import json, subprocess
userpass = {json.dumps(userpass)}
def sh(args):
    return subprocess.run(args, capture_output=True, text=True).stdout
raw = sh(['curl','-sk','-u',userpass,'https://localhost:9200/_all/_settings?flat_settings=true'])
try:
    settings = json.loads(raw)
except Exception as e:
    print('parse fail', e, raw[:200])
    settings = {{}}
n = 0
for idx, meta in settings.items():
    if idx.startswith('.'):
        continue
    s = meta.get('settings') or {{}}
    tier = s.get('index.routing.allocation.include._tier_preference') or ''
    try:
        reps_i = int(s.get('index.number_of_replicas', '1'))
    except Exception:
        reps_i = 1
    prefs = [p.strip() for p in str(tier).split(',') if p.strip()]
    # primary preference data_hot only (not content masters)
    if reps_i > 0 and prefs and prefs[0] == 'data_hot':
        body = json.dumps({{'index': {{'number_of_replicas': 0}}}})
        r = sh(['curl','-sk','-u',userpass,'-X','PUT','-H','Content-Type: application/json','-d',body,
                'https://localhost:9200/' + idx + '/_settings'])
        print('replicas0', idx, r[:160])
        n += 1
print('changed', n)
PY
""",
            check=False,
            timeout=300,
        ),
        flush=True,
    )
    c.close()


def phase_roles(auth: str, elastic_pwd: str) -> None:
    print("=== Set es01-03 roles: master + data_content + remote_cluster_client ===", flush=True)
    for key in ORDER:
        ip, fqdn = NODES[key]
        print(f"--- {fqdn} ---", flush=True)
        c = connect(ip)
        set_node_roles_yml(c, MASTER_ROLES)
        print("restart elasticsearch...", flush=True)
        force_restart_es(c)
        c.close()
        if not wait_api(ip, auth, timeout=400):
            print(f"WARN API not up on {fqdn}", flush=True)
        probe = NODES["es02"][0] if key == "es01" else NODES["es01"][0]
        # yellow ok mid-roll while replicas rebalance; wait green when possible
        if not wait_green(auth, probe, timeout=600):
            print(f"WARN not green after {fqdn}; trying hot replica fix", flush=True)
            reduce_hot_replicas_if_needed(auth, elastic_pwd)
            wait_green(auth, probe, timeout=600)

    reduce_hot_replicas_if_needed(auth, elastic_pwd)
    wait_green(auth, NODES["es01"][0], timeout=600)
    c = connect(NODES["es01"][0])
    print(
        run(
            c,
            f"curl -sk -u {auth} "
            f"'https://localhost:9200/_cat/nodes?v&h=name,ip,node.role,master,version'; "
            f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'",
            check=False,
        ),
        flush=True,
    )
    c.close()


def phase_hyperv_snap() -> int:
    print("=== Hyper-V: delete all snapshots + create new ===", flush=True)
    ps1 = ROOT / "Remove-AllHyperVSnaps-And-Snapshot.ps1"
    log = ROOT / "logs" / "hv-snap-reset.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # clear old log
    log.write_text("", encoding="utf-8")

    task = "ISMELK-HV-Snap-Reset"
    arg = (
        f"-NoProfile -ExecutionPolicy Bypass -File {shlex.quote(str(ps1))} "
        f"-SnapshotName {shlex.quote(SNAP_NAME)} "
        f"-LogPath {shlex.quote(str(log))}"
    )
    cmd = f"""
$ErrorActionPreference = 'Continue'
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {json.dumps(arg)}
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName '{task}' -Action $action -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
$deadline = (Get-Date).AddMinutes(90)
do {{
  Start-Sleep -Seconds 5
  $i = Get-ScheduledTaskInfo -TaskName '{task}'
  $state = (Get-ScheduledTask -TaskName '{task}').State
  Write-Host ("task_state=" + $state + " last=" + $i.LastTaskResult)
}} while ($state -eq 'Running' -and (Get-Date) -lt $deadline)
$info = Get-ScheduledTaskInfo -TaskName '{task}'
Write-Host ("LastTaskResult=" + $info.LastTaskResult)
Get-Content {json.dumps(str(log))} -ErrorAction SilentlyContinue | Select-Object -Last 60
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue
if ($info.LastTaskResult -ne 0) {{ exit $info.LastTaskResult }}
"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=6000,
    )
    print(r.stdout[-5000:] if r.stdout else "", flush=True)
    if r.stderr:
        print(r.stderr[-2000:], flush=True)
    print(f"hv exit={r.returncode}", flush=True)
    if log.is_file():
        print("--- log tail ---", flush=True)
        print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]), flush=True)
    return r.returncode


def phase_roles_remaining(auth: str, elastic_pwd: str, keys: tuple[str, ...]) -> None:
    """Continue role changes for remaining nodes (skip already done)."""
    print(f"=== Continue roles for {keys} ===", flush=True)
    for key in keys:
        ip, fqdn = NODES[key]
        print(f"--- {fqdn} ---", flush=True)
        c = connect(ip)
        set_node_roles_yml(c, MASTER_ROLES)
        print("restart elasticsearch...", flush=True)
        force_restart_es(c)
        c.close()
        if not wait_api(ip, auth, timeout=400):
            print(f"WARN API not up on {fqdn}", flush=True)
        probe = NODES["es02"][0] if key == "es01" else NODES["es01"][0]
        wait_green(auth, probe, timeout=300)

    reduce_hot_replicas_if_needed(auth, elastic_pwd)
    wait_green(auth, NODES["es01"][0], timeout=300)
    c = connect(NODES["es01"][0])
    print(
        run(
            c,
            f"curl -sk -u {auth} "
            f"'https://localhost:9200/_cat/nodes?v&h=name,ip,node.role,master,version'; "
            f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'",
            check=False,
        ),
        flush=True,
    )
    c.close()


def main() -> int:
    import sys

    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    es.close()
    print("auth ok", flush=True)

    # resume mode: only unfinished masters + hyperv
    if "--resume" in sys.argv:
        # es03 already mrs; finish es02, es01
        phase_roles_remaining(auth, elastic_pwd, ("es02", "es01"))
    else:
        phase_roles(auth, elastic_pwd)
    rc = phase_hyperv_snap()
    print(f"\nSnapshotName={SNAP_NAME}", flush=True)
    if rc != 0:
        print(
            "Hyper-V phase may need elevation. Re-run elevated:\n"
            f"  powershell -ExecutionPolicy Bypass -File {ROOT / 'Remove-AllHyperVSnaps-And-Snapshot.ps1'} "
            f"-SnapshotName {SNAP_NAME}",
            flush=True,
        )
        return rc
    print("ALL DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
