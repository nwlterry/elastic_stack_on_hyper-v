#!/usr/bin/env python3
"""
Downgrade es01–es03 only: RPM uninstall → install 8.18.4 → wipe data → form cluster
→ restore Elastic snapshot → test whether es04 (8.19.18) can rejoin.

Produces a procedure/requirements report under logs/.
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
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import (
    NODES,
    REMOTE,
    connect,
    curl_elastic_auth,
    get_elastic_password,
    run,
)
from fix_dashboard_search import kibana_curl
from scp import SCPClient
from upgrade_elastic_stack import stage_packages

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "downgrade_es01_03_rpm_restore.log"
REPORT = ROOT / "logs" / "downgrade_es01_03_procedure_report.txt"
TARGET = "8.18.4"
# Snapshot taken with APM + observability/APM sample alerts
SNAP_REPO = "fs_nfs_snapshots"
SNAP_NAME = "pre-upgrade-system-8.18.4-with-apm-20260724"
PWD = _lab_elastic_password()
DOWNGRADE_KEYS = ("es01", "es02", "es03")
ES04_KEY = "es04"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def es_api(c, auth: str, method: str, path: str, body=None, timeout: int = 300) -> str:
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


def stop_es04() -> None:
    log("=== Stop es04 (leave offline for rejoin test) ===")
    c = connect(NODES[ES04_KEY][0], attempts=20)
    print(
        run(
            c,
            "systemctl stop elasticsearch; sleep 2; "
            "systemctl is-active elasticsearch || echo stopped; "
            "rpm -q elasticsearch",
            check=False,
            timeout=120,
        ),
        flush=True,
    )
    c.close()


def downgrade_node(key: str) -> None:
    ip, fqdn = NODES[key]
    log(f"=== Downgrade {key} {fqdn} -> {TARGET} (RPM remove/install + wipe data) ===")
    c = connect(ip, attempts=30)
    try:
        stage_packages(c, roles=("elasticsearch",), versions=(TARGET,))
        # Also ensure GPG + rpm present
        out = run(
            c,
            f"""
set -e
echo "=== pre state ==="
rpm -q elasticsearch || true
systemctl stop elasticsearch 2>/dev/null || true
sleep 2
pkill -9 -f org.elasticsearch 2>/dev/null || true
sleep 1

echo "=== remove elasticsearch RPM ==="
dnf remove -y elasticsearch 2>/dev/null || rpm -e elasticsearch 2>/dev/null || true
# clean residual modules if any
rpm -q elasticsearch && exit 11 || true

RPM="/opt/elastic-setup/rpms/elasticsearch-{TARGET}-x86_64.rpm"
test -f "$RPM" || {{ ls -la /opt/elastic-setup/rpms; exit 12; }}
if [ -f /opt/elastic-setup/rpms/GPG-KEY-elasticsearch ]; then
  rpm --import /opt/elastic-setup/rpms/GPG-KEY-elasticsearch || true
fi

echo "=== install elasticsearch {TARGET} ==="
dnf install -y "$RPM"

echo "=== wipe data (required for binary downgrade) ==="
# Preserve config/certs under /etc/elasticsearch
systemctl stop elasticsearch 2>/dev/null || true
# Data path used by this cluster
for d in /data/elasticsearch /var/lib/elasticsearch; do
  if [ -d "$d" ]; then
    echo "wiping $d"
    rm -rf "$d"/*
    mkdir -p "$d"
    chown -R elasticsearch:elasticsearch "$d" || true
  fi
done
# Ensure logs writable
mkdir -p /var/log/elasticsearch
chown -R elasticsearch:elasticsearch /var/log/elasticsearch || true

# Ensure NFS snapshot path if configured
if grep -q path.repo /etc/elasticsearch/elasticsearch.yml 2>/dev/null; then
  mkdir -p /mnt/es-snapshots
  if ! mountpoint -q /mnt/es-snapshots; then
    mount -t nfs 10.44.40.41:/export/es-snapshots /mnt/es-snapshots 2>/dev/null || \
    mount -t nfs 10.44.40.41:/mnt/es-snapshots /mnt/es-snapshots 2>/dev/null || true
  fi
  timeout 5 ls /mnt/es-snapshots >/dev/null 2>&1 && echo NFS_OK || echo NFS_WARN
fi

# Ensure discovery seed hosts present
if ! grep -q 'ismelkesnode01' /etc/elasticsearch/elasticsearch.yml 2>/dev/null; then
  cat >> /etc/elasticsearch/elasticsearch.yml <<'EOF'

discovery.seed_hosts:
  - ismelkesnode01.ocplab.net
  - ismelkesnode02.ocplab.net
  - ismelkesnode03.ocplab.net
  - ismelkesnode04.ocplab.net
EOF
fi

# For empty cluster bootstrap after wipe: set initial masters temporarily if missing
# (Elastic 8 usually uses voting config from disk; empty needs initial_master_nodes once)
if ! grep -q 'cluster.initial_master_nodes' /etc/elasticsearch/elasticsearch.yml; then
  cat >> /etc/elasticsearch/elasticsearch.yml <<'EOF'
cluster.initial_master_nodes:
  - ismelkesnode01.ocplab.net
  - ismelkesnode02.ocplab.net
  - ismelkesnode03.ocplab.net
EOF
  echo "added cluster.initial_master_nodes for bootstrap"
fi

echo "=== start elasticsearch ==="
systemctl daemon-reload || true
systemctl enable elasticsearch
systemctl start elasticsearch
sleep 5
systemctl is-active elasticsearch || (journalctl -u elasticsearch -n 50 --no-pager; exit 13)
rpm -q elasticsearch
curl -sk -m 5 https://localhost:9200/ | head -c 300 || true
""",
            check=False,
            timeout=900,
        )
        print(out[-2500:], flush=True)
        if "elasticsearch-8.18.4" not in out and f"elasticsearch-{TARGET}" not in out:
            # rpm -q may print elasticsearch-8.18.4-1.x86_64
            if "8.18.4" not in out:
                log(f"WARN {key} may not be on 8.18.4")
    finally:
        try:
            c.close()
        except Exception:
            pass


def wait_cluster_8184(min_nodes: int = 3, timeout: int = 900) -> tuple[str, str]:
    log(f"=== Wait for {min_nodes}+ nodes on {TARGET} ===")
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        for key in DOWNGRADE_KEYS:
            try:
                c = connect(NODES[key][0], attempts=5)
                auth = curl_elastic_auth(PWD)
                # verify password works
                out = es_api(c, auth, "GET", "/_cat/nodes?h=name,version,master&format=json")
                rows = parse_json(out) or []
                hout = es_api(c, auth, "GET", "/_cluster/health")
                health = parse_json(hout) or {}
                c.close()
                vers = {r.get("name"): r.get("version") for r in rows if isinstance(r, dict)}
                last = {
                    "via": key,
                    "versions": vers,
                    "status": health.get("status"),
                    "nodes": health.get("number_of_nodes"),
                    "uprim": health.get("unassigned_primary_shards"),
                }
                log(f"  {last}")
                on_target = [v for v in vers.values() if v == TARGET]
                if (
                    len(rows) >= min_nodes
                    and len(on_target) >= min_nodes
                    and health.get("status") in ("green", "yellow", "red")
                ):
                    # red ok pre-restore if empty
                    return auth, key
            except Exception as e:
                log(f"  wait via {key}: {e}")
        time.sleep(15)
    raise RuntimeError(f"cluster not ready: {last}")


def ensure_repo_and_restore(auth: str, via_key: str) -> str:
    log(f"=== Ensure snapshot repo + restore {SNAP_NAME} ===")
    c = connect(NODES[via_key][0], attempts=20)
    # re-register repo if needed
    print(
        es_api(
            c,
            auth,
            "PUT",
            f"/_snapshot/{SNAP_REPO}",
            {
                "type": "fs",
                "settings": {
                    "location": "/mnt/es-snapshots",
                    "compress": True,
                    "readonly": False,
                },
            },
            timeout=120,
        ),
        flush=True,
    )
    print(es_api(c, auth, "POST", f"/_snapshot/{SNAP_REPO}/_verify?pretty", timeout=180), flush=True)
    listing = es_api(
        c, auth, "GET",
        f"/_snapshot/{SNAP_REPO}/{SNAP_NAME}?pretty&filter_path=snapshots.snapshot,snapshots.state,snapshots.feature_states.feature_name",
        timeout=120,
    )
    log(listing[:1200])

    body = {
        "indices": "*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "include_aliases": True,
        "partial": False,
        "feature_states": ["*"],
    }
    log(f"restore body={json.dumps(body)}")
    # close any existing indices that block restore of system state if needed
    print(
        es_api(
            c,
            auth,
            "POST",
            f"/_snapshot/{SNAP_REPO}/{SNAP_NAME}/_restore?wait_for_completion=false&pretty",
            body,
            timeout=180,
        ),
        flush=True,
    )

    # poll recovery
    state = "IN_PROGRESS"
    for i in range(180):
        # snapshot restore status
        st = es_api(c, auth, "GET", "/_recovery?active_only=true&pretty", timeout=60)
        health = parse_json(es_api(c, auth, "GET", "/_cluster/health")) or {}
        log(
            f"  restore poll {i}: status={health.get('status')} nodes={health.get('number_of_nodes')} "
            f"uprim={health.get('unassigned_primary_shards')} reloc={health.get('relocating_shards')} "
            f"init={health.get('initializing_shards')} active_only_recovery_len={len(st)}"
        )
        if (
            health.get("status") in ("green", "yellow")
            and health.get("number_of_nodes", 0) >= 3
            and health.get("initializing_shards", 1) == 0
            and health.get("relocating_shards", 1) == 0
            and health.get("unassigned_primary_shards", 1) == 0
        ):
            state = "RESTORED_STABLE"
            break
        time.sleep(15)
    else:
        state = "RESTORE_TIMEOUT_OR_UNSTABLE"

    # remove bootstrap initial_master_nodes after restore (best-effort)
    for key in DOWNGRADE_KEYS:
        try:
            nc = connect(NODES[key][0], attempts=5)
            run(
                nc,
                "python3 - <<'PY'\n"
                "from pathlib import Path\n"
                "p=Path('/etc/elasticsearch/elasticsearch.yml')\n"
                "t=p.read_text().splitlines()\n"
                "out=[]; skip=False\n"
                "for line in t:\n"
                "    if line.strip().startswith('cluster.initial_master_nodes'):\n"
                "        skip=True\n"
                "        continue\n"
                "    if skip:\n"
                "        if line.startswith(' ') or line.startswith('\\t') or line.lstrip().startswith('-'):\n"
                "            continue\n"
                "        skip=False\n"
                "    if not skip:\n"
                "        out.append(line)\n"
                "p.write_text('\\n'.join(out)+'\\n')\n"
                "print('stripped initial_master_nodes')\n"
                "PY",
                check=False,
            )
            nc.close()
        except Exception as e:
            log(f"  strip initial_master {key}: {e}")

    c.close()
    return state


def test_es04_rejoin(auth: str) -> dict:
    log("=== Test es04 (8.19.18) rejoin to 8.18.4 cluster ===")
    result = {"started": False, "joined": False, "version": None, "errors": []}
    c4 = connect(NODES[ES04_KEY][0], attempts=20)
    print(
        run(
            c4,
            "rpm -q elasticsearch; "
            "systemctl start elasticsearch; sleep 8; systemctl is-active elasticsearch; "
            "curl -sk -m 8 https://localhost:9200/ | head -c 400 || true",
            check=False,
            timeout=180,
        ),
        flush=True,
    )
    result["started"] = True
    # wait and collect logs
    for i in range(24):
        try:
            mon = connect(NODES["es01"][0], attempts=5)
            nodes = es_api(mon, auth, "GET", "/_cat/nodes?v&h=name,version,master,node.role")
            health = es_api(mon, auth, "GET", "/_cluster/health?pretty")
            mon.close()
            log(f"  poll {i} nodes:\n{nodes[-600:]}")
            if "ismelkesnode04" in nodes and "8.19" in nodes:
                result["joined"] = True
                result["version"] = "8.19.x"
                break
            if "ismelkesnode04" in nodes and "8.18" in nodes:
                result["joined"] = True
                result["version"] = "8.18.x"
                break
        except Exception as e:
            log(f"  mon err: {e}")
        # sample es04 logs
        err = run(
            c4,
            "journalctl -u elasticsearch -n 40 --no-pager 2>/dev/null | "
            "grep -iE 'join|version|incompatible|failed|reject|illegal|handshake' | tail -20 || true",
            check=False,
            timeout=60,
        )
        if err.strip():
            log(f"  es04 join hints:\n{err[-800:]}")
            result["errors"].append(err[-500:])
        time.sleep(15)
    # full version on es04
    ver = run(c4, "rpm -q elasticsearch; curl -sk -m 5 https://localhost:9200/ | head -c 250", check=False)
    result["es04_local"] = ver[-400:]
    c4.close()
    return result


def verify_functions(auth: str) -> dict:
    log("=== Verify alerts + APM after restore ===")
    out = {
        "cluster": {},
        "nodes": [],
        "alert_rules": [],
        "apm_server": {},
        "kibana": {},
    }
    c = connect(NODES["es01"][0], attempts=20)
    try:
        auth2 = curl_elastic_auth(get_elastic_password(c))
        auth = auth2
    except Exception:
        pass
    health = parse_json(es_api(c, auth, "GET", "/_cluster/health")) or {}
    nodes = parse_json(es_api(c, auth, "GET", "/_cat/nodes?h=name,version,master,node.role&format=json")) or []
    out["cluster"] = {
        k: health.get(k)
        for k in (
            "status",
            "number_of_nodes",
            "active_primary_shards",
            "unassigned_primary_shards",
            "unassigned_shards",
        )
    }
    out["nodes"] = nodes
    c.close()

    # Kibana rules
    try:
        kb = connect(NODES["kibana"][0], attempts=30)
        # wait status
        for _ in range(40):
            code = run(
                kb,
                "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status || true",
                check=False,
            )
            if "200" in code:
                break
            time.sleep(10)
        rules = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=100")
        out["alert_rules"] = [
            {
                "name": r.get("name"),
                "type": r.get("rule_type_id"),
                "enabled": r.get("enabled"),
                "exec": (r.get("execution_status") or {}).get("status"),
            }
            for r in (rules.get("data") or [])
        ]
        # upgrade-test tagged
        out["upgrade_test_rules"] = [
            x for x in out["alert_rules"] if "upgrade" in (x.get("name") or "").lower()
            or "sample" in (x.get("name") or "").lower()
        ]
        st = kibana_curl(kb, auth, "GET", "/api/status")
        out["kibana"] = {
            "overall": ((st.get("status") or {}).get("overall") or {}).get("level"),
            "version": (st.get("version") or {}).get("number"),
        }
        kb.close()
    except Exception as e:
        out["kibana_error"] = str(e)

    # APM server on fleet
    try:
        fl = connect(NODES["fleet"][0], attempts=10)
        apm = run(
            fl,
            "systemctl is-active apm-server 2>/dev/null || echo inactive; "
            "ss -lntp | grep 8200 || true; "
            "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://127.0.0.1:8200/ || true",
            check=False,
        )
        out["apm_server"] = {"raw": apm[-400:]}
        fl.close()
    except Exception as e:
        out["apm_error"] = str(e)

    log(json.dumps(out, indent=2, default=str)[:4000])
    return out


def write_report(restore_state: str, es04: dict, functions: dict) -> None:
    lines = []
    lines.append("=" * 72)
    lines.append("ES01–ES03 RPM DOWNGRADE TO 8.18.4 + SNAPSHOT RESTORE + ES04 REJOIN TEST")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("=" * 72)
    lines.append("")
    lines.append("1) REQUIREMENTS")
    lines.append("-" * 40)
    lines.append(
        """
- Local RPM: packages/elasticsearch-8.18.4-x86_64.rpm
- Elastic snapshot repo: fs_nfs_snapshots on NFS (/mnt/es-snapshots)
- Snapshot used: pre-upgrade-system-8.18.4-with-apm-20260724
  (system indices + feature_states kibana/security/fleet/async_search + global state)
- elastic password known (secrets / /root/.elastic-stack/elastic-password)
- SSH root access to es01–es04, kibana, fleet
- Hyper-V optional rollback: pre-upgrade-system-8.18.4-with-apm / pre-upgrade-system-8.18.4-20260724
- IMPORTANT: Elasticsearch does NOT support binary downgrade on existing data files.
  Data dirs on es01–es03 MUST be wiped before starting 8.18.4; state comes back from snapshot.
- es04 left at 8.19.18 intentionally for join compatibility test.
- Kibana/Fleet stay on their current versions (8.18.x stack UI) unless separately upgraded.
""".strip()
    )
    lines.append("")
    lines.append("2) STEP PROCEDURE (EXECUTED)")
    lines.append("-" * 40)
    lines.append(
        f"""
1. Stop es04 Elasticsearch (offline during es01–03 rebuild).
2. For each of es01, es02, es03 (in order):
   a. systemctl stop elasticsearch
   b. dnf/rpm remove elasticsearch
   c. dnf install elasticsearch-8.18.4 local RPM
   d. Wipe /data/elasticsearch (and /var/lib/elasticsearch if present)
   e. Keep /etc/elasticsearch (certs, yml, path.repo, roles)
   f. Ensure discovery.seed_hosts + temporary cluster.initial_master_nodes
   g. Mount NFS snapshot path; systemctl start elasticsearch
3. Wait until 3 nodes report version {TARGET} and form a cluster.
4. PUT/verify snapshot repository {SNAP_REPO}.
5. POST _restore of {SNAP_NAME} with include_global_state=true, feature_states=['*'], indices='*'.
6. Wait until cluster status green/yellow, unassigned_primary_shards=0.
7. Strip cluster.initial_master_nodes from yml (best-effort).
8. Start es04 (still 8.19.18) and observe join success/failure + logs.
9. Verify Kibana alert rules (Observability/APM/sample) and APM server :8200.
""".strip()
    )
    lines.append("")
    lines.append("3) RESULTS")
    lines.append("-" * 40)
    lines.append(f"Restore state: {restore_state}")
    lines.append(f"es04 rejoin: {json.dumps(es04, indent=2, default=str)[:2000]}")
    lines.append(f"Functions check: {json.dumps(functions, indent=2, default=str)[:3000]}")
    lines.append("")
    lines.append("4) EXPECTED / OBSERVED COMPATIBILITY (es04 rejoin)")
    lines.append("-" * 40)
    lines.append(
        """
Elasticsearch version compatibility:
- Rolling UPGRADE: older node can temporarily join a newer cluster.
- Rolling DOWNGRADE: a NEWER node (8.19.18) generally CANNOT join an OLDER cluster (8.18.4).
  Wire compatibility rejects reverse version skew.

Therefore if es04 remains 8.19.18 after es01–03 are restored to 8.18.4:
- EXPECTED: es04 does NOT rejoin; logs show version/incompatible handshake failures.
- To have a full 4-node 8.18.4 cluster: also downgrade es04 (same wipe+restore path)
  OR restore es04 from Hyper-V snapshot pre-upgrade-system-8.18.4-*.

If the 3-node 8.18.4 cluster is healthy after snapshot restore:
- Security users/roles: restored via security feature state + .security-* indices
- Kibana saved objects + alert rules: restored via kibana feature state
- Fleet agents/policies: restored via fleet feature state (agents may need re-checkin)
- APM Server on Fleet host: independent process; config/data in ES indices restored
  if they were in the snapshot; process itself was not removed by this ES-only procedure
""".strip()
    )
    lines.append("")
    lines.append("5) FUNCTION CHECKLIST AFTER SUCCESSFUL 8.18.4 RESTORE")
    lines.append("-" * 40)
    lines.append(
        """
[ ] Cluster health yellow/green, 0 unassigned primaries, es01–03 on 8.18.4
[ ] _cat/nodes shows expected roles (mrs on es01–03; es04 only if joined)
[ ] Snapshot repo fs_nfs_snapshots still registered
[ ] Kibana /api/status overall available
[ ] Alert rules present:
    - Upgrade Testing Sample Alert (.index-threshold)
    - Obs Sample Custom/Log/Metric/Inventory thresholds
    - APM Sample Latency/Error/Failed-tx/Anomaly
[ ] APM server listening :8200 on fleet host; ES/Kibana tracing.apm / elastic.apm settings
[ ] Stack Monitoring / Fleet agents online (may take minutes after restore)
[ ] If es04 must serve data_hot: downgrade or HV-restore es04 to 8.18.4 then start
""".strip()
    )
    lines.append("")
    lines.append("6) ROLLBACK")
    lines.append("-" * 40)
    lines.append(
        """
If this experiment fails badly:
- Hyper-V restore ES VMs to post-upgrade-system-8.19.18-20260724 (current 8.19.18)
  OR pre-upgrade-system-8.18.4-with-apm for full stack at 8.18.4 with APM/alerts.
""".strip()
    )
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote report {REPORT}")


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    log("START downgrade es01-03 via RPM to 8.18.4 + snapshot restore + es04 rejoin test")

    rpm = ROOT / "packages" / f"elasticsearch-{TARGET}-x86_64.rpm"
    if not rpm.is_file() or rpm.stat().st_size < 1_000_000:
        log(f"ERROR missing {rpm}")
        return 1

    stop_es04()
    for key in DOWNGRADE_KEYS:
        downgrade_node(key)
        time.sleep(10)

    auth, via = wait_cluster_8184(min_nodes=3, timeout=1200)
    log(f"cluster auth via {via}")

    restore_state = ensure_repo_and_restore(auth, via)
    log(f"restore_state={restore_state}")

    # re-auth after restore (security may have changed to snap state)
    try:
        c = connect(NODES[via][0], attempts=20)
        auth = curl_elastic_auth(get_elastic_password(c))
        c.close()
    except Exception as e:
        log(f"re-auth note: {e}; using known password")
        auth = curl_elastic_auth(PWD)

    functions = verify_functions(auth)
    es04 = test_es04_rejoin(auth)
    # final functions after es04 attempt
    functions_after = verify_functions(auth)
    write_report(restore_state, es04, {"before_es04_start": functions, "after_es04_attempt": functions_after})

    log(
        f"\n=== DONE ===\n"
        f"restore={restore_state}\n"
        f"es04_joined={es04.get('joined')}\n"
        f"report={REPORT}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
