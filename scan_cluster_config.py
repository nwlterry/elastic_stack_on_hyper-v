#!/usr/bin/env python3
"""Scan live cluster Kibana/ES config: dashboards, alerts, pipelines, Fleet, data views."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import (
    CLUSTER_INGEST_DASHBOARDS,
    INGEST_PIPELINE_DASHBOARD,
    INGEST_PIPELINE_DASHBOARD_FIXED,
    _is_ingest_pipeline_panel,
    list_elasticsearch_dashboards,
)

ROOT = Path(__file__).parent
OUT = ROOT / "cluster_config_snapshot.json"


def kibana_get(kb, auth: str, path: str) -> dict | list:
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' 'http://127.0.0.1:5601{path}'",
        check=False,
        timeout=120,
    )
    if not out.strip():
        return {}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {"raw": out[:500]}


def es_get(es, auth: str, path: str) -> dict | list:
    out = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200{path}'",
        check=False,
        timeout=120,
    )
    if not out.strip():
        return {}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {"raw": out[:500]}


def summarize_dashboard(dash: dict) -> dict:
    attrs = dash.get("attributes", {})
    panels = json.loads(attrs.get("panelsJSON", "[]"))
    lens = 0
    state_types: dict[str, int] = {}
    bad_kql = 0
    for p in panels:
        if p.get("type") != "lens":
            continue
        lens += 1
        st = p.get("embeddableConfig", {}).get("attributes", {}).get("state")
        state_types[type(st).__name__] = state_types.get(type(st).__name__, 0) + 1
        if isinstance(st, dict) and not _is_ingest_pipeline_panel(st) and (st.get("query", {}).get("query") or "").strip():
            bad_kql += 1
    return {
        "id": dash.get("id"),
        "title": attrs.get("title"),
        "managed": dash.get("managed"),
        "panels": len(panels),
        "lens_panels": lens,
        "lens_state_types": state_types,
        "bad_non_ingest_kql": bad_kql,
        "data_views": sorted(
            {r.get("id") for r in dash.get("references", []) if r.get("type") == "index-pattern"}
        ),
    }


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    auth = curl_elastic_auth(elastic_pwd)
    kb = connect(NODES["kibana"][0])

    snapshot: dict = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "cluster": {},
        "elasticsearch": {},
        "kibana": {},
        "fleet": {},
        "dashboards": [],
        "data_views": [],
        "alert_rules": [],
        "issues": [],
    }

    health = es_get(es, auth, "/_cluster/health")
    snapshot["cluster"] = {
        "name": health.get("cluster_name"),
        "status": health.get("status"),
        "nodes": health.get("number_of_nodes"),
    }

    pipelines_raw = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_ingest/pipeline' | "
        "python3 -c \"import sys,json; d=json.load(sys.stdin); print(chr(10).join(sorted(d)))\"",
        check=False,
    )
    snapshot["elasticsearch"]["ingest_pipelines"] = [
        ln.strip() for ln in (pipelines_raw or "").splitlines() if ln.strip()
    ][:80]

    templates = es_get(es, auth, "/_index_template")
    snapshot["elasticsearch"]["index_templates"] = [
        t.get("name") for t in templates.get("index_templates", [])
    ][:50]

    streams = es_get(es, auth, "/_data_stream/metrics-*")
    snapshot["elasticsearch"]["metrics_data_streams"] = sorted(
        ds.get("name", "") for ds in streams.get("data_streams", [])
    )

    for did in [
        INGEST_PIPELINE_DASHBOARD,
        INGEST_PIPELINE_DASHBOARD_FIXED,
        *sorted(CLUSTER_INGEST_DASHBOARDS),
    ]:
        dash = kibana_get(kb, auth, f"/api/saved_objects/dashboard/{did}")
        if dash.get("id"):
            summary = summarize_dashboard(dash)
            snapshot["dashboards"].append(summary)
            if summary["bad_non_ingest_kql"]:
                snapshot["issues"].append(
                    f"dashboard {summary['title']}: bad_kql={summary['bad_non_ingest_kql']}"
                )
            if summary["lens_state_types"].get("str"):
                snapshot["issues"].append(
                    f"dashboard {summary['title']}: string_lens_state="
                    f"{summary['lens_state_types']['str']}"
                )

    for item in list_elasticsearch_dashboards(kb, auth):
        did = item["id"]
        if did in {d["id"] for d in snapshot["dashboards"] if d.get("id")}:
            continue
        dash = kibana_get(kb, auth, f"/api/saved_objects/dashboard/{did}")
        if dash.get("id"):
            summary = summarize_dashboard(dash)
            snapshot["dashboards"].append(summary)
            if summary["bad_non_ingest_kql"] or summary["lens_state_types"].get("str"):
                snapshot["issues"].append(f"dashboard {summary['title']}: needs_lens_fix")

    dvs = kibana_get(
        kb,
        auth,
        "/api/saved_objects/_find?type=index-pattern&per_page=50&fields=title,runtimeFieldMap",
    )
    for dv in dvs.get("saved_objects", []) if isinstance(dvs, dict) else []:
        attrs = dv.get("attributes", {})
        runtime = {}
        try:
            runtime = json.loads(attrs.get("runtimeFieldMap", "{}") or "{}")
        except json.JSONDecodeError:
            pass
        snapshot["data_views"].append(
            {"id": dv.get("id"), "title": attrs.get("title"), "runtime_fields": list(runtime.keys())}
        )

    rules = kibana_get(kb, auth, "/api/alerting/rules/_find?per_page=100")
    for rule in rules.get("data", []) if isinstance(rules, dict) else []:
        snapshot["alert_rules"].append(
            {
                "id": rule.get("id"),
                "name": rule.get("name"),
                "rule_type_id": rule.get("rule_type_id"),
                "enabled": rule.get("enabled"),
                "consumer": rule.get("consumer"),
            }
        )

    agents = kibana_get(kb, auth, "/api/fleet/agents?perPage=50")
    snapshot["fleet"]["agents"] = [
        {"id": a.get("id"), "status": a.get("status"), "policy_id": a.get("policy_id")}
        for a in agents.get("items", [])
    ]
    policies = kibana_get(kb, auth, "/api/fleet/package_policies?perPage=50")
    snapshot["fleet"]["package_policies"] = [
        {
            "name": p.get("name"),
            "package": p.get("package", {}).get("name"),
            "streams": sum(len(i.get("streams") or []) for i in p.get("inputs", [])),
        }
        for p in policies.get("items", [])
    ]

    OUT.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}", flush=True)
    print(f"dashboards={len(snapshot['dashboards'])} issues={len(snapshot['issues'])}", flush=True)
    for issue in snapshot["issues"]:
        print(f"  ISSUE: {issue}", flush=True)
    for d in snapshot["dashboards"]:
        print(
            f"  {d['title']}: lens={d['lens_panels']} "
            f"state={d['lens_state_types']} bad_kql={d['bad_non_ingest_kql']}",
            flush=True,
        )

    kb.close()
    es.close()
    return len(snapshot["issues"])


if __name__ == "__main__":
    raise SystemExit(main())