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
PROCESSOR_EXISTS_FIELD = "elasticsearch.ingest_pipeline.processor.type_tag"
PIPELINE_EXISTS_FIELD = "elasticsearch.ingest_pipeline.total.count"
PROCESSOR_PANEL_KQL = (
    "(service.type:elasticsearch or data_stream.dataset:\"elasticsearch.ingest_pipeline\") "
    f"and {PROCESSOR_EXISTS_FIELD}:*"
)
PIPELINE_PANEL_KQL = (
    "(service.type:elasticsearch or data_stream.dataset:\"elasticsearch.ingest_pipeline\") "
    f"and {PIPELINE_EXISTS_FIELD}:*"
)

# Broad pattern causes multi_terms type conflicts between ingest_pipeline (long
# order_index) and stack_monitoring indices; only ingest dashboards use this view.
INGEST_PIPELINE_INDEX = "metrics-elasticsearch.ingest_pipeline-*"
INGEST_PIPELINE_DATA_VIEW = "elasticsearch-sm-metrics"
INGEST_PIPELINE_DASHBOARD = "elasticsearch-ea5b81a0-7fbf-11ed-8509-ddabeb9daeaf"

# Processor-level docs lack pipeline total.count; Lens orderAgg must use processor.count.
PIPELINE_TOTAL_COUNT_FIELD = "elasticsearch.ingest_pipeline.total.count"
PROCESSOR_COUNT_FIELD = "elasticsearch.ingest_pipeline.processor.count"
COUNTER_COUNT_FIELDS = frozenset(
    {
        PIPELINE_TOTAL_COUNT_FIELD,
        PROCESSOR_COUNT_FIELD,
        "elasticsearch.ingest_pipeline.total.failed",
        "elasticsearch.ingest_pipeline.processor.failed",
    }
)

CONTROL_FIELDS = (
    "elasticsearch.cluster.name",
    "elasticsearch.node.roles",
    "elasticsearch.node.name",
    "elasticsearch.ingest_pipeline.name",
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


def _runtime_map_for_view(view_id: str, runtime_map: dict) -> dict:
    """Return runtime fields appropriate for each data view."""
    runtime_map = dict(runtime_map)
    if view_id == INGEST_PIPELINE_DATA_VIEW:
        # Ingest pipeline Fleet metrics already have native @timestamp. A runtime
        # @timestamp that reads doc['@timestamp'] causes a cyclic dependency when
        # controls/Lens apply dashboard time filters (search_phase_execution_exception).
        runtime_map.pop("@timestamp", None)
        return runtime_map
    if "@timestamp" not in runtime_map:
        runtime_map["@timestamp"] = RUNTIME_TIMESTAMP["@timestamp"]
    return runtime_map


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
        runtime_map = _runtime_map_for_view(view_id, runtime_map)
        if view_id == INGEST_PIPELINE_DATA_VIEW:
            attrs["title"] = INGEST_PIPELINE_INDEX
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
            print(
                f"  updated {view_id} via saved_objects "
                f"(runtime_keys={list(runtime_map.keys())})",
                flush=True,
            )
        return

    runtime_map = _runtime_map_for_view(view_id, dict(dv.get("runtimeFieldMap") or {}))
    title = dv["title"]
    if view_id == INGEST_PIPELINE_DATA_VIEW:
        title = INGEST_PIPELINE_INDEX
    body = {
        "data_view": {
            "title": title,
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
        f"  updated {view_id} title={title} (fields={len(fields)}, "
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
        ("elasticsearch-sm-metrics", INGEST_PIPELINE_INDEX),
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


def _ingest_pipeline_query() -> dict:
    return {
        "bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
                {"query_string": {"query": INGEST_PIPELINE_KQL}},
            ]
        }
    }


def _search_ok(kb, auth: str, label: str, index: str, body: dict) -> bool:
    resp = kibana_curl(
        kb,
        auth,
        "POST",
        "/internal/search/ese",
        {"params": {"index": index, "body": body}},
    )
    raw = resp.get("rawResponse", {})
    failed = raw.get("_shards", {}).get("failed", 1)
    if failed or resp.get("statusCode", 200) >= 400:
        print(f"  FAIL {label}: {resp.get('message', resp)[:200]}", flush=True)
        return False
    total = raw.get("hits", {}).get("total", 0)
    print(f"  OK {label}: hits={total} failed_shards={failed}", flush=True)
    return True


def _iter_lens_columns(state: dict):
    layers = state.get("datasourceStates", {}).get("formBased", {}).get("layers", {})
    for layer in layers.values():
        for col in layer.get("columns", {}).values():
            yield col


def _column_text(col: dict) -> str:
    parts = [col.get("sourceField") or "", col.get("params", {}).get("formula", "")]
    order_agg = col.get("params", {}).get("orderAgg") or {}
    parts.append(order_agg.get("sourceField") or "")
    return " ".join(parts)


def _panel_uses_processor_fields(state: dict) -> bool:
    return any("ingest_pipeline.processor" in _column_text(col) for col in _iter_lens_columns(state))


def _panel_uses_pipeline_total_fields(state: dict) -> bool:
    return any("ingest_pipeline.total" in _column_text(col) for col in _iter_lens_columns(state))


def _is_legacy_unscoped_kql(kql: str) -> bool:
    """Detect Fleet default / whitespace-variant ingest pipeline KQL."""
    if not kql or not kql.strip():
        return True
    normalized = " ".join(kql.split())
    base = " ".join(INGEST_PIPELINE_KQL.split())
    return normalized == base


def _panel_scope_kql(state: dict) -> str | None:
    """Pick processor- vs pipeline-scoped KQL for this Lens panel."""
    if _panel_uses_processor_fields(state):
        return PROCESSOR_PANEL_KQL
    if _panel_uses_pipeline_total_fields(state):
        return PIPELINE_PANEL_KQL
    text = json.dumps(state.get("datasourceStates", {}))
    if "ingest_pipeline.processor" in text:
        return PROCESSOR_PANEL_KQL
    if "ingest_pipeline.total" in text:
        return PIPELINE_PANEL_KQL
    if _is_legacy_unscoped_kql(state.get("query", {}).get("query", "")):
        return PIPELINE_PANEL_KQL
    return None


def _patch_lens_panel_queries(state: dict) -> bool:
    """Scope queries to processor vs pipeline doc types (mutually exclusive in this index)."""
    target = _panel_scope_kql(state)
    if not target:
        return False
    query = state.setdefault("query", {"language": "kuery", "query": ""})
    if query.get("query") == target:
        return False
    query["language"] = "kuery"
    query["query"] = target
    return True


def _normalize_lens_columns(state: dict) -> bool:
    """Fix Lens state shapes that crash the dashboard UI (e.g. .map on undefined)."""
    changed = False
    filters = state.get("filters")
    if isinstance(filters, list):
        cleaned = [
            f
            for f in filters
            if not (
                f.get("meta", {}).get("type") == "exists"
                and f.get("meta", {}).get("key")
                in {PROCESSOR_EXISTS_FIELD, PIPELINE_EXISTS_FIELD}
            )
        ]
        if len(cleaned) != len(filters):
            state["filters"] = cleaned
            changed = True
    for col in _iter_lens_columns(state):
        if col.get("operationType") != "terms":
            continue
        params = col.setdefault("params", {})
        if params.get("parentFormat", {}).get("id") == "multi_terms":
            if params.get("secondaryFields") is None:
                params["secondaryFields"] = []
                changed = True
        elif "secondaryFields" in params and params.get("secondaryFields") is None:
            params["secondaryFields"] = []
            changed = True
    return changed


def _patch_lens_order_aggs(state: dict) -> bool:
    """Fix order-by metrics that are null or invalid for the doc type in each panel."""
    uses_processor = _panel_uses_processor_fields(state)
    changed = False
    for col in _iter_lens_columns(state):
        if col.get("operationType") != "terms":
            continue
        order_agg = col.get("params", {}).get("orderAgg")
        if not order_agg:
            continue
        source = order_agg.get("sourceField")
        # Processor panels: pipeline total.count is always null on processor docs.
        if uses_processor and source == PIPELINE_TOTAL_COUNT_FIELD:
            order_agg["sourceField"] = PROCESSOR_COUNT_FIELD
            label = order_agg.get("label", "")
            if PIPELINE_TOTAL_COUNT_FIELD in label:
                order_agg["label"] = label.replace(
                    PIPELINE_TOTAL_COUNT_FIELD,
                    PROCESSOR_COUNT_FIELD,
                )
            changed = True
            source = PROCESSOR_COUNT_FIELD
        # TSDS counter fields: Lens/ES behave more reliably with max than sum for ordering.
        if order_agg.get("operationType") == "sum" and source in COUNTER_COUNT_FIELDS:
            order_agg["operationType"] = "max"
            label = order_agg.get("label", "")
            if label.startswith("Sum of "):
                order_agg["label"] = label.replace("Sum of ", "Maximum of ", 1)
            changed = True
    return changed


def _patch_lens_state(state: dict) -> bool:
    changed = _normalize_lens_columns(state)
    changed = _patch_lens_panel_queries(state) or changed
    changed = _patch_lens_order_aggs(state) or changed
    return changed


def list_elasticsearch_dashboards(kb, auth: str) -> list[dict]:
    """Return Fleet [Elasticsearch] dashboards (managed package assets)."""
    resp = kibana_curl(
        kb,
        auth,
        "GET",
        "/api/saved_objects/_find?type=dashboard&per_page=100&search_fields=title&search=%5BElasticsearch%5D",
    )
    return resp.get("saved_objects", [])


def _patch_dashboard_lens_panels(attrs: dict) -> tuple[dict, list[str], int]:
    """Normalize/patch all Lens panels; returns (attrs, patched_titles, corrupt_fixed)."""
    panels = json.loads(attrs.get("panelsJSON", "[]"))
    patched_titles: list[str] = []
    corrupt_fixed = 0
    for panel in panels:
        if panel.get("type") != "lens":
            continue
        emb = panel.get("embeddableConfig", {})
        lens_attrs = emb.get("attributes", {})
        state_raw = lens_attrs.get("state")
        if not state_raw:
            continue
        if isinstance(state_raw, dict):
            corrupt_fixed += 1
        state = json.loads(state_raw) if isinstance(state_raw, str) else dict(state_raw)
        needs_write = not isinstance(state_raw, str) or _patch_lens_state(state)
        if not needs_write:
            continue
        lens_attrs["state"] = json.dumps(state)
        emb["attributes"] = lens_attrs
        panel["embeddableConfig"] = emb
        patched_titles.append(panel.get("title") or panel.get("id", "lens"))
    if patched_titles:
        attrs = dict(attrs)
        attrs["panelsJSON"] = json.dumps(panels)
    return attrs, patched_titles, corrupt_fixed


def patch_elasticsearch_dashboards(kb, auth: str) -> bool:
    """Patch all Fleet [Elasticsearch] dashboards (Lens state + ingest pipeline fixes)."""
    print("=== Patch [Elasticsearch] dashboard Lens panels ===", flush=True)
    ok = True
    dashboards = list_elasticsearch_dashboards(kb, auth)
    if not dashboards:
        print("  WARN no [Elasticsearch] dashboards found", flush=True)
        return False
    for item in dashboards:
        did = item["id"]
        title = item.get("attributes", {}).get("title", did)
        resp = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{did}")
        if resp.get("statusCode", 200) >= 400:
            print(f"  WARN fetch failed {title}: {resp.get('message', resp)[:120]}", flush=True)
            ok = False
            continue
        attrs = resp.get("attributes", {})
        new_attrs, patched_titles, corrupt_fixed = _patch_dashboard_lens_panels(attrs)
        if not patched_titles:
            print(f"  skip {title}: lens panels already normalized", flush=True)
            continue
        put = kibana_curl(
            kb,
            auth,
            "PUT",
            f"/api/saved_objects/dashboard/{did}?overwrite=true",
            {"attributes": new_attrs},
        )
        if put.get("statusCode", 200) >= 400:
            print(f"  FAIL {title}: {put.get('message', put)[:200]}", flush=True)
            ok = False
            continue
        print(
            f"  OK {title}: panels={len(patched_titles)} "
            f"corrupt_state_fixed={corrupt_fixed}",
            flush=True,
        )
    return ok


def patch_ingest_pipeline_dashboard(kb, auth: str) -> None:
    """Backward-compatible entry point for ingest pipeline dashboard patching."""
    patch_elasticsearch_dashboards(kb, auth)


def verify_controls_api(kb, auth: str) -> bool:
    """Verify options-list controls API (same path/body the Kibana UI uses)."""
    print("=== Verify dashboard controls API ===", flush=True)
    ok = True
    body = {
        "size": 50,
        "fieldName": "elasticsearch.cluster.name",
        "allowExpensiveQueries": True,
        "filters": [
            {"query_string": {"query": INGEST_PIPELINE_KQL}},
            {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
        ],
        "selectedOptions": [],
        "searchString": "",
    }
    for field in CONTROL_FIELDS:
        body["fieldName"] = field
        resp = kibana_curl(
            kb,
            auth,
            "POST",
            f"/internal/controls/optionsList/{INGEST_PIPELINE_INDEX}",
            body,
        )
        suggestions = resp.get("suggestions") or []
        if resp.get("statusCode", 200) >= 400 or not suggestions:
            print(
                f"  FAIL control {field}: "
                f"{resp.get('message', resp)[:200]}",
                flush=True,
            )
            ok = False
        else:
            print(
                f"  OK control {field}: "
                f"{len(suggestions)} options (e.g. {suggestions[0]['value']})",
                flush=True,
            )
    return ok


def verify_elasticsearch_dashboards(kb, auth: str) -> bool:
    """Ensure all [Elasticsearch] dashboards have string Lens state and working controls."""
    print("=== Verify [Elasticsearch] dashboards ===", flush=True)
    ok = True
    dv_index: dict[str, str] = {}
    for vid in (INGEST_PIPELINE_DATA_VIEW, "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9"):
        dv = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{vid}").get("data_view", {})
        if dv:
            dv_index[vid] = dv.get("title", vid)

    for item in list_elasticsearch_dashboards(kb, auth):
        did = item["id"]
        title = item.get("attributes", {}).get("title", did)
        dash = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{did}")
        attrs = dash.get("attributes", {})
        panels = json.loads(attrs.get("panelsJSON", "[]"))
        corrupt = 0
        lens_count = 0
        for panel in panels:
            if panel.get("type") != "lens":
                continue
            lens_count += 1
            state_raw = panel.get("embeddableConfig", {}).get("attributes", {}).get("state")
            if isinstance(state_raw, dict):
                corrupt += 1
        if corrupt:
            print(f"  FAIL {title}: corrupt_lens_state={corrupt}/{lens_count}", flush=True)
            ok = False
        else:
            print(f"  OK {title}: lens={lens_count} corrupt=0", flush=True)

        control_fields: list[str] = []
        ctrl_input = attrs.get("controlGroupInput")
        if ctrl_input:
            try:
                cg = json.loads(ctrl_input) if isinstance(ctrl_input, str) else ctrl_input
                cpanels = (
                    json.loads(cg.get("panelsJSON", "{}"))
                    if isinstance(cg.get("panelsJSON"), str)
                    else cg.get("panelsJSON", {})
                )
                for cp in cpanels.values():
                    if cp.get("type") == "optionsListControl":
                        field = cp.get("explicitInput", {}).get("fieldName", "")
                        if field:
                            control_fields.append(field)
            except (json.JSONDecodeError, TypeError, AttributeError):
                print(f"  FAIL {title}: corrupt controlGroupInput", flush=True)
                ok = False
                continue

        ctrl_dv_ids = [
            r["id"]
            for r in dash.get("references", [])
            if r.get("type") == "index-pattern" and "controlGroup" in r.get("name", "")
        ]
        ctrl_index = ""
        for dv_id in ctrl_dv_ids:
            if dv_id in dv_index:
                ctrl_index = dv_index[dv_id]
                break
        if not ctrl_index:
            panel_dv_ids = {
                r["id"] for r in dash.get("references", []) if r.get("type") == "index-pattern"
            }
            if INGEST_PIPELINE_DATA_VIEW in panel_dv_ids:
                ctrl_index = dv_index.get(INGEST_PIPELINE_DATA_VIEW, "")
            elif "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9" in panel_dv_ids:
                ctrl_index = dv_index.get("befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9", "")

        for field in control_fields:
            body = {
                "size": 50,
                "fieldName": field,
                "allowExpensiveQueries": True,
                "filters": [{"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}}],
                "selectedOptions": [],
                "searchString": "",
            }
            resp = kibana_curl(kb, auth, "POST", f"/internal/controls/optionsList/{ctrl_index}", body)
            suggestions = resp.get("suggestions") or []
            if resp.get("statusCode", 200) >= 400 or not suggestions:
                print(
                    f"  FAIL {title}: control {field} on {ctrl_index}: "
                    f"{resp.get('message', resp)[:120]}",
                    flush=True,
                )
                ok = False
            else:
                print(
                    f"  OK {title}: control {field} ({len(suggestions)} options)",
                    flush=True,
                )
    return ok


def verify_ingest_pipeline_dashboard(kb, auth: str) -> bool:
    """Verify controls and processor Lens queries on the ingest pipeline data view."""
    print("=== Verify Ingest Pipeline dashboard queries ===", flush=True)
    index = INGEST_PIPELINE_INDEX
    ok = True
    for field in CONTROL_FIELDS:
        ok &= _search_ok(
            kb,
            auth,
            f"control {field.split('.')[-1]}",
            index,
            {
                "size": 0,
                "query": _ingest_pipeline_query(),
                "aggs": {"opts": {"terms": {"field": field, "size": 20}}},
            },
        )
    ok &= _search_ok(
        kb,
        auth,
        "processor multi_terms (EPS/Time/Avg panels)",
        index,
        {
            "size": 0,
            "query": _ingest_pipeline_query(),
            "aggs": {
                "split": {
                    "multi_terms": {
                        "terms": [
                            {"field": "elasticsearch.ingest_pipeline.processor.type_tag"},
                            {"field": "elasticsearch.ingest_pipeline.processor.order_index"},
                        ],
                        "size": 10,
                    },
                    "aggs": {
                        "h": {
                            "date_histogram": {"field": "@timestamp", "fixed_interval": "1h"},
                            "aggs": {
                                "cnt_m": {"max": {"field": PROCESSOR_COUNT_FIELD}},
                                "cnt_r": {"derivative": {"buckets_path": "cnt_m"}},
                                "time_m": {
                                    "max": {"field": "elasticsearch.ingest_pipeline.processor.time.total.ms"}
                                },
                                "time_r": {"derivative": {"buckets_path": "time_m"}},
                            },
                        }
                    },
                }
            },
        },
    )
    resp = kibana_curl(
        kb,
        auth,
        "POST",
        "/internal/search/ese",
        {
            "params": {
                "index": index,
                "body": {
                    "size": 0,
                    "query": {
                        "bool": {
                            "filter": [
                                {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
                                {"query_string": {"query": INGEST_PIPELINE_KQL}},
                                {"exists": {"field": "elasticsearch.ingest_pipeline.processor.type_tag"}},
                            ]
                        }
                    },
                    "aggs": {
                        "procs": {
                            "multi_terms": {
                                "terms": [
                                    {"field": "elasticsearch.ingest_pipeline.processor.order_index"},
                                    {"field": "elasticsearch.ingest_pipeline.processor.type_tag"},
                                    {"field": "elasticsearch.node.name"},
                                ],
                                "size": 100,
                            },
                            "aggs": {
                                "order_metric": {"max": {"field": PROCESSOR_COUNT_FIELD}},
                                "sorter": {
                                    "bucket_sort": {
                                        "sort": [{"order_metric": {"order": "desc"}}],
                                        "size": 10,
                                    }
                                },
                            },
                        }
                    },
                },
            }
        },
    )
    raw = resp.get("rawResponse", {})
    buckets = raw.get("aggregations", {}).get("procs", {}).get("buckets", [])
    if raw.get("_shards", {}).get("failed", 1) or resp.get("statusCode", 200) >= 400 or not buckets:
        print(
            f"  FAIL processor orderAgg (Lens breakdown sort): "
            f"buckets={len(buckets)} err={resp.get('message', resp)[:200]}",
            flush=True,
        )
        ok = False
    else:
        print(
            f"  OK processor orderAgg (Lens breakdown sort): "
            f"buckets={len(buckets)} top={buckets[0].get('key_as_string', buckets[0].get('key'))}",
            flush=True,
        )
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
    for name, version in PACKAGES:
        reinstall_package_assets(kb, auth, name, version)

    print("=== Patch Fleet / Stack Monitoring data views (after package reinstall) ===", flush=True)
    for view_id in DATA_VIEWS:
        patch_data_view(kb, auth, view_id)

    patch_elasticsearch_dashboards(kb, auth)

    ok = (
        verify_dashboard_search(kb, auth)
        and verify_controls_api(kb, auth)
        and verify_elasticsearch_dashboards(kb, auth)
        and verify_ingest_pipeline_dashboard(kb, auth)
        and _search_ok(kb, auth, "processor panel scope", INGEST_PIPELINE_INDEX, {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
                        {"query_string": {"query": INGEST_PIPELINE_KQL}},
                        {"exists": {"field": PROCESSOR_EXISTS_FIELD}},
                    ]
                }
            },
        })
        and _search_ok(kb, auth, "pipeline panel scope", INGEST_PIPELINE_INDEX, {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
                        {"query_string": {"query": INGEST_PIPELINE_KQL}},
                        {"exists": {"field": PIPELINE_EXISTS_FIELD}},
                    ]
                }
            },
        })
    )
    kb.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())