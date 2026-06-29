#!/usr/bin/env python3
"""Fix Kibana dashboard 'error while executing search' issues."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from urllib.parse import quote

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
    "metrics-elasticsearch.ingest_pipeline-*",
    "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9",
)

# Elasticsearch integration reinstall resets managed SM data views; patch after install.
PACKAGES = (("elasticsearch", "1.12.0"),)

INGEST_PIPELINE_KQL = (
    'service.type:elasticsearch or data_stream.dataset:"elasticsearch.ingest_pipeline"'
)
PROCESSOR_EXISTS_FIELD = "elasticsearch.ingest_pipeline.processor.type_tag"
PIPELINE_EXISTS_FIELD = "elasticsearch.ingest_pipeline.total.count"
# KQL "field:*" is not translated by Elasticsearch query_string (Lens may fall back to it).
# Scope panels with exists filters in state.filters; keep KQL to the base ingest filter only.
PROCESSOR_PANEL_KQL = INGEST_PIPELINE_KQL
PIPELINE_PANEL_KQL = INGEST_PIPELINE_KQL

# Broad pattern causes multi_terms type conflicts between ingest_pipeline (long
# order_index) and stack_monitoring indices; only ingest dashboards use this view.
INGEST_PIPELINE_INDEX = "metrics-elasticsearch.ingest_pipeline-*"
# Fleet package id; must not be used as /internal/data_views/fields pattern (0 fields).
INGEST_PIPELINE_DATA_VIEW_LEGACY = "elasticsearch-sm-metrics"
INGEST_PIPELINE_DATA_VIEW = INGEST_PIPELINE_INDEX
INGEST_PIPELINE_DASHBOARD = "elasticsearch-ea5b81a0-7fbf-11ed-8509-ddabeb9daeaf"
INGEST_PIPELINE_DASHBOARD_FIXED = "elasticsearch-ingest-pipeline-detail-fixed"
INGEST_PIPELINE_DASHBOARD_FIXED_TITLE = (
    "[Elasticsearch] Ingest Pipeline Detail (Fixed)"
)
STACK_MONITORING_DATA_VIEW = "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9"
CLUSTER_INGEST_DASHBOARDS = frozenset(
    {
        "elasticsearch-ea888f80-61e4-11ee-b5a1-0d1803efe5cf",
        "elasticsearch-b1399af0-628c-11ee-9c63-732d7f759a7a",
    }
)

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


def _is_ingest_pipeline_view(view_id: str) -> bool:
    return view_id in {INGEST_PIPELINE_DATA_VIEW, INGEST_PIPELINE_DATA_VIEW_LEGACY}


def _runtime_map_for_view(view_id: str, runtime_map: dict) -> dict:
    """Return runtime fields appropriate for each data view."""
    runtime_map = dict(runtime_map)
    if _is_ingest_pipeline_view(view_id):
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
        if _is_ingest_pipeline_view(view_id):
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
    if _is_ingest_pipeline_view(view_id):
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


def _data_view_path(view_id: str) -> str:
    return quote(view_id, safe="")


def _replace_data_view_id_in_value(obj, old_id: str, new_id: str) -> bool:
    """Recursively replace index-pattern/data-view ids (incl. embedded JSON strings)."""
    changed = False
    if isinstance(obj, dict):
        if obj.get("id") == old_id and obj.get("type") in ("index-pattern", "data-view"):
            obj["id"] = new_id
            changed = True
        for key, val in list(obj.items()):
            if key in ("panelsJSON", "controlGroupInput") and isinstance(val, str) and old_id in val:
                try:
                    parsed = json.loads(val)
                except json.JSONDecodeError:
                    parsed = None
                if parsed is not None and _replace_data_view_id_in_value(parsed, old_id, new_id):
                    obj[key] = json.dumps(parsed)
                    changed = True
            elif _replace_data_view_id_in_value(val, old_id, new_id):
                changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _replace_data_view_id_in_value(item, old_id, new_id):
                changed = True
    return changed


def ensure_ingest_data_view_id_matches_title(kb, auth: str) -> bool:
    """Align ingest data view id with index title so Lens fields API returns fields.

    Working Fleet dashboards use id==title (e.g. metrics-*). elasticsearch-sm-metrics
    with title metrics-elasticsearch.ingest_pipeline-* makes the UI call
    /internal/data_views/fields?pattern=elasticsearch-sm-metrics → 0 fields → .map() crash.
    """
    old_id = INGEST_PIPELINE_DATA_VIEW_LEGACY
    new_id = INGEST_PIPELINE_DATA_VIEW
    print("=== Align ingest data view id with index pattern ===", flush=True)

    new_resp = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{_data_view_path(new_id)}")
    if new_resp.get("data_view"):
        print(f"  OK data view {new_id} already exists", flush=True)
    else:
        legacy = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{old_id}").get("data_view", {})
        if not legacy:
            print(f"  FAIL missing legacy data view {old_id}", flush=True)
            return False
        runtime_map = _runtime_map_for_view(old_id, dict(legacy.get("runtimeFieldMap") or {}))
        attrs = {
            "title": INGEST_PIPELINE_INDEX,
            "timeFieldName": legacy.get("timeFieldName", "@timestamp"),
            "runtimeFieldMap": json.dumps(runtime_map),
            "allowNoIndex": True,
        }
        post = kibana_curl(
            kb,
            auth,
            "POST",
            f"/api/saved_objects/index-pattern/{_data_view_path(new_id)}",
            {"attributes": attrs},
        )
        if post.get("statusCode", 200) >= 400:
            print(f"  FAIL create {new_id}: {post.get('message', post)[:200]}", flush=True)
            return False
        print(f"  OK created data view id={new_id}", flush=True)

    # Verify fields API works with id-as-pattern (what Lens uses).
    qs = (
        f"pattern={quote(INGEST_PIPELINE_INDEX, safe='')}"
        "&meta_fields=_source&allow_no_index=true&apiVersion=1"
    )
    fields_resp = kibana_curl(kb, auth, "GET", f"/internal/data_views/fields?{qs}")
    fields = fields_resp.get("fields") or []
    field_count = len(fields) if isinstance(fields, list) else len(fields or {})
    if field_count == 0:
        print(f"  FAIL fields API pattern={new_id}: 0 fields", flush=True)
        return False
    print(f"  OK fields API pattern={new_id}: {field_count} fields", flush=True)

    updated = 0
    for item in list_elasticsearch_dashboards(kb, auth):
        did = item["id"]
        dash = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{did}")
        if dash.get("statusCode", 200) >= 400:
            continue
        attrs = dash.get("attributes", {})
        refs = list(dash.get("references", []))
        payload = {"attributes": dict(attrs), "references": refs}
        if not _replace_data_view_id_in_value(payload, old_id, new_id):
            continue
        payload["attributes"]["version"] = int(attrs.get("version", 1) or 1) + 1
        put = kibana_curl(
            kb,
            auth,
            "PUT",
            f"/api/saved_objects/dashboard/{did}?overwrite=true",
            payload,
        )
        if put.get("statusCode", 200) >= 400:
            print(
                f"  FAIL repoint {item.get('attributes', {}).get('title', did)}: "
                f"{put.get('message', put)[:120]}",
                flush=True,
            )
            return False
        updated += 1
        print(f"  OK repointed {item.get('attributes', {}).get('title', did)}", flush=True)

    fixed = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{INGEST_PIPELINE_DASHBOARD_FIXED}")
    if fixed.get("id"):
        attrs = fixed.get("attributes", {})
        refs = list(fixed.get("references", []))
        payload = {"attributes": dict(attrs), "references": refs}
        if _replace_data_view_id_in_value(payload, old_id, new_id):
            payload["attributes"]["version"] = int(attrs.get("version", 1) or 1) + 1
            kibana_curl(
                kb,
                auth,
                "PUT",
                f"/api/saved_objects/dashboard/{INGEST_PIPELINE_DASHBOARD_FIXED}?overwrite=true",
                payload,
            )

    patch_data_view(kb, auth, new_id)
    print(f"  dashboards_updated={updated}", flush=True)
    return True


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
        (INGEST_PIPELINE_DATA_VIEW, INGEST_PIPELINE_INDEX),
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
    if normalized == base:
        return True
    # Prior patch used KQL "field:*" suffixes that do not work in Elasticsearch.
    if "ingest_pipeline.total.count:*" in normalized:
        return True
    if "ingest_pipeline.processor.type_tag:*" in normalized:
        return True
    if normalized.startswith("(") and "elasticsearch.ingest_pipeline" in normalized:
        return True
    return False


def _panel_exists_field(state: dict) -> str | None:
    """Pick exists filter field for processor vs pipeline doc types."""
    if _panel_uses_processor_fields(state):
        return PROCESSOR_EXISTS_FIELD
    if _panel_uses_pipeline_total_fields(state):
        return PIPELINE_EXISTS_FIELD
    text = json.dumps(state.get("datasourceStates", {}))
    if "ingest_pipeline.processor" in text:
        return PROCESSOR_EXISTS_FIELD
    if "ingest_pipeline.total" in text:
        return PIPELINE_EXISTS_FIELD
    if _is_legacy_unscoped_kql(state.get("query", {}).get("query", "")):
        return PIPELINE_EXISTS_FIELD
    return None


def _exists_filter(field: str) -> dict:
    return {
        "$state": {"store": "appState"},
        "meta": {
            "alias": None,
            "disabled": False,
            "key": field,
            "negate": False,
            "type": "exists",
            "value": "exists",
        },
        "query": {"exists": {"field": field}},
    }


def _has_exists_filter(state: dict, field: str) -> bool:
    for f in state.get("filters") or []:
        if (
            f.get("meta", {}).get("type") == "exists"
            and f.get("meta", {}).get("key") == field
        ):
            return True
    return False


def _is_ingest_pipeline_panel(state: dict) -> bool:
    """True when panel queries ingest-pipeline metrics (not stack-monitoring cluster views)."""
    if _panel_uses_processor_fields(state) or _panel_uses_pipeline_total_fields(state):
        return True
    blob = json.dumps(state.get("datasourceStates", {}))
    return "ingest_pipeline" in blob


def _patch_lens_panel_queries(state: dict) -> bool:
    """Normalize panel KQL; ingest panels get base filter, SM/cluster panels stay empty."""
    changed = False
    query = state.setdefault("query", {"language": "kuery", "query": ""})
    if _is_ingest_pipeline_panel(state):
        target_kql = INGEST_PIPELINE_KQL
        current = " ".join((query.get("query") or "").split())
        target_norm = " ".join(target_kql.split())
        if current != target_norm:
            query["language"] = "kuery"
            query["query"] = target_kql
            changed = True
    elif (query.get("query") or "").strip():
        # Cluster Ingest dashboards use metricset filters in state.filters, not KQL.
        query["language"] = "kuery"
        query["query"] = ""
        changed = True
    filters = state.get("filters")
    if filters is None:
        state["filters"] = []
        changed = True
        filters = []
    elif not isinstance(filters, list):
        state["filters"] = []
        changed = True
        filters = []
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
    return changed


def _normalize_lens_columns(state: dict) -> bool:
    """Fix Lens state shapes that crash the dashboard UI (e.g. .map on undefined)."""
    changed = False
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
    if not _is_ingest_pipeline_panel(state):
        return False
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
    changed = _normalize_lens_visualization(state) or changed
    changed = _ensure_lens_defaults(state) or changed
    return changed


def _migrate_legacy_metric(lens_attrs: dict, state: dict) -> bool:
    """lnsLegacyMetric crashes in Kibana 8.18 UI; migrate to lnsMetric."""
    if lens_attrs.get("visualizationType") != "lnsLegacyMetric":
        return False
    viz = state.setdefault("visualization", {})
    accessor = viz.pop("accessor", None)
    if accessor and not viz.get("metricAccessor"):
        viz["metricAccessor"] = accessor
    lens_attrs["visualizationType"] = "lnsMetric"
    return True


def _complete_lns_metric_viz(lens_attrs: dict, state: dict) -> bool:
    """Match working Fleet lnsMetric panels (palette/showBar present)."""
    if lens_attrs.get("visualizationType") != "lnsMetric":
        return False
    viz = state.setdefault("visualization", {})
    changed = False
    if viz.get("showBar") is None:
        viz["showBar"] = False
        changed = True
    if not viz.get("palette"):
        viz["palette"] = {"name": "default", "type": "palette"}
        changed = True
    return changed


def _normalize_lens_visualization(state: dict) -> bool:
    """Fix visualization shapes that crash Lens renderers (.map on undefined)."""
    changed = False
    viz = state.get("visualization")
    if not isinstance(viz, dict):
        return changed
    layers = viz.get("layers")
    if layers is None and "layerId" not in viz:
        viz["layers"] = []
        changed = True
    if isinstance(layers, list):
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            if layer.get("accessors") is None:
                layer["accessors"] = []
                changed = True
            if layer.get("yConfig") is None:
                layer["yConfig"] = []
                changed = True
    return changed


def _ensure_lens_defaults(state: dict) -> bool:
    """Ensure list fields exist so Lens client code can safely .map()."""
    changed = False
    if state.get("internalReferences") is None:
        state["internalReferences"] = []
        changed = True
    if state.get("filters") is None:
        state["filters"] = []
        changed = True
    if state.get("adHocDataViews") is None:
        state["adHocDataViews"] = {}
        changed = True
    layers = state.get("datasourceStates", {}).get("formBased", {}).get("layers", {})
    for layer in layers.values():
        if layer.get("columnOrder") is None:
            layer["columnOrder"] = list(layer.get("columns", {}).keys())
            changed = True
        if layer.get("incompleteColumns") is None:
            layer["incompleteColumns"] = {}
            changed = True
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


def _patch_dashboard_lens_panels(
    attrs: dict, *, force_touch: bool = True
) -> tuple[dict, list[str], int, int]:
    """Normalize/patch Lens panels; returns (attrs, titles, corrupt_fixed, legacy_migrated)."""
    panels = json.loads(attrs.get("panelsJSON", "[]"))
    patched_titles: list[str] = []
    corrupt_fixed = 0
    legacy_migrated = 0
    for panel in panels:
        if panel.get("type") != "lens":
            continue
        emb = panel.get("embeddableConfig", {})
        lens_attrs = emb.get("attributes", {})
        state_raw = lens_attrs.get("state")
        if not state_raw:
            continue
        if isinstance(state_raw, dict):
            state = dict(state_raw)
        elif isinstance(state_raw, str):
            state = json.loads(state_raw)
            if isinstance(state, str):
                corrupt_fixed += 1
                state = json.loads(state)
        else:
            continue
        changed = _patch_lens_state(state)
        if _migrate_legacy_metric(lens_attrs, state):
            legacy_migrated += 1
            changed = True
        changed = _complete_lns_metric_viz(lens_attrs, state) or changed
        # Kibana 8.18 dashboard embed expects state as an object, not a JSON string.
        needs_write = force_touch or isinstance(state_raw, str) or changed
        if not needs_write:
            continue
        lens_attrs["state"] = state
        emb["attributes"] = lens_attrs
        panel["embeddableConfig"] = emb
        panel.pop("version", None)
        patched_titles.append(panel.get("title") or panel.get("id", "lens"))
    if patched_titles:
        attrs = dict(attrs)
        attrs["panelsJSON"] = json.dumps(panels)
    return attrs, patched_titles, corrupt_fixed, legacy_migrated


def clone_ingest_pipeline_dashboard(kb, auth: str) -> bool:
    """Clone patched ingest dashboard to an unmanaged copy (avoids Fleet managed cache)."""
    print("=== Clone ingest pipeline dashboard (unmanaged) ===", flush=True)
    src = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{INGEST_PIPELINE_DASHBOARD}")
    if src.get("statusCode", 200) >= 400:
        print(f"  FAIL fetch source: {src.get('message', src)[:200]}", flush=True)
        return False
    attrs, _, _, _ = _patch_dashboard_lens_panels(src.get("attributes", {}))
    attrs["title"] = INGEST_PIPELINE_DASHBOARD_FIXED_TITLE
    attrs["version"] = 1
    refs = [
        r
        for r in src.get("references", [])
        if not (r.get("type") == "tag" and str(r.get("id", "")).startswith("fleet-"))
    ]
    existing = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{INGEST_PIPELINE_DASHBOARD_FIXED}")
    if existing.get("id"):
        put = kibana_curl(
            kb,
            auth,
            "PUT",
            f"/api/saved_objects/dashboard/{INGEST_PIPELINE_DASHBOARD_FIXED}?overwrite=true",
            {"attributes": attrs, "references": refs},
        )
        action = "updated"
    else:
        put = kibana_curl(
            kb,
            auth,
            "POST",
            f"/api/saved_objects/dashboard/{INGEST_PIPELINE_DASHBOARD_FIXED}",
            {"attributes": attrs, "references": refs},
        )
        action = "created"
    if put.get("statusCode", 200) >= 400:
        print(f"  FAIL clone: {put.get('message', put)[:200]}", flush=True)
        return False
    print(
        f"  OK {action} {INGEST_PIPELINE_DASHBOARD_FIXED} "
        f"title={INGEST_PIPELINE_DASHBOARD_FIXED_TITLE}",
        flush=True,
    )
    return True


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
        new_attrs, patched_titles, corrupt_fixed, legacy_migrated = _patch_dashboard_lens_panels(attrs)
        if not patched_titles:
            print(f"  skip {title}: no lens panels", flush=True)
            continue
        new_attrs["version"] = int(attrs.get("version", 1) or 1) + 1
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
            f"corrupt_state_fixed={corrupt_fixed} legacy_metric_migrated={legacy_migrated}",
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
    """Ensure all [Elasticsearch] dashboards have dict Lens state and working controls."""
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
            if not isinstance(state_raw, dict):
                corrupt += 1
        bad_kql = 0
        for panel in panels:
            if panel.get("type") != "lens":
                continue
            state_raw = panel.get("embeddableConfig", {}).get("attributes", {}).get("state")
            if not isinstance(state_raw, dict):
                continue
            if not _is_ingest_pipeline_panel(state_raw) and (state_raw.get("query", {}).get("query") or "").strip():
                bad_kql += 1
        if corrupt:
            print(f"  FAIL {title}: corrupt_lens_state={corrupt}/{lens_count}", flush=True)
            ok = False
        elif bad_kql:
            print(f"  FAIL {title}: non_ingest_kql={bad_kql}/{lens_count}", flush=True)
            ok = False
        else:
            print(f"  OK {title}: lens={lens_count} corrupt=0 kql_ok", flush=True)

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
    orderagg_body = {
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
    }
    # Direct ES search avoids Kibana async-search 200ms timeout on heavy aggs.
    es = connect(NODES["es01"][0])
    es_out = run(
        es,
        f"curl -sk -u {auth} -H 'Content-Type: application/json' "
        f"-X POST 'https://localhost:9200/{index}/_search' "
        f"-d '{json.dumps(orderagg_body)}'",
        check=False,
    )
    es.close()
    try:
        raw = json.loads(es_out.strip().splitlines()[-1])
    except json.JSONDecodeError:
        raw = {}
    buckets = raw.get("aggregations", {}).get("procs", {}).get("buckets", [])
    err_msg = str(raw.get("error", es_out))[:200]
    if raw.get("_shards", {}).get("failed", 1) or not buckets:
        print(
            f"  FAIL processor orderAgg (Lens breakdown sort): "
            f"buckets={len(buckets)} err={err_msg}",
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
    if os.environ.get("SKIP_FLEET_REINSTALL", "").lower() not in ("1", "true", "yes"):
        for name, version in PACKAGES:
            reinstall_package_assets(kb, auth, name, version)
        print("=== Patch Fleet / Stack Monitoring data views (after package reinstall) ===", flush=True)
    else:
        print("=== Skip Fleet reinstall (SKIP_FLEET_REINSTALL set) ===", flush=True)
    for view_id in DATA_VIEWS:
        patch_data_view(kb, auth, view_id)

    ensure_ingest_data_view_id_matches_title(kb, auth)
    patch_elasticsearch_dashboards(kb, auth)
    clone_ingest_pipeline_dashboard(kb, auth)

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