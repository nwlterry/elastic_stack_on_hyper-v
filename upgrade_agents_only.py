#!/usr/bin/env python3
"""Upgrade all Fleet-managed Elastic Agents (including Fleet Server) to target version."""
from __future__ import annotations

from deploy_ordered_stack import NODES, connect, get_elastic_password
from agent_artifact_upgrade import upgrade_fleet_managed_agents
from finish_agent_upgrade import verify_agents
from upgrade_elastic_stack import TARGET_VERSION


def main() -> int:
    version = TARGET_VERSION
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()

    ok = upgrade_fleet_managed_agents(version, elastic_pwd)
    print(f"\n=== Verify agents @ {version} ===", flush=True)
    verified = verify_agents(version, elastic_pwd)
    success = ok and verified
    print(f"\n{'SUCCESS' if success else 'WARN'}: agent upgrade to {version}", flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())