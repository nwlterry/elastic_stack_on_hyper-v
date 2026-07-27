#!/usr/bin/env python3
"""Add APM integration (correct 8.18 API), enable ES/Kibana APM, second ES snapshot."""
from __future__ import annotations


def _lab_elastic_password() -> str:
    from elastic_credentials import load_config_password, load_local_password
    pwd = load_local_password() or load_config_password()
    if not pwd:
        raise SystemExit(
            "Set secrets/elastic-password or ElasticPassword in config.psd1"
        )
    return pwd


import json
import shlex
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import kibana_curl

REPO = "fs_nfs_snapshots"
SECOND_ES_SNAP = "pre-upgrade-apm-alert-8.18.4-20260724"
APM_URL_IP = f"http://{NODES['fleet'][0]}:8200"
FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"
LOG = Path(__file__).resolve().parent / "logs" / "add_apm_and_finish.log"
PWD = _lab_elastic_password()

FEATURES = [
    "security", "kibana", "fleet", "async_search", "transform",
    "machine_learning", "watcher", "enrich", "geoip", "logstash_management",
    "synonyms", "tasks", "ent_search", "searchable_snapshots", "inference_plugin",
]


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def es_api(c, auth, method, path, body=None, timeout=300):
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def parse_json(out: str):
    for ch in ("{", "["):
        s = out.find(ch)
        if s >= 0:
            try:
                return json.loads(out[s:])
            except json.JSONDecodeError:
                pass
    return None


def ensure_apm(kb, auth) -> bool:
    log("=== Install APM package + package policy ===")
    # install package
    for path in (
        "/api/fleet/epm/packages/apm/8.18.4",
        "/api/fleet/epm/packages/apm/8.18.4?prerelease=true",
    ):
        r = kibana_curl(kb, auth, "POST", path, {})
        log(f"  install {path}: {json.dumps(r)[:300]}")

    pkg = kibana_curl(kb, auth, "GET", "/api/fleet/epm/packages/apm/8.18.4")
    item = pkg.get("item") or pkg.get("response") or {}
    log(f"  status={item.get('status')} templates={len(item.get('policy_templates') or [])}")

    pkgs = kibana_curl(kb, auth, "GET", "/api/fleet/package_policies?perPage=200")
    for pp in pkgs.get("items") or []:
        if (pp.get("package") or {}).get("name") == "apm":
            log(f"  already have APM policy {pp.get('name')} id={pp.get('id')}")
            return True

    # Try multiple body shapes used across Fleet API versions
    candidates = [
        {
            "name": "apm-server-upgrade-test",
            "description": "APM Server (Fleet elastic-agent) for upgrade testing",
            "namespace": "default",
            "policy_id": FLEET_POLICY_ID,
            "package": {"name": "apm", "version": "8.18.4"},
            "inputs": [
                {
                    "type": "apm",
                    "policy_template": "apmserver",
                    "enabled": True,
                    "streams": [],
                    "vars": {
                        "host": {"value": "0.0.0.0:8200", "type": "text"},
                        "url": {"value": "http://0.0.0.0:8200", "type": "text"},
                        "enable_rum": {"value": False, "type": "bool"},
                        "anonymous_enabled": {"value": True, "type": "bool"},
                    },
                }
            ],
        },
        {
            "name": "apm-server-upgrade-test",
            "description": "APM Server for upgrade testing",
            "namespace": "default",
            "policy_id": FLEET_POLICY_ID,
            "package": {"name": "apm", "version": "8.18.4"},
            "inputs": [
                {
                    "type": "apm",
                    "enabled": True,
                    "streams": [],
                    "vars": {
                        "host": {"value": "0.0.0.0:8200", "type": "text"},
                        "url": {"value": "http://0.0.0.0:8200", "type": "text"},
                    },
                }
            ],
        },
        {
            "name": "apm-server-upgrade-test",
            "description": "APM Server for upgrade testing",
            "namespace": "default",
            "policy_id": FLEET_POLICY_ID,
            "package": {"name": "apm", "version": "8.18.4"},
            "inputs": [],
            "force": True,
        },
    ]
    for i, body in enumerate(candidates):
        r = kibana_curl(kb, auth, "POST", "/api/fleet/package_policies", body)
        log(f"  create attempt {i}: {json.dumps(r)[:500]}")
        if r.get("item") or r.get("id") or (r.get("name") and not r.get("statusCode")):
            return True
        # sometimes nested under item
        if isinstance(r.get("item"), dict) and r["item"].get("id"):
            return True

    # last resort: use setup endpoint if present
    r = kibana_curl(
        kb,
        auth,
        "POST",
        "/api/fleet/package_policies",
        {
            "name": "apm-server-upgrade-test",
            "namespace": "default",
            "policy_id": FLEET_POLICY_ID,
            "package": {"name": "apm", "version": "8.18.4"},
            "inputs": {
                "apm-apm": {
                    "enabled": True,
                    "vars": {
                        "host": "0.0.0.0:8200",
                        "url": "http://0.0.0.0:8200",
                        "enable_rum": False,
                    },
                    "streams": {},
                }
            },
        },
    )
    log(f"  map-style inputs: {json.dumps(r)[:500]}")
    return bool(r.get("item") or (not r.get("statusCode")))


def wait_apm(timeout=420) -> bool:
    log(f"=== Wait APM on {APM_URL_IP} ===")
    deadline = time.time() + timeout
    while time.time() < deadline:
        fl = connect(NODES["fleet"][0], attempts=5)
        try:
            out = run(
                fl,
                "ss -lntp | grep 8200 || true; "
                "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://127.0.0.1:8200/ || true; "
                "curl -s -o /dev/null -w ' %{http_code}' --connect-timeout 3 http://127.0.0.1:8200/intake || true",
                check=False,
                timeout=30,
            )
            log(f"  {out[-250:]}")
            if ":8200" in out or any(c in out for c in ("200", "401", "405", "403")):
                # avoid false positive from 000 only
                if ":8200" in out or any(x in out for x in (" 200", " 401", " 405", " 403", "%{http_code}200")):
                    return True
                codes = [p for p in out.replace("\n", " ").split() if p.isdigit()]
                if any(c in ("200", "401", "405", "403") for c in codes):
                    return True
        finally:
            fl.close()
        time.sleep(15)
    return False


def enable_es_apm():
    log(f"=== Enable ES APM -> {APM_URL_IP} ===")
    block = (
        f"\n# APM collection (upgrade testing)\n"
        f"tracing.apm.enabled: true\n"
        f"tracing.apm.agent.server_url: \"{APM_URL_IP}\"\n"
        f"tracing.apm.agent.environment: \"upgrade-test\"\n"
    )
    for key in ("es04", "es03", "es02", "es01"):
        ip, fqdn = NODES[key]
        log(f"  {fqdn}")
        c = connect(ip, attempts=20)
        try:
            yml = run(c, "cat /etc/elasticsearch/elasticsearch.yml", check=False)
            if "tracing.apm.enabled" not in yml:
                run(
                    c,
                    f"cp -a /etc/elasticsearch/elasticsearch.yml /etc/elasticsearch/elasticsearch.yml.bak.apm; "
                    f"printf %s {shlex.quote(block)} >> /etc/elasticsearch/elasticsearch.yml",
                    check=False,
                )
            run(c, "systemctl restart elasticsearch", check=False, timeout=180)
        finally:
            c.close()
        for _ in range(40):
            try:
                c2 = connect(ip, attempts=3)
                code = run(
                    c2,
                    "curl -sk -m 5 -o /dev/null -w '%{http_code}' https://localhost:9200/",
                    check=False,
                    timeout=20,
                )
                c2.close()
                if "401" in code or "200" in code:
                    log(f"    up {code.strip().splitlines()[-1]}")
                    break
            except Exception:
                pass
            time.sleep(5)
        time.sleep(8)


def wait_cluster():
    log("=== Wait cluster stable ===")
    for i in range(60):
        c = connect(NODES["es02"][0], attempts=10)
        out = run(
            c,
            f"curl -sk -m 12 -u elastic:{PWD} 'https://localhost:9200/_cluster/health?pretty'; "
            f"curl -sk -m 12 -u elastic:{PWD} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'",
            check=False,
            timeout=40,
        )
        c.close()
        log(f"  poll {i}: {out[-400:]}")
        if ('"status" : "yellow"' in out or '"status" : "green"' in out) and out.count("8.18.4") >= 4:
            if "unassigned_primary" not in out or '"unassigned_primary_shards" : 0' in out:
                return True
        time.sleep(10)
    return False


def enable_kibana_apm():
    log(f"=== Enable Kibana APM -> {APM_URL_IP} ===")
    block = (
        f"\n# APM collection (upgrade testing)\n"
        f"elastic.apm.active: true\n"
        f"elastic.apm.serverUrl: \"{APM_URL_IP}\"\n"
        f"elastic.apm.environment: \"upgrade-test\"\n"
        f"elastic.apm.transactionSampleRate: 1.0\n"
    )
    kb = connect(NODES["kibana"][0], attempts=20)
    try:
        yml = run(kb, "cat /etc/kibana/kibana.yml", check=False)
        if "elastic.apm.active" not in yml:
            run(
                kb,
                f"cp -a /etc/kibana/kibana.yml /etc/kibana/kibana.yml.bak.apm; "
                f"printf %s {shlex.quote(block)} >> /etc/kibana/kibana.yml",
                check=False,
            )
        run(kb, "systemctl restart kibana", check=False, timeout=180)
    finally:
        kb.close()
    for i in range(60):
        try:
            kb = connect(NODES["kibana"][0], attempts=5)
            code = run(
                kb,
                "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status || true",
                check=False,
                timeout=20,
            )
            kb.close()
            log(f"  kibana {code.strip().splitlines()[-1]}")
            if "200" in code:
                return
        except Exception as e:
            log(f"  kibana wait {e}")
        time.sleep(10)


def create_second_snap(auth):
    log(f"=== Second ES snapshot {SECOND_ES_SNAP} ===")
    c = connect(NODES["es02"][0], attempts=20)
    listing = es_api(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state")
    log(listing[:800])
    snap = SECOND_ES_SNAP
    body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": FEATURES,
        "metadata": {
            "taken_by": "add_apm_and_finish",
            "taken_because": "second system snap after APM + upgrade-testing sample alert on 8.18.4",
            "es_version": "8.18.4",
        },
    }
    print(
        es_api(
            c,
            auth,
            "PUT",
            f"/_snapshot/{REPO}/{snap}?wait_for_completion=false&pretty",
            body,
            timeout=120,
        ),
        flush=True,
    )
    state = "IN_PROGRESS"
    for i in range(180):
        out = es_api(c, auth, "GET", f"/_snapshot/{REPO}/{snap}?pretty", timeout=120)
        data = parse_json(out) or {}
        snaps = data.get("snapshots") or []
        s = snaps[0] if snaps else data
        state = s.get("state") or state
        feats = [f.get("feature_name") for f in (s.get("feature_states") or [])]
        log(f"  poll {i}: state={state} shards={s.get('shards')} features={feats}")
        if state in ("SUCCESS", "FAILED", "PARTIAL"):
            log(json.dumps({
                "snapshot": s.get("snapshot"),
                "state": state,
                "include_global_state": s.get("include_global_state"),
                "indices_count": len(s.get("indices") or []),
                "feature_states": feats,
                "shards": s.get("shards"),
            }, indent=2))
            break
        time.sleep(10)
    log(es_api(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state")[:1500])
    c.close()
    return state


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    c = connect(NODES["es02"][0])
    auth = curl_elastic_auth(get_elastic_password(c))
    c.close()

    kb = connect(NODES["kibana"][0], attempts=30)
    try:
        ok = ensure_apm(kb, auth)
        log(f"APM policy ok={ok}")
        # verify alert still present
        found = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=20&search=Upgrade")
        log(f"alerts: {[x.get('name') for x in (found.get('data') or [])]}")
    finally:
        kb.close()

    fl = connect(NODES["fleet"][0])
    run(
        fl,
        "firewall-cmd --permanent --add-port=8200/tcp 2>/dev/null; firewall-cmd --reload 2>/dev/null; true",
        check=False,
    )
    fl.close()

    # force agent checkin by bumping policy if needed — wait for APM
    time.sleep(30)
    apm_up = wait_apm(timeout=360)
    log(f"APM listening={apm_up}")

    # Even if APM not yet listening, enable clients so they reconnect when server up
    enable_es_apm()
    wait_cluster()
    enable_kibana_apm()

    # recheck APM
    apm_up = wait_apm(timeout=120) or apm_up
    log(f"APM final listening={apm_up}")

    c = connect(NODES["es02"][0])
    auth = curl_elastic_auth(get_elastic_password(c))
    c.close()
    state = create_second_snap(auth)

    c = connect(NODES["es02"][0])
    print(es_api(c, auth, "GET", "/_cat/nodes?v&h=name,node.role,master,version"), flush=True)
    print(es_api(c, auth, "GET", "/_cluster/health?pretty"), flush=True)
    c.close()

    kb = connect(NODES["kibana"][0], attempts=20)
    pkgs = kibana_curl(kb, auth, "GET", "/api/fleet/package_policies?perPage=200")
    apm = [
        (p.get("name"), (p.get("package") or {}).get("name"), p.get("policy_id"))
        for p in (pkgs.get("items") or [])
        if (p.get("package") or {}).get("name") == "apm"
    ]
    log(f"APM policies: {apm}")
    kb.close()

    log(
        f"\n=== DONE ===\n"
        f"Alert: Upgrade Testing Sample Alert\n"
        f"APM listening={apm_up} url={APM_URL_IP}\n"
        f"Second ES snap: {SECOND_ES_SNAP} state={state}\n"
    )
    return 0 if state == "SUCCESS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
