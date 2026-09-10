#!/usr/bin/env python3
"""Print ES/Kibana RPM and elastic-agent binary versions. Never prints passwords."""
from __future__ import annotations

from agent_artifact_upgrade import fleet_agent_binary_version
from deploy_ordered_stack import NODES, connect, run
from restore_elastic_vms import remote_rpm_version


def agent_binary(ip: str) -> str:
    c = connect(ip)
    lines = (
        run(
            c,
            "/opt/Elastic/Agent/elastic-agent version --binary-only 2>/dev/null "
            "| grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1 || echo missing",
            check=False,
        )
        .strip()
        .splitlines()
    )
    c.close()
    return lines[-1] if lines else "missing"


def main() -> int:
    print("nodes:", ", ".join(sorted(NODES)))
    for key in ("es01", "es02", "es03", "es04"):
        if key not in NODES:
            continue
        print(f"es {key} rpm={remote_rpm_version(NODES[key][0], 'elasticsearch')} agent={agent_binary(NODES[key][0])}")
    if "kibana" in NODES:
        print(
            f"kibana rpm={remote_rpm_version(NODES['kibana'][0], 'kibana')} "
            f"agent={agent_binary(NODES['kibana'][0])}"
        )
    if "fleet" in NODES:
        print(f"fleet agent={fleet_agent_binary_version(NODES['fleet'][0])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
