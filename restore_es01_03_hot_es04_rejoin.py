#!/usr/bin/env python3
"""
1) Elevate HV restore es01-es03 to pre-upgrade-system-8.18.4-with-apm
2) Before ES fully serves traffic: set es03 node.roles to include data_hot, restart ES on es03
3) Wait cluster stable (es01-es03)
4) Restart es04 and monitor rejoin
"""
from __future__ import annotations


def _lab_elastic_password() -> str:
    from elastic_credentials import load_config_password, load_local_password
    pwd = load_local_password() or load_config_password()
    if not pwd:
        raise SystemExit(
            "Set secrets/elastic-password or ElasticPassword in config.psd1"
        )
    return pwd


import ctypes
import json
import shlex
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import NODES, connect, run

ROOT = Path(__file__).resolve().parent
PS1 = ROOT / "Restore-Es01to03-To-Snap.ps1"
HV_LOG = ROOT / "logs" / "hv-restore-es01-03-with-apm.log"
RUN_LOG = ROOT / "logs" / "restore_es01_03_hot_es04_rejoin.log"
SNAP = "pre-upgrade-system-8.18.4-with-apm"
PWD_CANDIDATES = (_lab_elastic_password(),)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def elevate_restore() -> bool:
    HV_LOG.parent.mkdir(parents=True, exist_ok=True)
    HV_LOG.write_text("", encoding="utf-8")
    arg = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{PS1}" '
        f'-SnapshotName "{SNAP}" -LogPath "{HV_LOG}"'
    )
    log(f"=== Hyper-V restore es01-es03 <- {SNAP} ===")
    log(f"arg={arg}")
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", arg, str(ROOT), 1
    )
    log(f"ShellExecuteW={rc}")
    if rc <= 32:
        return False
    for i in range(180):
        if HV_LOG.is_file():
            text = HV_LOG.read_text(encoding="utf-8", errors="replace")
            if "DONE" in text:
                log(text[-2000:])
                return "DONE_WITH_ERRORS" not in text
            if i % 6 == 0 and text.strip():
                log(f"--- hv @ {i*5}s ---\n{text[-600:]}")
        time.sleep(5)
    log("TIMEOUT HV restore")
    return False


def wait_ssh(keys, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = True
        for key in keys:
            try:
                c = connect(NODES[key][0], attempts=2)
                run(c, "hostname", check=False, timeout=20)
                c.close()
            except Exception as e:
                ok = False
                log(f"  wait ssh {key}: {e}")
        if ok:
            return
        time.sleep(10)
    raise TimeoutError("SSH not ready")


def patch_es03_data_hot_before_restart() -> None:
    """Stop ES on es03, add data_hot, start (and ensure es01/es02 running first)."""
    log("=== es03: stop ES, add data_hot, start ES ===")
    # ensure es01/es02 are starting/started
    for key in ("es01", "es02"):
        c = connect(NODES[key][0], attempts=20)
        run(
            c,
            "systemctl start elasticsearch 2>/dev/null || true; "
            "systemctl is-active elasticsearch || true",
            check=False,
            timeout=60,
        )
        c.close()

    c3 = connect(NODES["es03"][0], attempts=30)
    print(
        run(
            c3,
            r"""
set -e
# stop before role change if already running
systemctl stop elasticsearch 2>/dev/null || true
pkill -9 -f org.elasticsearch 2>/dev/null || true
sleep 2
YML=/etc/elasticsearch/elasticsearch.yml
cp -a "$YML" "${YML}.bak.before-hot-$(date +%Y%m%d%H%M%S)"
python3 - <<'PY'
from pathlib import Path


p = Path("/etc/elasticsearch/elasticsearch.yml")
lines = p.read_text().splitlines()
out = []
replaced = False
for line in lines:
    if line.strip().startswith("node.roles"):
        out.append("node.roles: [master, data_content, data_hot, remote_cluster_client]")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append("node.roles: [master, data_content, data_hot, remote_cluster_client]")
p.write_text("\n".join(out) + "\n")
print([l for l in p.read_text().splitlines() if "node.roles" in l])
PY
chown root:elasticsearch "$YML" 2>/dev/null || true
chmod 660 "$YML" 2>/dev/null || true
# start after patch
systemctl start elasticsearch
sleep 5
systemctl is-active elasticsearch || (journalctl -u elasticsearch -n 30 --no-pager; exit 1)
grep node.roles "$YML"
echo DONE_ES03_HOT
""",
            check=False,
            timeout=180,
        ),
        flush=True,
    )
    c3.close()


def resolve_auth(key: str = "es01") -> str:
    c = connect(NODES[key][0], attempts=15)
    for pwd in PWD_CANDIDATES:
        out = run(
            c,
            f"curl -sk -o /dev/null -w '%{{http_code}}' -u elastic:{pwd} "
            f"https://localhost:9200/_cluster/health",
            check=False,
            timeout=30,
        )
        if "200" in out:
            c.close()
            return pwd
    c.close()
    return PWD_CANDIDATES[0]


def api(c, pwd: str, path: str, method: str = "GET", timeout: int = 120) -> str:
    auth = shlex.quote(f"elastic:{pwd}")
    return run(
        c,
        f"curl -sk -u {auth} -X {method} 'https://localhost:9200{path}'",
        check=False,
        timeout=timeout,
    )


def parse_json(out: str):
    for ch in ("{", "["):
        s = out.find(ch)
        if s >= 0:
            try:
                return json.loads(out[s:])
            except json.JSONDecodeError:
                pass
    return None


def wait_cluster_stable(pwd: str, timeout: int = 900) -> dict:
    log("=== Wait es01-es03 cluster stable ===")
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            c = connect(NODES["es01"][0], attempts=8)
            nodes = api(c, pwd, "/_cat/nodes?v&h=name,version,master,node.role,ip")
            health = parse_json(api(c, pwd, "/_cluster/health")) or {}
            c.close()
            last = health
            log(
                f"  status={health.get('status')} nodes={health.get('number_of_nodes')} "
                f"prim={health.get('active_primary_shards')} "
                f"uprim={health.get('unassigned_primary_shards')} "
                f"reloc={health.get('relocating_shards')} init={health.get('initializing_shards')}"
            )
            for line in nodes.splitlines():
                if "ismelkes" in line or line.startswith("name"):
                    log(f"    {line}")
            ncount = health.get("number_of_nodes") or 0
            if (
                ncount >= 3
                and health.get("status") in ("green", "yellow")
                and health.get("relocating_shards", 1) == 0
                and health.get("initializing_shards", 1) == 0
                and health.get("unassigned_primary_shards", 1) == 0
            ):
                log("  cluster stable (yellow/green, 0 unassigned primaries)")
                return health
            # also accept yellow with only replica unassigned already covered by uprim==0
        except Exception as e:
            log(f"  wait: {e}")
        time.sleep(15)
    log(f"WARN timeout waiting full stable; last={last}")
    return last


def restart_es04_and_monitor(pwd: str, timeout: int = 360) -> bool:
    log("=== Restart es04 and monitor rejoin ===")
    c4 = connect(NODES["es04"][0], attempts=20)
    print(
        run(
            c4,
            "rpm -q elasticsearch; systemctl restart elasticsearch; sleep 10; "
            "systemctl is-active elasticsearch || true",
            check=False,
            timeout=180,
        ),
        flush=True,
    )
    joined = False
    deadline = time.time() + timeout
    i = 0
    while time.time() < deadline:
        i += 1
        try:
            c = connect(NODES["es01"][0], attempts=8)
            nodes = api(c, pwd, "/_cat/nodes?v&h=name,version,master,node.role,ip")
            health = parse_json(api(c, pwd, "/_cluster/health")) or {}
            c.close()
            log(
                f"  rejoin poll {i}: nodes={health.get('number_of_nodes')} "
                f"status={health.get('status')}"
            )
            log(nodes[-600:])
            if "ismelkesnode04" in nodes:
                joined = True
                log("ES04 JOINED")
                break
        except Exception as e:
            log(f"  poll err: {e}")
        err = run(
            c4,
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "import re\n"
            "pat=re.compile(r'version|incompatible|join|validate|uuid|reject', re.I)\n"
            "logs=sorted(Path('/var/log/elasticsearch').glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)\n"
            "for p in logs[:1]:\n"
            "  for ln in p.read_text(errors='ignore').splitlines()[-200:]:\n"
            "    low=ln.lower()\n"
            "    if pat.search(ln) and any(k in low for k in ('join','version','incompatible','uuid','validate')):\n"
            "      print(ln[-260:])\n"
            "PY",
            check=False,
            timeout=45,
        )
        if err.strip():
            log(f"  es04 hints:\n{err[-800:]}")
        time.sleep(12)
    c4.close()
    return joined


def main() -> int:
    RUN_LOG.write_text("", encoding="utf-8")
    log("START restore es01-es03 with-apm + es03 data_hot + es04 rejoin test")

    if not elevate_restore():
        log("ERROR HV restore failed")
        return 1

    log("Wait SSH after restore...")
    time.sleep(40)
    wait_ssh(("es01", "es02", "es03"), timeout=600)

    # Immediately patch es03 before relying on cluster (stop ES, add hot, start)
    patch_es03_data_hot_before_restart()

    # wait es01/es02 fully up
    for key in ("es01", "es02", "es03"):
        for i in range(40):
            try:
                c = connect(NODES[key][0], attempts=3)
                code = run(
                    c,
                    "systemctl is-active elasticsearch; "
                    "curl -sk -m 5 -o /dev/null -w '%{http_code}' https://localhost:9200/",
                    check=False,
                    timeout=30,
                )
                c.close()
                log(f"  {key} api: {code.strip().splitlines()[-2:]}")
                if "active" in code and ("401" in code or "200" in code):
                    break
            except Exception as e:
                log(f"  {key}: {e}")
            time.sleep(8)

    pwd = resolve_auth("es01")
    log(f"auth password ok (prefix={pwd[:6]}...)")
    health = wait_cluster_stable(pwd, timeout=900)

    joined = restart_es04_and_monitor(pwd, timeout=300)

    # final
    c = connect(NODES["es01"][0], attempts=15)
    log("=== FINAL ===")
    log(api(c, pwd, "/_cat/nodes?v&h=name,version,master,node.role,ip"))
    log(api(c, pwd, "/_cluster/health?pretty")[:900])
    c.close()
    log(f"RESULT es04_joined={joined} cluster_status={health.get('status')}")
    return 0 if joined else 2


if __name__ == "__main__":
    raise SystemExit(main())
