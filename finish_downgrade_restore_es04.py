#!/usr/bin/env python3
"""After es01-03 are on 8.18.4 empty cluster: restore snap, test es04, verify functions, write report."""
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

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from elastic_credentials import save_local_password, save_remote_password
from fix_dashboard_search import kibana_curl

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "finish_downgrade_restore_es04.log"
REPORT = ROOT / "logs" / "downgrade_es01_03_procedure_report.txt"
REPO = "fs_nfs_snapshots"
SNAP = "pre-upgrade-system-8.18.4-with-apm-20260724"
NEW_PWD = _lab_elastic_password()  # from elasticsearch-reset-password -f after wipe
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


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    log("START finish restore + es04 test")
    auth = curl_elastic_auth(NEW_PWD)
    c = connect(NODES["es01"][0], attempts=20)
    save_local_password(NEW_PWD)
    save_remote_password(c, run, NEW_PWD)
    print(es_api(c, auth, "GET", "/_cat/nodes?v&h=name,version,master,node.role"), flush=True)
    print(es_api(c, auth, "GET", "/_cluster/health?pretty"), flush=True)
    c.close()

    # restore
    log(f"=== Restore {SNAP} ===")
    c = connect(NODES["es01"][0], attempts=20)
    print(es_api(c, auth, "PUT", f"/_snapshot/{REPO}", {
        "type": "fs",
        "settings": {"location": "/mnt/es-snapshots", "compress": True},
    }), flush=True)
    print(es_api(c, auth, "POST", f"/_snapshot/{REPO}/_verify?pretty", timeout=180), flush=True)
    meta = es_api(c, auth, "GET", f"/_snapshot/{REPO}/{SNAP}?pretty")
    log(meta[:600])
    body = {
        "indices": "*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "include_aliases": True,
        "feature_states": ["*"],
    }
    print(
        es_api(
            c, auth, "POST",
            f"/_snapshot/{REPO}/{SNAP}/_restore?wait_for_completion=false&pretty",
            body, timeout=180,
        ),
        flush=True,
    )
    restore_state = "IN_PROGRESS"
    for i in range(200):
        # after restore, password may switch to snap security state
        for pwd in (NEW_PWD, _lab_elastic_password()):
            try:
                a = curl_elastic_auth(pwd)
                health = parse_json(es_api(c, a, "GET", "/_cluster/health")) or {}
                if health.get("status"):
                    auth = a
                    if pwd != NEW_PWD:
                        log(f"auth switched to snap password candidate")
                        save_local_password(pwd)
                        save_remote_password(c, run, pwd)
                    break
            except Exception:
                health = {}
        log(
            f"  poll {i}: status={health.get('status')} nodes={health.get('number_of_nodes')} "
            f"prim={health.get('active_primary_shards')} uprim={health.get('unassigned_primary_shards')} "
            f"init={health.get('initializing_shards')}"
        )
        if (
            health.get("status") in ("green", "yellow")
            and health.get("number_of_nodes", 0) >= 3
            and health.get("unassigned_primary_shards", 1) == 0
            and health.get("initializing_shards", 1) == 0
            and health.get("active_primary_shards", 0) > 10
        ):
            restore_state = "RESTORED_STABLE"
            break
        time.sleep(15)
    else:
        restore_state = "TIMEOUT"
    c.close()
    log(f"restore_state={restore_state}")

    # re-resolve password after restore
    for pwd in (_lab_elastic_password(), NEW_PWD):
        try:
            c = connect(NODES["es01"][0], attempts=10)
            a = curl_elastic_auth(pwd)
            code = run(
                c,
                f"curl -sk -o /dev/null -w '%{{http_code}}' -u elastic:{pwd} "
                f"https://localhost:9200/_cluster/health",
                check=False,
            )
            if "200" in code:
                auth = a
                save_local_password(pwd)
                save_remote_password(c, run, pwd)
                log(f"post-restore auth ok with known password")
                c.close()
                break
            c.close()
        except Exception as e:
            log(f"auth try fail: {e}")

    # functions
    functions = {}
    c = connect(NODES["es01"][0], attempts=15)
    try:
        auth = curl_elastic_auth(get_elastic_password(c))
    except Exception:
        pass
    functions["health"] = parse_json(es_api(c, auth, "GET", "/_cluster/health"))
    functions["nodes"] = parse_json(
        es_api(c, auth, "GET", "/_cat/nodes?h=name,version,master,node.role&format=json")
    )
    c.close()

    try:
        kb = connect(NODES["kibana"][0], attempts=30)
        for _ in range(40):
            code = run(
                kb,
                "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status || true",
                check=False,
            )
            if "200" in code:
                break
            time.sleep(8)
        rules = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=100")
        functions["rules"] = [
            {
                "name": r.get("name"),
                "type": r.get("rule_type_id"),
                "enabled": r.get("enabled"),
                "exec": (r.get("execution_status") or {}).get("status"),
            }
            for r in (rules.get("data") or [])
        ]
        st = kibana_curl(kb, auth, "GET", "/api/status")
        functions["kibana"] = ((st.get("status") or {}).get("overall") or {}).get("level")
        kb.close()
    except Exception as e:
        functions["kibana_err"] = str(e)

    try:
        fl = connect(NODES["fleet"][0], attempts=10)
        functions["apm"] = run(
            fl,
            "systemctl is-active apm-server 2>/dev/null; ss -lntp | grep 8200 || true; "
            "curl -s -o /dev/null -w 'http=%{http_code}' http://127.0.0.1:8200/ || true",
            check=False,
        )[-300:]
        fl.close()
    except Exception as e:
        functions["apm_err"] = str(e)

    log(json.dumps(functions, indent=2, default=str)[:5000])

    # es04 rejoin
    log("=== es04 rejoin test ===")
    es04 = {"joined": False, "errors": [], "version": None}
    c4 = connect(NODES["es04"][0], attempts=15)
    es04["version"] = run(c4, "rpm -q elasticsearch", check=False).strip().splitlines()[-1]
    run(c4, "systemctl start elasticsearch; sleep 15; systemctl is-active elasticsearch", check=False, timeout=180)
    for i in range(16):
        mon = connect(NODES["es01"][0], attempts=5)
        try:
            a = curl_elastic_auth(get_elastic_password(mon))
        except Exception:
            a = auth
        nodes = es_api(mon, a, "GET", "/_cat/nodes?v&h=name,version,master,node.role")
        mon.close()
        log(f"  poll {i}: {nodes[-450:]}")
        if "ismelkesnode04" in nodes:
            es04["joined"] = True
            es04["nodes"] = nodes[-500:]
            break
        err = run(
            c4,
            "journalctl -u elasticsearch -n 50 --no-pager | "
            "grep -iE 'join|version|incompatible|reject|illegal|handshake|failed' | tail -15 || true",
            check=False,
        )
        if err.strip():
            log(f"  hints: {err[-600:]}")
            es04["errors"].append(err[-400:])
        time.sleep(12)
    c4.close()

    # final nodes
    c = connect(NODES["es01"][0], attempts=10)
    try:
        auth = curl_elastic_auth(get_elastic_password(c))
    except Exception:
        pass
    final_nodes = es_api(c, auth, "GET", "/_cat/nodes?v&h=name,version,master,node.role")
    final_health = es_api(c, auth, "GET", "/_cluster/health?pretty")
    print(final_nodes, flush=True)
    print(final_health, flush=True)
    c.close()

    REPORT.write_text(
        f"""
================================================================================
ES01–ES03 RPM DOWNGRADE → 8.18.4 + SNAPSHOT RESTORE + ES04 REJOIN TEST
Generated: {datetime.now().isoformat(timespec='seconds')}
================================================================================

1) REQUIREMENTS
--------------------------------------------------------------------------------
- Local RPM: packages/elasticsearch-8.18.4-x86_64.rpm
- Preserved TLS material: /etc/elasticsearch/certs/{{http.p12,transport.p12}} + elasticsearch.keystore
- NFS repo path: /mnt/es-snapshots registered as fs_nfs_snapshots
- Snapshot: {SNAP}
- After data wipe, elastic password is NEW until restore applies security feature state
  (used temporary reset: elasticsearch-reset-password -u elastic -b -f)
- After successful restore, elastic password returns to snapshot/security state
- SSH root access to all ES nodes
- CRITICAL: cannot keep 8.19 data files under 8.18.4 binary — wipe path.data then restore
- CRITICAL: dnf reinstall can overwrite elasticsearch.yml — re-apply full production yml
  including xpack.security.*.ssl.enabled: true
- es04 intentionally remains 8.19.18 for rejoin compatibility test
- Kibana / Fleet binaries not downgraded here

2) STEP PROCEDURE
--------------------------------------------------------------------------------
1. Stop es04 elasticsearch.
2. For es01, es02, es03:
   a. Stop ES; dnf remove elasticsearch; dnf install 8.18.4 RPM
   b. Rewrite elasticsearch.yml (cluster.name, path.data=/data/elasticsearch, SSL enabled,
      seed_hosts, node.roles=[master,data_content,remote_cluster_client], path.repo, APM tracing)
   c. Wipe /data/elasticsearch (and /var/lib/elasticsearch)
   d. Mount NFS; start elasticsearch; bootstrap with cluster.initial_master_nodes if empty
3. Wait for 3-node 8.18.4 cluster (may be RED until security usable).
4. If elastic password unknown: elasticsearch-reset-password -u elastic -b -f
5. Register/verify snapshot repo; restore {SNAP} with
   indices=*, include_global_state=true, feature_states=["*"]
6. Wait yellow/green, unassigned_primary_shards=0, primaries restored.
7. Start es04 (8.19.18) and observe join logs.
8. Verify Kibana rules + APM :8200.

3) RESULTS
--------------------------------------------------------------------------------
restore_state: {restore_state}
es04_rejoin: {json.dumps(es04, indent=2, default=str)[:3000]}
functions: {json.dumps(functions, indent=2, default=str)[:5000]}
final_nodes:
{final_nodes[-800:]}

4) ES04 REJOIN COMPATIBILITY
--------------------------------------------------------------------------------
Elasticsearch rolling upgrade: older nodes may join a newer cluster.
Rolling downgrade: a NEWER node (8.19.18) generally CANNOT join an OLDER cluster (8.18.4).
Expected: es04 join FAILS (version / incompatible handshake).
To include es04 in 8.18.4: wipe+downgrade es04 the same way and restore, or HV-restore
es04 to pre-upgrade-system-8.18.4-*.

5) FULL FUNCTION CHECKLIST (healthy restored 8.18.4 cluster)
--------------------------------------------------------------------------------
[ ] es01–es03 version 8.18.4, roles mrs
[ ] Cluster yellow/green; 0 unassigned primaries
[ ] Security feature state restored (users/roles)
[ ] Kibana feature state restored — alert rules including:
    - Upgrade Testing Sample Alert
    - Obs Sample Custom/Log/Metric/Inventory thresholds
    - APM Sample Latency/Error/Failed-tx/Anomaly
[ ] Fleet feature state (agents re-checkin)
[ ] APM Server process on Fleet host :8200 (not removed by ES RPM work)
[ ] ES tracing.apm settings present on nodes
[ ] Snapshot repo still usable
[ ] es04 only after matching version

6) ROLLBACK
--------------------------------------------------------------------------------
Hyper-V: post-upgrade-system-8.19.18-20260724 (return ES to 8.19.18)
     or pre-upgrade-system-8.18.4-with-apm (8.18.4 + APM/alerts baseline)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    log(f"report -> {REPORT}")
    log(f"DONE restore={restore_state} es04_joined={es04.get('joined')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
