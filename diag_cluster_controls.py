#!/usr/bin/env python3
"""Diagnose Cluster Ingest dashboard controls."""
from __future__ import annotations

import json
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, run
from fix_dashboard_search import (
    STACK_MONITORING_DATA_VIEW,
    STACK_MONITORING_DATA_VIEW_LEGACY,
    kibana_curl,
)

IDS = [
    "elasticsearch-b1399af0-628c-11ee-9c63-732d7f759a7a",
    "elasticsearch-ea888f80-61e4-11ee-b5a1-0d1803efe5cf",
]

auth = curl_elastic_auth((Path(__file__).parent / "secrets" / "elastic-password").read_text().strip())
kb = connect(NODES["kibana"][0])

dv = kibana_curl(kb, auth, "GET", f"/api/data_views/data_view/{STACK_MONITORING_DATA_VIEW}")
dv_obj = dv.get("data_view", {})
print(f"SM data view id={STACK_MONITORING_DATA_VIEW}")
print(f"  title={dv_obj.get('title')} timeField={dv_obj.get('timeFieldName')}")
print(f"  runtime={list((dv_obj.get('runtimeFieldMap') or {}).keys())}")

for did in IDS:
    dash = kibana_curl(kb, auth, "GET", f"/api/saved_objects/dashboard/{did}")
    title = dash.get("attributes", {}).get("title", did)
    print(f"\n=== {title} ===")
    ctrl_input = dash.get("attributes", {}).get("controlGroupInput")
    cg = json.loads(ctrl_input) if isinstance(ctrl_input, str) else (ctrl_input or {})
    cpanels = json.loads(cg.get("panelsJSON", "{}")) if isinstance(cg.get("panelsJSON"), str) else cg.get("panelsJSON", {})
    for cid, cp in cpanels.items():
        ei = cp.get("explicitInput", {})
        field = ei.get("fieldName", "")
        dv_id = ei.get("dataViewId") or ei.get("selectedDataViewId") or ""
        print(f"  control {cid[:8]}... field={field!r} dataViewId={dv_id!r}")
    ctrl_refs = [r for r in dash.get("references", []) if "controlGroup" in r.get("name", "")]
    print(f"  control refs: {ctrl_refs}")

    for cid, cp in cpanels.items():
        if cp.get("type") != "optionsListControl":
            continue
        field = cp.get("explicitInput", {}).get("fieldName", "")
        for index in (
            dv_obj.get("title", "metrics-*"),
            STACK_MONITORING_DATA_VIEW,
            STACK_MONITORING_DATA_VIEW_LEGACY,
            "metrics-*",
        ):
            body = {
                "size": 50,
                "fieldName": field,
                "allowExpensiveQueries": True,
                "filters": [{"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}}],
                "selectedOptions": [],
                "searchString": "",
            }
            resp = kibana_curl(kb, auth, "POST", f"/internal/controls/optionsList/{index}", body)
            sc = resp.get("statusCode", 200)
            suggestions = resp.get("suggestions") or []
            err = resp.get("message", "")[:120]
            print(f"    optionsList/{index} field={field}: http={sc} suggestions={len(suggestions)} err={err}")

kb.close()