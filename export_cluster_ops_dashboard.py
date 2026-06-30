#!/usr/bin/env python3
"""Export cluster operations dashboard and dependencies from Kibana as NDJSON."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from create_cluster_ops_dashboard import DASHBOARD_ID
from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run

EXPORT_PATH = Path(__file__).parent / "kibana" / "exports" / f"{DASHBOARD_ID}.ndjson"


def export_dashboard_ndjson(kb, auth: str) -> str:
    body = {
        "objects": [{"type": "dashboard", "id": DASHBOARD_ID}],
        "includeReferencesDeep": True,
        "excludeExportDetails": False,
    }
    return run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' -H 'Content-Type: application/json' "
        f"-X POST 'http://127.0.0.1:5601/api/saved_objects/_export' "
        f"-d {shlex.quote(json.dumps(body))}",
        check=True,
        timeout=120,
    )


def main() -> int:
    es = connect(NODES["es01"][0])
    auth = curl_elastic_auth(get_elastic_password(es))
    kb = connect(NODES["kibana"][0])

    print(f"=== Export {DASHBOARD_ID} ===", flush=True)
    ndjson = export_dashboard_ndjson(kb, auth).strip()
    if not ndjson:
        print("FAIL empty export", flush=True)
        kb.close()
        es.close()
        return 1

    lines = [ln for ln in ndjson.splitlines() if ln.strip()]
    objects = 0
    for ln in lines:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if row.get("type") and row.get("id"):
            objects += 1

    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_PATH.write_text(ndjson + "\n", encoding="utf-8")
    print(f"OK {EXPORT_PATH} ({len(lines)} lines, {objects} saved objects)", flush=True)

    kb.close()
    es.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())