#!/usr/bin/env python3
"""Deploy local EPR on Kibana, create fleet_server integration, verify Fleet enrollment."""
import shlex
import time

from deploy_ordered_stack import NODES, REMOTE, connect, copy_scripts, get_elastic_password, run

POLICY = "9be39452-a297-4b8b-9fae-b12ab3cb9315"


def main():
    es = connect(NODES["es01"][0])
    pwd = get_elastic_password(es)
    es.close()
    print(f"elastic={pwd}", flush=True)

    kb = connect(NODES["kibana"][0])
    copy_scripts(kb, roles=("kibana",))
    run(kb, f"bash {REMOTE}/install-local-epr.sh", timeout=120)
    run(kb, f"FLEET_HOST={NODES['fleet'][1]} bash {REMOTE}/configure-fleet-airgap.sh", timeout=600)

    auth = shlex.quote(f"elastic:{pwd}")
    for i in range(30):
        code = run(
            kb,
            f"curl -s -o /dev/null -w '%{{http_code}}' -u {auth} -H 'kbn-xsrf:true' "
            f"http://127.0.0.1:5601/api/status",
            check=False,
            timeout=30,
        ).strip()
        if code == "200":
            break
        time.sleep(10)

    run(
        kb,
        f"ELASTIC_PASS={shlex.quote(pwd)} bash {REMOTE}/create-fleet-server-policy.sh",
        timeout=600,
        check=False,
    )
    print(
        run(
            kb,
            f"curl -s -u {auth} -H 'kbn-xsrf:true' "
            f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=20'",
            check=False,
            timeout=60,
        ),
        flush=True,
    )
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