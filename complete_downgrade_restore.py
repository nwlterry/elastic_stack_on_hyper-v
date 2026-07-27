#!/usr/bin/env python3
"""
Complete es01-03 downgrade to 8.18.4 with restored yml, wipe data, form cluster,
restore Elastic snapshot, test es04 rejoin, verify alerts/APM, write procedure report.
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


import json
import shlex
import textwrap
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import NODES, REMOTE, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import kibana_curl
from scp import SCPClient
from upgrade_elastic_stack import stage_packages

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "complete_downgrade_restore.log"
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


def yml_for(key: str, bootstrap: bool = True) -> str:
    _, fqdn = NODES[key]
    lines = [
        "cluster.name: ism-elk-cluster",
        f"node.name: {fqdn}",
        "path.data: /data/elasticsearch",
        "path.logs: /var/log/elasticsearch",
        'path.repo: ["/mnt/es-snapshots"]',
        "network.host: 0.0.0.0",
        "http.port: 9200",
        "transport.port: 9300",
        "node.roles: [master, data_content, remote_cluster_client]",
        "discovery.seed_hosts:",
        "  - ismelkesnode01.ocplab.net",
        "  - ismelkesnode02.ocplab.net",
        "  - ismelkesnode03.ocplab.net",
        "  - ismelkesnode04.ocplab.net",
    ]
    if bootstrap:
        lines += [
            "cluster.initial_master_nodes:",
            "  - ismelkesnode01.ocplab.net",
            "  - ismelkesnode02.ocplab.net",
            "  - ismelkesnode03.ocplab.net",
        ]
    lines += [
        "xpack.security.enabled: true",
        "xpack.security.enrollment.enabled: true",
        "xpack.security.http.ssl:",
        "  enabled: true",
        "  keystore.path: certs/http.p12",
        "xpack.security.transport.ssl:",
        "  enabled: true",
        "  verification_mode: certificate",
        "  keystore.path: certs/transport.p12",
        "  truststore.path: certs/transport.p12",
        "xpack.monitoring.collection.enabled: false",
        "xpack.monitoring.elasticsearch.collection.enabled: false",
        "tracing.apm.enabled: true",
        'tracing.apm.agent.server_url: "http://10.44.40.42:8200"',
        'tracing.apm.agent.environment: "upgrade-test"',
        "",
    ]
    return "\n".join(lines)


def prepare_node(key: str) -> None:
    ip, fqdn = NODES[key]
    log(f"=== Prepare {key} {fqdn} as {TARGET} ===")
    c = connect(ip, attempts=30)
    stage_packages(c, roles=("elasticsearch",), versions=(TARGET,))
    yml = yml_for(key, bootstrap=True)
    # write yml via remote python to avoid shell quoting issues
    run(
        c,
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        f"Path('/etc/elasticsearch/elasticsearch.yml').write_text({yml!r})\n"
        "print('yml_written', len(Path('/etc/elasticsearch/elasticsearch.yml').read_text()))\n"
        "PY",
        check=False,
    )
    out = run(
        c,
        f"""
set -e
systemctl stop elasticsearch 2>/dev/null || true
pkill -9 -f org.elasticsearch 2>/dev/null || true
sleep 1
# install 8.18.4 if needed
if ! rpm -q elasticsearch-{TARGET} &>/dev/null; then
  dnf remove -y elasticsearch || rpm -e --nodeps elasticsearch || true
  RPM=/opt/elastic-setup/rpms/elasticsearch-{TARGET}-x86_64.rpm
  test -f "$RPM"
  dnf install -y "$RPM" || true
  # dnf may return non-zero due to security autoconfig abort (exit 78) but package installs
  rpm -q elasticsearch-{TARGET} || rpm -q elasticsearch | grep -q {TARGET}
fi
rpm -q elasticsearch
# wipe data
for d in /data/elasticsearch /var/lib/elasticsearch; do
  mkdir -p "$d"
  rm -rf "$d"/*
  chown -R elasticsearch:elasticsearch "$d"
done
mkdir -p /var/log/elasticsearch /mnt/es-snapshots
chown -R elasticsearch:elasticsearch /var/log/elasticsearch
# ensure certs exist
ls -la /etc/elasticsearch/certs/
test -f /etc/elasticsearch/certs/http.p12
test -f /etc/elasticsearch/certs/transport.p12
# NFS
if ! mountpoint -q /mnt/es-snapshots; then
  mount -t nfs 10.44.40.41:/export/es-snapshots /mnt/es-snapshots 2>/dev/null || \
  mount -t nfs 10.44.40.41:/mnt/es-snapshots /mnt/es-snapshots 2>/dev/null || true
fi
timeout 5 ls /mnt/es-snapshots >/dev/null && echo NFS_OK || echo NFS_WARN
# re-apply yml after package install (rpm can overwrite)
python3 - <<'PY'
from pathlib import Path


Path('/etc/elasticsearch/elasticsearch.yml').write_text({yml!r})
print('yml_reapplied')
PY
chown root:elasticsearch /etc/elasticsearch/elasticsearch.yml
chmod 660 /etc/elasticsearch/elasticsearch.yml
systemctl daemon-reload
systemctl enable elasticsearch
systemctl start elasticsearch
sleep 10
systemctl is-active elasticsearch || (journalctl -u elasticsearch -n 30 --no-pager; tail -n 40 /var/log/elasticsearch/*.log 2>/dev/null | tail -40; exit 1)
curl -sk -m 8 https://localhost:9200/ | head -c 300 || true
echo
echo DONE_{key}
""",
        check=False,
        timeout=900,
    )
    print(out[-2500:], flush=True)
    if f"DONE_{key}" not in out and "active" not in out:
        # one more start attempt with log
        out2 = run(
            c,
            "systemctl start elasticsearch; sleep 12; systemctl is-active elasticsearch; "
            "tail -n 30 /var/log/elasticsearch/ism-elk-cluster.log 2>/dev/null || "
            "tail -n 30 /var/log/elasticsearch/elasticsearch.log 2>/dev/null || true",
            check=False,
            timeout=120,
        )
        print(out2[-1500:], flush=True)
        if "active" not in out2.splitlines()[-20:]:
            # check is-active explicitly
            st = run(c, "systemctl is-active elasticsearch; rpm -q elasticsearch", check=False)
            log(f"status: {st}")
            if "active" not in st or TARGET not in st:
                c.close()
                raise RuntimeError(f"{key} failed to start on {TARGET}")
    c.close()


def wait_cluster():
    log("=== Wait 3-node 8.18.4 ===")
    for i in range(80):
        for key in KEYS:
            try:
                c = connect(NODES[key][0], attempts=4)
                auth = curl_elastic_auth(PWD)
                try:
                    auth = curl_elastic_auth(get_elastic_password(c))
                except Exception:
                    pass
                rows = parse_json(es_api(c, auth, "GET", "/_cat/nodes?h=name,version,master&format=json")) or []
                health = parse_json(es_api(c, auth, "GET", "/_cluster/health")) or {}
                c.close()
                vers = {r.get("name"): r.get("version") for r in rows if isinstance(r, dict)}
                log(f"  poll {i} via={key} status={health.get('status')} n={health.get('number_of_nodes')} {vers}")
                if (
                    len(rows) >= 3
                    and all(v == TARGET for v in vers.values())
                    and health.get("status") in ("green", "yellow", "red")
                ):
                    return auth, key
            except Exception as e:
                log(f"  {key}: {e}")
        time.sleep(12)
    raise RuntimeError("cluster not ready")


def do_restore(auth, via):
    log(f"=== Restore {SNAP} ===")
    c = connect(NODES[via][0], attempts=20)
    print(es_api(c, auth, "PUT", f"/_snapshot/{REPO}", {
        "type": "fs",
        "settings": {"location": "/mnt/es-snapshots", "compress": True},
    }), flush=True)
    print(es_api(c, auth, "POST", f"/_snapshot/{REPO}/_verify?pretty", timeout=180), flush=True)
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
    state = "IN_PROGRESS"
    for i in range(200):
        health = parse_json(es_api(c, auth, "GET", "/_cluster/health")) or {}
        log(
            f"  restore {i}: status={health.get('status')} nodes={health.get('number_of_nodes')} "
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
            state = "RESTORED_STABLE"
            break
        time.sleep(15)
    else:
        state = "TIMEOUT"
    c.close()
    # strip initial masters
    for key in KEYS:
        try:
            c = connect(NODES[key][0], attempts=5)
            yml = yml_for(key, bootstrap=False)
            run(
                c,
                "python3 - <<'PY'\n"
                "from pathlib import Path\n"
                f"Path('/etc/elasticsearch/elasticsearch.yml').write_text({yml!r})\n"
                "print('yml_no_bootstrap')\n"
                "PY",
                check=False,
            )
            c.close()
        except Exception as e:
            log(f"strip {key}: {e}")
    return state


def test_es04(auth):
    log("=== es04 rejoin test (expect fail if still 8.19.18) ===")
    result = {"joined": False, "errors": [], "es04_version": None}
    c4 = connect(NODES["es04"][0], attempts=15)
    ver = run(c4, "rpm -q elasticsearch", check=False)
    result["es04_version"] = ver.strip().splitlines()[-1]
    run(c4, "systemctl start elasticsearch; sleep 12; systemctl is-active elasticsearch", check=False, timeout=180)
    for i in range(18):
        mon = connect(NODES["es01"][0], attempts=5)
        try:
            a = curl_elastic_auth(get_elastic_password(mon))
        except Exception:
            a = auth
        nodes = es_api(mon, a, "GET", "/_cat/nodes?v&h=name,version,master,node.role")
        mon.close()
        log(f"  poll {i}: {nodes[-400:]}")
        if "ismelkesnode04" in nodes:
            result["joined"] = True
            result["nodes"] = nodes[-500:]
            break
        err = run(
            c4,
            "journalctl -u elasticsearch -n 40 --no-pager | "
            "grep -iE 'join|version|incompatible|reject|illegal|handshake|failed to' | tail -12 || true",
            check=False,
        )
        if err.strip():
            log(f"  es04: {err[-500:]}")
            result["errors"].append(err[-350:])
        time.sleep(12)
    c4.close()
    return result


def verify(auth):
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
        for _ in range(40):
            code = run(kb, "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status || true", check=False)
            if "200" in code:
                break
            time.sleep(8)
        rules = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=100")
        out["rules"] = [
            {"name": r.get("name"), "type": r.get("rule_type_id"), "enabled": r.get("enabled"),
             "exec": (r.get("execution_status") or {}).get("status")}
            for r in (rules.get("data") or [])
        ]
        st = kibana_curl(kb, auth, "GET", "/api/status")
        out["kibana_level"] = ((st.get("status") or {}).get("overall") or {}).get("level")
        kb.close()
    except Exception as e:
        out["kibana_err"] = str(e)
    try:
        fl = connect(NODES["fleet"][0], attempts=10)
        out["apm"] = run(
            fl,
            "systemctl is-active apm-server 2>/dev/null; ss -lntp | grep 8200 || true; "
            "curl -s -o /dev/null -w 'http=%{http_code}' http://127.0.0.1:8200/ || true",
            check=False,
        )[-250:]
        fl.close()
    except Exception as e:
        out["apm_err"] = str(e)
    log(json.dumps(out, indent=2, default=str)[:5000])
    return out


def write_report(restore_state, es04, functions):
    REPORT.write_text(
        f"""
================================================================================
ES01–ES03 RPM DOWNGRADE TO 8.18.4 + SNAPSHOT RESTORE + ES04 REJOIN TEST
Generated: {datetime.now().isoformat(timespec='seconds')}
================================================================================

1) REQUIREMENTS
--------------------------------------------------------------------------------
- Local RPM: packages/elasticsearch-8.18.4-x86_64.rpm
- Preserved TLS certs + elasticsearch.keystore under /etc/elasticsearch/
- NFS snapshot repo: fs_nfs_snapshots -> /mnt/es-snapshots
- Snapshot: {SNAP} (global state + feature_states + indices from 8.18.4 baseline with APM/alerts)
- elastic password (restored with security feature state / known lab password)
- SSH root to es01–es04
- CRITICAL: Elasticsearch does not support binary downgrade on existing data directories.
  path.data MUST be wiped; cluster state returns only via snapshot restore.
- Package reinstall may OVERWRITE elasticsearch.yml — must re-apply production yml
  (cluster.name, path.data=/data/elasticsearch, SSL enabled, seed_hosts, node.roles).
- es04 left on 8.19.18 for rejoin experiment (expected incompatible).
- Kibana/Fleet not package-downgraded by this procedure.

2) STEP PROCEDURE (EXECUTED / REQUIRED)
--------------------------------------------------------------------------------
1. Stop elasticsearch on es04.
2. For each of es01, es02, es03:
   a. Stage RPM; dnf remove elasticsearch; dnf install 8.18.4
   b. Re-write elasticsearch.yml (SSL transport/http enabled, path.data, roles mrs, seeds)
   c. Wipe /data/elasticsearch (and /var/lib/elasticsearch)
   d. Mount NFS; systemctl start elasticsearch
   e. Optional: cluster.initial_master_nodes for empty-cluster bootstrap
3. Wait until three nodes report version 8.18.4 and form a cluster.
4. PUT/verify _snapshot/{REPO}; restore {SNAP} with:
   indices=*, include_global_state=true, feature_states=["*"]
5. Wait yellow/green, unassigned_primary_shards=0, primaries restored.
6. Remove cluster.initial_master_nodes from yml after stable restore.
7. Start es04 (still 8.19.18); observe join success/failure + logs.
8. Verify Kibana alert rules and APM server :8200.

3) RESULTS
--------------------------------------------------------------------------------
restore_state: {restore_state}
es04_rejoin: {json.dumps(es04, indent=2, default=str)[:2500]}
functions: {json.dumps(functions, indent=2, default=str)[:4500]}

4) ES04 REJOIN EXPECTATION
--------------------------------------------------------------------------------
Rolling upgrade allows older nodes to join a newer cluster.
A NEWER node (8.19.18) generally CANNOT join an OLDER cluster (8.18.4).
Expect: es04 join FAIL with version / incompatible handshake messages.
For a full 4-node 8.18.4 cluster: also wipe+downgrade es04 (same method) then restore,
or Hyper-V restore es04 to pre-upgrade-system-8.18.4-*.

5) FULL FUNCTION CHECKLIST (healthy 8.18.4 cluster after restore)
--------------------------------------------------------------------------------
[ ] es01–es03 on 8.18.4 with roles master,data_content,remote_cluster_client
[ ] Cluster yellow/green; 0 unassigned primaries
[ ] Security restored (.security, users)
[ ] Kibana feature state: saved objects + alert rules
    - Upgrade Testing Sample Alert
    - Observability sample (custom/log/metric/inventory)
    - APM sample (latency/error/failed-tx/anomaly)
[ ] Fleet feature state (agents re-checkin over time)
[ ] APM Server on Fleet host listening :8200
[ ] ES tracing.apm / Kibana elastic.apm settings present
[ ] Snapshot repository still registered
[ ] es04 only after version match (8.18.4)

6) ROLLBACK
--------------------------------------------------------------------------------
Hyper-V restore to post-upgrade-system-8.19.18-20260724 (return to 8.19.18)
or pre-upgrade-system-8.18.4-with-apm (full 8.18.4 + APM/alerts baseline).
""".strip()
        + "\n",
        encoding="utf-8",
    )
    log(f"report written: {REPORT}")


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    log("START complete downgrade restore")

    c4 = connect(NODES["es04"][0], attempts=15)
    run(c4, "systemctl stop elasticsearch || true", check=False)
    c4.close()

    for key in KEYS:
        prepare_node(key)
        time.sleep(5)

    auth, via = wait_cluster()
    restore_state = do_restore(auth, via)

    try:
        c = connect(NODES[via][0], attempts=20)
        auth = curl_elastic_auth(get_elastic_password(c))
        c.close()
    except Exception as e:
        log(f"reauth: {e}")
        auth = curl_elastic_auth(PWD)

    functions = verify(auth)
    es04 = test_es04(auth)
    functions2 = verify(auth)
    write_report(restore_state, es04, {"after_restore": functions, "after_es04": functions2})
    log(f"DONE restore={restore_state} es04_joined={es04.get('joined')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
