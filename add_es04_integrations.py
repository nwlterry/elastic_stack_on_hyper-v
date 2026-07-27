#!/usr/bin/env python3
"""Add system + elasticsearch integrations to es04 Fleet policy (ELASTIC_PASS env)."""
from __future__ import annotations

import json
import shlex

from deploy_ordered_stack import (
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

SHORT = "ismelkesnode04"
POLICY_NAME = f"Elastic-Agents-ES-{SHORT}"


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    mon_user, mon_pwd = ensure_monitoring_user(es, run, elastic_pwd)
    es.close()

    kb = connect(NODES["kibana"][0])
    copy_scripts(kb, roles=("kibana", "elastic-agent"))
    es_nodes_json = json.dumps(
        [
            {
                "fqdn": NODES["es04"][1],
                "short": SHORT,
                "policy_name": POLICY_NAME,
            }
        ]
    )
    print(
        run(
            kb,
            f"ELASTIC_PASS={shlex.quote(elastic_pwd)} "
            f"ELASTIC_PASSWORD={shlex.quote(elastic_pwd)} "
            f"MONITORING_USER={shlex.quote(mon_user)} "
            f"MONITORING_PASS={shlex.quote(mon_pwd)} "
            f"FLEET_POLICY_NAME='Fleet-Server-Policy' "
            f"ES_POLICY_NAME='Elastic-Agents-ES' "
            f"KIBANA_POLICY_NAME='Elastic-Agents-Kibana' "
            f"ES_NODES_JSON={shlex.quote(es_nodes_json)} "
            f"PHASE=agents "
            f"bash {REMOTE}/setup-fleet-kibana.sh 2>&1 | tail -80",
            timeout=900,
            check=False,
        ),
        flush=True,
    )

    policies = kibana_curl(kb, auth, "GET", "/api/fleet/agent_policies?perPage=50")
    policy_id = ""
    for p in policies.get("items") or []:
        if p.get("name") == POLICY_NAME:
            policy_id = p["id"]
            print("policy", p.get("name"), policy_id, "agents", p.get("agents"), flush=True)
    pkgs = kibana_curl(kb, auth, "GET", "/api/fleet/package_policies?perPage=200")
    for pp in pkgs.get("items") or []:
        if pp.get("policy_id") == policy_id:
            print(
                "  pkg",
                pp.get("name"),
                (pp.get("package") or {}).get("name"),
                flush=True,
            )
    agents = kibana_curl(kb, auth, "GET", "/api/fleet/agents?perPage=100")
    for a in agents.get("items") or []:
        host = ((a.get("local_metadata") or {}).get("host") or {}).get("hostname") or ""
        if SHORT in host or "esnode04" in host.lower():
            print("agent", a.get("id"), host, a.get("status"), flush=True)
    kb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
