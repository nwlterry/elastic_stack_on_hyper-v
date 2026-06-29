#!/usr/bin/env python3
"""Test whether runtime @timestamp breaks browser-style controls."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import quote

BASE = "http://10.44.40.41:5601"
pw = (Path(__file__).parent / "secrets" / "elastic-password").read_text().strip()
SM = ".ds-.monitoring-es-*,.monitoring-es*,.ds-metrics-elasticsearch.stack_monitoring.*"


def post(path: str, body: dict) -> tuple[int, dict]:
    proc = subprocess.run(
        [
            "curl.exe",
            "-s",
            "-w",
            "\nHTTP:%{http_code}",
            "-u",
            f"elastic:{pw}",
            "-H",
            "kbn-xsrf: true",
            "-H",
            "Content-Type: application/json",
            "-X",
            "POST",
            f"{BASE}{path}",
            "-d",
            json.dumps(body),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = proc.stdout.strip().splitlines()
    http = int(lines[-1].split(":", 1)[1]) if lines and lines[-1].startswith("HTTP:") else 0
    body_text = "\n".join(lines[:-1])
    return http, json.loads(body_text) if body_text.startswith("{") else {}


dv = json.loads(
    subprocess.check_output(
        [
            "curl.exe",
            "-s",
            "-u",
            f"elastic:{pw}",
            "-H",
            "kbn-xsrf: true",
            f"{BASE}/api/data_views/data_view/{quote(SM, safe='')}",
        ],
        text=True,
    )
)["data_view"]
runtime = dv.get("runtimeFieldMap") or {}
print(f"runtime keys: {list(runtime.keys())}")

path = f"/internal/controls/optionsList/{quote(SM, safe='')}"
body = {
    "sort": {"by": "_count", "direction": "desc"},
    "searchString": "",
    "searchTechnique": "prefix",
    "allowExpensiveQueries": True,
    "fieldName": "cluster_uuid",
    "fieldSpec": {
        "name": "cluster_uuid",
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
    "selectedOptions": [],
    "size": 10,
}

for label, rmap in [("empty_runtime", {}), ("with_runtime", runtime)]:
    b = dict(body)
    b["runtimeFieldMap"] = rmap
    http, resp = post(path, b)
    print(
        f"{label}: http={http} suggestions={len(resp.get('suggestions') or [])} "
        f"err={(resp.get('message') or '')[:120]}"
    )