#!/usr/bin/env python3
"""Finish APM on remaining ES nodes, Kibana APM, second ES snapshot."""
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

PWD = _lab_elastic_password()
APM_URL = f"http://{NODES['fleet'][0]}:8200"
REPO = "fs_nfs_snapshots"
SNAP = "pre-upgrade-apm-alert-8.18.4-20260724"
LOG = Path(__file__).resolve().parent / "logs" / "finish_apm_snap.log"
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


def ensure_node_apm(key: str) -> None:
    ip, fqdn = NODES[key]
    log(f"ensure APM on {fqdn}")
    c = connect(ip, attempts=30)
    try:
        yml = run(c, "grep -E 'tracing.apm|elastic.apm' /etc/elasticsearch/elasticsearch.yml || true", check=False)
        if "tracing.apm.enabled" not in yml:
            block = (
                f"\n# APM collection (upgrade testing)\n"
                f"tracing.apm.enabled: true\n"
                f"tracing.apm.agent.server_url: \"{APM_URL}\"\n"
                f"tracing.apm.agent.environment: \"upgrade-test\"\n"
            )
            run(
                c,
                f"printf %s {shlex.quote(block)} >> /etc/elasticsearch/elasticsearch.yml",
                check=False,
            )
        # non-blocking restart
        run(
            c,
            "systemctl restart elasticsearch </dev/null >/dev/null 2>&1 & "
            "echo restart_issued",
            check=False,
            timeout=30,
        )
    finally:
        c.close()
    for i in range(60):
        try:
            c2 = connect(ip, attempts=3)
            code = run(
                c2,
                "curl -sk -m 5 -o /dev/null -w '%{http_code}' https://localhost:9200/",
                check=False,
                timeout=20,
            )
            has = run(
                c2,
                "grep -c 'tracing.apm.enabled' /etc/elasticsearch/elasticsearch.yml || true",
                check=False,
            )
            c2.close()
            if ("401" in code or "200" in code) and "1" in has:
                log(f"  {fqdn} up with APM config")
                return
        except Exception as e:
            log(f"  wait {fqdn}: {e}")
        time.sleep(5)
    log(f"  WARN {fqdn} not confirmed")


def wait_cluster() -> bool:
    log("wait cluster")
    for i in range(60):
        try:
            c = connect(NODES["es02"][0], attempts=8)
            out = run(
                c,
                f"curl -sk -m 12 -u elastic:{PWD} 'https://localhost:9200/_cluster/health?pretty'; "
                f"curl -sk -m 12 -u elastic:{PWD} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'",
                check=False,
                timeout=40,
            )
            c.close()
            log(f"  poll {i} status_line={out[out.find('status'):out.find('status')+40] if 'status' in out else out[-200:]}")
            if ('"status" : "yellow"' in out or '"status" : "green"' in out) and out.count("8.18.4") >= 4:
                if '"unassigned_primary_shards" : 0' in out:
                    return True
        except Exception as e:
            log(f"  {e}")
        time.sleep(10)
    return False


def enable_kibana():
    log("enable kibana APM")
    block = (
        f"\n# APM collection (upgrade testing)\n"
        f"elastic.apm.active: true\n"
        f"elastic.apm.serverUrl: \"{APM_URL}\"\n"
        f"elastic.apm.environment: \"upgrade-test\"\n"
        f"elastic.apm.transactionSampleRate: 1.0\n"
    )
    kb = connect(NODES["kibana"][0], attempts=20)
    try:
        yml = run(kb, "grep elastic.apm /etc/kibana/kibana.yml || true", check=False)
        if "elastic.apm.active" not in yml:
            run(kb, f"printf %s {shlex.quote(block)} >> /etc/kibana/kibana.yml", check=False)
        run(
            kb,
            "systemctl restart kibana </dev/null >/dev/null 2>&1 & echo kibana_restart_issued",
            check=False,
            timeout=30,
        )
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
            if "200" in code:
                log("  kibana 200")
                return
            log(f"  kibana {code.strip().splitlines()[-1]}")
        except Exception as e:
            log(f"  kibana wait {e}")
        time.sleep(10)


def create_snap(auth) -> str:
    log(f"create snap {SNAP}")
    c = connect(NODES["es02"][0], attempts=20)
    log(es_api(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state")[:600])
    body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": FEATURES,
        "metadata": {
            "taken_by": "finish_apm_snap",
            "taken_because": "second system snap after 8.18.4 HV restore + APM + sample alert",
            "es_version": "8.18.4",
        },
    }
    # if exists and SUCCESS, skip recreate
    existing = es_api(c, auth, "GET", f"/_snapshot/{REPO}/{SNAP}")
    s0 = parse_json(existing) or {}
    snaps = s0.get("snapshots") or []
    if snaps and snaps[0].get("state") == "SUCCESS":
        log("  already SUCCESS")
        c.close()
        return "SUCCESS"
    print(
        es_api(
            c, auth, "PUT",
            f"/_snapshot/{REPO}/{SNAP}?wait_for_completion=false&pretty",
            body, timeout=120,
        ),
        flush=True,
    )
    state = "IN_PROGRESS"
    for i in range(180):
        out = es_api(c, auth, "GET", f"/_snapshot/{REPO}/{SNAP}?pretty", timeout=120)
        data = parse_json(out) or {}
        snaps = data.get("snapshots") or []
        s = snaps[0] if snaps else data
        state = s.get("state") or state
        feats = [f.get("feature_name") for f in (s.get("feature_states") or [])]
        log(f"  poll {i}: {state} shards={s.get('shards')} features={feats}")
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
    log(es_api(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state")[:1200])
    c.close()
    return state


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    # verify APM server
    fl = connect(NODES["fleet"][0], attempts=10)
    print(run(fl, "systemctl is-active apm-server; ss -lntp | grep 8200 || true", check=False), flush=True)
    fl.close()

    for key in ("es04", "es03", "es02", "es01"):
        ensure_node_apm(key)
    ok = wait_cluster()
    log(f"cluster_ok={ok}")
    enable_kibana()

    c = connect(NODES["es02"][0])
    auth = curl_elastic_auth(get_elastic_password(c))
    c.close()
    state = create_snap(auth)

    c = connect(NODES["es02"][0])
    print(es_api(c, auth, "GET", "/_cat/nodes?v&h=name,node.role,master,version"), flush=True)
    print(es_api(c, auth, "GET", "/_cluster/health?pretty"), flush=True)
    c.close()

    log(
        f"\n=== DONE ===\n"
        f"APM: {APM_URL}\n"
        f"Second ES snap: {SNAP} state={state}\n"
    )
    return 0 if state == "SUCCESS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
