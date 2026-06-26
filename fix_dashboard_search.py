#!/usr/bin/env python3
"""Fix Kibana dashboard 'error while executing search' issues."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from deploy_ordered_stack import (
    NODES,
    REMOTE,
    connect,
    copy_scripts,
    curl_elastic_auth,
    get_elastic_password,
    run,
    wait_kibana_stable,
)
from monitoring_credentials import ensure_monitoring_user

ROOT = Path(__file__).parent

# Unify legacy Stack Monitoring (timestamp) and Fleet metrics (@timestamp).
RUNTIME_TIMESTAMP = {
    "@timestamp": {
        "type": "date",
        "script": {
            "source": (
                "if (doc.containsKey('@timestamp') && doc['@timestamp'].size() != 0) { "
                "emit(doc['@timestamp'].value); "
                "} else if (doc.containsKey('timestamp') && doc['timestamp'].size() != 0) { "
                "emit(doc['timestamp'].value); "
                "}"
            )
        },
    }
}

DATA_VIEWS = (
    "metrics-*",
    "logs-*",
    "elasticsearch-sm-metrics",
    "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9",
)

PACKAGES = (
    ("system", "1.60.0"),
    ("elasticsearch", "1.12.0"),
    ("kibana", "2.3.1"),
    ("elastic_agent", "2.3.0"),
)


def kibana_curl(kb, auth: str, method: str, path: str, body: dict | None = None) -> dict:
    data = ""
    if body is not None:
        data = f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' -X {method} "
        f"'http://127.0.0.1:5601{path}' {data}",
        check=False,
        timeout=120,
    )
    if not out.strip():
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out[:500]}


def fix_monitoring_ui_creds(kb, monitoring_pwd: str) -> None:
    print("=== Configure Stack Monitoring UI ES credentials ===", flush=True)
    copy_scripts(kb, roles=("kibana",))
    run(
        kb,
        f"MONITORING_PASS={shlex.quote(monitoring_pwd)} bash {REMOTE}/fix-dashboard-search.sh",
        timeout=600,
        check=False,
    )


def patch_data_view(kb, auth: str, view_id: str) -> None:
    obj = kibana_curl(kb, auth, "GET", f"/api/saved_objects/index-pattern/{view_id}")
    if obj.get("statusCode") == 404:
        print(f"  skip missing data view {view_id}", flush=True)
        return
    attrs = obj.get("attributes", {})
    runtime = attrs.get("runtimeFieldMap", "{}")
    try:
        runtime_map = json.loads(runtime) if runtime else {}
    except json.JSONDecodeError:
        runtime_map = {}
    if "@timestamp" not in runtime_map:
        runtime_map["@timestamp"] = RUNTIME_TIMESTAMP["@timestamp"]
    attrs["runtimeFieldMap"] = json.dumps(runtime_map)
    attrs["allowNoIndex"] = True
    body = {"attributes": attrs}
    resp = kibana_curl(
        kb,
        auth,
        "PUT",
        f"/api/saved_objects/index-pattern/{view_id}?overwrite=true",
        body,
    )
    if resp.get("statusCode", 200) >= 400:
        print(f"  WARN update {view_id}: {resp}", flush=True)
    else:
        print(f"  updated data view {view_id} (runtime @timestamp)", flush=True)

    refresh = kibana_curl(
        kb,
        auth,
        "POST",
        f"/api/index_patterns/index_pattern/{view_id}/fields/_refresh?metaFields=_source&metaFields=_id&metaFields=_type&metaFields=_index&metaFields=_score",
    )
    if refresh.get("statusCode", 200) >= 400:
        refresh = kibana_curl(kb, auth, "POST", f"/api/data_views/data_view/{view_id}/fields/refresh")
    if refresh.get("statusCode", 200) >= 400:
        print(f"  field refresh {view_id}: {str(refresh)[:200]}", flush=True)
    else:
        print(f"  refreshed fields for {view_id}", flush=True)


def reinstall_package_assets(kb, auth: str, name: str, version: str) -> None:
    print(f"=== Reinstall {name}@{version} Kibana assets ===", flush=True)
    resp = kibana_curl(
        kb,
        auth,
        "POST",
        f"/api/fleet/epm/packages/{name}/{version}",
        {"force": True},
    )
    status = resp.get("statusCode", 200)
    item = resp.get("item", resp)
    install_status = item.get("install_status") if isinstance(item, dict) else None
    print(f"  {name}: http={status} install_status={install_status}", flush=True)


def verify_dashboard_search(kb, auth: str) -> bool:
    print("=== Verify dashboard search paths ===", flush=True)
    ok = True
    checks = [
        ("metrics-*", "metrics-*"),
        ("elasticsearch-sm-metrics", "metrics-*,metricbeat-*,.monitoring-*"),
        ("es-stack-monitoring", ".ds-.monitoring-es-*,.monitoring-es*,.ds-metrics-elasticsearch.stack_monitoring.*"),
    ]
    for label, index in checks:
        body = {
            "params": {
                "index": index,
                "body": {
                    "size": 0,
                    "query": {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}},
                },
            }
        }
        resp = kibana_curl(kb, auth, "POST", "/internal/search/ese", body)
        raw = resp.get("rawResponse", {})
        failed = raw.get("_shards", {}).get("failed", 1)
        if failed or resp.get("statusCode", 200) >= 400:
            print(f"  FAIL {label}: {resp.get('message', resp)[:200]}", flush=True)
            ok = False
        else:
            total = raw.get("hits", {}).get("total", 0)
            print(f"  OK {label}: hits={total} failed_shards={failed}", flush=True)
    return ok


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    _user, monitoring_pwd = ensure_monitoring_user(es, run, elastic_pwd)
    es.close()

    kb_ip = NODES["kibana"][0]
    kb = connect(kb_ip)
    auth = curl_elastic_auth(elastic_pwd)

    fix_monitoring_ui_creds(kb, monitoring_pwd)
    kb.close()

    if not wait_kibana_stable(kb_ip, elastic_pwd=elastic_pwd, max_attempts=30):
        print("ERROR: Kibana not stable after monitoring UI config", flush=True)
        return 1

    kb = connect(kb_ip)
    print("=== Patch Fleet data views ===", flush=True)
    for view_id in DATA_VIEWS:
        patch_data_view(kb, auth, view_id)

    for name, version in PACKAGES:
        reinstall_package_assets(kb, auth, name, version)

    ok = verify_dashboard_search(kb, auth)
    kb.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())