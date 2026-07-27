#!/usr/bin/env python3
"""Fix discovery.seed_hosts after HV restore and restart ES until cluster forms."""
from __future__ import annotations


def _lab_elastic_password() -> str:
    from elastic_credentials import load_config_password, load_local_password
    pwd = load_local_password() or load_config_password()
    if not pwd:
        raise SystemExit(
            "Set secrets/elastic-password or ElasticPassword in config.psd1"
        )
    return pwd


import time

from deploy_ordered_stack import NODES, connect, run

PWD = _lab_elastic_password()
KEYS = ("es01", "es02", "es03", "es04")


def patch_seed_hosts(c) -> None:
    # remote python rewrite
    script = r'''
from pathlib import Path


p = Path("/etc/elasticsearch/elasticsearch.yml")
lines = p.read_text().splitlines()
seed = [
    "discovery.seed_hosts:",
    "  - ismelkesnode01.ocplab.net",
    "  - ismelkesnode02.ocplab.net",
    "  - ismelkesnode03.ocplab.net",
    "  - ismelkesnode04.ocplab.net",
]
out = []
i = 0
replaced = False
while i < len(lines):
    line = lines[i]
    if line.strip().startswith("discovery.seed_hosts"):
        out.extend(seed)
        replaced = True
        i += 1
        # skip existing list items / blank under the key
        while i < len(lines):
            nxt = lines[i]
            if nxt.strip() == "":
                i += 1
                break
            if nxt.startswith(" ") or nxt.startswith("\t") or nxt.lstrip().startswith("-"):
                i += 1
                continue
            break
        continue
    out.append(line)
    i += 1
if not replaced:
    out.append("")
    out.extend(seed)
p.write_text("\n".join(out) + "\n")
print("seed_hosts_ok")
'''
    run(c, f"python3 - <<'PY'\n{script}\nPY", check=False, timeout=60)
    print(run(c, "grep -A8 '^discovery.seed_hosts' /etc/elasticsearch/elasticsearch.yml", check=False))


def main() -> int:
    # stop + patch all
    for key in KEYS:
        ip = NODES[key][0]
        print(f"\n=== stop/patch {key} ({ip}) ===", flush=True)
        c = connect(ip, attempts=20)
        run(
            c,
            "systemctl stop elasticsearch 2>/dev/null || true; "
            "sleep 2; pkill -9 -f org.elasticsearch.bootstrap 2>/dev/null || true; "
            "pkill -9 -f '/usr/share/elasticsearch' 2>/dev/null || true; "
            "sleep 1; systemctl is-active elasticsearch || true",
            check=False,
            timeout=90,
        )
        # best-effort NFS
        run(
            c,
            "if ! mountpoint -q /mnt/es-snapshots; then "
            "  mkdir -p /mnt/es-snapshots; "
            "  mount -t nfs 10.44.40.41:/export/es-snapshots /mnt/es-snapshots 2>/dev/null || "
            "  mount -t nfs 10.44.40.41:/mnt/es-snapshots /mnt/es-snapshots 2>/dev/null || true; "
            "fi; "
            "timeout 3 ls /mnt/es-snapshots >/dev/null 2>&1 && echo NFS_OK || echo NFS_WARN; "
            "grep -E 'path.repo|seed_hosts' /etc/elasticsearch/elasticsearch.yml | head -20",
            check=False,
            timeout=60,
        )
        patch_seed_hosts(c)
        c.close()

    # start es01-03 first (masters), then es04
    for key in KEYS:
        ip = NODES[key][0]
        print(f"\n=== start {key} ===", flush=True)
        c = connect(ip, attempts=10)
        run(c, "systemctl start elasticsearch", check=False, timeout=120)
        c.close()
        time.sleep(12)

    print("\n=== wait for cluster health ===", flush=True)
    for i in range(90):
        try:
            c = connect(NODES["es02"][0], attempts=5)
            out = run(
                c,
                f"curl -sk -m 12 -u elastic:{PWD} "
                f"'https://localhost:9200/_cluster/health?pretty'; echo; "
                f"curl -sk -m 12 -u elastic:{PWD} "
                f"'https://localhost:9200/_cat/nodes?v&h=name,version,master,node.role'",
                check=False,
                timeout=50,
            )
            print(f"poll {i}:\n{out[-1500:]}", flush=True)
            c.close()
            yellow_or_green = ('"status" : "yellow"' in out) or ('"status" : "green"' in out)
            enough = out.count("ismelkesnode") >= 4
            if yellow_or_green and enough and "8.18.4" in out:
                print("CLUSTER OK", flush=True)
                return 0
        except Exception as e:
            print("wait err", e, flush=True)
        time.sleep(10)
    print("CLUSTER NOT READY", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
