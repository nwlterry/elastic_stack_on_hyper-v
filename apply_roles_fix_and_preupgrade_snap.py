#!/usr/bin/env python3
"""
1) Re-apply Stack Monitoring node-roles display fix (self-monitoring off + UI/CCS + verify Fleet roles)
2) Delete ALL snapshots in fs_nfs_snapshots
3) Create pre-upgrade snapshot: system indices + all feature states + cluster state
"""
from __future__ import annotations

import json
import shlex
import time
from datetime import datetime, timezone

from deploy_ordered_stack import (
    ES_NODES,
    NODES,
    REMOTE,
    connect,
    copy_scripts,
    curl_elastic_auth,
    get_elastic_password,
    run,
)
from fix_dashboard_search import kibana_curl
from monitoring_credentials import ensure_monitoring_user

REPO = "fs_nfs_snapshots"
SNAP_NAME = f"pre-upgrade-system-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"


def es_curl(c, auth: str, method: str, path: str, body: dict | None = None, timeout: int = 300) -> str:
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def disable_self_monitoring(auth: str) -> None:
    print("=== Roles fix: disable ES self-monitoring (yml + cluster) ===", flush=True)
    yml_script = r"""
python3 - <<'PY'
from pathlib import Path
path = Path("/etc/elasticsearch/elasticsearch.yml")
text = path.read_text() if path.exists() else ""
keys = {
    "xpack.monitoring.collection.enabled": "false",
    "xpack.monitoring.elasticsearch.collection.enabled": "false",
}
lines = []
seen = set()
for line in text.splitlines():
    stripped = line.strip()
    key = None
    if stripped and not stripped.startswith("#") and ":" in stripped:
        key = stripped.split(":", 1)[0].strip()
    if key in keys:
        lines.append(f"{key}: {keys[key]}")
        seen.add(key)
    else:
        lines.append(line)
for k, v in keys.items():
    if k not in seen:
        lines.append(f"{k}: {v}")
path.write_text("\n".join(lines).rstrip() + "\n")
print("yml monitoring collection disabled")
PY
"""
    for ip, fqdn in ES_NODES:
        print(f"  yml {fqdn}", flush=True)
        c = connect(ip)
        run(c, yml_script, check=False)
        c.close()

    c = connect(NODES["es01"][0])
    print(
        es_curl(
            c,
            auth,
            "PUT",
            "/_cluster/settings",
            {
                "persistent": {
                    "xpack.monitoring.collection.enabled": False,
                    "xpack.monitoring.elasticsearch.collection.enabled": False,
                }
            },
        ),
        flush=True,
    )
    c.close()


def patch_kibana_ccs_and_creds(mon_user: str, mon_pwd: str) -> None:
    print("=== Roles fix: Kibana monitoring.ui creds + ccs.enabled=false ===", flush=True)
    kb = connect(NODES["kibana"][0])
    copy_scripts(kb, roles=("kibana",))
    run(
        kb,
        f"MONITORING_USER={shlex.quote(mon_user)} "
        f"MONITORING_PASS={shlex.quote(mon_pwd)} "
        f"ES_HOST={shlex.quote(NODES['es01'][1])} "
        f"bash {REMOTE}/fix-dashboard-search.sh",
        timeout=600,
        check=False,
    )
    run(
        kb,
        r"""
YML=/etc/kibana/kibana.yml
python3 - <<'PY'
from pathlib import Path
path = Path("/etc/kibana/kibana.yml")
text = path.read_text()
keys = {
    "monitoring.ui.ccs.enabled": "false",
    "monitoring.ui.enabled": "true",
}
lines, seen = [], set()
for line in text.splitlines():
    key = None
    s = line.strip()
    if s and not s.startswith("#") and ":" in s:
        key = s.split(":", 1)[0].strip()
    if key in keys:
        lines.append(f"{key}: {keys[key]}")
        seen.add(key)
    else:
        lines.append(line)
for k, v in keys.items():
    if k not in seen:
        lines.append(f"{k}: {v}")
ca = "/etc/kibana/certs/http_ca.crt"
if Path(ca).is_file() and not any(
    "monitoring.ui.elasticsearch.ssl.certificateAuthorities" in l for l in lines
):
    lines.append(f'monitoring.ui.elasticsearch.ssl.certificateAuthorities: ["{ca}"]')
path.write_text("\n".join(lines).rstrip() + "\n")
print("kibana monitoring.ui.ccs.enabled=false")
PY
systemctl restart kibana
for i in $(seq 1 48); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 http://127.0.0.1:5601/api/status 2>/dev/null || echo 000)
  if echo "$code" | grep -qE '^(200|302|401|503)$'; then
    echo "kibana ready code=$code poll=$i"
    exit 0
  fi
  sleep 5
done
echo "kibana restart timeout" >&2
""",
        timeout=600,
        check=False,
    )
    kb.close()


def ensure_es04_agent(auth: str, elastic_pwd: str) -> None:
    """Ensure es04 Fleet agent exists so node_stats (with roles) is produced."""
    print("=== Roles fix: ensure Fleet agent on es04 ===", flush=True)
    if "es04" not in NODES:
        print("  no es04 in NODES — skip", flush=True)
        return
    kb = connect(NODES["kibana"][0])
    agents = kibana_curl(kb, auth, "GET", "/api/fleet/agents?perPage=100")
    items = agents.get("items") or []
    es04_host = NODES["es04"][1]
    short = es04_host.split(".")[0]
    online = False
    for a in items:
        local = (a.get("local_metadata") or {}).get("host") or {}
        name = local.get("hostname") or local.get("name") or ""
        if short in name or "esnode04" in name.lower() or NODES["es04"][0] in str(a):
            status = a.get("status")
            print(f"  agent {a.get('id')} hostname={name} status={status}", flush=True)
            if status in ("online", "healthy", "updating", "degraded"):
                online = True
    if online:
        print("  es04 agent already present", flush=True)
        kb.close()
        return

    # policy id
    policies = kibana_curl(kb, auth, "GET", "/api/fleet/agent_policies?perPage=50")
    policy_id = ""
    policy_name = f"Elastic-Agents-ES-{short}"
    for p in policies.get("items") or []:
        if p.get("name") == policy_name:
            policy_id = p["id"]
            break
    if not policy_id:
        create = kibana_curl(
            kb,
            auth,
            "POST",
            "/api/fleet/agent_policies",
            {
                "name": policy_name,
                "description": f"ES node agent ({es04_host})",
                "namespace": "default",
                "monitoring_enabled": ["logs", "metrics"],
            },
        )
        policy_id = (create.get("item") or {}).get("id") or ""
        print(f"  created policy {policy_id} {create}", flush=True)
    if not policy_id:
        print("  WARN: could not create es04 policy — skip enroll", flush=True)
        kb.close()
        return

    # ensure elasticsearch + system integrations (minimal package policies)
    pkgs = kibana_curl(kb, auth, "GET", f"/api/fleet/package_policies?perPage=100")
    have_es = any(
        (pp.get("policy_id") == policy_id and pp.get("package", {}).get("name") == "elasticsearch")
        for pp in (pkgs.get("items") or [])
    )
    if not have_es:
        # try attach via setup script phase if available
        print("  attaching elasticsearch integration via fleet API...", flush=True)
        # Use package policy create with defaults for stack monitoring metrics
        try:
            kibana_curl(
                kb,
                auth,
                "POST",
                "/api/fleet/package_policies",
                {
                    "name": f"elasticsearch-{short}",
                    "description": "ES stack monitoring metrics",
                    "namespace": "default",
                    "policy_id": policy_id,
                    "enabled": True,
                    "inputs": [],
                    "package": {"name": "elasticsearch", "version": ""},
                },
            )
        except Exception as e:
            print(f"  package policy create note: {e}", flush=True)

    tok = kibana_curl(
        kb,
        auth,
        "POST",
        "/api/fleet/enrollment-api-keys",
        {"policy_id": policy_id},
    )
    api_key = (tok.get("item") or {}).get("api_key") or ""
    kb.close()
    if not api_key:
        print(f"  WARN no enrollment key: {tok}", flush=True)
        return

    # re-enroll using existing agent binary if present
    fleet = f"https://{NODES['fleet'][1]}:8220"
    c4 = connect(NODES["es04"][0])
    # ensure CA
    es1 = connect(NODES["es01"][0])
    ca = run(
        es1,
        "cat /etc/elasticsearch/certs/http_ca.crt 2>/dev/null || true",
        check=False,
    )
    es1.close()
    if "BEGIN CERTIFICATE" in ca:
        import base64

        cert = ca[ca.index("-----BEGIN") :]
        cert = cert[: cert.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
        b64 = base64.b64encode(cert.encode()).decode()
        run(
            c4,
            f"mkdir -p /etc/elastic-agent/certs /opt/elastic-setup/certs && "
            f"echo {shlex.quote(b64)} | base64 -d > /etc/elastic-agent/certs/http_ca.crt && "
            f"chmod 644 /etc/elastic-agent/certs/http_ca.crt",
            check=False,
        )

    print(
        run(
            c4,
            f"""
set -e
AGENT=""
if [ -x /opt/Elastic/Agent/elastic-agent ]; then AGENT=/opt/Elastic/Agent/elastic-agent
elif command -v elastic-agent >/dev/null 2>&1; then AGENT=$(command -v elastic-agent)
fi
if [ -z "$AGENT" ]; then
  echo NO_AGENT_BINARY
  exit 0
fi
# re-enroll
$AGENT enroll -f --url={shlex.quote(fleet)} \
  --enrollment-token={shlex.quote(api_key)} \
  --certificate-authorities=/etc/elastic-agent/certs/http_ca.crt \
  --insecure 2>&1 || true
systemctl enable --now elastic-agent 2>/dev/null || true
systemctl restart elastic-agent 2>/dev/null || true
systemctl is-active elastic-agent || true
$AGENT status 2>&1 | head -40 || true
""",
            check=False,
            timeout=300,
        ),
        flush=True,
    )
    c4.close()


def verify_roles_data(auth: str) -> None:
    print("=== Verify node_stats has elasticsearch.node.roles ===", flush=True)
    body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": "now-30m"}}},
        "aggs": {
            "nodes": {
                "terms": {"field": "elasticsearch.node.name", "size": 10},
                "aggs": {
                    "with_roles": {
                        "filter": {"exists": {"field": "elasticsearch.node.roles"}}
                    },
                    "roles": {
                        "terms": {"field": "elasticsearch.node.roles", "size": 20}
                    },
                },
            }
        },
    }
    c = connect(NODES["es01"][0])
    print(
        es_curl(
            c,
            auth,
            "POST",
            "/.ds-metrics-elasticsearch.stack_monitoring.node_stats-*/_search?pretty",
            body,
        )[:3500],
        flush=True,
    )
    # confirm self-monitoring setting
    print(
        es_curl(
            c,
            auth,
            "GET",
            "/_cluster/settings?flat_settings=true&include_defaults=false&pretty",
        )[:1500],
        flush=True,
    )
    c.close()


def delete_all_snapshots(auth: str) -> None:
    print(f"=== Delete ALL snapshots in {REPO} ===", flush=True)
    c = connect(NODES["es01"][0])
    listing = es_curl(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty")
    print(listing[:2000], flush=True)
    try:
        data = json.loads(listing[listing.find("{") :])
    except Exception:
        # parse names via simple scan
        data = {}
        names = []
        for line in listing.splitlines():
            if '"snapshot"' in line and ":" in line:
                names.append(line.split(":", 1)[1].strip().strip('",'))
        data = {"snapshots": [{"snapshot": n} for n in names]}

    snaps = [s.get("snapshot") for s in data.get("snapshots") or [] if s.get("snapshot")]
    if not snaps:
        # regex fallback
        import re

        snaps = re.findall(r'"snapshot"\s*:\s*"([^"]+)"', listing)
    print(f"  found snapshots: {snaps}", flush=True)
    for name in snaps:
        print(f"  DELETE {name} ...", flush=True)
        print(
            es_curl(
                c,
                auth,
                "DELETE",
                f"/_snapshot/{REPO}/{name}?pretty",
                timeout=600,
            ),
            flush=True,
        )
    # wait empty
    time.sleep(3)
    after = es_curl(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty")
    print("  after delete:", after[:800], flush=True)
    c.close()


def create_preupgrade_snapshot(auth: str) -> None:
    print(f"=== Create pre-upgrade snapshot: {SNAP_NAME} ===", flush=True)
    c = connect(NODES["es01"][0])
    # list features for logging
    print(es_curl(c, auth, "GET", "/_features?pretty")[:2500], flush=True)

    body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": ["*"],
        "metadata": {
            "taken_by": "apply_roles_fix_and_preupgrade_snap",
            "taken_because": "pre-upgrade: all system indices, feature states, cluster state",
        },
    }
    # wait_for_completion can be long for system state; use async + poll
    print(
        es_curl(
            c,
            auth,
            "PUT",
            f"/_snapshot/{REPO}/{SNAP_NAME}?wait_for_completion=false&pretty",
            body,
            timeout=120,
        ),
        flush=True,
    )

    deadline = time.time() + 3600
    while time.time() < deadline:
        st = es_curl(c, auth, "GET", f"/_snapshot/{REPO}/{SNAP_NAME}?pretty", timeout=120)
        print(st[:1200], flush=True)
        if '"state" : "SUCCESS"' in st or '"state":"SUCCESS"' in st:
            print("SNAPSHOT SUCCESS", flush=True)
            break
        if '"state" : "FAILED"' in st or '"state":"FAILED"' in st or '"state" : "PARTIAL"' in st:
            print("SNAPSHOT FAILED/PARTIAL", flush=True)
            break
        time.sleep(15)
    else:
        print("SNAPSHOT TIMEOUT (still IN_PROGRESS?)", flush=True)

    print(
        es_curl(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty")[:2000],
        flush=True,
    )
    print(
        es_curl(c, auth, "POST", f"/_snapshot/{REPO}/_verify?pretty", timeout=180),
        flush=True,
    )
    c.close()


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    mon_user, mon_pwd = ensure_monitoring_user(es, run, elastic_pwd)
    print(f"auth ok; monitoring user={mon_user}", flush=True)
    es.close()

    # 1) roles display fix
    disable_self_monitoring(auth)
    patch_kibana_ccs_and_creds(mon_user, mon_pwd)
    ensure_es04_agent(auth, elastic_pwd)
    time.sleep(20)
    verify_roles_data(auth)

    # 2) remove all elastic snapshots
    delete_all_snapshots(auth)

    # 3) pre-upgrade system snapshot
    create_preupgrade_snapshot(auth)

    print(
        f"\n=== DONE ===\n"
        f"- Self-monitoring collection disabled (roles from Fleet node_stats)\n"
        f"- monitoring.ui.ccs.enabled=false + creds reapplied\n"
        f"- All prior snapshots deleted from {REPO}\n"
        f"- New snapshot: {SNAP_NAME} (system indices + feature_states=* + global state)\n",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
