#!/usr/bin/env python3
"""Upgrade Fleet-managed Elastic Agents via Fleet bulk_upgrade + Elastic artifacts mirror."""
from __future__ import annotations

from deploy_ordered_stack import NODES, connect, get_elastic_password
from agent_artifact_upgrade import upgrade_fleet_managed_agents
from upgrade_elastic_stack import TARGET_VERSION


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()
    ok = upgrade_fleet_managed_agents(TARGET_VERSION, elastic_pwd)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())