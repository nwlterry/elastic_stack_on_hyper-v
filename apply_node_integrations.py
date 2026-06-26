#!/usr/bin/env python3
"""
Apply node-role Fleet integrations (Linux system, Elasticsearch, Kibana) to agent
policies and trigger enrolled agents to pick up the updated policy.

Uses stored elastic + monitoring credentials (secrets/) — never resets elastic password.
Run directly: python apply_node_integrations.py
"""
from __future__ import annotations

import json
import shlex
import time

from deploy_ordered_stack import (
    ES_NODES,
    ES_POLICY_NAME,
    KIBANA_POLICY_NAME,
    NODES,
    _run_fleet_setup,
    connect,
    curl_elastic_auth,
    ensure_fleet_epr_ready,
    get_elastic_password,
    run,
    wait_kibana_stable,
)
from monitoring_credentials import ensure_monitoring_user

KIBANA_POLICY_ID = "3b226858-3140-4a6b-b044-05dc7819a338"
LEGACY_ES_POLICY_ID = "f9b17f0b-f0d4-42ad-8761-2bdec42f4588"


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


def resolve_policy_map(kb, elastic_pwd: str) -> tuple[dict[str, str], str | None]:
    """Map ES node short hostname -> policy id; return kibana policy id."""
    data = fleet_api(kb, elastic_pwd, "GET", "/api/fleet/agent_policies?perPage=100")
    es_map: dict[str, str] = {}
    kibana_id: str | None = None
    prefix = f"{ES_POLICY_NAME}-"
    for policy in data.get("items", []):
        name = policy.get("name", "")
        pid = policy.get("id")
        if not pid:
            continue
        if name == KIBANA_POLICY_NAME:
            kibana_id = pid
        elif name.startswith(prefix):
            es_map[name[len(prefix) :]] = pid
        elif name == ES_POLICY_NAME:
            es_map["_legacy"] = pid
    return es_map, kibana_id


def required_integrations(es_policy_map: dict[str, str], kibana_policy_id: str | None) -> dict[str, set[str]]:
    required: dict[str, set[str]] = {}
    for short, pid in es_policy_map.items():
        if short == "_legacy":
            continue
        required[pid] = {"system", "elasticsearch"}
    if kibana_policy_id:
        required[kibana_policy_id] = {"system", "kibana"}
    return required


def integrations_have_streams(kb, elastic_pwd: str, policy_ids: set[str] | None = None) -> bool:
    for pkg in get_package_policies(kb, elastic_pwd):
        pid = pkg.get("policy_id")
        if policy_ids and pid not in policy_ids:
            continue
        pname = pkg.get("package", {}).get("name")
        if pname not in ("system", "elasticsearch", "kibana"):
            continue
        for inp in pkg.get("inputs", []):
            if not inp.get("enabled", True):
                continue
            if not inp.get("streams"):
                print(
                    f"  policy {pid} {pname}/{inp.get('type')} has empty streams",
                    flush=True,
                )
                return False
    return True


def integrations_satisfied(kb, elastic_pwd: str, required: dict[str, set[str]]) -> bool:
    by_policy: dict[str, set[str]] = {pid: set() for pid in required}
    for pkg in get_package_policies(kb, elastic_pwd):
        pid = pkg.get("policy_id")
        name = pkg.get("package", {}).get("name")
        if pid in by_policy and name:
            by_policy[pid].add(name)
    for pid, need in required.items():
        have = by_policy.get(pid, set())
        if not need.issubset(have):
            print(f"  policy {pid}: have={sorted(have)} need={sorted(need)}", flush=True)
            return False
    if not integrations_have_streams(kb, elastic_pwd, set(required)):
        return False
    return True


def integration_hosts_use_fqdn(
    kb,
    elastic_pwd: str,
    es_policy_map: dict[str, str],
    kibana_policy_id: str | None,
    active_policy_ids: set[str] | None = None,
) -> bool:
    kibana_fqdn = NODES["kibana"][1]
    fqdn_by_short = {fqdn.split(".")[0]: fqdn for _, fqdn in ES_NODES}
    for pkg in get_package_policies(kb, elastic_pwd):
        pid = pkg.get("policy_id")
        if active_policy_ids and pid not in active_policy_ids:
            continue
        pname = pkg.get("package", {}).get("name")
        inputs = pkg.get("inputs") or []
        if not inputs:
            continue
        vars_body = inputs[0].get("vars") or {}
        hosts = vars_body.get("hosts", {}).get("value") or []
        host = hosts[0] if hosts else ""
        if pname == "elasticsearch" and pid in es_policy_map.values():
            short = next((s for s, p in es_policy_map.items() if p == pid and s != "_legacy"), None)
            if short and fqdn_by_short.get(short) not in host:
                print(f"  elasticsearch integration on {pid} host={host!r} want FQDN", flush=True)
                return False
            if not vars_body.get("username", {}).get("value"):
                print(f"  elasticsearch integration on {pid} missing monitoring username", flush=True)
                return False
        if pname == "kibana" and pid == kibana_policy_id:
            if kibana_fqdn not in host:
                print(f"  kibana integration host={host!r} want {kibana_fqdn}", flush=True)
                return False
            if not vars_body.get("username", {}).get("value"):
                print(f"  kibana integration missing monitoring username", flush=True)
                return False
    return True


def reassign_es_agents(kb, elastic_pwd: str, es_policy_map: dict[str, str]) -> None:
    data = fleet_api(kb, elastic_pwd, "GET", "/api/fleet/agents?perPage=50")
    for agent in data.get("items", []):
        host = agent.get("local_metadata", {}).get("host", {}).get("name", "")
        short = host.split(".")[0] if host else ""
        target = es_policy_map.get(short)
        if not target or agent.get("policy_id") == target:
            continue
        aid = agent.get("id")
        if not aid:
            continue
        fleet_api(kb, elastic_pwd, "POST", f"/api/fleet/agents/{aid}/reassign", {"policy_id": target})
        print(f"  reassigned {host} -> policy {target}", flush=True)


def bump_agent_policies(kb, elastic_pwd: str, policy_ids: list[str]) -> None:
    for policy_id in policy_ids:
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


def expected_policy_revisions(kb, elastic_pwd: str, policy_ids: list[str]) -> dict[str, int]:
    revisions: dict[str, int] = {}
    for policy_id in policy_ids:
        detail = fleet_api(kb, elastic_pwd, "GET", f"/api/fleet/agent_policies/{policy_id}")
        rev = detail.get("item", {}).get("revision")
        if rev is not None:
            revisions[policy_id] = int(rev)
    return revisions


def agents_synced(kb, elastic_pwd: str, policy_ids: list[str]) -> bool:
    auth = curl_elastic_auth(elastic_pwd)
    expected = expected_policy_revisions(kb, elastic_pwd, policy_ids)
    if not expected:
        return False
    py = (
        "import sys,json; "
        f"expected={json.dumps(expected)}; "
        "d=json.load(sys.stdin); "
        f"ids=set({json.dumps(list(expected))}); "
        "items=[a for a in d.get('items',[]) if a.get('policy_id') in ids]; "
        "synced=sum(1 for a in items "
        "if a.get('last_checkin_status')=='online' "
        "and a.get('policy_revision',0)>=expected.get(a.get('policy_id'),0)); "
        "print(len(items), synced)"
    )
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/agents?perPage=20' | "
        f"python3 -c {shlex.quote(py)}",
        check=False,
        timeout=60,
    ).strip()
    parts = out.split()
    if len(parts) == 2:
        total, synced = map(int, parts)
        return total >= 4 and synced >= 4
    return False


def wait_agents_policy_sync(kb, elastic_pwd: str, policy_ids: list[str], max_polls: int = 24) -> bool:
    auth = curl_elastic_auth(elastic_pwd)
    expected = expected_policy_revisions(kb, elastic_pwd, policy_ids)
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


def fleet_api_authenticated(kb, elastic_pwd: str) -> bool:
    data = fleet_api(kb, elastic_pwd, "GET", "/api/fleet/agents?perPage=1")
    return "items" in data and "statusCode" not in data


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
            f"policy={{p.get('policy_id')[:8]}}... hosts={{(p.get('inputs',[{{}}])[0].get('vars',{{}}).get('hosts',{{}}).get('value') or ['?'])[0]}} "
            f"user={{p.get('inputs',[{{}}])[0].get('vars',{{}}).get('username',{{}}).get('value','-')}}\\\") "
            f"for p in d.get('items',[]) if p.get('package',{{}}).get('name') in ('system','elasticsearch','kibana')]\"",
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
            f"policy={{a.get('policy_id')[:8]}}... rev={{a.get('policy_revision')}} status={{a.get('status')}}\\\") "
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
    monitoring_user, monitoring_pass = ensure_monitoring_user(es, run, elastic_pwd)
    es.close()
    print(f"monitoring user: {monitoring_user} (password in secrets/monitoring-password)", flush=True)

    if not wait_kibana_stable(kb_ip, elastic_pwd=elastic_pwd):
        print("Kibana not stable", flush=True)
        return 1

    kb = connect(kb_ip)
    if not fleet_api_authenticated(kb, elastic_pwd):
        kb.close()
        print("Fleet API authentication failed — check secrets/elastic-password", flush=True)
        return 1

    es_policy_map, kibana_policy_id = resolve_policy_map(kb, elastic_pwd)
    if not kibana_policy_id:
        kibana_policy_id = KIBANA_POLICY_ID
    required = required_integrations(es_policy_map, kibana_policy_id)
    if not required:
        for _, fqdn in ES_NODES:
            required[LEGACY_ES_POLICY_ID] = {"system", "elasticsearch"}
        required[kibana_policy_id] = {"system", "kibana"}

    active_ids = set(required)
    needs_update = (
        not integrations_satisfied(kb, elastic_pwd, required)
        or not integration_hosts_use_fqdn(
            kb, elastic_pwd, es_policy_map, kibana_policy_id, active_ids
        )
        or "_legacy" in es_policy_map
        or len([k for k in es_policy_map if k != "_legacy"]) < len(ES_NODES)
    )

    if not needs_update and agents_synced(kb, elastic_pwd, list(active_ids)):
        print("=== Integrations, FQDN endpoints, and agents already in sync ===", flush=True)
        print_summary(kb, elastic_pwd)
        kb.close()
        print("\nNODE INTEGRATIONS APPLIED", flush=True)
        return 0
    kb.close()

    print("=== Stage EPR packages + air-gap ===", flush=True)
    ensure_fleet_epr_ready(elastic_pwd)

    print("=== Create/update agent policies + integrations ===", flush=True)
    result = _run_fleet_setup(
        elastic_pwd,
        "agents",
        monitoring_user=monitoring_user,
        monitoring_pass=monitoring_pass,
    )
    for k, v in sorted(result.items()):
        if k.startswith("INTEGRATION_"):
            print(f"  {k}={v}", flush=True)

    kb = connect(kb_ip)
    es_policy_map, kibana_policy_id = resolve_policy_map(kb, elastic_pwd)
    if not kibana_policy_id:
        kibana_policy_id = KIBANA_POLICY_ID
    required = required_integrations(es_policy_map, kibana_policy_id)
    if not required:
        required[kibana_policy_id] = {"system", "kibana"}

    print("=== Reassign ES agents to per-node policies ===", flush=True)
    reassign_es_agents(kb, elastic_pwd, es_policy_map)

    if not integrations_satisfied(kb, elastic_pwd, required):
        print("Integrations still missing after setup", flush=True)
        print_summary(kb, elastic_pwd)
        kb.close()
        return 1

    policy_ids = list(required.keys())
    print("=== Bump agent policies + restart agents ===", flush=True)
    bump_agent_policies(kb, elastic_pwd, policy_ids)
    kb.close()
    restart_agents_on_nodes(elastic_pwd)

    kb = connect(kb_ip)
    wait_agents_policy_sync(kb, elastic_pwd, policy_ids)
    es_policy_map, kibana_policy_id = resolve_policy_map(kb, elastic_pwd)
    required = required_integrations(es_policy_map, kibana_policy_id or KIBANA_POLICY_ID)
    ok = integrations_satisfied(kb, elastic_pwd, required) and integration_hosts_use_fqdn(
        kb,
        elastic_pwd,
        es_policy_map,
        kibana_policy_id or KIBANA_POLICY_ID,
        set(required),
    )
    print_summary(kb, elastic_pwd)
    kb.close()

    if ok:
        print("\nNODE INTEGRATIONS APPLIED", flush=True)
        return 0
    print("\nNODE INTEGRATIONS INCOMPLETE — check INTEGRATION_WARN above", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())