#!/usr/bin/env python3
"""Audit all [Elasticsearch] dashboards: controls, lens state, data views."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, run

auth = curl_elastic_auth((Path(__file__).parent / "secrets" / "elastic-password").read_text().strip())
kb = connect(NODES["kibana"][0])


def kb_api(method: str, path: str, body: dict | None = None) -> dict:
    data = ""
    if body is not None:
        data = f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' -X {method} 'http://127.0.0.1:5601{path}' {data}",
        check=False,
        timeout=120,
    )
    for line in reversed(out.strip().splitlines()):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"raw": out[:2000]}


print("=== Data views (elasticsearch / stack monitoring) ===")
for vid in (
    "elasticsearch-sm-metrics",
    "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9",
    "metrics-*",
):
    dv = kb_api("GET", f"/api/data_views/data_view/{vid}").get("data_view", {})
    if not dv:
        print(f"  {vid}: MISSING")
        continue
    runtime = list((dv.get("runtimeFieldMap") or {}).keys())
    print(f"  {vid}: title={dv.get('title')!r} runtime={runtime}")

print("\n=== [Elasticsearch] dashboards ===")
find = kb_api(
    "GET",
    "/api/saved_objects/_find?type=dashboard&per_page=100&search_fields=title&search=%5BElasticsearch%5D",
)
dashboards = find.get("saved_objects", [])
print(f"found {len(dashboards)}")

issues: list[str] = []
for d in dashboards:
    did = d["id"]
    title = d.get("attributes", {}).get("title", did)
    print(f"\n--- {title} ({did}) ---")
    dash = kb_api("GET", f"/api/saved_objects/dashboard/{did}")
    attrs = dash.get("attributes", {})
    panels = json.loads(attrs.get("panelsJSON", "[]"))

    corrupt_lens = 0
    lens_count = 0
    data_views_used: set[str] = set()
    control_fields: list[str] = []

    ctrl_input = attrs.get("controlGroupInput")
    if ctrl_input:
        try:
            cg = json.loads(ctrl_input) if isinstance(ctrl_input, str) else ctrl_input
            cpanels = json.loads(cg.get("panelsJSON", "{}")) if isinstance(cg.get("panelsJSON"), str) else cg.get("panelsJSON", {})
            for cp in cpanels.values():
                if cp.get("type") == "optionsListControl":
                    field = cp.get("explicitInput", {}).get("fieldName", "")
                    control_fields.append(field)
        except (json.JSONDecodeError, TypeError, AttributeError):
            issues.append(f"{title}: corrupt controlGroupInput")

    for i, panel in enumerate(panels):
        ptype = panel.get("type")
        if ptype == "lens":
            lens_count += 1
            emb = panel.get("embeddableConfig", {})
            lens_attrs = emb.get("attributes", {})
            state_raw = lens_attrs.get("state")
            if isinstance(state_raw, dict):
                corrupt_lens += 1
            elif state_raw:
                state = json.loads(state_raw)
                for ref in state.get("internalReferences", []):
                    if ref.get("type") == "index-pattern":
                        data_views_used.add(ref.get("id", ""))
                q = state.get("query", {}).get("query", "")
                if q and len(q) < 80:
                    pass  # sample only

    refs = dash.get("references", [])
    dv_refs = [r for r in refs if r.get("type") == "index-pattern"]
    for r in dv_refs:
        if r.get("id"):
            data_views_used.add(r["id"])

    print(f"  panels={len(panels)} lens={lens_count} corrupt_lens_state={corrupt_lens}")
    print(f"  controls={control_fields or 'none'}")
    print(f"  data_views={sorted(data_views_used) or ['(from embed only)']}")

    if corrupt_lens:
        issues.append(f"{title}: {corrupt_lens} lens panel(s) with dict state")

    # Test controls if present (use control group's data view, not a hardcoded index)
    if control_fields:
        dv_index_map = {}
        for vid in ("elasticsearch-sm-metrics", "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9"):
            dv = kb_api("GET", f"/api/data_views/data_view/{vid}").get("data_view", {})
            if dv:
                dv_index_map[vid] = dv.get("title", vid)
        ctrl_dv_ids = [
            r["id"]
            for r in dash.get("references", [])
            if r.get("type") == "index-pattern" and "controlGroup" in r.get("name", "")
        ]
        idx = ""
        for dv_id in ctrl_dv_ids:
            if dv_id in dv_index_map:
                idx = dv_index_map[dv_id]
                break
        if not idx:
            idx = dv_index_map.get("elasticsearch-sm-metrics") or dv_index_map.get(
                "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9", ""
            )
        for field in control_fields[:3]:
            body = {
                "size": 50,
                "fieldName": field,
                "allowExpensiveQueries": True,
                "filters": [{"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}}],
                "selectedOptions": [],
                "searchString": "",
            }
            resp = kb_api("POST", f"/internal/controls/optionsList/{idx}", body)
            suggestions = resp.get("suggestions")
            if resp.get("statusCode", 200) >= 400 or not suggestions:
                msg = str(resp.get("message", resp))[:120]
                issues.append(f"{title}: control {field} FAIL on {idx}: {msg}")
                print(f"  FAIL control {field}: {msg}")
            else:
                print(f"  OK control {field}: {len(suggestions)} options")

    # Quick search on primary data view
    if data_views_used:
        primary = sorted(data_views_used)[0]
        dv = kb_api("GET", f"/api/data_views/data_view/{primary}").get("data_view", {})
        index = dv.get("title", primary)
        resp = kb_api(
            "POST",
            "/internal/search/ese",
            {
                "params": {
                    "index": index,
                    "body": {
                        "size": 0,
                        "query": {"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}},
                    },
                }
            },
        )
        raw = resp.get("rawResponse", {})
        failed = raw.get("_shards", {}).get("failed", 1)
        total = raw.get("hits", {}).get("total", "?")
        if failed or resp.get("statusCode", 200) >= 400:
            issues.append(f"{title}: search FAIL index={index}")
            print(f"  FAIL search {index}: {str(resp.get('message',''))[:100]}")
        else:
            print(f"  OK search {index}: hits={total}")

print("\n=== SUMMARY ISSUES ===")
if issues:
    for issue in issues:
        print(f"  - {issue}")
else:
    print("  none")

kb.close()