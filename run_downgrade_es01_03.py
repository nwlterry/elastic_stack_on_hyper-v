#!/usr/bin/env python3
"""Reliable es01-03 RPM downgrade to 8.18.4, snapshot restore, es04 rejoin test."""
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

from deploy_ordered_stack import NODES, REMOTE, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import kibana_curl
from scp import SCPClient
from upgrade_elastic_stack import stage_packages

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "downgrade-es-node-8184.sh"
LOG = ROOT / "logs" / "run_downgrade_es01_03.log"
REPORT = ROOT / "logs" / "downgrade_es01_03_procedure_report.txt"
TARGET = "8.18.4"
REPO = "fs_nfs_snapshots"
SNAP = "pre-upgrade-system-8.18.4-with-apm-20260724"
PWD = _lab_elastic_password()
KEYS = ("es01", "es02", "es03")


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def es_api(c, auth, method, path, body=None, timeout=300):
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


def downgrade_one(key: str) -> None:
    ip, fqdn = NODES[key]
    log(f"=== Downgrade {key} {fqdn} ===")
    c = connect(ip, attempts=30)
    stage_packages(c, roles=("elasticsearch",), versions=(TARGET,))
    with SCPClient(c.get_transport()) as scp:
        scp.put(str(SCRIPT), f"{REMOTE}/downgrade-es-node-8184.sh")
    out = run(
        c,
        f"chmod +x {REMOTE}/downgrade-es-node-8184.sh && "
        f"bash {REMOTE}/downgrade-es-node-8184.sh {TARGET}",
        check=False,
        timeout=1200,
    )
    print(out[-3000:], flush=True)
    if "8.18.4" not in out:
        raise RuntimeError(f"{key} downgrade failed — 8.18.4 not in output")
    if "DONE node" not in out and "active" not in out:
        # still verify
        v = run(c, "rpm -q elasticsearch; systemctl is-active elasticsearch", check=False)
        log(f"verify: {v}")
        if "8.18.4" not in v:
            c.close()
            raise RuntimeError(f"{key} not on 8.18.4")
    c.close()


def wait_cluster(min_nodes=3, timeout=900):
    log("=== Wait cluster 8.18.4 ===")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for key in KEYS:
            try:
                c = connect(NODES[key][0], attempts=5)
                auth = curl_elastic_auth(PWD)
                # password may fail if security not ready
                try:
                    auth = curl_elastic_auth(get_elastic_password(c))
                except Exception:
                    pass
                rows = parse_json(es_api(c, auth, "GET", "/_cat/nodes?h=name,version,master&format=json")) or []
                health = parse_json(es_api(c, auth, "GET", "/_cluster/health")) or {}
                c.close()
                vers = {r.get("name"): r.get("version") for r in rows if isinstance(r, dict)}
                log(f"  via={key} status={health.get('status')} nodes={health.get('number_of_nodes')} vers={vers}")
                if len(rows) >= min_nodes and all(v == TARGET for v in vers.values()) and len(vers) >= min_nodes:
                    if health.get("status") in ("green", "yellow", "red"):
                        return auth, key
            except Exception as e:
                log(f"  wait {key}: {e}")
        time.sleep(15)
    raise RuntimeError("cluster not ready")


def restore(auth, via):
    log(f"=== Restore {SNAP} ===")
    c = connect(NODES[via][0], attempts=20)
    print(es_api(c, auth, "PUT", f"/_snapshot/{REPO}", {
        "type": "fs",
        "settings": {"location": "/mnt/es-snapshots", "compress": True},
    }), flush=True)
    print(es_api(c, auth, "POST", f"/_snapshot/{REPO}/_verify?pretty", timeout=180), flush=True)
    print(es_api(c, auth, "GET", f"/_snapshot/{REPO}/{SNAP}?pretty")[:800], flush=True)
    body = {
        "indices": "*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "include_aliases": True,
        "feature_states": ["*"],
    }
    print(es_api(c, auth, "POST",
                 f"/_snapshot/{REPO}/{SNAP}/_restore?wait_for_completion=false&pretty",
                 body, timeout=180), flush=True)
    state = "IN_PROGRESS"
    for i in range(180):
        health = parse_json(es_api(c, auth, "GET", "/_cluster/health")) or {}
        log(f"  poll {i}: { {k: health.get(k) for k in ('status','number_of_nodes','active_primary_shards','unassigned_primary_shards','initializing_shards','relocating_shards')} }")
        if (
            health.get("status") in ("green", "yellow")
            and health.get("number_of_nodes", 0) >= 3
            and health.get("unassigned_primary_shards", 1) == 0
            and health.get("initializing_shards", 1) == 0
        ):
            # wait a bit more for recovery to settle
            if health.get("active_primary_shards", 0) > 0:
                state = "RESTORED_STABLE"
                break
        time.sleep(15)
    else:
        state = "TIMEOUT"
    c.close()
    return state


def strip_initial_masters():
    for key in KEYS:
        try:
            c = connect(NODES[key][0], attempts=5)
            run(c, r"""python3 - <<'PY'
from pathlib import Path


p=Path('/etc/elasticsearch/elasticsearch.yml')
if not p.exists():
    raise SystemExit(0)
lines=p.read_text().splitlines()
out=[]; skip=False
for line in lines:
    if line.strip().startswith('cluster.initial_master_nodes'):
        skip=True
        continue
    if skip:
        if line.startswith(' ') or line.startswith('\t') or line.lstrip().startswith('-'):
            continue
        skip=False
    if not skip:
        out.append(line)
p.write_text('\n'.join(out)+'\n')
print('stripped')
PY""", check=False)
            c.close()
        except Exception as e:
            log(f"strip {key}: {e}")


def test_es04(auth):
    log("=== es04 rejoin test ===")
    result = {"joined": False, "errors": []}
    c4 = connect(NODES["es04"][0], attempts=20)
    print(run(c4, "rpm -q elasticsearch; systemctl start elasticsearch; sleep 10; systemctl is-active elasticsearch", check=False, timeout=180), flush=True)
    for i in range(20):
        mon = connect(NODES["es01"][0], attempts=5)
        try:
            auth2 = curl_elastic_auth(get_elastic_password(mon))
        except Exception:
            auth2 = auth
        nodes = es_api(mon, auth2, "GET", "/_cat/nodes?v&h=name,version,master,node.role")
        mon.close()
        log(f"  poll {i}:\n{nodes[-500:]}")
        if "ismelkesnode04" in nodes:
            result["joined"] = True
            result["nodes_out"] = nodes[-500:]
            break
        err = run(c4, "journalctl -u elasticsearch -n 30 --no-pager | grep -iE 'join|version|incompatible|reject|illegal|handshake|failed' | tail -15 || true", check=False)
        if err.strip():
            log(f"  hints: {err[-600:]}")
            result["errors"].append(err[-400:])
        time.sleep(15)
    result["es04_rpm"] = run(c4, "rpm -q elasticsearch", check=False)
    c4.close()
    return result


def verify(auth):
    log("=== function verification ===")
    out = {}
    c = connect(NODES["es01"][0], attempts=20)
    try:
        auth = curl_elastic_auth(get_elastic_password(c))
    except Exception:
        pass
    out["health"] = parse_json(es_api(c, auth, "GET", "/_cluster/health"))
    out["nodes"] = parse_json(es_api(c, auth, "GET", "/_cat/nodes?h=name,version,master,node.role&format=json"))
    c.close()
    try:
        kb = connect(NODES["kibana"][0], attempts=30)
        for _ in range(36):
            code = run(kb, "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status || true", check=False)
            if "200" in code:
                break
            time.sleep(10)
        rules = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=100")
        out["rules"] = [
            {"name": r.get("name"), "type": r.get("rule_type_id"), "enabled": r.get("enabled"),
             "exec": (r.get("execution_status") or {}).get("status")}
            for r in (rules.get("data") or [])
        ]
        st = kibana_curl(kb, auth, "GET", "/api/status")
        out["kibana"] = ((st.get("status") or {}).get("overall") or {}).get("level")
        kb.close()
    except Exception as e:
        out["kibana_err"] = str(e)
    try:
        fl = connect(NODES["fleet"][0], attempts=10)
        out["apm"] = run(fl, "systemctl is-active apm-server 2>/dev/null; ss -lntp | grep 8200 || true; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8200/ || true", check=False)[-300:]
        fl.close()
    except Exception as e:
        out["apm_err"] = str(e)
    log(json.dumps(out, indent=2, default=str)[:5000])
    return out


def write_report(restore_state, es04, functions):
    text = f"""
================================================================================
ES01–ES03 RPM DOWNGRADE → 8.18.4 + ELASTIC SNAPSHOT RESTORE + ES04 REJOIN TEST
Generated: {datetime.now().isoformat(timespec='seconds')}
================================================================================

1) REQUIREMENTS
--------------------------------------------------------------------------------
- RPM: packages/elasticsearch-8.18.4-x86_64.rpm
- Snapshot repo: fs_nfs_snapshots (NFS /mnt/es-snapshots from Kibana)
- Snapshot: {SNAP}
  (include_global_state + feature_states + system/data indices from 8.18.4 pre-upgrade)
- elastic password available
- SSH root to es01–es04
- CRITICAL: binary downgrade requires WIPE of path.data; restore rebuilds cluster state
- es04 intentionally left on 8.19.18 for join test
- Kibana / Fleet not downgraded by this procedure

2) STEP PROCEDURE (EXECUTED)
--------------------------------------------------------------------------------
1. Stop elasticsearch on es04 (offline).
2. For es01, es02, es03:
   a. systemctl stop elasticsearch
   b. dnf remove elasticsearch
   c. dnf install elasticsearch-8.18.4 from local RPM
   d. Wipe /data/elasticsearch contents
   e. Keep /etc/elasticsearch (certs, yml, path.repo, roles)
   f. Ensure discovery.seed_hosts + cluster.initial_master_nodes for bootstrap
   g. Mount NFS; systemctl start elasticsearch
3. Wait for 3-node cluster all reporting {TARGET}.
4. Register/verify snapshot repo {REPO}.
5. Restore snapshot {SNAP}:
   indices=*, include_global_state=true, feature_states=['*']
6. Wait yellow/green, unassigned_primary_shards=0.
7. Strip cluster.initial_master_nodes (best-effort).
8. Start es04 (8.19.18) and observe join.
9. Verify Kibana alert rules + APM :8200.

3) RESULTS
--------------------------------------------------------------------------------
restore_state: {restore_state}
es04_rejoin: {json.dumps(es04, indent=2, default=str)[:2500]}
functions: {json.dumps(functions, indent=2, default=str)[:4000]}

4) VERSION COMPATIBILITY (es04 rejoin)
--------------------------------------------------------------------------------
Elasticsearch allows older nodes to join a NEWER cluster during rolling upgrade.
A NEWER node (8.19.18) generally CANNOT join an OLDER cluster (8.18.4).

Expected if es04 stays 8.19.18: join FAILS with version/incompatible errors.
To restore full 4-node 8.18.4: also wipe+downgrade es04 and restore (or HV restore
es04 to pre-upgrade-system-8.18.4-*).

5) FULL FUNCTION CHECKLIST (when 8.18.4 cluster is healthy)
--------------------------------------------------------------------------------
[ ] es01–es03 version 8.18.4; roles mrs as designed
[ ] Cluster yellow/green; 0 unassigned primaries
[ ] Security (.security) from feature_states
[ ] Kibana feature state restored (saved objects, dashboards)
[ ] Alert rules restored including:
    - Upgrade Testing Sample Alert
    - Obs Sample Custom / Log / Metric / Inventory
    - APM Sample Latency / Error / Failed-tx / Anomaly
[ ] Fleet feature state (policies/agents may re-checkin)
[ ] APM Server process on Fleet host :8200 (not removed by ES RPM work)
[ ] ES tracing.apm / Kibana elastic.apm settings (from config, not wiped)
[ ] Snapshot repo still usable
[ ] es04: only after matching version (8.18.4) and clean data or HV restore

6) ROLLBACK
--------------------------------------------------------------------------------
Hyper-V: post-upgrade-system-8.19.18-20260724 (all ES back to 8.19.18)
      or pre-upgrade-system-8.18.4-with-apm (8.18.4 + APM/alerts baseline)
"""
    REPORT.write_text(text.strip() + "\n", encoding="utf-8")
    log(f"report -> {REPORT}")


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    log("START reliable downgrade es01-03")

    # ensure es04 stopped
    c4 = connect(NODES["es04"][0], attempts=15)
    run(c4, "systemctl stop elasticsearch; systemctl is-active elasticsearch || echo stopped", check=False)
    c4.close()

    for key in KEYS:
        downgrade_one(key)
        time.sleep(5)

    auth, via = wait_cluster()
    restore_state = restore(auth, via)
    strip_initial_masters()

    try:
        c = connect(NODES[via][0], attempts=15)
        auth = curl_elastic_auth(get_elastic_password(c))
        c.close()
    except Exception as e:
        log(f"reauth: {e}")
        auth = curl_elastic_auth(PWD)

    functions = verify(auth)
    es04 = test_es04(auth)
    functions2 = verify(auth)
    write_report(restore_state, es04, {"after_restore": functions, "after_es04_attempt": functions2})
    log(f"DONE restore={restore_state} es04_joined={es04.get('joined')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
