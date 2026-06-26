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

# Elasticsearch integration reinstall resets managed SM data views; patch after install.
PACKAGES = (("elasticsearch", "1.12.0"),)

INGEST_PIPELINE_KQL = (
    'service.type:elasticsearch or data_stream.dataset:"elasticsearch.ingest_pipeline"'
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
    resp = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{view_id}")
    dv = resp.get("data_view")
    if not dv:
        obj = kibana_curl(kb, auth, "GET", f"/api/saved_objects/index-pattern/{view_id}")
        if obj.get("statusCode") == 404:
            print(f"  skip missing data view {view_id}", flush=True)
            return
        attrs = obj.get("attributes", {})
        runtime_map = {}
        try:
            runtime_map = json.loads(attrs.get("runtimeFieldMap", "{}") or "{}")
        except json.JSONDecodeError:
            runtime_map = {}
        if "@timestamp" not in runtime_map:
            runtime_map["@timestamp"] = RUNTIME_TIMESTAMP["@timestamp"]
        attrs["runtimeFieldMap"] = json.dumps(runtime_map)
        attrs["allowNoIndex"] = True
        put = kibana_curl(
            kb,
            auth,
            "PUT",
            f"/api/saved_objects/index-pattern/{view_id}?overwrite=true",
            {"attributes": attrs},
        )
        if put.get("statusCode", 200) >= 400:
            print(f"  WARN saved_objects update {view_id}: {put}", flush=True)
        else:
            print(f"  updated {view_id} via saved_objects (runtime @timestamp)", flush=True)
        return

    runtime_map = dict(dv.get("runtimeFieldMap") or {})
    if "@timestamp" not in runtime_map:
        runtime_map["@timestamp"] = RUNTIME_TIMESTAMP["@timestamp"]
    body = {
        "data_view": {
            "title": dv["title"],
            "name": dv.get("name"),
            "timeFieldName": dv.get("timeFieldName", "@timestamp"),
            "runtimeFieldMap": runtime_map,
            "allowNoIndex": True,
        }
    }
    put = kibana_curl(kb, auth, "POST", f"/api/data_views/data_view/{view_id}", body)
    if put.get("statusCode", 200) >= 400:
        print(f"  WARN data_views update {view_id}: {put}", flush=True)
        return
    fields = put.get("data_view", {}).get("fields", {})
    runtime = put.get("data_view", {}).get("runtimeFieldMap", {})
    print(
        f"  updated {view_id} (runtime @timestamp, fields={len(fields)}, "
        f"runtime_keys={list(runtime.keys())})",
        flush=True,
    )


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
                    "query": {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
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


def verify_ingest_pipeline_dashboard(kb, auth: str) -> bool:
    """Verify Lens-style queries used by [Elasticsearch] Ingest Pipeline Detail."""
    print("=== Verify Ingest Pipeline Detail dashboard queries ===", flush=True)
    index = "metrics-*,metricbeat-*,.monitoring-*"
    body = {
        "params": {
            "index": index,
            "body": {
                "size": 0,
                "query": {
                    "bool": {
                        "filter": [
                            {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
                            {"query_string": {"query": INGEST_PIPELINE_KQL}},
                        ]
                    }
                },
                "aggs": {
                    "pipelines": {
                        "terms": {"field": "elasticsearch.ingest_pipeline.name", "size": 5},
                        "aggs": {
                            "m": {"max": {"field": "elasticsearch.ingest_pipeline.total.count"}},
                            "h": {
                                "date_histogram": {"field": "@timestamp", "fixed_interval": "1h"},
                                "aggs": {
                                    "m2": {"max": {"field": "elasticsearch.ingest_pipeline.total.count"}},
                                    "rate": {"derivative": {"buckets_path": "m2"}},
                                },
                            },
                        },
                    }
                },
            },
        }
    }
    resp = kibana_curl(kb, auth, "POST", "/internal/search/ese", body)
    raw = resp.get("rawResponse", {})
    failed = raw.get("_shards", {}).get("failed", 1)
    total = raw.get("hits", {}).get("total", 0)
    if failed or resp.get("statusCode", 200) >= 400:
        print(f"  FAIL ingest pipeline lens query: {resp.get('message', resp)[:200]}", flush=True)
        return False
    print(f"  OK ingest pipeline lens query: hits={total} failed_shards={failed}", flush=True)
    return True


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
    for name, version in PACKAGES:
        reinstall_package_assets(kb, auth, name, version)

    print("=== Patch Fleet / Stack Monitoring data views (after package reinstall) ===", flush=True)
    for view_id in DATA_VIEWS:
        patch_data_view(kb, auth, view_id)

    ok = verify_dashboard_search(kb, auth) and verify_ingest_pipeline_dashboard(kb, auth)
    kb.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())