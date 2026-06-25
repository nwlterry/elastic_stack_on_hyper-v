#!/usr/bin/env python3
"""Resume ordered deploy from Fleet policies (ES + Kibana already up)."""
import sys

from deploy_ordered_stack import (
    create_service_token,
    deploy_agents,
    deploy_fleet_server,
    get_elastic_password,
    connect,
    NODES,
    setup_fleet_policies,
    verify_stack,
    wait_kibana_ready,
)

def main():
    c = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(c)
    c.close()

    if not wait_kibana_ready(NODES["kibana"][0]):
        print("Kibana not ready")
        return 1

    fleet_info = setup_fleet_policies(elastic_pwd)
    svc_token, ca = create_service_token(elastic_pwd)

    if not deploy_fleet_server(fleet_info["FLEET_POLICY_ID"], svc_token, ca):
        print("Fleet Server did not start")
        return 1

    deploy_agents(fleet_info)
    verify_stack(elastic_pwd)
    print(f"\nRESUME COMPLETE\n  elastic: {elastic_pwd}")
    return 0

if __name__ == "__main__":
    sys.exit(main())