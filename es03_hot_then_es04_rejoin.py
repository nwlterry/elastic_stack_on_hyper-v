#!/usr/bin/env python3
"""Add data_hot to es03, wait for cluster stable, restart es04 and test rejoin."""
from __future__ import annotations


def _lab_elastic_password() -> str:
    from elastic_credentials import load_config_password, load_local_password
    pwd = load_local_password() or load_config_password()
    if not pwd:
        raise SystemExit(
            "Set secrets/elastic-password or ElasticPassword in config.psd1"
        )
    return pwd


import json
import shlex
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import NODES, connect, run

PWD = _lab_elastic_password()
LOG = Path(__file__).resolve().parent / "logs" / "es03_hot_es04_rejoin.log"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(c, path: str, method: str = "GET", body=None, timeout: int = 120) -> str:
    auth = shlex.quote(f"elastic:{PWD}")
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def parse_json(out: str):
    for ch in ("{", "["):
        s = out.find(ch)
        if s >= 0:
            try:
                return json.loads(out[s:])
            except json.JSONDecodeError:
                pass
    return None


def health_via(key: str = "es01") -> dict:
    c = connect(NODES[key][0], attempts=15)
    try:
        h = parse_json(api(c, "/_cluster/health")) or {}
        return h
    finally:
        c.close()


def nodes_via(key: str = "es01") -> str:
    c = connect(NODES[key][0], attempts=15)
    try:
        return api(c, "/_cat/nodes?v&h=name,version,master,node.role,ip")
    finally:
        c.close()


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    log("=== Pre state ===")
    log(nodes_via())
    log(json.dumps(health_via(), indent=2)[:800])

    # 1) Add data_hot on es03
    log("=== Add data_hot role on es03 + restart ===")
    c3 = connect(NODES["es03"][0], attempts=20)
    print(
        run(
            c3,
            r"""
set -e
YML=/etc/elasticsearch/elasticsearch.yml
cp -a "$YML" "${YML}.bak.before-hot-$(date +%Y%m%d%H%M%S)"
python3 - <<'PY'
from pathlib import Path


p = Path("/etc/elasticsearch/elasticsearch.yml")
text = p.read_text()
lines = text.splitlines()
out = []
replaced = False
for line in lines:
    if line.strip().startswith("node.roles"):
        # force desired roles including data_hot
        out.append("node.roles: [master, data_content, data_hot, remote_cluster_client]")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append("node.roles: [master, data_content, data_hot, remote_cluster_client]")
p.write_text("\n".join(out) + "\n")
print("roles line:", [l for l in p.read_text().splitlines() if "node.roles" in l])
PY
chown root:elasticsearch "$YML"
chmod 660 "$YML"
# non-blocking restart
systemctl restart elasticsearch </dev/null >/dev/null 2>&1 &
echo restart_issued
""",
            check=False,
            timeout=60,
        ),
        flush=True,
    )
    c3.close()

    # wait es03 back
    log("=== Wait es03 API back ===")
    for i in range(60):
        try:
            c3 = connect(NODES["es03"][0], attempts=3)
            code = run(
                c3,
                "systemctl is-active elasticsearch; "
                "curl -sk -m 5 -o /dev/null -w '%{http_code}' https://localhost:9200/",
                check=False,
                timeout=30,
            )
            c3.close()
            log(f"  es03 poll {i}: {code.strip().splitlines()[-2:]}")
            if "active" in code and ("401" in code or "200" in code):
                break
        except Exception as e:
            log(f"  es03 wait: {e}")
        time.sleep(5)

    # 2) Wait cluster stable
    log("=== Wait cluster stable ===")
    # Stable for this topology: no relocating/init; prefer fewer unassigned primaries
    # after hot role added. Full green may still be blocked if some shards need es04.
    stable = None
    for i in range(80):
        try:
            h = health_via("es01")
            n = nodes_via("es01")
            log(
                f"  poll {i}: status={h.get('status')} nodes={h.get('number_of_nodes')} "
                f"prim={h.get('active_primary_shards')} uprim={h.get('unassigned_primary_shards')} "
                f"reloc={h.get('relocating_shards')} init={h.get('initializing_shards')}"
            )
            # show es03 roles once visible
            if "ismelkesnode03" in n and i % 3 == 0:
                for line in n.splitlines():
                    if "ismelkesnode03" in line or "name" in line:
                        log(f"    {line}")
            if (
                h.get("status") in ("green", "yellow", "red")
                and h.get("relocating_shards", 1) == 0
                and h.get("initializing_shards", 1) == 0
                and h.get("number_of_nodes", 0) >= 3
                and h.get("unassigned_primary_shards", 999) < 62  # improved vs prior 62
            ):
                # require at least some improvement and settled movement
                if h.get("status") in ("green", "yellow") or h.get("unassigned_primary_shards", 999) <= 5:
                    stable = h
                    if h.get("status") in ("green", "yellow") and h.get("unassigned_primary_shards") == 0:
                        break
                    # if yellow with 0 uprim, good enough
                    if h.get("unassigned_primary_shards") == 0 and h.get("relocating_shards") == 0:
                        break
            # also accept yellow/green with 0 init/reloc even if some unassigned replicas
            if (
                h.get("status") in ("green", "yellow")
                and h.get("relocating_shards", 1) == 0
                and h.get("initializing_shards", 1) == 0
                and h.get("unassigned_primary_shards", 1) == 0
            ):
                stable = h
                break
        except Exception as e:
            log(f"  wait err: {e}")
        time.sleep(15)
    else:
        # take last health even if not ideal
        stable = health_via("es01")
        log(f"WARN not fully stable; continuing with last health={stable}")

    log("=== Cluster after es03 data_hot ===")
    log(nodes_via())
    log(json.dumps(stable or health_via(), indent=2)[:1000])

    # 3) Restart es04 and test rejoin
    log("=== Restart es04 and test rejoin ===")
    c4 = connect(NODES["es04"][0], attempts=20)
    print(
        run(
            c4,
            "rpm -q elasticsearch; systemctl restart elasticsearch; sleep 5; "
            "systemctl is-active elasticsearch || true",
            check=False,
            timeout=180,
        ),
        flush=True,
    )

    joined = False
    last_nodes = ""
    for i in range(24):
        last_nodes = nodes_via("es01")
        h = health_via("es01")
        log(f"  rejoin poll {i}: nodes={h.get('number_of_nodes')} status={h.get('status')}")
        log(last_nodes[-500:])
        if "ismelkesnode04" in last_nodes:
            joined = True
            break
        err = run(
            c4,
            "journalctl -u elasticsearch -n 40 --no-pager 2>/dev/null | "
            "python3 -c \"import sys,re; p=re.compile(r'version|incompatible|join|reject|validate|uuid',re.I);\\n"
            "[print(l[-220:]) for l in sys.stdin if p.search(l)][-15:]\" 2>/dev/null || true",
            check=False,
            timeout=45,
        )
        # simpler grep via python on node
        err = run(
            c4,
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "import re\n"
            "pat=re.compile(r'version|incompatible|join|reject|validate|uuid|failed', re.I)\n"
            "logs=sorted(Path('/var/log/elasticsearch').glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)\n"
            "for p in logs[:1]:\n"
            "  for ln in p.read_text(errors='ignore').splitlines()[-200:]:\n"
            "    if pat.search(ln) and ('join' in ln.lower() or 'version' in ln.lower() or 'incompatible' in ln.lower() or 'uuid' in ln.lower()):\n"
            "      print(ln[-240:])\n"
            "PY",
            check=False,
            timeout=45,
        )
        if err.strip():
            log(f"  es04 hints:\n{err[-900:]}")
        time.sleep(12)
    c4.close()

    log("=== FINAL ===")
    log(nodes_via())
    log(json.dumps(health_via(), indent=2)[:1000])
    log(f"es04_joined={joined}")
    print(f"\nRESULT es04_joined={joined}\n", flush=True)
    return 0 if joined else 2


if __name__ == "__main__":
    raise SystemExit(main())
