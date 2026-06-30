#!/usr/bin/env python3
"""Reinstall Fleet Server (if needed) and upgrade all Elastic Agents via artifact mirror."""
from __future__ import annotations

import shlex

from agent_artifact_upgrade import upgrade_fleet_managed_agents
from deploy_ordered_stack import (
    FLEET_VM,
    NODES,
    REMOTE,
    connect,
    create_service_token,
    curl_elastic_auth,
    ensure_vm_running,
    get_elastic_password,
    install_es_ca_on_node,
    run,
    set_fleet_vm_memory,
    wait_fleet_server_ready,
)
from upgrade_elastic_stack import TARGET_VERSION, stage_packages

FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"


def fleet_server_healthy(ip: str) -> bool:
    c = connect(ip)
    out = run(
        c,
        "ss -tlnp | grep 8220 || echo NO_8220; "
        "curl -sk https://127.0.0.1:8220/api/status 2>/dev/null",
        check=False,
    )
    c.close()
    return ":8220" in out and "NO_8220" not in out and '"status":"HEALTHY"' in out


def fleet_agent_installed(ip: str) -> bool:
    c = connect(ip)
    out = run(
        c,
        "test -x /opt/Elastic/Agent/elastic-agent && echo yes || echo no; "
        "systemctl is-active elastic-agent 2>/dev/null || echo inactive",
        check=False,
    )
    c.close()
    return "yes" in out and "inactive" not in out.splitlines()[-1]


def reinstall_fleet_server(version: str, elastic_pwd: str) -> None:
    print(f"\n=== Reinstall Fleet Server @ {version} ===", flush=True)
    set_fleet_vm_memory()
    ensure_vm_running(FLEET_VM, 45)
    svc_token, ca = create_service_token(elastic_pwd)
    ip, es_fqdn = NODES["fleet"][0], NODES["es01"][1]
    c = connect(ip)
    stage_packages(c, roles=("elastic-agent",), versions=(version,))
    run(
        c,
        "pkill -9 -f elastic-agent 2>/dev/null || true; "
        "pkill -9 -f install-fleet-server 2>/dev/null || true",
        check=False,
        timeout=60,
    )
    install_es_ca_on_node(c, ca)
    run(
        c,
        f"nohup env FLEET_MEMORY_MAX=8G FLEET_MEMORY_HIGH=6G bash {REMOTE}/install-fleet-server.sh "
        f"--version {shlex.quote(version)} --es-host {es_fqdn} "
        f"--ca-file {REMOTE}/certs/http_ca.crt "
        f"--service-token {shlex.quote(svc_token)} "
        f"--policy-id {shlex.quote(FLEET_POLICY_ID)} "
        f"> /var/log/fleet-install.log 2>&1 &",
        timeout=30,
    )
    c.close()
    if not wait_fleet_server_ready(ip):
        raise RuntimeError("Fleet Server reinstall did not become healthy")
    print("Fleet Server reinstalled and healthy", flush=True)


def verify_agents(version: str, elastic_pwd: str) -> bool:
    auth = curl_elastic_auth(elastic_pwd)
    kb = connect(NODES["kibana"][0])
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/agents?perPage=20' | "
        f"python3 -c \"import sys,json; "
        f"d=json.load(sys.stdin); "
        f"print('\\n'.join(f\\\"{{a['local_metadata']['host']['hostname']}} "
        f"{{a.get('agent',{{}}).get('version','?')}} {{a.get('status','?')}}\\\" "
        f"for a in d.get('items',[])))\"",
        check=False,
        timeout=60,
    )
    kb.close()
    print(out, flush=True)

    ok = True
    for key in ("fleet", "es01", "es02", "es03", "kibana"):
        ip, fqdn = NODES[key]
        c = connect(ip)
        ver = run(
            c,
            "/opt/Elastic/Agent/elastic-agent version --binary-only 2>/dev/null | "
            f"grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1 || echo missing",
            check=False,
        ).strip().splitlines()[-1]
        c.close()
        print(f"  {fqdn}: binary={ver}", flush=True)
        if ver != version:
            ok = False
    return ok


def main() -> int:
    version = TARGET_VERSION
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()

    ensure_vm_running(FLEET_VM, 45)
    fleet_ip = NODES["fleet"][0]
    if not fleet_agent_installed(fleet_ip):
        reinstall_fleet_server(version, elastic_pwd)
    elif not fleet_server_healthy(fleet_ip):
        print("WARN: Fleet Server not healthy; waiting or reinstalling", flush=True)
        if not wait_fleet_server_ready(fleet_ip, max_polls=10):
            reinstall_fleet_server(version, elastic_pwd)
    else:
        print("Fleet Server healthy on 8220", flush=True)

    ok = upgrade_fleet_managed_agents(version, elastic_pwd)

    print(f"\n=== Verify agents @ {version} ===", flush=True)
    verified = verify_agents(version, elastic_pwd)
    success = ok and verified
    print(f"\n{'SUCCESS' if success else 'WARN'}: agent upgrade to {version}", flush=True)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())