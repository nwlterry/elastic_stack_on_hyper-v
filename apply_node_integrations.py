#!/usr/bin/env python3
"""
Apply node-role Fleet integrations (Linux system, Elasticsearch, Kibana) to agent
policies and trigger enrolled agents to pick up the updated policy.
"""
from __future__ import annotations

import json
import shlex
import time

from deploy_ordered_stack import (
    ES_NODES,
    NODES,
    _run_fleet_setup,
    connect,
    curl_elastic_auth,
    ensure_fleet_epr_ready,
    get_elastic_password,
    run,
    wait_kibana_stable,
)

ES_POLICY_ID = "f9b17f0b-f0d4-42ad-8761-2bdec42f4588"
KIBANA_POLICY_ID = "3b226858-3140-4a6b-b044-05dc7819a338"
REQUIRED_INTEGRATIONS = {
    ES_POLICY_ID: {"system", "elasticsearch"},
    KIBANA_POLICY_ID: {"system", "kibana"},
}


def fleet_api(kb, elastic_pwd: str, method: str, path: str, body: dict | None = None) -> dict:
    auth = curl_elastic_auth(elastic_pwd)
    if method == "GET":
        cmd = (
            f"curl -s -u {auth} -H 'kbn-xsrf:true' "
            f"'http://127.0.0.1:5601{path}'"
        )
    else:
        payload = json.dumps(body) if body is not None else "{}"
        cmd = (
            f"curl -s -X {method} -u {auth} -H 'kbn-xsrf:true' -H 'Content-Type: application/json' "
            f"-d {shlex.quote(payload)} "
            f"'http://127.0.0.1:5601{path}'"
        )
    out = run(kb, cmd, check=False, timeout=120).strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def get_package_policies(kb, elastic_pwd: str) -> list[dict]:
    data = fleet_api(kb, elastic_pwd, "GET", "/api/fleet/package_policies?perPage=100")
    return data.get("items", [])


def integrations_satisfied(kb, elastic_pwd: str) -> bool:
    by_policy: dict[str, set[str]] = {pid: set() for pid in REQUIRED_INTEGRATIONS}
    for pkg in get_package_policies(kb, elastic_pwd):
        pid = pkg.get("policy_id")
        name = pkg.get("package", {}).get("name")
        if pid in by_policy and name:
            by_policy[pid].add(name)
    for pid, required in REQUIRED_INTEGRATIONS.items():
        have = by_policy.get(pid, set())
        if not required.issubset(have):
            print(f"  policy {pid}: have={sorted(have)} need={sorted(required)}", flush=True)
            return False
    return True


def bump_agent_policies(kb, elastic_pwd: str) -> None:
    """Touch agent policies so Fleet bumps revision and redeploys to agents."""
    for policy_id in (ES_POLICY_ID, KIBANA_POLICY_ID):
        detail = fleet_api(kb, elastic_pwd, "GET", f"/api/fleet/agent_policies/{policy_id}")
        item = detail.get("item", {})
        if not item:
            continue
        body = {
            "name": item.get("name"),
            "description": item.get("description") or "Node monitoring integrations",
            "namespace": item.get("namespace", "default"),
            "monitoring_enabled": item.get("monitoring_enabled", ["logs", "metrics"]),
        }
        fleet_api(kb, elastic_pwd, "PUT", f"/api/fleet/agent_policies/{policy_id}", body)
        print(f"  bumped policy {item.get('name')} ({policy_id})", flush=True)


def restart_agents_on_nodes(elastic_pwd: str) -> None:
    """Restart elastic-agent on ES/Kibana nodes to apply policy immediately."""
    targets = list(ES_NODES) + [NODES["kibana"]]
    for ip, fqdn in targets:
        c = connect(ip)
        out = run(
            c,
            "systemctl restart elastic-agent; sleep 3; elastic-agent status 2>&1 | head -30",
            check=False,
            timeout=120,
        )
        c.close()
        print(f"  restarted agent on {fqdn}", flush=True)
        if "HEALTHY" not in out and "Failed" in out:
            print(out[-800:], flush=True)


def expected_policy_revisions(kb, elastic_pwd: str) -> dict[str, int]:
    revisions: dict[str, int] = {}
    for policy_id in (ES_POLICY_ID, KIBANA_POLICY_ID):
        detail = fleet_api(kb, elastic_pwd, "GET", f"/api/fleet/agent_policies/{policy_id}")
        rev = detail.get("item", {}).get("revision")
        if rev is not None:
            revisions[policy_id] = int(rev)
    return revisions


def wait_agents_policy_sync(kb, elastic_pwd: str, max_polls: int = 24) -> bool:
    auth = curl_elastic_auth(elastic_pwd)
    expected = expected_policy_revisions(kb, elastic_pwd)
    py = (
        "import sys,json; "
        f"expected={json.dumps(expected)}; "
        "d=json.load(sys.stdin); "
        f"ids=set({json.dumps(list(expected))}); "
        "items=[a for a in d.get('items',[]) if a.get('policy_id') in ids]; "
        "online=sum(1 for a in items if a.get('last_checkin_status')=='online'); "
        "synced=sum(1 for a in items "
        "if a.get('last_checkin_status')=='online' "
        "and a.get('policy_revision',0)>=expected.get(a.get('policy_id'),0)); "
        "print(len(items), online, synced)"
    )
    for i in range(max_polls):
        out = run(
            kb,
            f"curl -s -u {auth} -H 'kbn-xsrf:true' "
            f"'http://127.0.0.1:5601/api/fleet/agents?perPage=20' | "
            f"python3 -c {shlex.quote(py)}",
            check=False,
            timeout=60,
        ).strip()
        parts = out.split()
        if len(parts) == 3:
            total, online, synced = map(int, parts)
            print(
                f"  agent sync poll {i}: agents={total} online={online} "
                f"synced={synced} expected={expected}",
                flush=True,
            )
            if total >= 4 and synced >= 4:
                return True
        time.sleep(15)
    return False


def print_summary(kb, elastic_pwd: str) -> None:
    auth = curl_elastic_auth(elastic_pwd)
    print("\n=== Package policies ===", flush=True)
    print(
        run(
            kb,
            f"curl -s -u {auth} -H 'kbn-xsrf:true' "
            f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' | "
            f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
            f"[print(f\\\"{{p.get('package',{{}}).get('name')}}@{{p.get('package',{{}}).get('version')}} "
            f"policy={{p.get('policy_id')[:8]}}... enabled={{p.get('enabled')}}\\\") "
            f"for p in d.get('items',[])]\"",
            check=False,
            timeout=60,
        ),
        flush=True,
    )
    print("\n=== Agents ===", flush=True)
    print(
        run(
            kb,
            f"curl -s -u {auth} -H 'kbn-xsrf:true' "
            f"'http://127.0.0.1:5601/api/fleet/agents?perPage=20' | "
            f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
            f"[print(f\\\"{{a.get('local_metadata',{{}}).get('host',{{}}).get('name','?')}} "
            f"policy_rev={{a.get('policy_revision')}} status={{a.get('status')}}\\\") "
            f"for a in d.get('items',[])]\"",
            check=False,
            timeout=60,
        ),
        flush=True,
    )


def main() -> int:
    kb_ip = NODES["kibana"][0]
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()
    print("elastic password loaded from stored credentials", flush=True)

    if not wait_kibana_stable(kb_ip, elastic_pwd=elastic_pwd):
        print("Kibana not stable", flush=True)
        return 1

    print("=== Stage EPR packages + air-gap ===", flush=True)
    ensure_fleet_epr_ready(elastic_pwd)

    kb = connect(kb_ip)
    if not integrations_satisfied(kb, elastic_pwd):
        print("=== Create agent policies + integrations ===", flush=True)
        result = _run_fleet_setup(elastic_pwd, "agents")
        for k, v in sorted(result.items()):
            if k.startswith("INTEGRATION_"):
                print(f"  {k}={v}", flush=True)
        kb.close()
        kb = connect(kb_ip)
        if not integrations_satisfied(kb, elastic_pwd):
            print("Integrations still missing after setup", flush=True)
            print_summary(kb, elastic_pwd)
            kb.close()
            return 1
    else:
        print("=== Integrations already present ===", flush=True)

    print("=== Bump agent policies + restart agents ===", flush=True)
    bump_agent_policies(kb, elastic_pwd)
    kb.close()
    restart_agents_on_nodes(elastic_pwd)

    kb = connect(kb_ip)
    wait_agents_policy_sync(kb, elastic_pwd)
    kb.close()

    # Spot-check one ES node for system/elasticsearch inputs
    c = connect(NODES["es01"][0])
    status = run(c, "elastic-agent status 2>&1", check=False, timeout=60)
    c.close()
    print("\n=== ES01 agent status ===", flush=True)
    print(status, flush=True)

    kb = connect(kb_ip)
    satisfied = integrations_satisfied(kb, elastic_pwd)
    print_summary(kb, elastic_pwd)
    kb.close()

    if satisfied:
        print("\nNODE INTEGRATIONS APPLIED", flush=True)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())