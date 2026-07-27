#!/usr/bin/env python3
"""
Rolling-upgrade all Elasticsearch nodes 8.18.4 -> 8.19.18.

- Times each node and total wall-clock
- Upgrades non-master nodes first; current master last
- Monitors until cluster is stable (all nodes on target, no relocating/
  initializing, zero unassigned primaries)
"""
from __future__ import annotations

import json
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

from deploy_ordered_stack import (
    NODES,
    REMOTE,
    connect,
    curl_elastic_auth,
    get_elastic_password,
    run,
)
from upgrade_elastic_stack import stage_packages

TARGET = "8.19.18"
EXPECTED_NODES = 4
LOG = Path(__file__).resolve().parent / "logs" / "upgrade_es_8_19_18.log"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s ({seconds:.1f}s)"
    if m:
        return f"{m}m {sec}s ({seconds:.1f}s)"
    return f"{sec}s ({seconds:.1f}s)"


def es_curl(host_ip: str, auth: str, path: str, timeout: int = 120) -> str:
    c = connect(host_ip, attempts=30)
    try:
        return run(
            c,
            f"curl -sk -u {auth} 'https://localhost:9200{path}'",
            check=False,
            timeout=timeout,
        )
    finally:
        c.close()


def parse_json(out: str):
    # last JSON object/array in output
    text = out.strip()
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.rfind(start_char)
        if start < 0:
            continue
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    # try first {
    start = text.find("{")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    start = text.find("[")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    return None


def get_password() -> str:
    for key in ("es02", "es01", "es03", "es04"):
        try:
            c = connect(NODES[key][0], attempts=20)
            pwd = get_elastic_password(c)
            c.close()
            return pwd
        except Exception as e:
            log(f"  password via {key}: {e}")
    raise RuntimeError("could not resolve elastic password")


def pick_monitor(exclude_key: str | None = None) -> str:
    """Return IP of a live ES node, optionally excluding one being upgraded."""
    order = ["es02", "es01", "es03", "es04"]
    if exclude_key:
        order = [k for k in order if k != exclude_key] + [exclude_key]
    for key in order:
        if key == exclude_key:
            continue
        ip = NODES[key][0]
        try:
            c = connect(ip, attempts=5)
            out = run(
                c,
                "curl -sk -o /dev/null -w '%{http_code}' https://localhost:9200/",
                check=False,
                timeout=30,
            )
            c.close()
            if "200" in out or "401" in out:
                return ip
        except Exception:
            continue
    # last resort: any remaining
    for key, (ip, _) in NODES.items():
        if key.startswith("es") and key != exclude_key:
            return ip
    raise RuntimeError("no monitor ES node available")


def cluster_health(auth: str, exclude_key: str | None = None) -> dict:
    ip = pick_monitor(exclude_key)
    out = es_curl(ip, auth, "/_cluster/health?pretty")
    data = parse_json(out)
    return data if isinstance(data, dict) else {}


def cat_nodes(auth: str, exclude_key: str | None = None) -> list[dict]:
    ip = pick_monitor(exclude_key)
    out = es_curl(ip, auth, "/_cat/nodes?h=name,node.role,master,version,ip&format=json")
    data = parse_json(out)
    return data if isinstance(data, list) else []


def find_master_key(auth: str) -> str | None:
    rows = cat_nodes(auth)
    master_name = None
    for r in rows:
        if r.get("master") == "*":
            master_name = r.get("name", "")
            break
    if not master_name:
        return None
    for key, (_, fqdn) in NODES.items():
        if not key.startswith("es"):
            continue
        if fqdn in master_name or master_name in fqdn or master_name.split(".")[0] in fqdn:
            return key
    # hostname without domain
    short = master_name.split(".")[0].lower()
    for key, (_, fqdn) in NODES.items():
        if key.startswith("es") and short in fqdn.lower():
            return key
    return None


def upgrade_order(auth: str) -> list[tuple[str, str]]:
    """Return [(key, label), ...] — non-master first, master last."""
    master = find_master_key(auth)
    log(f"Current master key: {master}")
    keys = ["es04", "es03", "es01", "es02"]
    # ensure all ES keys present
    for k in NODES:
        if k.startswith("es") and k not in keys:
            keys.insert(0, k)
    if master and master in keys:
        keys = [k for k in keys if k != master] + [master]
    labels = {
        "es01": "ES es01 (data_content/master-eligible)",
        "es02": "ES es02 (data_content/master-eligible)",
        "es03": "ES es03 (data_content/master-eligible)",
        "es04": "ES es04 (data_hot/ingest/transform)",
    }
    return [(k, labels.get(k, k)) for k in keys if k in NODES]


def wait_stable(
    auth: str,
    target: str,
    exclude_key: str | None = None,
    timeout: int = 1200,
    require_all_nodes: bool = True,
) -> dict:
    """
    Stable means:
      - status green or yellow
      - relocating=0, initializing=0
      - unassigned_primary_shards=0
      - number_of_nodes == EXPECTED_NODES (if require_all_nodes)
      - all listed nodes report target version (if require_all_nodes)
    """
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            h = cluster_health(auth, exclude_key)
            last = h
            status = h.get("status", "")
            nodes = h.get("number_of_nodes")
            reloc = h.get("relocating_shards", -1)
            init = h.get("initializing_shards", -1)
            uprim = h.get("unassigned_primary_shards", -1)
            unassigned = h.get("unassigned_shards", -1)
            log(
                f"  health status={status} nodes={nodes} reloc={reloc} init={init} "
                f"unassigned={unassigned} unassigned_primary={uprim}"
            )
            versions_ok = True
            if require_all_nodes:
                rows = cat_nodes(auth, exclude_key)
                vers = {r.get("name"): r.get("version") for r in rows}
                log(f"  nodes versions: {vers}")
                if len(rows) < EXPECTED_NODES:
                    versions_ok = False
                elif any(r.get("version") != target for r in rows):
                    # during partial upgrade, only require the nodes that are up
                    # if exclude set, allow mixed; when require full, all must match
                    if exclude_key is None:
                        versions_ok = all(r.get("version") == target for r in rows)
                    else:
                        versions_ok = True  # mid-upgrade: health is enough
            health_ok = (
                status in ("green", "yellow")
                and reloc == 0
                and init == 0
                and uprim == 0
                and (not require_all_nodes or nodes == EXPECTED_NODES)
            )
            if health_ok and (not require_all_nodes or versions_ok or exclude_key is not None):
                if exclude_key is not None:
                    return h
                if versions_ok and nodes == EXPECTED_NODES:
                    return h
        except Exception as e:
            log(f"  wait_stable error: {e}")
        time.sleep(15)
    raise RuntimeError(f"cluster not stable within {timeout}s last={last}")


def wait_node_version(
    auth: str, fqdn: str, version: str, exclude_key: str, timeout: int = 900
) -> None:
    deadline = time.time() + timeout
    short = fqdn.split(".")[0]
    while time.time() < deadline:
        rows = cat_nodes(auth, exclude_key)
        for r in rows:
            name = r.get("name") or ""
            if short in name or fqdn in name:
                if r.get("version") == version:
                    log(f"  node {name} reports {version}")
                    return
                log(f"  node {name} version={r.get('version')} (want {version})")
        log(f"  waiting for {fqdn} on {version}; seen={[r.get('name') for r in rows]}")
        time.sleep(10)
    raise RuntimeError(f"{fqdn} did not report version {version}")


def upgrade_one_node(key: str, label: str, version: str, elastic_pwd: str) -> float:
    ip, fqdn = NODES[key]
    auth = curl_elastic_auth(elastic_pwd)
    log(f"=== START {label} ({fqdn} / {ip}) -> {version} ===")
    t0 = time.time()

    c = connect(ip, attempts=30)
    try:
        stage_packages(c, roles=("elasticsearch",), versions=(version,))
        run(
            c,
            f"bash {REMOTE}/upgrade-elasticsearch-node.sh "
            f"--version {shlex.quote(version)} "
            f"--es-auth {shlex.quote(f'elastic:{elastic_pwd}')}",
            timeout=1800,
        )
    finally:
        try:
            c.close()
        except Exception:
            pass

    # node should be back; confirm version via other monitors first then self
    wait_node_version(auth, fqdn, version, exclude_key=None, timeout=900)
    # wait mid-upgrade stable (all nodes present not required if master flipped)
    wait_stable(
        auth,
        target=version,
        exclude_key=key,  # allow mixed versions during rolling upgrade
        timeout=1200,
        require_all_nodes=False,
    )
    # after node upgrade, ensure all currently-up nodes are healthy and this node is on target
    h = cluster_health(auth)
    log(
        f"  post-node health: status={h.get('status')} nodes={h.get('number_of_nodes')} "
        f"unassigned_primary={h.get('unassigned_primary_shards')}"
    )
    elapsed = time.time() - t0
    log(f"=== DONE {label} in {fmt_dur(elapsed)} ===")
    return elapsed


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    total_t0 = time.time()
    log(f"ES rolling upgrade to {TARGET}")
    log(f"Expected nodes: {EXPECTED_NODES}")

    elastic_pwd = get_password()
    auth = curl_elastic_auth(elastic_pwd)

    log("--- Pre-upgrade state ---")
    rows = cat_nodes(auth)
    log(json.dumps(rows, indent=2))
    h = cluster_health(auth)
    log(json.dumps({k: h.get(k) for k in (
        "status", "number_of_nodes", "active_primary_shards", "active_shards",
        "relocating_shards", "initializing_shards", "unassigned_shards",
        "unassigned_primary_shards",
    )}, indent=2))

    if not rows:
        log("ERROR: no nodes visible")
        return 1

    # skip nodes already on target
    order = upgrade_order(auth)
    timings: list[tuple[str, float]] = []

    for key, label in order:
        ip, fqdn = NODES[key]
        already = False
        for r in cat_nodes(auth):
            name = r.get("name") or ""
            if fqdn.split(".")[0] in name and r.get("version") == TARGET:
                already = True
                break
        if already:
            log(f"SKIP {label}: already on {TARGET}")
            timings.append((key, 0.0))
            continue
        try:
            elapsed = upgrade_one_node(key, label, TARGET, elastic_pwd)
            timings.append((key, elapsed))
        except Exception as e:
            log(f"ERROR upgrading {key}: {e}")
            # try to re-enable allocation via another node
            try:
                mon = pick_monitor(key)
                c = connect(mon)
                run(
                    c,
                    f"curl -sk -u {auth} -X PUT -H 'Content-Type: application/json' "
                    f"-d '{{\"persistent\":{{\"cluster.routing.allocation.enable\":null}}}}' "
                    f"'https://localhost:9200/_cluster/settings'",
                    check=False,
                )
                c.close()
            except Exception as e2:
                log(f"WARN re-enable allocation failed: {e2}")
            log(f"ABORT after {key}")
            return 2

    log("--- Post-upgrade: wait for full cluster stability ---")
    final = wait_stable(
        auth,
        target=TARGET,
        exclude_key=None,
        timeout=1800,
        require_all_nodes=True,
    )
    rows = cat_nodes(auth)
    log("--- Final nodes ---")
    log(json.dumps(rows, indent=2))
    log("--- Final health ---")
    log(json.dumps(final, indent=2)[:2000])

    total = time.time() - total_t0
    log("")
    log("========== TIMING SUMMARY ==========")
    for key, elapsed in timings:
        log(f"  {key} ({NODES[key][1]}): {fmt_dur(elapsed)}")
    log(f"  TOTAL wall-clock: {fmt_dur(total)}")
    log("====================================")

    all_ok = (
        len(rows) == EXPECTED_NODES
        and all(r.get("version") == TARGET for r in rows)
        and final.get("unassigned_primary_shards", 1) == 0
        and final.get("status") in ("green", "yellow")
    )
    if all_ok:
        log(f"SUCCESS: all {EXPECTED_NODES} ES nodes on {TARGET}; cluster stable")
        return 0
    log("WARN: upgrade finished but stability checks incomplete")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
