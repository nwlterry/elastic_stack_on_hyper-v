#!/usr/bin/env python3
"""Load config.psd1 and build deploy-time constants (NODES, DOMAIN, etc.)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.psd1"
EXAMPLE_PATH = ROOT / "config.psd1.example"

EPR_PACKAGES = {
    "fleet_server": "1.6.0",
    "elastic_agent": "2.3.0",
    "system": "1.60.0",
    "elasticsearch": "1.12.0",
    "kibana": "1.11.0",
}


def config_exists(path: Path | None = None) -> bool:
    return (path or CONFIG_PATH).is_file()


def _load_via_powershell(path: Path) -> dict[str, Any] | None:
    ps = f"""
$c = Import-PowerShellDataFile -Path '{path}'
$nodes = @()
foreach ($n in $c.Nodes) {{
    $nodes += [ordered]@{{
        VMName = $n.VMName
        Hostname = $n.Hostname
        IPAddress = $n.IPAddress
        Role = $n.Role
        MemoryGB = [int]$n.MemoryGB
        ProcessorCount = [int]$n.ProcessorCount
        OSDiskGB = [int]$n.OSDiskGB
        DataDiskGB = [int]$n.DataDiskGB
    }}
}}
[ordered]@{{
    VMSwitchName = $c.VMSwitchName
    VMPath = $c.VMPath
    VHDPath = $c.VHDPath
    Generation = [int]$c.Generation
    RHELDvdIso = $c.RHELDvdIso
    RHELVersion = $c.RHELVersion
    RootPassword = $c.RootPassword
    Domain = $c.Domain
    Gateway = $c.Gateway
    DnsServers = @($c.DnsServers)
    Timezone = $c.Timezone
    ClusterName = $c.ClusterName
    ElasticVersion = $c.ElasticVersion
    Nodes = $nodes
    FleetServerPolicyName = $c.FleetServerPolicyName
    EsAgentPolicyName = $c.EsAgentPolicyName
    KibanaAgentPolicyName = $c.KibanaAgentPolicyName
}} | ConvertTo-Json -Depth 6 -Compress
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass
    return None


def _load_via_regex(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")

    def scalar(key: str, default: str = "") -> str:
        m = re.search(rf"{key}\s*=\s*'([^']*)'", text)
        return m.group(1) if m else default

    nodes: list[dict[str, Any]] = []
    for block in re.finditer(r"@\{([^}]+)\}", text):
        chunk = block.group(1)
        if "Role" not in chunk:
            continue

        def node_val(key: str, default: str = "") -> str:
            m = re.search(rf"{key}\s*=\s*'([^']*)'", chunk)
            if m:
                return m.group(1)
            m = re.search(rf"{key}\s*=\s*(\d+)", chunk)
            return m.group(1) if m else default

        nodes.append(
            {
                "VMName": node_val("VMName"),
                "Hostname": node_val("Hostname"),
                "IPAddress": node_val("IPAddress"),
                "Role": node_val("Role"),
                "MemoryGB": int(node_val("MemoryGB", "8")),
                "ProcessorCount": int(node_val("ProcessorCount", "2")),
                "OSDiskGB": int(node_val("OSDiskGB", "127")),
                "DataDiskGB": int(node_val("DataDiskGB", "0")),
            }
        )

    dns = re.findall(r"DnsServers\s*=\s*@\(([^)]+)\)", text)
    dns_servers = []
    if dns:
        dns_servers = re.findall(r"'([^']+)'", dns[0])

    return {
        "VMSwitchName": scalar("VMSwitchName", "Lab vSwitch"),
        "VMPath": scalar("VMPath", r"D:\Virtual Machines"),
        "VHDPath": scalar("VHDPath", r"D:\Virtual Machines"),
        "Generation": int(scalar("Generation", "2")),
        "RHELDvdIso": scalar("RHELDvdIso"),
        "RHELVersion": scalar("RHELVersion", "8.10"),
        "RootPassword": scalar("RootPassword"),
        "Domain": scalar("Domain", "ocplab.net"),
        "Gateway": scalar("Gateway", "10.44.40.1"),
        "DnsServers": dns_servers or ["10.44.40.1", "8.8.8.8"],
        "Timezone": scalar("Timezone", "Asia/Hong_Kong"),
        "ClusterName": scalar("ClusterName", "ism-elk-cluster"),
        "ElasticVersion": scalar("ElasticVersion", "8.18.4"),
        "Nodes": nodes,
        "FleetServerPolicyName": scalar("FleetServerPolicyName", "Fleet-Server-Policy"),
        "EsAgentPolicyName": scalar("EsAgentPolicyName", "Elastic-Agents-ES"),
        "KibanaAgentPolicyName": scalar("KibanaAgentPolicyName", "Elastic-Agents-Kibana"),
    }


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or CONFIG_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Missing config: {path}")
    cfg = _load_via_powershell(path)
    if not cfg or not cfg.get("RootPassword") or not cfg.get("Nodes"):
        cfg = _load_via_regex(path)
    if not cfg.get("RootPassword"):
        raise ValueError("RootPassword is empty in config.psd1")
    if not cfg.get("Nodes"):
        raise ValueError("Nodes array is empty in config.psd1")
    return cfg


def build_deploy_context(cfg: dict[str, Any]) -> dict[str, Any]:
    domain = cfg["Domain"]
    nodes: dict[str, tuple[str, str]] = {}
    es_nodes: list[tuple[str, str]] = []
    vm_names: dict[str, str] = {}
    es_count = 0

    for node in cfg["Nodes"]:
        fqdn = f"{node['Hostname']}.{domain}"
        entry = (node["IPAddress"], fqdn)
        role = node["Role"]
        if role == "elasticsearch":
            es_count += 1
            key = f"es{es_count:02d}"
            es_nodes.append(entry)
            nodes[key] = entry
            vm_names[key] = node["VMName"]
        elif role == "kibana":
            nodes["kibana"] = entry
            vm_names["kibana"] = node["VMName"]
        elif role == "fleet":
            nodes["fleet"] = entry
            vm_names["fleet"] = node["VMName"]

    fleet_node = next((n for n in cfg["Nodes"] if n["Role"] == "fleet"), None)
    kibana_node = next((n for n in cfg["Nodes"] if n["Role"] == "kibana"), None)
    es_primary = es_nodes[0] if es_nodes else ("127.0.0.1", "localhost")

    return {
        "DOMAIN": domain,
        "CLUSTER": cfg["ClusterName"],
        "VERSION": cfg["ElasticVersion"],
        "PASSWORD": cfg["RootPassword"],
        "NODES": nodes,
        "ES_NODES": es_nodes,
        "ES_PRIMARY_IP": es_primary[0],
        "ES_PRIMARY_FQDN": es_primary[1],
        "FLEET_VM": fleet_node["VMName"] if fleet_node else "",
        "KIBANA_VM": kibana_node["VMName"] if kibana_node else "",
        "FLEET_MEMORY_GB": int(fleet_node["MemoryGB"]) if fleet_node else 8,
        "VM_NAMES": vm_names,
        "FLEET_POLICY_NAME": cfg.get("FleetServerPolicyName", "Fleet-Server-Policy"),
        "ES_POLICY_NAME": cfg.get("EsAgentPolicyName", "Elastic-Agents-ES"),
        "KIBANA_POLICY_NAME": cfg.get("KibanaAgentPolicyName", "Elastic-Agents-Kibana"),
        "CFG": cfg,
    }


def ensure_config(interactive: bool = True) -> dict[str, Any]:
    if config_exists():
        return load_config()
    if not interactive:
        raise FileNotFoundError(
            f"No {CONFIG_PATH.name} found. Run: python init_config.py"
        )
    from init_config import run_wizard

    run_wizard()
    return load_config()


def apply_deploy_context(ctx: dict[str, Any], module_name: str = "deploy_ordered_stack") -> None:
    """Inject deploy constants into a module namespace (for backward compatibility)."""
    mod = sys.modules.get(module_name)
    if mod is None:
        return
    for key in (
        "DOMAIN",
        "CLUSTER",
        "VERSION",
        "PASSWORD",
        "NODES",
        "ES_NODES",
        "FLEET_VM",
        "KIBANA_VM",
        "FLEET_MEMORY_GB",
        "FLEET_POLICY_NAME",
        "ES_POLICY_NAME",
        "KIBANA_POLICY_NAME",
    ):
        setattr(mod, key, ctx[key])
    setattr(mod, "DEPLOY_CTX", ctx)