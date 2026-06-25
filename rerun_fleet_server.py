#!/usr/bin/env python3
"""
Phased Fleet deploy:
  1. Kibana stable → Fleet Server policy + service token
  2. Archive install Fleet Server → wait enrollment (8220)
  3. Agent policies + integrations (system, ES, Kibana)
  4. Deploy agents on ES + Kibana nodes
"""
import sys

from deploy_ordered_stack import (
    NODES,
    connect,
    create_service_token,
    deploy_agents,
    deploy_fleet_server,
    fleet_server_is_healthy,
    get_elastic_password,
    setup_agent_policies,
    setup_fleet_server_policy,
    verify_stack,
    wait_kibana_stable,
)


def main():
    if fleet_server_is_healthy(NODES["fleet"][0]):
        print(
            "Fleet Server already HEALTHY on :8220 — skipping destructive rerun.",
            flush=True,
        )
        print("Use redeploy_fleet_only.py if you need a clean Fleet redeploy.", flush=True)
        return 0

    kb_ip = NODES["kibana"][0]
    es_ip = NODES["es01"][0]

    c = connect(es_ip)
    elastic_pwd = get_elastic_password(c)
    c.close()
    print(f"elastic={elastic_pwd}", flush=True)

    print("=== Step 1: Kibana stable ===", flush=True)
    if not wait_kibana_stable(kb_ip, elastic_pwd=elastic_pwd):
        print("Kibana must be stable before Fleet Server setup", flush=True)
        return 1

    print("=== Step 2: Fleet Server policy only (no agent policies yet) ===", flush=True)
    fleet_policy = setup_fleet_server_policy(elastic_pwd)
    svc_token, ca = create_service_token(elastic_pwd)

    print("=== Step 3: Fleet Server archive install + enrollment ===", flush=True)
    if not deploy_fleet_server(fleet_policy["FLEET_POLICY_ID"], svc_token, ca):
        print("Fleet Server enrollment failed — check /var/log/fleet-install.log", flush=True)
        return 1

    print("=== Step 4: Agent policies + integrations (after Fleet up) ===", flush=True)
    agent_info = setup_agent_policies(elastic_pwd)

    print("=== Step 5: Deploy agents ===", flush=True)
    deploy_agents(agent_info, ca)
    verify_stack(elastic_pwd)

    print("\n" + "=" * 60)
    print("PHASED FLEET DEPLOY COMPLETE")
    print("  Fleet:  https://ismelkflnode01.ocplab.net:8220")
    print("  Kibana: http://10.44.40.41:5601")
    print(f"  elastic: {elastic_pwd}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())