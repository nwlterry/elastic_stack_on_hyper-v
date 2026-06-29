#!/usr/bin/env python3
"""Apply dashboard Lens patches without Fleet reinstall."""
from __future__ import annotations

from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password
from fix_dashboard_search import (
    DATA_VIEWS,
    INGEST_PIPELINE_DASHBOARD_FIXED,
    INGEST_PIPELINE_DATA_VIEW,
    INGEST_PIPELINE_INDEX,
    clone_ingest_pipeline_dashboard,
    ensure_ingest_data_view_id_matches_title,
    patch_data_view,
    patch_elasticsearch_dashboards,
    verify_dashboard_search,
    verify_elasticsearch_dashboards,
    verify_ingest_pipeline_dashboard,
)

ROOT = Path(__file__).parent


def main() -> int:
    kb_ip = NODES["kibana"][0]
    es = connect(NODES["es01"][0])
    auth = curl_elastic_auth(get_elastic_password(es))
    es.close()

    kb = connect(kb_ip)
    for view_id in DATA_VIEWS:
        patch_data_view(kb, auth, view_id)
    ok = ensure_ingest_data_view_id_matches_title(kb, auth)
    ok = patch_elasticsearch_dashboards(kb, auth) and ok
    ok = clone_ingest_pipeline_dashboard(kb, auth) and ok
    ok = (
        ok
        and verify_dashboard_search(kb, auth)
        and verify_elasticsearch_dashboards(kb, auth)
        and verify_ingest_pipeline_dashboard(kb, auth)
    )
    kb.close()
    print(f"data_view={INGEST_PIPELINE_INDEX}", flush=True)
    print(f"fixed_dashboard_id={INGEST_PIPELINE_DASHBOARD_FIXED}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())