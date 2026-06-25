#!/usr/bin/env python3
"""Redeploy Fleet Server only (CA + archive). Policy integration must exist in Kibana."""
import sys

from deploy_ordered_stack import (
    NODES,
    connect,
    create_service_token,
    deploy_fleet_server,
    fleet_server_is_healthy,
    get_elastic_password,
    wait_kibana_stable,
)

POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"


def main():
    c = connect(NODES["es01"][0])
    pwd = get_elastic_password(c)
    c.close()
    print(f"elastic={pwd}", flush=True)

    if not wait_kibana_stable(NODES["kibana"][0], elastic_pwd=pwd):
        print("Kibana not ready", flush=True)
        return 1

    if fleet_server_is_healthy():
        print(
            "Fleet Server already HEALTHY on :8220 — skipping redeploy.",
            flush=True,
        )
        return 0

    svc, ca = create_service_token(pwd)
    if not deploy_fleet_server(POLICY_ID, svc, ca):
        print("Fleet enrollment failed", flush=True)
        return 1

    print("Fleet Server enrollment complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())