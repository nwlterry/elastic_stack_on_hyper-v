#!/usr/bin/env python3
"""Force re-enroll Fleet Server and all Elastic Agents at a given version.

Use when Fleet API has zero enrolled agents (bulk_upgrade cannot run) even if
:8220 is still listening. Does not reset the elastic password.
"""
from __future__ import annotations

import argparse
import ctypes
import time
import traceback
from pathlib import Path

from agent_artifact_upgrade import list_agents, reenroll_fleet_server_inplace
from deploy_ordered_stack import (
    FLEET_VM,
    NODES,
    connect,
    deploy_agents,
    get_elastic_password,
    run,
    setup_agent_policies,
)
from finish_agent_upgrade import verify_agents
from upgrade_elastic_stack import INTERMEDIATE_VERSION

FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
EXPECTED_KEYS = ("fleet", "es01", "es02", "es03", "es04", "kibana")
RESULT_PATH = (
    Path.home() / ".grok" / "long-running-background-tasks" / "reenroll_result.txt"
)


def _print_fleet_agents(elastic_pwd: str) -> list[dict]:
    kb = connect(NODES["kibana"][0])
    try:
        agents = [a for a in list_agents(kb, elastic_pwd) if a.get("status") != "unenrolled"]
    finally:
        kb.close()
    print(f"Fleet API enrolled={len(agents)}", flush=True)
    for a in agents:
        host = ((a.get("local_metadata") or {}).get("host") or {}).get("hostname", "?")
        ver = ((a.get("agent") or {}).get("version") or "?")
        print(f"  {host} {ver} {a.get('status', '?')}", flush=True)
    return agents


def _online_expected(agents: list[dict], version: str) -> bool:
    online_hosts = set()
    for a in agents:
        if a.get("status") != "online":
            continue
        if ((a.get("agent") or {}).get("version") or "") != version:
            continue
        host = ((a.get("local_metadata") or {}).get("host") or {}).get("hostname", "")
        online_hosts.add(host.lower())
        online_hosts.add(host.split(".")[0].lower())
    missing = []
    for key in EXPECTED_KEYS:
        if key not in NODES:
            continue
        fqdn = NODES[key][1]
        short = fqdn.split(".")[0]
        if fqdn.lower() not in online_hosts and short.lower() not in online_hosts:
            missing.append(fqdn)
    if missing:
        print(f"Missing online {version} agents: {', '.join(missing)}", flush=True)
        return False
    return True


def _fleet_ssh_up(attempts: int = 2) -> bool:
    try:
        c = connect(NODES["fleet"][0], attempts=attempts)
        c.close()
        return True
    except Exception:
        return False


def _elevate_start_fleet_vm() -> None:
    """Start the Fleet VM via UAC; this host is not in the Hyper-V admin role."""
    root = Path(__file__).resolve().parent
    log = root / "logs" / "start-fleet-vm.log"
    script = root / "logs" / "_start_fleet_vm.ps1"
    log.parent.mkdir(exist_ok=True)
    if log.exists():
        log.unlink()
    log_path = str(log).replace("'", "''")
    script.write_text(
        f"""
$ErrorActionPreference = 'Continue'
$log = '{log_path}'
function W($m) {{
  $line = '{{0}} {{1}}' -f (Get-Date -Format o), $m
  Add-Content -Path $log -Value $line
  Write-Host $line
}}
try {{
  W "begin Start-VM {FLEET_VM}"
  Get-VM | Select-Object Name, State, Status | Format-Table -AutoSize | Out-String | ForEach-Object {{ W $_.Trim() }}
  $vm = Get-VM -Name '{FLEET_VM}' -ErrorAction Stop
  W "pre state=$($vm.State) status=$($vm.Status)"
  if ($vm.State -ne 'Running') {{
    W "calling Start-VM"
    Start-VM -Name '{FLEET_VM}' -ErrorAction Stop
    W "Start-VM returned"
  }} else {{
    W "already running"
  }}
  Start-Sleep -Seconds 5
  $vm = Get-VM -Name '{FLEET_VM}'
  W "post state=$($vm.State) status=$($vm.Status)"
}} catch {{
  W "ERROR: $($_.Exception.GetType().FullName) $($_.Exception.Message)"
  if ($_.ScriptStackTrace) {{ W $_.ScriptStackTrace }}
}}
W 'DONE'
""",
        encoding="utf-8",
    )
    args = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe", args, str(root), 1
    )
    print(f"UAC Start-VM ShellExecute={rc} (approve the prompt if shown)", flush=True)
    if rc <= 32:
        raise RuntimeError(f"UAC Start-VM launch failed rc={rc}")
    for _ in range(180):
        text = log.read_text(errors="replace") if log.exists() else ""
        if "DONE" in text:
            print(text, flush=True)
            if "ERROR:" in text:
                raise RuntimeError("UAC Start-VM failed:\n" + text)
            return
        time.sleep(2)
    raise RuntimeError(
        "UAC Start-VM timed out: " + (log.read_text(errors="replace") if log.exists() else "no log")
    )


def _ensure_fleet_ssh() -> None:
    if _fleet_ssh_up(attempts=2):
        print("Fleet SSH already up", flush=True)
        return
    print("Fleet SSH down — starting VM (Hyper-V elevation)", flush=True)
    _elevate_start_fleet_vm()
    deadline = time.time() + 600
    while time.time() < deadline:
        if _fleet_ssh_up(attempts=2):
            print("Fleet SSH is up", flush=True)
            return
        print("waiting for Fleet SSH...", flush=True)
        time.sleep(15)
    raise RuntimeError("Fleet VM SSH did not come up after Start-VM")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", default=INTERMEDIATE_VERSION)
    args = p.parse_args()
    version = args.version

    _ensure_fleet_ssh()

    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt", echo=False)
    es.close()

    print(f"\n=== Re-enroll Fleet Server + agents @ {version} ===", flush=True)
    print("--- before ---", flush=True)
    _print_fleet_agents(elastic_pwd)

    if not reenroll_fleet_server_inplace(
        elastic_pwd,
        policy_id=FLEET_POLICY_ID,
        agent_version=version,
        skip_vm_memory=True,
    ):
        print("ERROR: Fleet Server re-enroll failed", flush=True)
        return 1

    fleet_info = setup_agent_policies(elastic_pwd)
    deploy_agents(fleet_info, ca, version=version)

    print(f"\n=== Verify Fleet agents @ {version} ===", flush=True)
    agents = _print_fleet_agents(elastic_pwd)
    verified = verify_agents(version, elastic_pwd)
    online_ok = _online_expected(agents, version)
    success = verified and online_ok
    print(
        f"\n{'SUCCESS' if success else 'WARN'}: re-enroll @ {version} "
        f"(api={len(agents)} verified={verified} online_expected={online_ok})",
        flush=True,
    )
    return 0 if success else 1


def _write_result(line: str) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(line + "\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as exc:
        traceback.print_exc()
        _write_result(f"FAILED: {exc}")
        raise SystemExit(1) from exc
    _write_result("SUCCESS: re-enroll complete" if rc == 0 else "FAILED: re-enroll incomplete")
    raise SystemExit(rc)

