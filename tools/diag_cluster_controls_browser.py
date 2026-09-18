#!/usr/bin/env python3
"""Simulate browser optionsList requests for Cluster Ingest dashboard controls."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

BASE = "http://10.44.40.41:5601"
ROOT = Path(__file__).parent
elastic_pw = (ROOT / "secrets" / "elastic-password").read_text().strip()
mon_pw = (ROOT / "secrets" / "monitoring-password").read_text().strip()

DASH_IDS = [
    "elasticsearch-b1399af0-628c-11ee-9c63-732d7f759a7a",
    "elasticsearch-ea888f80-61e4-11ee-b5a1-0d1803efe5cf",
]
SM_DV = ".ds-.monitoring-es-*,.monitoring-es*,.ds-metrics-elasticsearch.stack_monitoring.*"
SM_DV_LEGACY = "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9"


def curl_json(method: str, path: str, body: dict | None = None, user: str = "elastic") -> tuple[int, dict | str]:
    pwd = elastic_pw if user == "elastic" else mon_pw
    auth = f"{user}:{pwd}"
    cmd = [
        "curl.exe",
        "-s",
        "-w",
        "\nHTTP:%{http_code}",
        "-u",
        auth,
        "-H",
        "kbn-xsrf: true",
        "-H",
        "Content-Type: application/json",
        "-X",
        method,
        "--max-time",
        "60",
        f"{BASE}{path}",
    ]
    if body is not None:
        cmd.extend(["-d", json.dumps(body)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=65)
    lines = proc.stdout.strip().splitlines()
    http = int(lines[-1].split(":", 1)[1]) if lines and lines[-1].startswith("HTTP:") else 0
    body_text = "\n".join(lines[:-1]) if lines and lines[-1].startswith("HTTP:") else proc.stdout
    if body_text.strip().startswith("{"):
        try:
            return http, json.loads(body_text)
        except json.JSONDecodeError:
            pass
    return http, body_text[:500]


def browser_body(field: str, runtime_map: dict | None = None) -> dict:
    return {
        "sort": {"by": "_count", "direction": "desc"},
        "searchString": "",
        "searchTechnique": "prefix",
        "allowExpensiveQueries": True,
        "fieldName": field,
        "fieldSpec": {
            "name": field,
            "type": "string",
            "esTypes": ["keyword"],
            "scripted": False,
            "searchable": True,
            "aggregatable": True,
        },
        "filters": [
            {
                "bool": {
                    "must": [],
                    "filter": [{"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}}],
                    "should": [],
                    "must_not": [],
                }
            }
        ],
        "ignoreValidations": False,
        "runtimeFieldMap": runtime_map or {},
        "selectedOptions": [],
        "size": 10,
    }


def main() -> None:
    print("=== Fetch dashboard control config ===")
    for did in DASH_IDS:
        http, dash = curl_json("GET", f"/api/saved_objects/dashboard/{did}")
        title = dash.get("attributes", {}).get("title", did) if isinstance(dash, dict) else did
        print(f"\n{title} (http={http})")
        if not isinstance(dash, dict):
            continue
        refs = [r for r in dash.get("references", []) if "controlGroup" in r.get("name", "")]
        print(f"  control refs: {refs}")
        cg_raw = dash.get("attributes", {}).get("controlGroupInput")
        if not cg_raw:
            print("  no controlGroupInput")
            continue
        cg = json.loads(cg_raw) if isinstance(cg_raw, str) else cg_raw
        cpanels = (
            json.loads(cg.get("panelsJSON", "{}"))
            if isinstance(cg.get("panelsJSON"), str)
            else cg.get("panelsJSON", {})
        )
        for cid, cp in cpanels.items():
            if cp.get("type") != "optionsListControl":
                continue
            inp = cp.get("explicitInput", {})
            print(
                f"  control {inp.get('title') or cid[:8]}: "
                f"field={inp.get('fieldName')!r} dataViewId={inp.get('dataViewId')!r}"
            )
        if SM_DV_LEGACY in json.dumps(dash):
            print("  WARN: legacy UUID still present in dashboard JSON")

    print("\n=== Data view runtime fields ===")
    for dv_id in (SM_DV, SM_DV_LEGACY):
        path = f"/api/data_views/data_view/{quote(dv_id, safe='')}"
        http, resp = curl_json("GET", path)
        dv = resp.get("data_view", {}) if isinstance(resp, dict) else {}
        runtime = dv.get("runtimeFieldMap") or {}
        print(f"  {dv_id[:50]}... http={http} runtime={list(runtime.keys())}")

    runtime_map = {}
    _, dv_resp = curl_json("GET", f"/api/data_views/data_view/{quote(SM_DV, safe='')}")
    if isinstance(dv_resp, dict):
        runtime_map = dv_resp.get("data_view", {}).get("runtimeFieldMap") or {}

    print("\n=== Browser-style optionsList (elastic user) ===")
    paths = [
        f"/internal/controls/optionsList/{quote(SM_DV, safe='')}",
        f"/internal/controls/optionsList/{SM_DV}",
        f"/internal/controls/optionsList/{SM_DV_LEGACY}",
    ]
    fields = ["cluster_uuid", "elasticsearch.node.name", "elasticsearch.index.name"]
    for path in paths:
        print(f"\nPOST {path[:80]}...")
        for field in fields:
            body = browser_body(field, runtime_map)
            http, resp = curl_json("POST", path, body, user="elastic")
            if isinstance(resp, dict):
                n = len(resp.get("suggestions") or [])
                err = (resp.get("message") or resp.get("error") or "")[:120]
                print(f"  {field}: http={http} suggestions={n} err={err}")
            else:
                print(f"  {field}: http={http} raw={resp}")

    print("\n=== Browser-style optionsList (elastic_monitoring user) ===")
    path = f"/internal/controls/optionsList/{quote(SM_DV, safe='')}"
    for field in fields:
        body = browser_body(field, runtime_map)
        http, resp = curl_json("POST", path, body, user="elastic_monitoring")
        if isinstance(resp, dict):
            n = len(resp.get("suggestions") or [])
            err = (resp.get("message") or resp.get("error") or "")[:120]
            print(f"  {field}: http={http} suggestions={n} err={err}")
        else:
            print(f"  {field}: http={http} raw={resp}")


if __name__ == "__main__":
    main()