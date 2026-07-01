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
    STACK_MONITORING_INDEX,
    _complete_lns_metric_viz,
    _ensure_lens_defaults,
    _runtime_map_for_view,
    fix_monitoring_ui_creds,
    kibana_curl,
)
from monitoring_credentials import ensure_monitoring_user

DASHBOARD_ID = "ism-cluster-operations-dashboard"
DASHBOARD_TITLE = "[Elasticsearch] Cluster Operations Overview"
FLEET_DASH_ID = "elasticsearch-b1399af0-628c-11ee-9c63-732d7f759a7a"

ELK_MONITORING_DV_ID = "elk_cluster_monitoring"
ELK_MONITORING_DV_NAME = "elk_cluster_monitoring"
ELK_MONITORING_DV_TITLE = STACK_MONITORING_INDEX

ELK_SYSTEMS_DV_ID = "elk_cluster_systems"
ELK_SYSTEMS_DV_NAME = "elk_cluster_systems"
ELK_SYSTEMS_DV_TITLE = "metrics-system.*"

NODE_REGEX = r"ismelk.*\.ocplab\.net"
ES_NODE_REGEX = r"ismelkesnode\d+\.ocplab\.net"
ELK_SERVICE_NAMES = ("elasticsearch", "kibana", "logstash")

_DEFAULT_METRIC_PALETTE = {"name": "default", "type": "palette"}

_UPTIME_DECOMPOSE_SCRIPT = (
    "long totalSec = doc['system.uptime.duration.ms'].value / 1000L; "
    "totalSec = totalSec % 31536000L; "
    "totalSec = totalSec % 2592000L; "
    "long days = totalSec / 86400L; totalSec = totalSec % 86400L; "
    "long hours = totalSec / 3600L; totalSec = totalSec % 3600L; "
    "long minutes = totalSec / 60L; "
    "long seconds = totalSec % 60L; "
)
_PROCESS_UPTIME_DECOMPOSE_SCRIPT = (
    "if (doc['process.cpu.start_time'].size() == 0) return; "
    "long startMs = doc['process.cpu.start_time'].value.toInstant().toEpochMilli(); "
    "long totalSec = (new Date().getTime() - startMs) / 1000L; "
    "totalSec = totalSec % 31536000L; "
    "totalSec = totalSec % 2592000L; "
    "long days = totalSec / 86400L; totalSec = totalSec % 86400L; "
    "long hours = totalSec / 3600L; totalSec = totalSec % 3600L; "
    "long minutes = totalSec / 60L; "
    "long seconds = totalSec % 60L; "
)

_NODE_ROLE_FROM_ES = (
    "if (doc['elasticsearch.node.name'].size() == 0) return; "
    "String node = doc['elasticsearch.node.name'].value; "
    "if (node.equals('ismelkesnode01.ocplab.net')) emit('master, data_warm'); "
    "else if (node.equals('ismelkesnode02.ocplab.net')) emit('data_hot, ml'); "
    "else if (node.equals('ismelkesnode03.ocplab.net')) emit('data_cold');"
)
_NODE_ROLE_FROM_HOST = (
    "if (doc['host.name'].size() == 0) return; "
    "String node = doc['host.name'].value; "
    "if (node.equals('ismelkesnode01.ocplab.net')) emit('master, data_warm'); "
    "else if (node.equals('ismelkesnode02.ocplab.net')) emit('data_hot, ml'); "
    "else if (node.equals('ismelkesnode03.ocplab.net')) emit('data_cold'); "
    "else if (node.equals('ismelkkbnnode01.ocplab.net')) emit('kibana'); "
    "else if (node.equals('ismelkflnode01.ocplab.net')) emit('fleet');"
)

ELK_MONITORING_RUNTIME_FIELDS = {
    "elk.node.disk.used.pct": {
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
    "elk.node.disk.used.bytes": {
        "type": "double",
        "script": {
            "source": (
                "if (doc['elasticsearch.node.stats.fs.total.total_in_bytes'].size() == 0 "
                "|| doc['elasticsearch.node.stats.fs.total.available_in_bytes'].size() == 0) return; "
                "double total = doc['elasticsearch.node.stats.fs.total.total_in_bytes'].value; "
                "double avail = doc['elasticsearch.node.stats.fs.total.available_in_bytes'].value; "
                "emit(total - avail);"
            )
        },
    },
    "elk.shard.key": {
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
    "elk.deployment.tier": {
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
    "elk.node.role": {
        "type": "keyword",
        "script": {"source": _NODE_ROLE_FROM_ES},
    },
}

ELK_SYSTEM_RUNTIME_FIELDS = {
    "elk.cluster.node.name": {
        "type": "keyword",
        "script": {
            "source": (
                "if (doc['host.name'].size() == 0) return; "
                "emit(doc['host.name'].value);"
            )
        },
    },
    "elk.node.role": {
        "type": "keyword",
        "script": {"source": _NODE_ROLE_FROM_HOST},
    },
    "elk.uptime.duration.sec": {
        "type": "double",
        "script": {
            "source": (
                "if (doc['system.uptime.duration.ms'].size() == 0) return; "
                "emit(doc['system.uptime.duration.ms'].value / 1000.0);"
            )
        },
    },
    "elk.service.name": {
        "type": "keyword",
        "script": {
            "source": (
                "if (doc['system.process.cgroup.cpu.id'].size() == 0) return; "
                "String id = doc['system.process.cgroup.cpu.id'].value; "
                "if (id.equals('elasticsearch.service')) emit('elasticsearch'); "
                "else if (id.equals('kibana.service')) emit('kibana'); "
                "else if (id.equals('logstash.service')) emit('logstash');"
            )
        },
    },
    **{
        field: {
            "type": "long",
            "script": {
                "source": (
                    "if (doc['system.uptime.duration.ms'].size() == 0) return; "
                    f"{_UPTIME_DECOMPOSE_SCRIPT}emit({part});"
                )
            },
        }
        for field, part in [
            ("elk.uptime.days", "days"),
            ("elk.uptime.hours", "hours"),
            ("elk.uptime.minutes", "minutes"),
            ("elk.uptime.seconds", "seconds"),
        ]
    },
    **{
        field: {
            "type": "long",
            "script": {
                "source": (
                    "if (doc['system.process.cgroup.cpu.id'].size() == 0) return; "
                    "String id = doc['system.process.cgroup.cpu.id'].value; "
                    "if (!id.equals('elasticsearch.service') && !id.equals('kibana.service') "
                    "&& !id.equals('logstash.service')) return; "
                    f"{_PROCESS_UPTIME_DECOMPOSE_SCRIPT}emit({part});"
                )
            },
        }
        for field, part in [
            ("elk.service.uptime.days", "days"),
            ("elk.service.uptime.hours", "hours"),
            ("elk.service.uptime.minutes", "minutes"),
            ("elk.service.uptime.seconds", "seconds"),
        ]
    },
}

CLUSTER_STATUS_COLOR_MAPPING = {
    "assignments": [
        {
            "rule": {"type": "matchExactly", "values": ["green"]},
            "color": {"type": "colorCode", "colorCode": "#209280"},
            "touched": True,
        },
        {
            "rule": {"type": "matchExactly", "values": ["yellow"]},
            "color": {"type": "colorCode", "colorCode": "#fec514"},
            "touched": True,
        },
        {
            "rule": {"type": "matchExactly", "values": ["red"]},
            "color": {"type": "colorCode", "colorCode": "#bd271e"},
            "touched": True,
        },
    ],
    "specialAssignments": [
        {
            "rule": {"type": "other"},
            "color": {"type": "loop"},
            "touched": False,
        }
    ],
    "paletteId": "eui_amsterdam_color_blind",
    "colorMode": {"type": "categorical"},
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
    attrs = panel["embeddableConfig"]["attributes"]
    state_raw = attrs.get("state", {})
    state = state_raw if isinstance(state_raw, dict) else json.loads(state_raw)
    adhoc_ids = set((state.get("adHocDataViews") or {}).keys())
    refs = []
    for ref in attrs.get("references", []):
        # Ad-hoc data views live in panel state only — never promote to dashboard refs.
        if ref.get("type") == "index-pattern" and ref.get("id") in adhoc_ids:
            continue
        refs.append(
            {
                "id": ref["id"],
                "name": f"{panel_id}:{ref['name']}",
                "type": ref["type"],
            }
        )
    return refs


def _finalize_panel(panel: dict) -> None:
    """Ensure Lens state is complete and strip orphan ad-hoc data view refs."""
    attrs = panel["embeddableConfig"]["attributes"]
    state = _panel_state(panel)
    _ensure_lens_defaults(state)
    _complete_lns_metric_viz(attrs, state)
    adhoc_ids = set((state.get("adHocDataViews") or {}).keys())
    if adhoc_ids:
        state["adHocDataViews"] = {}
    attrs["references"] = [
        ref
        for ref in (attrs.get("references") or [])
        if not (ref.get("type") == "index-pattern" and ref.get("id") in adhoc_ids)
    ]
    attrs["state"] = state


def _rewrite_panel_refs(panel: dict, *, data_view_id: str = ELK_MONITORING_DV_ID) -> None:
    attrs = panel["embeddableConfig"]["attributes"]
    refs = attrs.get("references") or []
    for ref in refs:
        if ref.get("type") == "index-pattern":
            ref["id"] = data_view_id
    state = _panel_state(panel)
    for flt in state.get("filters") or []:
        meta = flt.get("meta") or {}
        if meta.get("index") and meta["index"] != data_view_id:
            meta["index"] = data_view_id
        for param in meta.get("params") or []:
            pmeta = param.get("meta") or {}
            if pmeta.get("index"):
                pmeta["index"] = data_view_id
    attrs["state"] = state


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
                        "index": filter_index,
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
                        "index": filter_index,
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


def _service_terms_col(
    col_id: str,
    *,
    label: str,
    order_by: str | None = None,
) -> dict:
    return {
        "customLabel": True,
        "dataType": "string",
        "isBucketed": True,
        "label": label,
        "operationType": "terms",
        "params": {
            "exclude": [],
            "excludeIsRegex": False,
            "include": list(ELK_SERVICE_NAMES),
            "includeIsRegex": False,
            "missingBucket": False,
            "orderBy": {
                "type": "column",
                "columnId": order_by or col_id,
            },
            "orderDirection": "desc",
            "otherBucket": False,
            "parentFormat": {"id": "terms"},
            "size": len(ELK_SERVICE_NAMES),
        },
        "scale": "ordinal",
        "sourceField": "elk.service.name",
    }


def _regex_terms_col(
    col_id: str,
    *,
    label: str,
    field: str,
    pattern: str,
    size: int = 10,
    order_by: str | None = None,
) -> dict:
    return {
        "customLabel": True,
        "dataType": "string",
        "isBucketed": True,
        "label": label,
        "operationType": "terms",
        "params": {
            "exclude": [],
            "excludeIsRegex": False,
            "include": [pattern],
            "includeIsRegex": True,
            "missingBucket": False,
            "orderBy": {
                "type": "column",
                "columnId": order_by or col_id,
            },
            "orderDirection": "desc",
            "otherBucket": False,
            "parentFormat": {"id": "terms"},
            "size": size,
        },
        "scale": "ordinal",
        "sourceField": field,
    }


def _lv_col(
    col_id: str,
    label: str,
    field: str,
    *,
    fmt: dict | None = None,
    kql: str | None = None,
) -> dict:
    col = {
        "customLabel": True,
        "dataType": "number",
        "filter": {"language": "kuery", "query": kql or f"{field}: *"},
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


def _uptime_metric_col(col_id: str, label: str, field: str, *, kql: str) -> dict:
    return _lv_col(
        col_id,
        label,
        field,
        kql=kql,
        fmt={"id": "number", "params": {"decimals": 0}},
    )


def _role_terms_col(col_id: str, *, order_by: str) -> dict:
    """Terms bucket — last_value/top_metrics cannot aggregate runtime keyword fields."""
    return {
        "customLabel": True,
        "dataType": "string",
        "isBucketed": True,
        "label": "Role",
        "operationType": "terms",
        "params": {
            "exclude": [],
            "excludeIsRegex": False,
            "include": [],
            "includeIsRegex": False,
            "missingBucket": False,
            "orderBy": {"type": "column", "columnId": order_by},
            "orderDirection": "desc",
            "otherBucket": False,
            "parentFormat": {"id": "terms"},
            "size": 1,
        },
        "scale": "ordinal",
        "sourceField": "elk.node.role",
    }


def _table_viz_columns(
    specs: list[tuple[str, str]],
    *,
    left_align: set[str] | None = None,
) -> list[dict]:
    left = left_align or set()
    cols = []
    for col_id, _label in specs:
        align = "left" if col_id in left else "right"
        cols.append(
            {
                "columnId": col_id,
                "alignment": align,
                "headerAlignment": align,
            }
        )
    return cols


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
    data_view_id: str = ELK_MONITORING_DV_ID,
) -> None:
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
    viz.pop("colorMode", None)
    viz["showBar"] = False
    viz["palette"] = copy.deepcopy(_DEFAULT_METRIC_PALETTE)
    filter_ref = state["filters"][0]["meta"]["index"]
    state["filters"] = [_fleet_metricset_filter(metricset, data_view_id)]
    panel["embeddableConfig"]["attributes"]["state"] = state
    _rewrite_panel_refs(panel, data_view_id=data_view_id)


def _set_count_metric(
    panel: dict,
    *,
    label: str,
    kql: str,
    field: str = "elk.shard.key",
    metricset: str = "shard",
    data_view_id: str = ELK_MONITORING_DV_ID,
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
        data_view_id=data_view_id,
    )


def _set_node_table(
    panel: dict,
    *,
    title: str,
    metricset: str,
    bucket_field: str,
    bucket_label: str,
    node_regex: str,
    metrics: list[dict],
    order_by_metric_id: str | None = None,
    data_view_id: str = ELK_MONITORING_DV_ID,
) -> None:
    state = _panel_state(panel)
    layer_id, _old_layer = _layer_bundle(state)
    bucket_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    cols: dict = {
        bucket_id: _regex_terms_col(
            bucket_id,
            label=bucket_label,
            field=bucket_field,
            pattern=node_regex,
            order_by=order_by_metric_id or metrics[0]["id"],
        ),
        role_id: _role_terms_col(role_id, order_by=order_by_metric_id or metrics[0]["id"]),
    }
    column_order = [bucket_id, role_id]
    for spec in metrics:
        cols[spec["id"]] = spec["column"]
        column_order.append(spec["id"])

    state["datasourceStates"]["formBased"]["layers"][layer_id] = {
        "columnOrder": column_order,
        "columns": cols,
        "incompleteColumns": {},
        "sampling": 0.1,
    }
    state["filters"] = [_fleet_metricset_filter(metricset, data_view_id)]
    state["visualization"] = {
        "columns": _table_viz_columns(
            [(bucket_id, bucket_label), (role_id, "Role"), *[(s["id"], "") for s in metrics]],
            left_align={bucket_id, role_id},
        ),
        "layerId": layer_id,
        "layerType": "data",
    }
    attrs = panel["embeddableConfig"]["attributes"]
    attrs["title"] = title
    attrs["state"] = state
    panel["title"] = title
    _rewrite_panel_refs(panel, data_view_id=data_view_id)


def _set_server_uptime_table(panel: dict) -> None:
    title = "Server uptime — all nodes"
    state = _panel_state(panel)
    layer_id, _old_layer = _layer_bundle(state)
    node_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    part_specs = [
        ("Day", "elk.uptime.days"),
        ("Hour", "elk.uptime.hours"),
        ("Minute", "elk.uptime.minutes"),
        ("Second", "elk.uptime.seconds"),
    ]
    metric_ids = [str(uuid.uuid4()) for _ in part_specs]
    uptime_kql = 'data_stream.dataset : "system.uptime"'
    cols: dict = {
        node_id: _regex_terms_col(
            node_id,
            label="Node",
            field="elk.cluster.node.name",
            pattern=NODE_REGEX,
            size=10,
            order_by=metric_ids[0],
        ),
        role_id: _role_terms_col(role_id, order_by=metric_ids[0]),
    }
    column_order = [node_id, role_id]
    for (label, field), col_id in zip(part_specs, metric_ids, strict=True):
        cols[col_id] = _uptime_metric_col(col_id, label, field, kql=uptime_kql)
        column_order.append(col_id)

    state["datasourceStates"]["formBased"]["layers"][layer_id] = {
        "columnOrder": column_order,
        "columns": cols,
        "incompleteColumns": {},
        "sampling": 0.1,
    }
    state["filters"] = []
    state["visualization"] = {
        "columns": _table_viz_columns(
            [
                (node_id, "Node"),
                (role_id, "Role"),
                *zip(metric_ids, [p[0] for p in part_specs], strict=True),
            ],
            left_align={node_id, role_id},
        ),
        "layerId": layer_id,
        "layerType": "data",
    }
    attrs = panel["embeddableConfig"]["attributes"]
    attrs["title"] = title
    attrs["state"] = state
    panel["title"] = title
    _rewrite_panel_refs(panel, data_view_id=ELK_SYSTEMS_DV_ID)


def _set_cluster_status_table(panel: dict) -> None:
    """Colored status cell — green/yellow/red via Lens datatable color mapping."""
    title = "Cluster status"
    state = _panel_state(panel)
    layer_id, _old_layer = _layer_bundle(state)
    status_id = str(uuid.uuid4())
    count_id = str(uuid.uuid4())
    status_kql = "elasticsearch.cluster.stats.status: *"
    cols: dict = {
        status_id: {
            "customLabel": True,
            "dataType": "string",
            "filter": {"language": "kuery", "query": status_kql},
            "isBucketed": True,
            "label": "Status",
            "operationType": "terms",
            "params": {
                "exclude": [],
                "excludeIsRegex": False,
                "include": [],
                "includeIsRegex": False,
                "missingBucket": False,
                "orderBy": {"type": "column", "columnId": count_id},
                "orderDirection": "desc",
                "otherBucket": False,
                "parentFormat": {"id": "terms"},
                "size": 1,
            },
            "scale": "ordinal",
            "sourceField": "elasticsearch.cluster.stats.status",
        },
        count_id: {
            "customLabel": True,
            "dataType": "number",
            "filter": {"language": "kuery", "query": status_kql},
            "isBucketed": False,
            "label": "Count",
            "operationType": "count",
            "params": {"emptyAsNull": True},
            "scale": "ratio",
            "sourceField": "___records___",
        },
    }
    state["datasourceStates"]["formBased"]["layers"][layer_id] = {
        "columnOrder": [status_id, count_id],
        "columns": cols,
        "incompleteColumns": {},
        "sampling": 0.1,
    }
    state["filters"] = [_fleet_metricset_filter("cluster_stats", ELK_MONITORING_DV_ID)]
    state["headerRowHeight"] = "hide"
    state["visualization"] = {
        "columns": [
            {
                "columnId": status_id,
                "alignment": "center",
                "colorMode": "cell",
                "colorMapping": copy.deepcopy(CLUSTER_STATUS_COLOR_MAPPING),
            },
            {"columnId": count_id, "hidden": True},
        ],
        "layerId": layer_id,
        "layerType": "data",
    }
    attrs = panel["embeddableConfig"]["attributes"]
    attrs["title"] = title
    attrs["state"] = state
    panel["title"] = title
    _rewrite_panel_refs(panel)


def _set_service_uptime_table(panel: dict) -> None:
    title = "Service uptime — all nodes"
    state = _panel_state(panel)
    layer_id, _old_layer = _layer_bundle(state)
    node_id = str(uuid.uuid4())
    role_id = str(uuid.uuid4())
    service_id = str(uuid.uuid4())
    part_specs = [
        ("Day", "elk.service.uptime.days"),
        ("Hour", "elk.service.uptime.hours"),
        ("Minute", "elk.service.uptime.minutes"),
        ("Second", "elk.service.uptime.seconds"),
    ]
    metric_ids = [str(uuid.uuid4()) for _ in part_specs]
    service_kql = (
        'data_stream.dataset : "system.process" and '
        '(elk.service.name : "elasticsearch" or elk.service.name : "kibana" '
        'or elk.service.name : "logstash")'
    )
    cols: dict = {
        node_id: _regex_terms_col(
            node_id,
            label="Node",
            field="elk.cluster.node.name",
            pattern=NODE_REGEX,
            size=10,
            order_by=metric_ids[0],
        ),
        role_id: _role_terms_col(role_id, order_by=metric_ids[0]),
        service_id: _service_terms_col(
            service_id,
            label="Service",
            order_by=metric_ids[0],
        ),
    }
    column_order = [node_id, role_id, service_id]
    for (label, field), col_id in zip(part_specs, metric_ids, strict=True):
        cols[col_id] = _uptime_metric_col(col_id, label, field, kql=service_kql)
        column_order.append(col_id)

    state["datasourceStates"]["formBased"]["layers"][layer_id] = {
        "columnOrder": column_order,
        "columns": cols,
        "incompleteColumns": {},
        "sampling": 0.1,
    }
    state["filters"] = []
    state["visualization"] = {
        "columns": _table_viz_columns(
            [
                (node_id, "Node"),
                (role_id, "Role"),
                (service_id, "Service"),
                *zip(metric_ids, [p[0] for p in part_specs], strict=True),
            ],
            left_align={node_id, role_id, service_id},
        ),
        "layerId": layer_id,
        "layerType": "data",
    }
    attrs = panel["embeddableConfig"]["attributes"]
    attrs["title"] = title
    attrs["state"] = state
    panel["title"] = title
    _rewrite_panel_refs(panel, data_view_id=ELK_SYSTEMS_DV_ID)


def build_dashboard(kb, auth: str) -> tuple[dict, list[dict]]:
    metric_tpl, table_tpl = _load_fleet_templates(kb, auth)
    panels: list[dict] = []
    dash_refs: list[dict] = [
        {"id": ELK_MONITORING_DV_ID, "name": ELK_MONITORING_DV_ID, "type": "index-pattern"},
        {"id": ELK_SYSTEMS_DV_ID, "name": ELK_SYSTEMS_DV_ID, "type": "index-pattern"},
    ]

    def add(panel: dict) -> None:
        _finalize_panel(panel)
        panels.append(panel)
        dash_refs.extend(_dash_refs_for_panel(panel))

    cluster_status = _clone_panel(table_tpl, "Cluster status", x=0, y=0, w=12, h=8)
    _set_cluster_status_table(cluster_status)
    add(cluster_status)

    kpis = [
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
    for i, (title, spec) in enumerate(kpis, start=1):
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

    y_uptime = 8
    server_uptime = _clone_panel(
        table_tpl, "Server uptime — all nodes", x=0, y=y_uptime, w=24, h=12
    )
    _set_server_uptime_table(server_uptime)
    add(server_uptime)

    service_uptime = _clone_panel(
        table_tpl, "Service uptime — all nodes", x=24, y=y_uptime, w=24, h=12
    )
    _set_service_uptime_table(service_uptime)
    add(service_uptime)

    bottom_y = y_uptime + 12
    total_id, used_id, avail_id, pct_id = (
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    )
    disk = _clone_panel(table_tpl, "ES node disk usage by tier", x=0, y=bottom_y, w=24, h=14)
    _set_node_table(
        disk,
        title="ES node disk usage by tier",
        metricset="node_stats",
        bucket_field="elasticsearch.node.name",
        bucket_label="Node",
        node_regex=ES_NODE_REGEX,
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
                "id": used_id,
                "column": _lv_col(
                    used_id,
                    "Disk used",
                    "elk.node.disk.used.bytes",
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
                    "elk.node.disk.used.pct",
                    fmt={"id": "number", "params": {"decimals": 1, "suffix": "%"}},
                ),
            },
        ],
        order_by_metric_id=pct_id,
    )
    add(disk)

    jvm_used_id, jvm_max_id, jvm_pct_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    jvm = _clone_panel(table_tpl, "ES node JVM heap usage", x=24, y=bottom_y, w=24, h=14)
    _set_node_table(
        jvm,
        title="ES node JVM heap usage",
        metricset="node_stats",
        bucket_field="elasticsearch.node.name",
        bucket_label="Node",
        node_regex=ES_NODE_REGEX,
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
            "Cluster health from Fleet stack monitoring "
            f"({ELK_MONITORING_DV_NAME}) and system metrics "
            f"({ELK_SYSTEMS_DV_NAME}). Tier mapping: es01=warm/master, "
            "es02=hot/ml, es03=cold, kbn01=kibana."
        ),
        "timeRestore": True,
        "timeFrom": "now-24h",
        "timeTo": "now",
        "refreshInterval": {"pause": False, "value": 60000},
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


def _ensure_elk_data_view(
    kb,
    auth: str,
    *,
    dv_id: str,
    dv_name: str,
    dv_title: str,
    runtime_fields: dict,
) -> bool:
    view_path = _data_view_path(dv_id)
    resp = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{view_path}")
    dv = resp.get("data_view")
    if not dv:
        create = kibana_curl(
            kb,
            auth,
            "POST",
            f"/api/saved_objects/index-pattern/{view_path}",
            {
                "attributes": {
                    "title": dv_title,
                    "name": dv_name,
                    "timeFieldName": "@timestamp",
                    "runtimeFieldMap": json.dumps({}),
                    "allowNoIndex": True,
                }
            },
        )
        if create.get("statusCode", 200) >= 400:
            print(f"  FAIL create {dv_id}: {create.get('message', create)[:200]}", flush=True)
            return False
        resp = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{view_path}")
        dv = resp.get("data_view")
        if not dv:
            print(f"  FAIL missing data view {dv_id} after create", flush=True)
            return False

    runtime_map = _runtime_map_for_view(dv_id, {})
    runtime_map.update(runtime_fields)
    put = kibana_curl(
        kb,
        auth,
        "POST",
        f"/api/data_views/data_view/{view_path}",
        {
            "data_view": {
                "title": dv_title,
                "name": dv_name,
                "timeFieldName": dv.get("timeFieldName", "@timestamp"),
                "runtimeFieldMap": runtime_map,
                "allowNoIndex": True,
            }
        },
    )
    if put.get("statusCode", 200) >= 400:
        print(f"  FAIL runtime fields on {dv_id}: {put.get('message', put)[:200]}", flush=True)
        return False
    print(f"  OK data view {dv_name} ({dv_id})", flush=True)
    print(f"  OK runtime fields {list(runtime_fields)}", flush=True)
    return True


def ensure_elk_cluster_monitoring_data_view(kb, auth: str) -> bool:
    return _ensure_elk_data_view(
        kb,
        auth,
        dv_id=ELK_MONITORING_DV_ID,
        dv_name=ELK_MONITORING_DV_NAME,
        dv_title=ELK_MONITORING_DV_TITLE,
        runtime_fields=ELK_MONITORING_RUNTIME_FIELDS,
    )


def ensure_elk_cluster_systems_data_view(kb, auth: str) -> bool:
    return _ensure_elk_data_view(
        kb,
        auth,
        dv_id=ELK_SYSTEMS_DV_ID,
        dv_name=ELK_SYSTEMS_DV_NAME,
        dv_title=ELK_SYSTEMS_DV_TITLE,
        runtime_fields=ELK_SYSTEM_RUNTIME_FIELDS,
    )


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    kb = connect(NODES["kibana"][0])

    _mon_user, mon_pwd = ensure_monitoring_user(es, run, elastic_pwd)
    fix_monitoring_ui_creds(kb, mon_pwd)

    print("=== Ensure data views ===", flush=True)
    if not ensure_elk_cluster_monitoring_data_view(kb, auth):
        kb.close()
        es.close()
        return 1
    if not ensure_elk_cluster_systems_data_view(kb, auth):
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