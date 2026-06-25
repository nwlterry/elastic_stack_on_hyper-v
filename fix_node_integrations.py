#!/usr/bin/env python3
"""Fix EPR + install packages + create integrations + restart agents."""
import shlex
import sys
import time

from config_loader import EPR_PACKAGES
from deploy_ordered_stack import (
    ES_NODES,
    NODES,
    REMOTE,
    _run_fleet_setup,
    connect,
    copy_scripts,
    curl_elastic_auth,
    get_elastic_password,
    run,
    wait_kibana_stable,
)

NODE_PACKAGES = {"system", "elasticsearch", "kibana"}


def main() -> int:
    kb_ip = NODES["kibana"][0]
    es = connect(NODES["es01"][0])
    pwd = get_elastic_password(es)
    es.close()
    print("elastic=" + pwd, flush=True)

    kb = connect(kb_ip)
    copy_scripts(kb, roles=("kibana",))
    run(kb, f"bash {REMOTE}/stage-epr-packages.sh", timeout=300, check=False)
    run(kb, f"bash {REMOTE}/install-local-epr.sh", timeout=180, check=False)
    print("epr_manifest=" + run(
        kb,
        "curl -s http://127.0.0.1:8080/package/system/1.60.0/manifest.yml | head -5",
        check=False,
    ), flush=True)

    run(kb, f"FLEET_HOST={NODES['fleet'][1]} bash {REMOTE}/configure-fleet-airgap.sh", timeout=600, check=False)
    kb.close()

    if not wait_kibana_stable(kb_ip, elastic_pwd=pwd):
        print("kibana not stable after airgap", flush=True)
        return 1

    kb = connect(kb_ip)
    print(run(kb, f"ELASTIC_PASS={shlex.quote(pwd)} bash {REMOTE}/upload-fleet-packages.sh", timeout=900), flush=True)
    print(run(kb, f"ELASTIC_PASS={shlex.quote(pwd)} bash {REMOTE}/install-fleet-packages.sh", timeout=300, check=False), flush=True)

    auth = curl_elastic_auth(pwd)
    for i in range(12):
        installed = run(
            kb,
            f"curl -s -u {auth} -H 'kbn-xsrf:true' "
            f"http://127.0.0.1:5601/api/fleet/epm/packages/installed | "
            f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
            f"print(','.join(sorted(p.get('name','') for p in d.get('items',[]))))\"",
            check=False,
        ).strip()
        print(f"installed_poll_{i}={installed}", flush=True)
        have = set(installed.split(",")) if installed else set()
        if NODE_PACKAGES.issubset(have):
            break
        time.sleep(10)
    kb.close()

    print("=== setup integrations ===", flush=True)
    result = _run_fleet_setup(pwd, "agents")
    for k, v in sorted(result.items()):
        print(f"{k}={v}", flush=True)

    kb = connect(kb_ip)
    pkgs = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' 'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' | "
        f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
        f"[print(p.get('package',{{}}).get('name'), p.get('policy_id')[:8]) for p in d.get('items',[])]\"",
        check=False,
    )
    print("package_policies:\n" + pkgs, flush=True)
    kb.close()

    if not all(result.get(f"INTEGRATION_OK={x}") or f"INTEGRATION_OK={x}" in str(result) for x in (
        "es-system", "es-elasticsearch", "kibana-system", "kibana-kibana"
    )):
        ok_labels = [k for k in result if k.startswith("INTEGRATION_OK")]
        if len(ok_labels) < 4:
            print("WARN: not all integrations confirmed OK", flush=True)

    print("=== restart agents ===", flush=True)
    for ip, fqdn in list(ES_NODES) + [NODES["kibana"]]:
        c = connect(ip)
        out = run(c, "systemctl restart elastic-agent; sleep 8; elastic-agent status 2>&1 | head -40", check=False, timeout=120)
        c.close()
        print(f"--- {fqdn} ---\n{out[-1800:]}", flush=True)

    kb = connect(kb_ip)
    print(run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' 'http://127.0.0.1:5601/api/fleet/agents?perPage=20' | "
        f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
        f"[print(a.get('local_metadata',{{}}).get('host',{{}}).get('name'), 'rev', a.get('policy_revision'), a.get('status')) "
        f"for a in d.get('items',[])]\"",
        check=False,
    ), flush=True)
    kb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())