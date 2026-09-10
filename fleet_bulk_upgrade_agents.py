#!/usr/bin/env python3
"""Upgrade Fleet-managed Elastic Agents via Fleet bulk_upgrade + Elastic artifacts mirror."""
from __future__ import annotations

import argparse

from deploy_ordered_stack import NODES, connect, get_elastic_password
from agent_artifact_upgrade import upgrade_fleet_managed_agents
from upgrade_elastic_stack import INTERMEDIATE_VERSION, TARGET_VERSION


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--version",
        default=INTERMEDIATE_VERSION,
        help=f"Agent version (default {INTERMEDIATE_VERSION}; {TARGET_VERSION} after ES/Kibana 9.x)",
    )
    args = p.parse_args()
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()
    ok = upgrade_fleet_managed_agents(args.version, elastic_pwd)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())