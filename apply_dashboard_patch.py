#!/usr/bin/env python3
"""Apply dashboard Lens patches without Fleet reinstall."""
from __future__ import annotations

from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password
from fix_dashboard_search import (
    INGEST_PIPELINE_DATA_VIEW,
    INGEST_PIPELINE_INDEX,
    patch_data_view,
    patch_elasticsearch_dashboards,
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
    patch_data_view(kb, auth, INGEST_PIPELINE_DATA_VIEW)
    ok = patch_elasticsearch_dashboards(kb, auth)
    ok = (
        ok
        and verify_elasticsearch_dashboards(kb, auth)
        and verify_ingest_pipeline_dashboard(kb, auth)
    )
    kb.close()
    print(f"data_view={INGEST_PIPELINE_INDEX}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())