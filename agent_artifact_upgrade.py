#!/usr/bin/env python3
"""
Upgrade Fleet-managed Elastic Agents (including Fleet Server) via Fleet bulk_upgrade
using a local mirror of artifacts.elastic.co agent binaries.

Air-gap clusters cannot reach https://artifacts.elastic.co; this module:
  1. Serves agent tarballs + official .sha512/.asc from the Fleet node
  2. Registers the mirror as the default Fleet agent download source
  3. Triggers bulk_upgrade for every enrolled agent on the target version
"""
from __future__ import annotations

import shlex
import time
from pathlib import Path

from apply_node_integrations import fleet_api
from deploy_ordered_stack import NODES, REMOTE, connect, copy_scripts, run, wait_fleet_server_ready

ROOT = Path(__file__).parent
PKG_DIR = ROOT / "packages"

ARTIFACT_PORT = 8081
ARTIFACT_ROOT = "/opt/elastic-artifacts/downloads"
DOWNLOAD_SOURCE_NAME = "Elastic Artifacts (air-gap mirror)"

AGENT_NODE_KEYS = ("fleet", "es01", "es02", "es03", "kibana")


def agent_archive_name(version: str) -> str:
    return f"elastic-agent-{version}-linux-x86_64.tar.gz"


def agent_rel_path(version: str) -> str:
    return f"beats/elastic-agent/{agent_archive_name(version)}"


def agent_package_paths(version: str, pkg_dir: Path | None = None) -> dict[str, Path]:
    base = pkg_dir or PKG_DIR
    archive = agent_archive_name(version)
    paths = {
        "archive": base / archive,
        "sha512": base / f"{archive}.sha512",
        "asc": base / f"{archive}.asc",
        "gpg": base / "GPG-KEY-elastic-agent",
    }
    missing = [str(p) for p in paths.values() if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            "Missing agent packages (run download_upgrade_packages.py):\n  "
            + "\n  ".join(missing)
        )
    return paths


def setup_artifact_server(fleet_ip: str, version: str, pkg_dir: Path | None = None) -> str:
    """Stage Elastic artifact layout on Fleet and start the HTTP mirror."""
    paths = agent_package_paths(version, pkg_dir)
    rel = agent_rel_path(version)
    rel_dir = f"{ARTIFACT_ROOT}/beats/elastic-agent"

    c = connect(fleet_ip)
    copy_scripts(c, roles=("elastic-agent",))
    run(c, f"mkdir -p {rel_dir}", check=False)

    from scp import SCPClient

    with SCPClient(c.get_transport()) as scp:
        scp.put(str(paths["archive"]), f"{ARTIFACT_ROOT}/{rel}")
        scp.put(str(paths["sha512"]), f"{rel_dir}/{paths['sha512'].name}")
        scp.put(str(paths["asc"]), f"{rel_dir}/{paths['asc'].name}")
        scp.put(str(paths["gpg"]), f"{ARTIFACT_ROOT}/GPG-KEY-elastic-agent")

    run(
        c,
        f"""cat > /etc/systemd/system/agent-artifact-server.service <<'EOF'
[Unit]
Description=Elastic Agent artifact HTTP server (air-gap mirror)
After=network.target

[Service]
Type=simple
Environment=AGENT_ARTIFACT_ROOT={ARTIFACT_ROOT}
Environment=AGENT_ARTIFACT_PORT={ARTIFACT_PORT}
Environment=AGENT_ARTIFACT_HOST=0.0.0.0
ExecStart=/usr/bin/python3 {REMOTE}/agent-artifact-server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now agent-artifact-server""",
        check=False,
        timeout=60,
    )
    run(
        c,
        f"firewall-cmd --permanent --add-port={ARTIFACT_PORT}/tcp 2>/dev/null; "
        "firewall-cmd --reload 2>/dev/null; true",
        check=False,
    )
    time.sleep(3)

    archive = agent_archive_name(version)
    for suffix in ("", ".sha512", ".asc"):
        probe_path = f"beats/elastic-agent/{archive}{suffix}"
        probe = (
            run(
                c,
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"http://127.0.0.1:{ARTIFACT_PORT}/downloads/{probe_path}",
                check=False,
            )
            .strip()
            .splitlines()[-1]
        )
        if probe != "200":
            c.close()
            raise RuntimeError(f"Artifact server probe failed for {probe_path} (http={probe})")

    c.close()
    return f"http://{fleet_ip}:{ARTIFACT_PORT}/downloads/"


def ensure_agent_download_source(kb, elastic_pwd: str, source_uri: str) -> dict:
    """Register or update the Fleet agent download source (default = artifact mirror)."""
    host = source_uri if source_uri.endswith("/") else f"{source_uri}/"
    data = fleet_api(kb, elastic_pwd, "GET", "/api/fleet/agent_download_sources")
    items = data.get("items", [])
    existing = next(
        (s for s in items if s.get("host", "").rstrip("/") == host.rstrip("/")),
        None,
    )
    body = {"name": DOWNLOAD_SOURCE_NAME, "host": host, "is_default": True}
    if existing:
        result = fleet_api(
            kb,
            elastic_pwd,
            "PUT",
            f"/api/fleet/agent_download_sources/{existing['id']}",
            body,
        )
        print(f"Updated agent download source -> {host}", flush=True)
        return result

    for s in items:
        if s.get("is_default"):
            fleet_api(
                kb,
                elastic_pwd,
                "PUT",
                f"/api/fleet/agent_download_sources/{s['id']}",
                {"name": s["name"], "host": s["host"], "is_default": False},
            )

    result = fleet_api(kb, elastic_pwd, "POST", "/api/fleet/agent_download_sources", body)
    print(f"Created agent download source -> {host}", flush=True)
    return result


def list_agents(kb, elastic_pwd: str) -> list[dict]:
    data = fleet_api(kb, elastic_pwd, "GET", "/api/fleet/agents?perPage=100")
    return data.get("items", [])


def agents_for_hostname(agents: list[dict], hostname: str) -> list[dict]:
    return [
        a
        for a in agents
        if a.get("local_metadata", {}).get("host", {}).get("hostname") == hostname
    ]


def unenroll_agents(
    kb,
    elastic_pwd: str,
    agent_ids: list[str],
    *,
    force: bool = True,
    revoke: bool = True,
) -> None:
    if not agent_ids:
        return
    print(f"Unenrolling {len(agent_ids)} agent(s)...", flush=True)
    fleet_api(
        kb,
        elastic_pwd,
        "POST",
        "/api/fleet/agents/bulk_unenroll",
        {"agents": agent_ids, "force": force, "revoke": revoke},
    )


def wait_fleet_hostname_agent(
    kb,
    elastic_pwd: str,
    hostname: str,
    *,
    timeout: int = 600,
    require_online: bool = True,
) -> dict | None:
    """Wait until a Fleet record exists for hostname (optionally online)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = [
            a
            for a in agents_for_hostname(list_agents(kb, elastic_pwd), hostname)
            if a.get("status") != "unenrolled"
        ]
        if matches:
            online = [a for a in matches if a.get("status") == "online"]
            if not require_online or online:
                chosen = online[0] if online else matches[0]
                print(
                    f"Fleet hostname agent: {chosen.get('agent', {}).get('version')} "
                    f"{chosen.get('status')} id={chosen.get('id')}",
                    flush=True,
                )
                return chosen
        print(f"Waiting for Fleet agent {hostname}...", flush=True)
        time.sleep(15)
    return None


def unenroll_hostname_agents(kb, elastic_pwd: str, hostname: str) -> list[str]:
    """Remove every Fleet record for a host (online + offline ghosts) before clean reinstall."""
    agents = list_agents(kb, elastic_pwd)
    matches = agents_for_hostname(agents, hostname)
    ids = [a["id"] for a in matches if a.get("status") != "unenrolled"]
    for a in matches:
        host = a.get("local_metadata", {}).get("host", {}).get("hostname", "?")
        print(
            f"  unenroll {host} id={a['id']} "
            f"{a.get('agent', {}).get('version', '?')} {a.get('status', '?')}",
            flush=True,
        )
    unenroll_agents(kb, elastic_pwd, ids)
    return ids


def restart_agents(node_keys: tuple[str, ...] = AGENT_NODE_KEYS) -> None:
    for key in node_keys:
        ip, fqdn = NODES[key]
        c = connect(ip)
        run(c, "systemctl restart elastic-agent 2>/dev/null || true", check=False)
        c.close()
        print(f"Restarted agent on {fqdn}", flush=True)


def prestage_verification_files(version: str, pkg_dir: Path | None = None) -> None:
    """Copy tarball + verification files into each agent's local downloads dir (fallback)."""
    paths = agent_package_paths(version, pkg_dir)
    archive = agent_archive_name(version)
    for key in AGENT_NODE_KEYS:
        ip, fqdn = NODES[key]
        c = connect(ip)
        find_out = run(
            c,
            "find /opt/Elastic/Agent/data -type d -name downloads 2>/dev/null | head -1",
            check=False,
        ).strip()
        lines = find_out.splitlines()
        dl_dir = lines[-1] if lines else ""
        if not dl_dir or "downloads" not in dl_dir:
            c.close()
            print(f"  {fqdn}: no agent downloads dir (skipped)", flush=True)
            continue
        run(c, f"mkdir -p {dl_dir}", check=False)
        from scp import SCPClient

        with SCPClient(c.get_transport()) as scp:
            for local in (paths["archive"], paths["sha512"], paths["asc"]):
                scp.put(str(local), f"{dl_dir}/{local.name}")
        c.close()
        print(f"  {fqdn}: staged {archive} + verification in {dl_dir}", flush=True)


def wait_agents_upgraded(
    kb,
    elastic_pwd: str,
    version: str,
    *,
    include_offline: bool = False,
    timeout: int = 1800,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        agents = list_agents(kb, elastic_pwd)
        lines = []
        pending = 0
        for a in agents:
            host = a.get("local_metadata", {}).get("host", {}).get("hostname", "?")
            ver = a.get("agent", {}).get("version", "?")
            status = a.get("status", "?")
            lines.append(f"  {host} {ver} {status}")
            if status == "offline" and not include_offline:
                continue
            if ver != version or status not in ("online", "degraded"):
                pending += 1
        print("\n".join(lines), flush=True)
        if pending == 0 and agents:
            return True
        time.sleep(30)
    return False


def bulk_upgrade_agents(
    kb,
    elastic_pwd: str,
    version: str,
    source_uri: str,
    *,
    agent_ids: list[str] | None = None,
) -> dict:
    if agent_ids is None:
        agents = list_agents(kb, elastic_pwd)
        agent_ids = [
            a["id"]
            for a in agents
            if a.get("agent", {}).get("version") != version and a.get("status") != "unenrolled"
        ]
    if not agent_ids:
        print("All enrolled agents already on target version", flush=True)
        return {}

    host = source_uri if source_uri.endswith("/") else f"{source_uri}/"
    print(f"Bulk upgrade {len(agent_ids)} agents -> {version} via {host}", flush=True)
    return fleet_api(
        kb,
        elastic_pwd,
        "POST",
        "/api/fleet/agents/bulk_upgrade",
        {
            "agents": agent_ids,
            "version": version,
            "source_uri": host.rstrip("/"),
            "rollout_duration_seconds": 600,
            "force": True,
        },
    )


def fleet_agent_binary_version(fleet_ip: str) -> str:
    c = connect(fleet_ip)
    ver = (
        run(
            c,
            "/opt/Elastic/Agent/elastic-agent version --binary-only 2>/dev/null | "
            "grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1 || echo missing",
            check=False,
        )
        .strip()
        .splitlines()[-1]
    )
    c.close()
    return ver


def reenroll_fleet_server_inplace(
    elastic_pwd: str,
    *,
    policy_id: str,
    agent_version: str,
    fleet_ip: str | None = None,
    es_fqdn: str | None = None,
) -> bool:
    """
    Re-enroll Fleet Server using the local agent archive on the Fleet VM.
    Does not call install-fleet-server.sh; re-extracts the rolled-back version
    from /opt/elastic-setup/archives and enrolls synchronously.
    """
    from deploy_ordered_stack import (
        AGENT_CLEANUP,
        REMOTE,
        copy_scripts,
        create_service_token,
        install_es_ca_on_node,
        set_fleet_vm_memory,
    )
    from upgrade_elastic_stack import stage_packages

    fleet_ip = fleet_ip or NODES["fleet"][0]
    es_fqdn = es_fqdn or NODES["es01"][1]
    print(
        f"\n=== Enroll Fleet Server @ {agent_version} (local archive + artifact enroll) ===",
        flush=True,
    )

    from deploy_ordered_stack import FLEET_VM, ensure_vm_running

    set_fleet_vm_memory()
    ensure_vm_running(FLEET_VM, 30)
    svc_token, ca = create_service_token(elastic_pwd)
    c = connect(fleet_ip, attempts=60)
    stage_packages(c, roles=("elastic-agent",), versions=(agent_version,))
    run(c, AGENT_CLEANUP, check=False, timeout=300)
    install_es_ca_on_node(c, ca)
    run(
        c,
        f"nohup env FLEET_MEMORY_MAX=8G FLEET_MEMORY_HIGH=6G bash {REMOTE}/install-fleet-server.sh "
        f"--version {shlex.quote(agent_version)} --es-host {es_fqdn} "
        f"--ca-file {REMOTE}/certs/http_ca.crt "
        f"--service-token {shlex.quote(svc_token)} "
        f"--policy-id {shlex.quote(policy_id)} "
        f"> /var/log/fleet-reenroll.log 2>&1 &",
        timeout=30,
    )
    c.close()

    if not wait_fleet_server_ready(fleet_ip):
        print("WARN: Fleet enroll did not bring :8220 healthy", flush=True)
        return False

    kb = connect(NODES["kibana"][0])
    ok = wait_fleet_hostname_agent(
        kb, elastic_pwd, NODES["fleet"][1], timeout=900, require_online=True
    ) is not None
    kb.close()
    if ok:
        return True
    print("WARN: Fleet enroll did not produce an online Fleet API record", flush=True)
    return False


def upgrade_fleet_server_local(
    version: str,
    elastic_pwd: str,
    *,
    pkg_dir: Path | None = None,
) -> bool:
    """
    Upgrade Fleet Server agent in place via the local Elastic artifacts HTTP mirror.
    Does not run install-fleet-server.sh or Fleet bulk_upgrade on the fleet node.
    """
    fleet_ip = NODES["fleet"][0]
    current = fleet_agent_binary_version(fleet_ip)
    if current == version:
        print(f"Fleet Server agent already on {version}", flush=True)
        return True

    print(f"\n=== Fleet Server local artifact upgrade {current} -> {version} ===", flush=True)
    source_uri = setup_artifact_server(fleet_ip, version, pkg_dir)
    host = source_uri.rstrip("/")

    kb = connect(NODES["kibana"][0])
    ensure_agent_download_source(kb, elastic_pwd, source_uri)
    kb.close()

    c = connect(fleet_ip)
    run(
        c,
        f"yes | /opt/Elastic/Agent/elastic-agent upgrade {shlex.quote(version)} "
        f"--source-uri {shlex.quote(host)}",
        timeout=1800,
        check=False,
    )
    run(c, "systemctl restart elastic-agent 2>/dev/null || true", check=False)
    time.sleep(15)
    c.close()

    if not wait_fleet_server_ready(fleet_ip, max_polls=20):
        print("WARN: Fleet Server not healthy after local upgrade", flush=True)
        return False
    upgraded = fleet_agent_binary_version(fleet_ip)
    ok = upgraded == version
    print(f"Fleet Server binary after upgrade: {upgraded}", flush=True)
    return ok


def upgrade_fleet_managed_agents(
    version: str,
    elastic_pwd: str,
    *,
    pkg_dir: Path | None = None,
    prestage: bool = True,
    restart_before: bool = True,
) -> bool:
    """
    Upgrade every Fleet-managed agent (ES nodes, Kibana, Fleet Server) using the
    local Elastic artifacts mirror on the Fleet node.
    """
    print(f"\n=== Fleet-managed agents upgrade -> {version} (Elastic artifacts mirror) ===", flush=True)
    agent_package_paths(version, pkg_dir)

    fleet_ip = NODES["fleet"][0]
    kb = connect(NODES["kibana"][0])

    if restart_before:
        restart_agents()

    source_uri = setup_artifact_server(fleet_ip, version, pkg_dir)
    print(f"Artifact mirror: {source_uri}", flush=True)
    ensure_agent_download_source(kb, elastic_pwd, source_uri)

    if prestage:
        print("Pre-staging verification files on agent nodes...", flush=True)
        prestage_verification_files(version, pkg_dir)

    result = bulk_upgrade_agents(kb, elastic_pwd, version, source_uri)
    if result:
        print(f"Fleet action: {result}", flush=True)

    ok = wait_agents_upgraded(kb, elastic_pwd, version)
    kb.close()

    if not wait_fleet_server_ready(fleet_ip, max_polls=30):
        print("WARN: Fleet Server not healthy after agent upgrade", flush=True)
        ok = False

    print("SUCCESS" if ok else "WARN: not all agents upgraded", flush=True)
    return ok