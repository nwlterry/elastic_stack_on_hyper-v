#!/usr/bin/env python3
"""
Create / refresh Kibana cluster operations dashboard.

Builds panels by cloning working Fleet [Elasticsearch] Lens templates and
mutating fields — hand-built Lens JSON does not mount in Kibana 8.18 dashboards.
"""
from __future__ import annotations

import copy
import json
import uuid
from urllib.parse import quote

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import (
    STACK_MONITORING_DATA_VIEW,
    _data_view_path,
    _runtime_map_for_view,
    ensure_stack_monitoring_data_view_id_matches_title,
    fix_monitoring_ui_creds,
    kibana_curl,
)

SYSTEM_UPTIME_RUNTIME_FIELDS = {
    "ism.uptime.duration.sec": {
        "type": "double",
        "script": {
            "source": (
                "if (doc['system.uptime.duration.ms'].size() == 0) return; "
                "emit(doc['system.uptime.duration.ms'].value / 1000.0);"
            )
        },
    },
}
CLUSTER_OPS_RUNTIME_FIELDS = {
    "ism.node.disk.used.pct": {
        "type": "double",
        "script": {
            "source": (
                "if (doc['elasticsearch.node.stats.fs.total.total_in_bytes'].size() == 0 "
                "|| doc['elasticsearch.node.stats.fs.total.available_in_bytes'].size() == 0) return; "
                "double total = doc['elasticsearch.node.stats.fs.total.total_in_bytes'].value; "
                "double avail = doc['elasticsearch.node.stats.fs.total.available_in_bytes'].value; "
                "emit(Math.round((1.0 - avail / total) * 1000.0) / 10.0);"
            )
        },
    },
    "ism.shard.key": {
        "type": "keyword",
        "script": {
            "source": (
                "if (doc['elasticsearch.index.name'].size() == 0 "
                "|| doc['elasticsearch.shard.number'].size() == 0) return; "
                "String pri = doc['elasticsearch.shard.primary'].size() > 0 "
                "? doc['elasticsearch.shard.primary'].value.toString() : '?'; "
                "emit(doc['elasticsearch.index.name'].value + ':' + "
                "doc['elasticsearch.shard.number'].value + ':' + pri);"
            )
        },
    },
    "ism.deployment.tier": {
        "type": "keyword",
        "script": {
            "source": (
                "if (doc['elasticsearch.node.name'].size() == 0) return; "
                "String node = doc['elasticsearch.node.name'].value; "
                "if (node.equals('ismelkesnode01.ocplab.net')) emit('master / warm'); "
                "else if (node.equals('ismelkesnode02.ocplab.net')) emit('data_hot / ml'); "
                "else if (node.equals('ismelkesnode03.ocplab.net')) emit('data_cold');"
            )
        },
    },
}
from monitoring_credentials import ensure_monitoring_user

DASHBOARD_ID = "ism-cluster-operations-dashboard"
DASHBOARD_TITLE = "[Elasticsearch] Cluster Operations Overview"
FLEET_DASH_ID = "elasticsearch-b1399af0-628c-11ee-9c63-732d7f759a7a"
SM_DV = STACK_MONITORING_DATA_VIEW
SYS_DV = "metrics-system.*"

TIER_HOSTS = {
    "master (es01)": "ismelkesnode01.ocplab.net",
    "data_hot (es02)": "ismelkesnode02.ocplab.net",
    "data_cold (es03)": "ismelkesnode03.ocplab.net",
    "data_warm (es01)": "ismelkesnode01.ocplab.net",
    "ml (es02)": "ismelkesnode02.ocplab.net",
    "kibana": "ismelkkbnnode01.ocplab.net",
}


def _uid() -> str:
    return str(uuid.uuid4())


def _data_view_path(view_id: str) -> str:
    return quote(view_id, safe="")


def _load_fleet_templates(kb, auth: str) -> tuple[dict, dict]:
    resp = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{FLEET_DASH_ID}")
    panels = json.loads(resp["attributes"]["panelsJSON"])
    metric_tpl = next(
        p
        for p in panels
        if p.get("embeddableConfig", {}).get("attributes", {}).get("title")
        == "Total Storage per cluster (largest clusters first)"
    )
    table_tpl = next(
        p
        for p in panels
        if p.get("embeddableConfig", {}).get("attributes", {}).get("visualizationType")
        == "lnsDatatable"
    )
    return copy.deepcopy(metric_tpl), copy.deepcopy(table_tpl)


def _panel_state(panel: dict) -> dict:
    attrs = panel["embeddableConfig"]["attributes"]
    state = attrs["state"]
    return state if isinstance(state, dict) else json.loads(state)


def _layer_bundle(state: dict) -> tuple[str, dict]:
    layers = state["datasourceStates"]["formBased"]["layers"]
    layer_id = next(iter(layers))
    return layer_id, layers[layer_id]


def _clone_panel(template: dict, title: str, *, x: int, y: int, w: int, h: int) -> dict:
    panel = copy.deepcopy(template)
    panel_id = str(uuid.uuid4())
    panel["panelIndex"] = panel_id
    panel["gridData"] = {"x": x, "y": y, "w": w, "h": h, "i": panel_id}
    panel["title"] = title
    attrs = panel["embeddableConfig"]["attributes"]
    attrs["title"] = title
    return panel


def _dash_refs_for_panel(panel: dict) -> list[dict]:
    panel_id = panel["panelIndex"]
    return [
        {
            "id": ref["id"],
            "name": f"{panel_id}:{ref['name']}",
            "type": ref["type"],
        }
        for ref in panel["embeddableConfig"]["attributes"].get("references", [])
    ]


def _strip_metric_breakdown(state: dict) -> None:
    layer_id, layer = _layer_bundle(state)
    viz = state["visualization"]
    terms_id = viz.pop("breakdownByAccessor", None)
    if terms_id:
        layer["columnOrder"] = [cid for cid in layer["columnOrder"] if cid != terms_id]
        layer["columns"].pop(terms_id, None)
    metric_id = viz["metricAccessor"]
    layer["columnOrder"] = [metric_id]
    viz.pop("collapseFn", None)
    viz.pop("maxCols", None)
    viz.pop("palette", None)


def _set_metric_field(
    panel: dict,
    *,
    label: str,
    field: str,
    field_query: str,
    data_type: str = "number",
    format_id: str | None = None,
    format_params: dict | None = None,
    metricset: str = "cluster_stats",
    metrics_only: bool = False,
) -> None:
    state = _panel_state(panel)
    _, layer = _layer_bundle(state)
    viz = state["visualization"]
    metric_id = viz["metricAccessor"]
    metric = layer["columns"][metric_id]
    metric["label"] = label
    metric["customLabel"] = True
    metric["dataType"] = data_type
    metric["sourceField"] = field
    metric["scale"] = "ordinal" if data_type == "string" else "ratio"
    metric["filter"] = {"language": "kuery", "query": field_query}
    if format_id:
        metric["params"]["format"] = {"id": format_id, "params": format_params or {}}
    elif "format" in metric.get("params", {}):
        metric["params"].pop("format", None)

    if metrics_only:
        _strip_metric_breakdown(state)

    filter_ref = state["filters"][0]["meta"]["index"]
    state["filters"] = [_fleet_metricset_filter(metricset, filter_ref)]
    panel["embeddableConfig"]["attributes"]["state"] = state


def _set_simple_metric(
    panel: dict,
    *,
    label: str,
    kql: str,
    field: str,
    operation_type: str,
    data_type: str = "number",
    format_id: str | None = None,
    format_params: dict | None = None,
    metricset: str = "cluster_stats",
    sort_field: str = "@timestamp",
) -> None:
    """Single-value metric panel matching the unassigned-shards Lens style."""
    state = _panel_state(panel)
    _, layer = _layer_bundle(state)
    viz = state["visualization"]
    terms_id = viz.pop("breakdownByAccessor", None)
    if terms_id:
        layer["columnOrder"] = [cid for cid in layer["columnOrder"] if cid != terms_id]
        layer["columns"].pop(terms_id, None)
    metric_id = viz["metricAccessor"]
    params: dict = {}
    if format_id:
        params["format"] = {"id": format_id, "params": format_params or {}}
    if operation_type == "last_value":
        params["sortField"] = sort_field
    layer["columns"][metric_id] = {
        "customLabel": True,
        "dataType": data_type,
        "filter": {"language": "kuery", "query": kql},
        "isBucketed": False,
        "label": label,
        "operationType": operation_type,
        "params": params,
        "scale": "ordinal" if data_type == "string" else "ratio",
        "sourceField": field,
    }
    layer["columnOrder"] = [metric_id]
    viz.pop("collapseFn", None)
    viz.pop("maxCols", None)
    filter_ref = state["filters"][0]["meta"]["index"]
    state["filters"] = [_fleet_metricset_filter(metricset, filter_ref)]
    panel["embeddableConfig"]["attributes"]["state"] = state


def _set_count_metric(
    panel: dict,
    *,
    label: str,
    kql: str,
    field: str = "ism.shard.key",
    metricset: str = "shard",
) -> None:
    _set_simple_metric(
        panel,
        label=label,
        kql=kql,
        field=field,
        operation_type="unique_count",
        format_id="number",
        format_params={"decimals": 0},
        metricset=metricset,
    )


def _fleet_metricset_filter(metricset: str, filter_index: str) -> dict:
    return {
        "$state": {"store": "appState"},
        "meta": {
            "alias": None,
            "disabled": False,
            "index": filter_index,
            "negate": False,
            "type": "combined",
            "relation": "OR",
            "params": [
                {
                    "meta": {
                        "alias": None,
                        "disabled": False,
                        "field": "metricset.name",
                        "index": SM_DV,
                        "key": "metricset.name",
                        "negate": False,
                        "params": {"query": metricset},
                        "type": "phrase",
                    },
                    "query": {"match_phrase": {"metricset.name": metricset}},
                },
                {
                    "meta": {
                        "alias": None,
                        "disabled": False,
                        "field": "metricset.name",
                        "index": SM_DV,
                        "key": "metricset.name",
                        "negate": True,
                        "type": "exists",
                        "value": "exists",
                    },
                    "query": {"exists": {"field": "metricset.name"}},
                },
            ],
        },
        "query": {},
    }


def _set_uptime_metric(panel: dict, *, host: str, tier: str) -> None:
    state = _panel_state(panel)
    layer_id, layer = _layer_bundle(state)
    viz = state["visualization"]
    metric_id = viz["metricAccessor"]
    host_id = viz.get("breakdownByAccessor") or str(uuid.uuid4())
    viz["breakdownByAccessor"] = host_id
    viz.setdefault("collapseFn", "")
    viz.setdefault("maxCols", 5)
    layer["columns"][host_id] = {
        "customLabel": True,
        "dataType": "string",
        "isBucketed": True,
        "label": "Host",
        "operationType": "terms",
        "params": {
            "exclude": [],
            "excludeIsRegex": False,
            "include": [host],
            "includeIsRegex": False,
            "missingBucket": False,
            "orderBy": {"type": "column", "columnId": metric_id},
            "orderDirection": "desc",
            "otherBucket": False,
            "parentFormat": {"id": "terms"},
            "size": 1,
        },
        "scale": "ordinal",
        "sourceField": "host.name",
    }
    layer["columns"][metric_id] = {
        "customLabel": True,
        "dataType": "number",
        "filter": {
            "language": "kuery",
            "query": f'host.name : "{host}" and data_stream.dataset : "system.uptime"',
        },
        "isBucketed": False,
        "label": f"Uptime ({tier})",
        "operationType": "last_value",
        "params": {
            "sortField": "@timestamp",
            "format": {
                "id": "duration",
                "params": {
                    "inputFormat": "seconds",
                    "outputFormat": "humanizePrecise",
                    "useShortSuffix": True,
                    "decimals": 1,
                },
            },
        },
        "scale": "ratio",
        "sourceField": "ism.uptime.duration.sec",
    }
    layer["columnOrder"] = [host_id, metric_id]
    state["filters"] = []
    attrs = panel["embeddableConfig"]["attributes"]
    attrs["state"] = state
    attrs["references"] = [
        {"id": SYS_DV, "name": f"indexpattern-datasource-layer-{layer_id}", "type": "index-pattern"},
    ]


def _set_node_table(
    panel: dict,
    *,
    title: str,
    metricset: str,
    bucket_field: str,
    bucket_label: str,
    metrics: list[dict],
    order_by_metric_id: str | None = None,
) -> None:
    state = _panel_state(panel)
    layer_id, _old_layer = _layer_bundle(state)
    bucket_id = str(uuid.uuid4())
    cols: dict = {
        bucket_id: {
            "customLabel": True,
            "dataType": "string",
            "isBucketed": True,
            "label": bucket_label,
            "operationType": "terms",
            "params": {
                "exclude": [],
                "excludeIsRegex": False,
                "include": [],
                "includeIsRegex": False,
                "missingBucket": False,
                "orderBy": {
                    "type": "column",
                    "columnId": order_by_metric_id or metrics[0]["id"],
                },
                "orderDirection": "desc",
                "otherBucket": False,
                "parentFormat": {"id": "terms"},
                "size": 10,
            },
            "scale": "ordinal",
            "sourceField": bucket_field,
        }
    }
    column_order = [bucket_id]
    viz_columns = [{"columnId": bucket_id}]
    for spec in metrics:
        cols[spec["id"]] = spec["column"]
        column_order.append(spec["id"])
        viz_columns.append({"columnId": spec["id"]})

    state["datasourceStates"]["formBased"]["layers"][layer_id] = {
        "columnOrder": column_order,
        "columns": cols,
        "incompleteColumns": {},
        "sampling": 0.1,
    }
    filter_ref = state["filters"][0]["meta"]["index"]
    state["filters"] = [_fleet_metricset_filter(metricset, filter_ref)]
    state["visualization"] = {
        "columns": viz_columns,
        "layerId": layer_id,
        "layerType": "data",
    }
    attrs = panel["embeddableConfig"]["attributes"]
    attrs["title"] = title
    attrs["state"] = state
    panel["title"] = title


def _lv_col(col_id: str, label: str, field: str, *, fmt: dict | None = None) -> dict:
    col = {
        "customLabel": True,
        "dataType": "number",
        "filter": {"language": "kuery", "query": f"{field}: *"},
        "isBucketed": False,
        "label": label,
        "operationType": "last_value",
        "params": {"sortField": "@timestamp"},
        "scale": "ratio",
        "sourceField": field,
    }
    if fmt:
        col["params"]["format"] = fmt
    return col


def build_dashboard(kb, auth: str) -> tuple[dict, list[dict]]:
    metric_tpl, table_tpl = _load_fleet_templates(kb, auth)
    panels: list[dict] = []
    dash_refs: list[dict] = [
        {"id": SM_DV, "name": SM_DV, "type": "index-pattern"},
    ]

    def add(panel: dict) -> None:
        panels.append(panel)
        dash_refs.extend(_dash_refs_for_panel(panel))

    kpis = [
        (
            "Cluster status",
            {
                "label": "Cluster status",
                "field": "elasticsearch.cluster.stats.status",
                "kql": "elasticsearch.cluster.stats.status: *",
                "data_type": "string",
            },
        ),
        (
            "Index count",
            {
                "label": "Index count",
                "field": "elasticsearch.cluster.stats.indices.total",
                "kql": "elasticsearch.cluster.stats.indices.total: *",
                "format_id": "number",
                "format_params": {"decimals": 0},
            },
        ),
        (
            "Shard count",
            {
                "label": "Shard count",
                "field": "elasticsearch.cluster.stats.indices.shards.count",
                "kql": "elasticsearch.cluster.stats.indices.shards.count: *",
                "format_id": "number",
                "format_params": {"decimals": 0},
            },
        ),
    ]
    for i, (title, spec) in enumerate(kpis):
        p = _clone_panel(metric_tpl, title, x=i * 12, y=0, w=12, h=8)
        _set_simple_metric(
            p,
            metricset="cluster_stats",
            operation_type="last_value",
            **spec,
        )
        add(p)

    unassigned = _clone_panel(metric_tpl, "Unassigned shards (5m)", x=36, y=0, w=12, h=8)
    _set_count_metric(
        unassigned,
        label="Unassigned shards",
        kql=(
            'elasticsearch.shard.state : "UNASSIGNED" '
            "and @timestamp >= now-5m"
        ),
        metricset="shard",
    )
    add(unassigned)

    y0 = 8
    for i, (tier, host) in enumerate(TIER_HOSTS.items()):
        title = f"System uptime — {host} ({tier})"
        p = _clone_panel(metric_tpl, title, x=(i % 3) * 16, y=y0 + (i // 3) * 7, w=16, h=7)
        _set_uptime_metric(p, host=host, tier=tier)
        add(p)

    total_id, avail_id, pct_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    disk = _clone_panel(table_tpl, "ES node disk usage by tier", x=0, y=22, w=24, h=14)
    _set_node_table(
        disk,
        title="ES node disk usage by tier",
        metricset="node_stats",
        bucket_field="elasticsearch.node.name",
        bucket_label="Node",
        metrics=[
            {
                "id": total_id,
                "column": _lv_col(
                    total_id,
                    "Disk total",
                    "elasticsearch.node.stats.fs.total.total_in_bytes",
                    fmt={"id": "bytes", "params": {"decimals": 1}},
                ),
            },
            {
                "id": avail_id,
                "column": _lv_col(
                    avail_id,
                    "Disk available",
                    "elasticsearch.node.stats.fs.total.available_in_bytes",
                    fmt={"id": "bytes", "params": {"decimals": 1}},
                ),
            },
            {
                "id": pct_id,
                "column": _lv_col(
                    pct_id,
                    "Disk used %",
                    "ism.node.disk.used.pct",
                    fmt={"id": "number", "params": {"decimals": 1, "suffix": "%"}},
                ),
            },
        ],
        order_by_metric_id=pct_id,
    )
    add(disk)

    jvm_used_id, jvm_max_id, jvm_pct_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    jvm = _clone_panel(table_tpl, "ES node JVM heap usage", x=24, y=22, w=24, h=14)
    _set_node_table(
        jvm,
        title="ES node JVM heap usage",
        metricset="node_stats",
        bucket_field="elasticsearch.node.name",
        bucket_label="Node",
        metrics=[
            {
                "id": jvm_used_id,
                "column": _lv_col(
                    jvm_used_id,
                    "JVM heap used",
                    "elasticsearch.node.stats.jvm.mem.heap.used.bytes",
                    fmt={"id": "bytes", "params": {"decimals": 1}},
                ),
            },
            {
                "id": jvm_max_id,
                "column": _lv_col(
                    jvm_max_id,
                    "JVM heap max",
                    "elasticsearch.node.stats.jvm.mem.heap.max.bytes",
                    fmt={"id": "bytes", "params": {"decimals": 1}},
                ),
            },
            {
                "id": jvm_pct_id,
                "column": _lv_col(
                    jvm_pct_id,
                    "JVM heap %",
                    "elasticsearch.node.stats.jvm.mem.heap.used.pct",
                    fmt={"id": "number", "params": {"decimals": 0, "suffix": "%"}},
                ),
            },
        ],
        order_by_metric_id=jvm_pct_id,
    )
    add(jvm)

    attributes = {
        "title": DASHBOARD_TITLE,
        "description": (
            "Cluster health from Fleet stack monitoring. "
            "Tier mapping: es01=warm/master, es02=hot/ml, es03=cold, kbn01=kibana."
        ),
        "timeRestore": True,
        "timeFrom": "now-24h/h",
        "timeTo": "now",
        "refreshInterval": {"pause": True, "value": 60000},
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps(
                {"query": {"language": "kuery", "query": ""}, "filter": []}
            )
        },
        "optionsJSON": json.dumps(
            {
                "useMargins": True,
                "syncColors": False,
                "syncCursor": True,
                "syncTooltips": False,
                "hidePanelTitles": False,
            }
        ),
        "panelsJSON": json.dumps(panels),
        "version": 1,
    }
    return attributes, dash_refs


def ensure_cluster_ops_runtime_fields(kb, auth: str) -> bool:
    view_path = _data_view_path(SM_DV)
    resp = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{view_path}")
    dv = resp.get("data_view")
    if not dv:
        print(f"  FAIL missing data view {SM_DV}", flush=True)
        return False
    runtime_map = _runtime_map_for_view(SM_DV, dict(dv.get("runtimeFieldMap") or {}))
    runtime_map.update(CLUSTER_OPS_RUNTIME_FIELDS)
    put = kibana_curl(
        kb,
        auth,
        "POST",
        f"/api/data_views/data_view/{view_path}",
        {
            "data_view": {
                "title": dv["title"],
                "name": dv.get("name"),
                "timeFieldName": dv.get("timeFieldName", "@timestamp"),
                "runtimeFieldMap": runtime_map,
                "allowNoIndex": True,
            }
        },
    )
    if put.get("statusCode", 200) >= 400:
        print(f"  FAIL runtime fields: {put.get('message', put)[:200]}", flush=True)
        return False
    print(f"  OK runtime fields {list(CLUSTER_OPS_RUNTIME_FIELDS)}", flush=True)
    return True


def ensure_system_metrics_data_view(kb, auth: str) -> bool:
    dv_id = SYS_DV
    view_path = _data_view_path(dv_id)
    resp = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{view_path}")
    dv = resp.get("data_view")
    if not dv:
        attrs = {
            "title": dv_id,
            "timeFieldName": "@timestamp",
            "runtimeFieldMap": json.dumps({}),
            "allowNoIndex": True,
        }
        create = kibana_curl(
            kb,
            auth,
            "POST",
            f"/api/saved_objects/index-pattern/{view_path}",
            {"attributes": attrs},
        )
        if create.get("statusCode", 200) >= 400:
            print(f"  FAIL create {dv_id}: {create.get('message', create)[:200]}", flush=True)
            return False
        resp = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{view_path}")
        dv = resp.get("data_view")
        if not dv:
            print(f"  FAIL missing data view {dv_id} after create", flush=True)
            return False

    runtime_map = _runtime_map_for_view(dv_id, dict(dv.get("runtimeFieldMap") or {}))
    runtime_map.update(SYSTEM_UPTIME_RUNTIME_FIELDS)
    put = kibana_curl(
        kb,
        auth,
        "POST",
        f"/api/data_views/data_view/{view_path}",
        {
            "data_view": {
                "title": dv["title"],
                "name": dv.get("name"),
                "timeFieldName": dv.get("timeFieldName", "@timestamp"),
                "runtimeFieldMap": runtime_map,
                "allowNoIndex": True,
            }
        },
    )
    if put.get("statusCode", 200) >= 400:
        print(f"  FAIL system runtime fields: {put.get('message', put)[:200]}", flush=True)
        return False
    print(f"  OK runtime fields {list(SYSTEM_UPTIME_RUNTIME_FIELDS)} on {dv_id}", flush=True)
    return True


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    kb = connect(NODES["kibana"][0])

    _mon_user, mon_pwd = ensure_monitoring_user(es, run, elastic_pwd)
    fix_monitoring_ui_creds(kb, mon_pwd)

    print("=== Ensure data views ===", flush=True)
    ensure_stack_monitoring_data_view_id_matches_title(kb, auth)
    if not ensure_cluster_ops_runtime_fields(kb, auth):
        kb.close()
        es.close()
        return 1
    if not ensure_system_metrics_data_view(kb, auth):
        kb.close()
        es.close()
        return 1

    print("=== Deploy dashboard ===", flush=True)
    attributes, references = build_dashboard(kb, auth)
    resp = kibana_curl(
        kb,
        auth,
        "POST",
        f"/api/saved_objects/dashboard/{DASHBOARD_ID}?overwrite=true",
        {"attributes": attributes, "references": references},
    )
    if resp.get("statusCode", 200) >= 400:
        print(json.dumps(resp, indent=2)[:3000], flush=True)
        kb.close()
        es.close()
        return 1

    print(f"OK {DASHBOARD_ID} (refs={len(references)})", flush=True)
    print(
        f"URL: http://{NODES['kibana'][0]}:5601/app/dashboards#/view/{DASHBOARD_ID}",
        flush=True,
    )
    kb.close()
    es.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())