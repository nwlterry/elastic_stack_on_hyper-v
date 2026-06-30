#!/usr/bin/env python3
"""
Create a Kibana operations dashboard for ism-elk-cluster:
  - Cluster status, index count, shard count, unassigned shards
  - System uptime by node tier (master, data_hot, data_cold, data_warm, ml, kibana)
  - ES node disk usage table by tier
  - ES JVM heap usage
"""
from __future__ import annotations

import json
import uuid

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password
from fix_dashboard_search import kibana_curl

DASHBOARD_ID = "ism-cluster-operations-dashboard"
DASHBOARD_TITLE = "[Elasticsearch] Cluster Operations Overview"
DATA_VIEW_ID = "ism-cluster-ops-metrics"
DATA_VIEW_PATTERN = (
    ".ds-metrics-elasticsearch.stack_monitoring.*,"
    "metrics-system.*,"
    "metrics-kibana.stack_monitoring.*"
)

TIER_HOSTS = {
    "master (current)": "ismelkesnode01.ocplab.net",
    "data_hot": "ismelkesnode02.ocplab.net",
    "data_cold": "ismelkesnode03.ocplab.net",
    "data_warm": "ismelkesnode01.ocplab.net",
    "ml": "ismelkesnode02.ocplab.net",
    "kibana": "ismelkkbnnode01.ocplab.net",
}

DEPLOYMENT_TIER_SCRIPT = """
String h = null;
if (doc.containsKey('elasticsearch.node.name') && doc['elasticsearch.node.name'].size() > 0) {
  h = doc['elasticsearch.node.name'].value;
} else if (doc.containsKey('host.name') && doc['host.name'].size() > 0) {
  h = doc['host.name'].value;
}
if (h != null) {
  if (h == 'ismelkesnode01.ocplab.net') emit('data_warm / master');
  else if (h == 'ismelkesnode02.ocplab.net') emit('data_hot / ml');
  else if (h == 'ismelkesnode03.ocplab.net') emit('data_cold');
  else if (h == 'ismelkkbnnode01.ocplab.net') emit('kibana');
}
""".strip()


def _uid() -> str:
    return str(uuid.uuid4())


def _dv_ref(layer_id: str) -> dict:
    return {
        "id": DATA_VIEW_ID,
        "name": f"indexpattern-datasource-layer-{layer_id}",
        "type": "index-pattern",
    }


def _metric_state(
    field: str,
    label: str,
    *,
    layer_id: str | None = None,
    metric_id: str | None = None,
    kql_filter: str = "",
    format_id: str | None = None,
    format_params: dict | None = None,
) -> dict:
    layer_id = layer_id or _uid()
    metric_id = metric_id or _uid()
    col = {
        "customLabel": True,
        "dataType": "number",
        "isBucketed": False,
        "label": label,
        "operationType": "last_value",
        "params": {"sortField": "@timestamp"},
        "scale": "ratio",
        "sourceField": field,
    }
    if kql_filter:
        col["filter"] = {"language": "kuery", "query": kql_filter}
    if format_id:
        col["params"]["format"] = {"id": format_id, "params": format_params or {}}

    return {
        "layer_id": layer_id,
        "metric_id": metric_id,
        "state": {
            "adHocDataViews": {},
            "datasourceStates": {
                "formBased": {
                    "layers": {
                        layer_id: {
                            "columnOrder": [metric_id],
                            "columns": {metric_id: col},
                            "incompleteColumns": {},
                        }
                    }
                }
            },
            "filters": [],
            "internalReferences": [],
            "query": {"language": "kuery", "query": ""},
            "visualization": {
                "layerId": layer_id,
                "layerType": "data",
                "metricAccessor": metric_id,
                "icon": "visLine",
                "palette": {
                    "name": "status",
                    "type": "palette",
                    "params": {
                        "name": "status",
                        "type": "palette",
                        "progression": "fixed",
                        "stops": [
                            {"color": "#209280", "stop": 0},
                            {"color": "#d6bf57", "stop": 50},
                            {"color": "#cc5642", "stop": 100},
                        ],
                    },
                },
                "showBar": False,
            },
        },
        "references": [_dv_ref(layer_id)],
    }


def _count_metric_state(label: str, kql_filter: str) -> dict:
    layer_id = _uid()
    metric_id = _uid()
    col = {
        "customLabel": True,
        "dataType": "number",
        "isBucketed": False,
        "label": label,
        "operationType": "count",
        "params": {"emptyAsNull": False},
        "scale": "ratio",
        "filter": {"language": "kuery", "query": kql_filter},
    }
    return {
        "layer_id": layer_id,
        "metric_id": metric_id,
        "state": {
            "adHocDataViews": {},
            "datasourceStates": {
                "formBased": {
                    "layers": {
                        layer_id: {
                            "columnOrder": [metric_id],
                            "columns": {metric_id: col},
                            "incompleteColumns": {},
                        }
                    }
                }
            },
            "filters": [],
            "internalReferences": [],
            "query": {"language": "kuery", "query": ""},
            "visualization": {
                "layerId": layer_id,
                "layerType": "data",
                "metricAccessor": metric_id,
                "icon": "shard",
                "showBar": False,
            },
        },
        "references": [_dv_ref(layer_id)],
    }


def _text_metric_state(field: str, label: str, kql_filter: str = "") -> dict:
    layer_id = _uid()
    metric_id = _uid()
    col = {
        "customLabel": True,
        "dataType": "string",
        "isBucketed": False,
        "label": label,
        "operationType": "last_value",
        "params": {"sortField": "@timestamp"},
        "scale": "ordinal",
        "sourceField": field,
    }
    if kql_filter:
        col["filter"] = {"language": "kuery", "query": kql_filter}
    return {
        "layer_id": layer_id,
        "metric_id": metric_id,
        "state": {
            "adHocDataViews": {},
            "datasourceStates": {
                "formBased": {
                    "layers": {
                        layer_id: {
                            "columnOrder": [metric_id],
                            "columns": {metric_id: col},
                            "incompleteColumns": {},
                        }
                    }
                }
            },
            "filters": [],
            "internalReferences": [],
            "query": {"language": "kuery", "query": ""},
            "visualization": {
                "layerId": layer_id,
                "layerType": "data",
                "metricAccessor": metric_id,
                "icon": "cluster",
                "showBar": False,
            },
        },
        "references": [_dv_ref(layer_id)],
    }


def _uptime_metric_state(label: str, host: str) -> dict:
    return _metric_state(
        "system.uptime.duration.ms",
        label,
        kql_filter=f'data_stream.dataset : "system.uptime" and host.name : "{host}"',
        format_id="duration",
        format_params={"decimals": 1},
    )


def _table_state() -> dict:
    layer_id = _uid()
    node_col = _uid()
    tier_col = _uid()
    roles_col = _uid()
    disk_col = _uid()
    jvm_col = _uid()
    layers = {
        layer_id: {
            "columnOrder": [node_col, tier_col, roles_col, disk_col, jvm_col],
            "columns": {
                node_col: {
                    "customLabel": True,
                    "dataType": "string",
                    "isBucketed": True,
                    "label": "Node",
                    "operationType": "terms",
                    "params": {
                        "size": 20,
                        "orderBy": {"type": "column", "columnId": disk_col},
                        "orderDirection": "desc",
                        "otherBucket": False,
                        "missingBucket": False,
                    },
                    "scale": "ordinal",
                    "sourceField": "elasticsearch.node.name",
                    "filter": {
                        "language": "kuery",
                        "query": 'data_stream.dataset : "elasticsearch.stack_monitoring.node_stats"',
                    },
                },
                tier_col: {
                    "customLabel": True,
                    "dataType": "string",
                    "isBucketed": False,
                    "label": "Deployment tier",
                    "operationType": "last_value",
                    "params": {"sortField": "@timestamp"},
                    "scale": "ordinal",
                    "sourceField": "node.deployment_tier",
                },
                roles_col: {
                    "customLabel": True,
                    "dataType": "string",
                    "isBucketed": False,
                    "label": "ES roles",
                    "operationType": "last_value",
                    "params": {"sortField": "@timestamp"},
                    "scale": "ordinal",
                    "sourceField": "elasticsearch.node.roles",
                },
                disk_col: {
                    "customLabel": True,
                    "dataType": "number",
                    "isBucketed": False,
                    "label": "Disk used %",
                    "operationType": "last_value",
                    "params": {
                        "sortField": "@timestamp",
                        "format": {"id": "percent", "params": {"decimals": 1}},
                    },
                    "scale": "ratio",
                    "sourceField": "elasticsearch.node.stats.fs.total.total_in_bytes",
                    "filter": {
                        "language": "kuery",
                        "query": "elasticsearch.node.stats.fs.total.total_in_bytes: *",
                    },
                },
                jvm_col: {
                    "customLabel": True,
                    "dataType": "number",
                    "isBucketed": False,
                    "label": "JVM heap %",
                    "operationType": "last_value",
                    "params": {
                        "sortField": "@timestamp",
                        "format": {"id": "percent", "params": {"decimals": 0}},
                    },
                    "scale": "ratio",
                    "sourceField": "elasticsearch.node.stats.jvm.mem.heap.used.pct",
                },
            },
            "incompleteColumns": {},
        }
    }

    # Fix disk % - use formula column instead
    disk_pct_id = _uid()
    total_id = disk_col
    avail_id = _uid()
    layers[layer_id]["columns"][avail_id] = {
        "customLabel": True,
        "dataType": "number",
        "isBucketed": False,
        "hidden": True,
        "label": "disk avail",
        "operationType": "last_value",
        "params": {"sortField": "@timestamp"},
        "scale": "ratio",
        "sourceField": "elasticsearch.node.stats.fs.total.available_in_bytes",
    }
    layers[layer_id]["columns"][disk_pct_id] = {
        "customLabel": True,
        "dataType": "number",
        "isBucketed": False,
        "label": "Disk used %",
        "operationType": "formula",
        "params": {
            "format": {"id": "percent", "params": {"decimals": 1}},
            "formula": f"(1 - {avail_id} / {total_id}) * 100",
            "isFormulaBroken": False,
        },
        "references": [total_id, avail_id],
    }
    layers[layer_id]["columnOrder"] = [
        node_col,
        tier_col,
        roles_col,
        disk_pct_id,
        jvm_col,
    ]
    del layers[layer_id]["columns"][disk_col]

    return {
        "layer_id": layer_id,
        "state": {
            "adHocDataViews": {},
            "datasourceStates": {"formBased": {"layers": layers}},
            "filters": [],
            "internalReferences": [],
            "query": {
                "language": "kuery",
                "query": 'data_stream.dataset : "elasticsearch.stack_monitoring.node_stats"',
            },
            "visualization": {
                "layerId": layer_id,
                "layerType": "data",
                "columns": [
                    {"columnId": node_col},
                    {"columnId": tier_col},
                    {"columnId": roles_col},
                    {"columnId": disk_pct_id},
                    {"columnId": jvm_col},
                ],
            },
        },
        "references": [_dv_ref(layer_id)],
    }


def _jvm_bar_state() -> dict:
    layer_id = _uid()
    node_col = _uid()
    jvm_col = _uid()
    return {
        "layer_id": layer_id,
        "state": {
            "adHocDataViews": {},
            "datasourceStates": {
                "formBased": {
                    "layers": {
                        layer_id: {
                            "columnOrder": [node_col, jvm_col],
                            "columns": {
                                node_col: {
                                    "customLabel": True,
                                    "dataType": "string",
                                    "isBucketed": True,
                                    "label": "Node",
                                    "operationType": "terms",
                                    "params": {
                                        "size": 10,
                                        "orderBy": {"type": "column", "columnId": jvm_col},
                                        "orderDirection": "desc",
                                    },
                                    "scale": "ordinal",
                                    "sourceField": "elasticsearch.node.name",
                                },
                                jvm_col: {
                                    "customLabel": True,
                                    "dataType": "number",
                                    "isBucketed": False,
                                    "label": "JVM heap %",
                                    "operationType": "last_value",
                                    "params": {
                                        "sortField": "@timestamp",
                                        "format": {"id": "percent", "params": {"decimals": 0}},
                                    },
                                    "scale": "ratio",
                                    "sourceField": "elasticsearch.node.stats.jvm.mem.heap.used.pct",
                                },
                            },
                            "incompleteColumns": {},
                        }
                    }
                }
            },
            "filters": [],
            "internalReferences": [],
            "query": {
                "language": "kuery",
                "query": 'data_stream.dataset : "elasticsearch.stack_monitoring.node_stats"',
            },
            "visualization": {
                "axisTitlesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
                "fittingFunction": "None",
                "gridlinesVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
                "labelsOrientation": {"x": 0, "yLeft": 0, "yRight": 0},
                "layers": [
                    {
                        "accessors": [jvm_col],
                        "layerId": layer_id,
                        "layerType": "data",
                        "position": "top",
                        "seriesType": "bar_horizontal",
                        "showGridlines": False,
                        "splitAccessor": node_col,
                        "xAccessor": node_col,
                    }
                ],
                "legend": {"isVisible": True, "position": "right"},
                "preferredSeriesType": "bar_horizontal",
                "tickLabelsVisibilitySettings": {"x": True, "yLeft": True, "yRight": True},
                "valueLabels": "hide",
            },
        },
        "references": [_dv_ref(layer_id)],
    }


def _lens_panel(
    title: str,
    viz_type: str,
    state: dict,
    references: list[dict],
    *,
    x: int,
    y: int,
    w: int,
    h: int,
) -> dict:
    panel_id = _uid()
    return {
        "version": "8.18.0",
        "type": "lens",
        "gridData": {"x": x, "y": y, "w": w, "h": h, "i": panel_id},
        "panelIndex": panel_id,
        "embeddableConfig": {
            "attributes": {
                "title": title,
                "visualizationType": viz_type,
                "state": state,
                "references": references,
            },
            "enhancements": {},
        },
        "title": title,
    }


def ensure_data_view(kb, auth: str) -> None:
    existing = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{DATA_VIEW_ID}")
    runtime_map = {
        "node.deployment_tier": {
            "type": "keyword",
            "script": {"source": DEPLOYMENT_TIER_SCRIPT},
        }
    }
    body = {
        "data_view": {
            "id": DATA_VIEW_ID,
            "title": DATA_VIEW_PATTERN,
            "name": "ISM Cluster Operations",
            "timeFieldName": "@timestamp",
            "runtimeFieldMap": runtime_map,
        }
    }
    if existing.get("data_view"):
        print(f"  data view {DATA_VIEW_ID} already exists", flush=True)
        return

    resp = kibana_curl(kb, auth, "POST", "/api/data_views/data_view", body)
    if resp.get("statusCode", 200) >= 400:
        raise RuntimeError(f"data view create failed: {resp}")
    print(f"  created data view {DATA_VIEW_ID}", flush=True)


def build_dashboard() -> tuple[dict, list[dict]]:
    panels: list[dict] = []
    refs: list[dict] = [{"id": DATA_VIEW_ID, "name": DATA_VIEW_ID, "type": "index-pattern"}]

    # Row 0: cluster KPIs
    kpis = [
        (
            "Cluster status",
            _text_metric_state(
                "elasticsearch.cluster.stats.status",
                "Cluster status",
                'data_stream.dataset : "elasticsearch.stack_monitoring.cluster_stats"',
            ),
        ),
        (
            "Index count",
            _metric_state(
                "elasticsearch.cluster.stats.indices.total",
                "Index count",
                kql_filter='data_stream.dataset : "elasticsearch.stack_monitoring.cluster_stats"',
            ),
        ),
        (
            "Shard count",
            _metric_state(
                "elasticsearch.cluster.stats.indices.shards.count",
                "Shard count",
                kql_filter='data_stream.dataset : "elasticsearch.stack_monitoring.cluster_stats"',
            ),
        ),
        (
            "Unassigned shards",
            _count_metric_state(
                "Unassigned shards (last 5m)",
                'data_stream.dataset : "elasticsearch.stack_monitoring.shard" '
                'and elasticsearch.shard.state : "UNASSIGNED" '
                "and @timestamp >= now-5m",
            ),
        ),
    ]
    for i, (title, spec) in enumerate(kpis):
        panels.append(
            _lens_panel(
                title,
                "lnsMetric",
                spec["state"],
                spec["references"],
                x=i * 12,
                y=0,
                w=12,
                h=8,
            )
        )

    # Row 1: system uptime by tier
    y_uptime = 8
    for i, (tier, host) in enumerate(TIER_HOSTS.items()):
        spec = _uptime_metric_state(f"Uptime — {tier}", host)
        panels.append(
            _lens_panel(
                f"System uptime ({tier})",
                "lnsMetric",
                spec["state"],
                spec["references"],
                x=(i % 3) * 16,
                y=y_uptime + (i // 3) * 7,
                w=16,
                h=7,
            )
        )

    # Row 3: node table + JVM chart
    table = _table_state()
    panels.append(
        _lens_panel(
            "ES nodes — disk & JVM by tier",
            "lnsDatatable",
            table["state"],
            table["references"],
            x=0,
            y=22,
            w=30,
            h=14,
        )
    )
    jvm = _jvm_bar_state()
    panels.append(
        _lens_panel(
            "ES JVM heap usage",
            "lnsXY",
            jvm["state"],
            jvm["references"],
            x=30,
            y=22,
            w=18,
            h=14,
        )
    )

    attributes = {
        "title": DASHBOARD_TITLE,
        "description": "Cluster health, shards, per-tier system uptime, disk and JVM usage.",
        "timeRestore": True,
        "timeFrom": "now-24h",
        "timeTo": "now",
        "refreshInterval": {"pause": True, "value": 60000},
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps(
                {
                    "query": {"language": "kuery", "query": ""},
                    "filter": [],
                }
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
    return attributes, refs


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    es.close()
    auth = curl_elastic_auth(elastic_pwd)
    kb = connect(NODES["kibana"][0])

    print("=== Ensure cluster ops data view ===", flush=True)
    ensure_data_view(kb, auth)

    print("=== Create dashboard ===", flush=True)
    attributes, references = build_dashboard()
    payload = {"attributes": attributes, "references": references}
    resp = kibana_curl(
        kb,
        auth,
        "POST",
        f"/api/saved_objects/dashboard/{DASHBOARD_ID}?overwrite=true",
        payload,
    )
    if resp.get("statusCode", 200) >= 400:
        print(json.dumps(resp, indent=2)[:2000], flush=True)
        kb.close()
        return 1

    url = f"http://{NODES['kibana'][1]}:5601/app/dashboards#/view/{DASHBOARD_ID}"
    print(f"OK dashboard id={DASHBOARD_ID}", flush=True)
    print(f"URL: {url}", flush=True)
    kb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())