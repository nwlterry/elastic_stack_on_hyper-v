#!/usr/bin/env python3
"""Wait for Fleet enrollment to finish, then agent policies → integrations → deploy agents."""
import sys

from deploy_ordered_stack import (
    NODES,
    connect,
    curl_elastic_auth,
    deploy_agents,
    fleet_server_is_healthy,
    get_elastic_password,
    run,
    setup_agent_policies,
    verify_stack,
    wait_fleet_server_ready,
    wait_kibana_stable,
)

EXPECTED_AGENTS = 5  # Fleet Server + 3 ES + 1 Kibana


def agents_already_deployed(elastic_pwd: str) -> bool:
    if not fleet_server_is_healthy():
        return False
    kb = connect(NODES["kibana"][0])
    auth = curl_elastic_auth(elastic_pwd)
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        "'http://127.0.0.1:5601/api/fleet/agents?perPage=20' "
        "| python3 -c \"import sys,json; d=json.load(sys.stdin); "
        "items=d.get('items',[]); "
        "print(len(items), sum(1 for a in items if a.get('active')))\"",
        check=False,
        timeout=60,
    ).strip()
    kb.close()
    parts = out.split()
    if len(parts) != 2:
        return False
    total, active = int(parts[0]), int(parts[1])
    return total >= EXPECTED_AGENTS and active >= EXPECTED_AGENTS


def main():
    kb_ip = NODES["kibana"][0]
    es_ip = NODES["es01"][0]
    fleet_ip = NODES["fleet"][0]

    c = connect(es_ip)
    elastic_pwd = get_elastic_password(c)
    ca = run(c, "cat /etc/elasticsearch/certs/http_ca.crt")
    c.close()
    print(f"elastic={elastic_pwd}", flush=True)

    if agents_already_deployed(elastic_pwd):
        print(
            f"Fleet HEALTHY and {EXPECTED_AGENTS}+ agents already enrolled — skipping redeploy.",
            flush=True,
        )
        verify_stack(elastic_pwd)
        return 0

    if not wait_kibana_stable(kb_ip, elastic_pwd=elastic_pwd):
        print("Kibana not stable", flush=True)
        return 1

    print("=== Waiting for Fleet Server enrollment to complete ===", flush=True)
    if not wait_fleet_server_ready(fleet_ip):
        print("Fleet enrollment did not complete — check /var/log/fleet-install.log", flush=True)
        return 1

    print("=== Agent policies + integrations ===", flush=True)
    agent_info = setup_agent_policies(elastic_pwd)

    print("=== Deploy agents ===", flush=True)
    deploy_agents(agent_info, ca)
    verify_stack(elastic_pwd)

    print("\n" + "=" * 60)
    print("AGENT DEPLOY COMPLETE")
    print(f"  elastic: {elastic_pwd}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())