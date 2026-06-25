#!/usr/bin/env python3
"""Interactive first-run wizard — writes config.psd1 from prompts."""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.psd1"
EXAMPLE_PATH = ROOT / "config.psd1.example"


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        print("  (required)")


def _prompt_int(label: str, default: int) -> int:
    while True:
        raw = _prompt(label, str(default))
        try:
            return int(raw)
        except ValueError:
            print("  enter a number")


def _prompt_password(label: str = "OS root password (SSH)") -> str:
    while True:
        pwd = getpass.getpass(f"{label}: ")
        if not pwd:
            print("  (required)")
            continue
        confirm = getpass.getpass("Confirm password: ")
        if pwd == confirm:
            return pwd
        print("  passwords do not match")


def _escape_ps(s: str) -> str:
    return s.replace("'", "''")


def _default_nodes(domain: str) -> list[dict]:
    return [
        {
            "VMName": "ISMELKESNODE01",
            "Hostname": "ismelkesnode01",
            "IPAddress": "10.44.40.31",
            "Role": "elasticsearch",
            "MemoryGB": 8,
            "ProcessorCount": 4,
            "OSDiskGB": 127,
            "DataDiskGB": 500,
        },
        {
            "VMName": "ISMELKESNODE02",
            "Hostname": "ismelkesnode02",
            "IPAddress": "10.44.40.32",
            "Role": "elasticsearch",
            "MemoryGB": 8,
            "ProcessorCount": 4,
            "OSDiskGB": 127,
            "DataDiskGB": 500,
        },
        {
            "VMName": "ISMELKESNODE03",
            "Hostname": "ismelkesnode03",
            "IPAddress": "10.44.40.33",
            "Role": "elasticsearch",
            "MemoryGB": 8,
            "ProcessorCount": 4,
            "OSDiskGB": 127,
            "DataDiskGB": 500,
        },
        {
            "VMName": "ISMELKKBNNODE01",
            "Hostname": "ismelkkbnnode01",
            "IPAddress": "10.44.40.41",
            "Role": "kibana",
            "MemoryGB": 8,
            "ProcessorCount": 2,
            "OSDiskGB": 127,
            "DataDiskGB": 0,
        },
        {
            "VMName": "ISMELKFLNODE01",
            "Hostname": "ismelkflnode01",
            "IPAddress": "10.44.40.42",
            "Role": "fleet",
            "MemoryGB": 8,
            "ProcessorCount": 2,
            "OSDiskGB": 127,
            "DataDiskGB": 0,
        },
    ]


def _load_example_defaults() -> dict:
    if not EXAMPLE_PATH.is_file():
        return {}
    text = EXAMPLE_PATH.read_text(encoding="utf-8")

    def scalar(key: str, default: str = "") -> str:
        m = re.search(rf"{key}\s*=\s*'([^']*)'", text)
        return m.group(1) if m else default

    dns = re.findall(r"DnsServers\s*=\s*@\(([^)]+)\)", text)
    dns_servers = re.findall(r"'([^']+)'", dns[0]) if dns else ["10.44.40.1", "8.8.8.8"]

    return {
        "VMSwitchName": scalar("VMSwitchName", "Lab vSwitch"),
        "VMPath": scalar("VMPath", r"D:\Virtual Machines"),
        "VHDPath": scalar("VHDPath", r"D:\Virtual Machines"),
        "Generation": int(scalar("Generation", "2")),
        "RHELDvdIso": scalar("RHELDvdIso"),
        "RHELVersion": scalar("RHELVersion", "8.10"),
        "Domain": scalar("Domain", "ocplab.net"),
        "Gateway": scalar("Gateway", "10.44.40.1"),
        "DnsServers": dns_servers,
        "Timezone": scalar("Timezone", "Asia/Hong_Kong"),
        "ClusterName": scalar("ClusterName", "ism-elk-cluster"),
        "ElasticVersion": scalar("ElasticVersion", "8.18.4"),
        "FleetServerPolicyName": scalar("FleetServerPolicyName", "Fleet-Server-Policy"),
        "EsAgentPolicyName": scalar("EsAgentPolicyName", "Elastic-Agents-ES"),
        "KibanaAgentPolicyName": scalar("KibanaAgentPolicyName", "Elastic-Agents-Kibana"),
    }


def _prompt_node(index: int, role: str, defaults: dict, domain: str) -> dict:
    print(f"\n--- Node {index}: {role} ---")
    vm_default = defaults.get("VMName", f"ISMELK{index:02d}")
    host_default = defaults.get("Hostname", f"ismelknode{index:02d}")
    ip_default = defaults.get("IPAddress", f"10.44.40.{30 + index}")

    vm_name = _prompt("Hyper-V VM name", vm_default).upper()
    hostname = _prompt("Hostname (short, no domain)", host_default).lower()
    ip = _prompt("IP address", ip_default)
    memory_gb = _prompt_int("Memory (GB)", int(defaults.get("MemoryGB", 8)))
    processors = _prompt_int("vCPU count", int(defaults.get("ProcessorCount", 2)))
    os_disk_gb = _prompt_int("OS disk size (GB)", int(defaults.get("OSDiskGB", 127)))

    data_disk_gb = 0
    if role == "elasticsearch":
        data_disk_gb = _prompt_int("Data disk size (GB)", int(defaults.get("DataDiskGB", 500)))

    fqdn = f"{hostname}.{domain}"
    print(f"  FQDN: {fqdn}")

    return {
        "VMName": vm_name,
        "Hostname": hostname,
        "IPAddress": ip,
        "Role": role,
        "MemoryGB": memory_gb,
        "ProcessorCount": processors,
        "OSDiskGB": os_disk_gb,
        "DataDiskGB": data_disk_gb,
    }


def run_wizard(force: bool = False) -> Path:
    if CONFIG_PATH.is_file() and not force:
        print(f"{CONFIG_PATH.name} already exists. Use --force to overwrite.")
        return CONFIG_PATH

    print("=" * 60)
    print("ELK Stack — first-run configuration")
    print("=" * 60)

    ex = _load_example_defaults()
    domain = _prompt("Domain", ex.get("Domain", "ocplab.net"))
    root_password = _prompt_password()
    cluster = _prompt("Elasticsearch cluster name", ex.get("ClusterName", "ism-elk-cluster"))
    elastic_version = _prompt("Elastic Stack version", ex.get("ElasticVersion", "8.18.4"))
    gateway = _prompt("Default gateway", ex.get("Gateway", "10.44.40.1"))

    print("\nConfigure five nodes (3 ES, 1 Kibana, 1 Fleet).")
    print("Press Enter to accept defaults shown in [brackets].\n")

    default_nodes = _default_nodes(domain)
    roles = ["elasticsearch", "elasticsearch", "elasticsearch", "kibana", "fleet"]
    nodes = []
    for i, (role, defaults) in enumerate(zip(roles, default_nodes), start=1):
        nodes.append(_prompt_node(i, role, defaults, domain))

    dns_raw = _prompt("DNS servers (comma-separated)", ",".join(ex.get("DnsServers", ["10.44.40.1", "8.8.8.8"])))
    dns_servers = [s.strip() for s in dns_raw.split(",") if s.strip()]

    vm_switch = _prompt("Hyper-V switch name", ex.get("VMSwitchName", "Lab vSwitch"))
    vm_path = _prompt("VM parent path", ex.get("VMPath", r"D:\Virtual Machines"))
    rhel_iso = _prompt("RHEL DVD ISO path", ex.get("RHELDvdIso", r"D:\Source\rhel-8.10-x86_64-dvd.iso"))

    dns_ps = ", ".join(f"'{_escape_ps(d)}'" for d in dns_servers)
    node_blocks = []
    for n in nodes:
        node_blocks.append(
            f"""        @{{
            VMName         = '{_escape_ps(n["VMName"])}'
            Hostname       = '{_escape_ps(n["Hostname"])}'
            IPAddress      = '{_escape_ps(n["IPAddress"])}'
            Role           = '{_escape_ps(n["Role"])}'
            MemoryGB       = {n["MemoryGB"]}
            ProcessorCount = {n["ProcessorCount"]}
            OSDiskGB       = {n["OSDiskGB"]}
            DataDiskGB     = {n["DataDiskGB"]}
        }}"""
        )

    content = f"""@{{

    # Hyper-V
    VMSwitchName       = '{_escape_ps(vm_switch)}'
    VMPath             = '{_escape_ps(vm_path)}'
    VHDPath            = '{_escape_ps(vm_path)}'
    Generation         = {ex.get("Generation", 2)}

    # RHEL 8.10 flash install
    RHELDvdIso         = '{_escape_ps(rhel_iso)}'
    RHELVersion        = '{_escape_ps(ex.get("RHELVersion", "8.10"))}'
    RootPassword       = '{_escape_ps(root_password)}'
    Domain             = '{_escape_ps(domain)}'
    Gateway            = '{_escape_ps(gateway)}'
    DnsServers         = @({dns_ps})
    Timezone           = '{_escape_ps(ex.get("Timezone", "Asia/Hong_Kong"))}'

    # Elastic Stack
    ClusterName        = '{_escape_ps(cluster)}'
    ElasticVersion     = '{_escape_ps(elastic_version)}'

    # All nodes — Hyper-V name, FQDN hostname, IP
    Nodes = @(
{",".join(node_blocks)}
    )

    FleetServerPolicyName = '{_escape_ps(ex.get("FleetServerPolicyName", "Fleet-Server-Policy"))}'
    EsAgentPolicyName     = '{_escape_ps(ex.get("EsAgentPolicyName", "Elastic-Agents-ES"))}'
    KibanaAgentPolicyName = '{_escape_ps(ex.get("KibanaAgentPolicyName", "Elastic-Agents-Kibana"))}'
}}
"""

    CONFIG_PATH.write_text(content, encoding="utf-8")
    print(f"\nWrote {CONFIG_PATH}")
    print("Summary:")
    for n in nodes:
        print(f"  {n['VMName']:18} {n['Hostname']}.{domain:20} {n['IPAddress']:15} {n['Role']}")
    return CONFIG_PATH


def main() -> int:
    force = "--force" in sys.argv or "-f" in sys.argv
    run_wizard(force=force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())