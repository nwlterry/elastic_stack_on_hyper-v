#!/usr/bin/env python3
"""Deploy local EPR on Kibana with all node-role integration packages."""
import shlex
import time

from deploy_ordered_stack import (
    EPR_PACKAGES,
    NODES,
    REMOTE,
    connect,
    copy_scripts,
    curl_elastic_auth,
    ensure_fleet_epr_ready,
    get_elastic_password,
    run,
    wait_kibana_stable,
)


def main():
    es = connect(NODES["es01"][0])
    pwd = get_elastic_password(es)
    es.close()
    print(f"elastic={pwd}", flush=True)

    kb_ip = NODES["kibana"][0]
    if not wait_kibana_stable(kb_ip, elastic_pwd=pwd):
        print("Kibana not stable", flush=True)
        return 1

    ensure_fleet_epr_ready(pwd)

    kb = connect(kb_ip)
    auth = curl_elastic_auth(pwd)
    run(
        kb,
        f"ELASTIC_PASS={shlex.quote(pwd)} bash {REMOTE}/create-fleet-server-policy.sh",
        timeout=600,
        check=False,
    )
    installed = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"http://127.0.0.1:5601/api/fleet/epm/packages/installed | "
        f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
        f"print([p.get('name') for p in d.get('items',[])])\"",
        check=False,
        timeout=60,
    )
    print(f"installed={installed.strip()}", flush=True)
    missing = [p for p in EPR_PACKAGES if p not in installed]
    if missing:
        print(f"WARN: still missing packages: {missing}", flush=True)

    print(run(kb, "journalctl -u kibana -n 10 --no-pager | grep -iE 'epr|deploy_agent|fleet_server|error' || true", check=False))
    kb.close()

    fleet_ip = NODES["fleet"][0]
    for i in range(20):
        time.sleep(15)
        fl = connect(fleet_ip)
        status = run(fl, "elastic-agent status 2>&1 | head -22", check=False)
        fl.close()
        print(f"\n=== fleet poll {i} ===\n{status}", flush=True)
        if "Waiting on fleet-server input" not in status and "└─ status: (HEALTHY)" in status:
            print("Fleet enrollment complete", flush=True)
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())