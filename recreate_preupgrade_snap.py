#!/usr/bin/env python3
"""Recreate pre-upgrade snapshot with proper feature_states + system indices + global state."""
from __future__ import annotations

import json
import shlex
import time
from datetime import datetime, timezone

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run

REPO = "fs_nfs_snapshots"
OLD = "pre-upgrade-system-20260724-0335"
NEW = f"pre-upgrade-system-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"


def es(c, auth, method, path, body=None, timeout=600):
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def parse_snap(out: str) -> dict:
    start = out.find("{")
    if start < 0:
        return {}
    data = json.loads(out[start:])
    snaps = data.get("snapshots") or []
    return snaps[0] if snaps else data.get("snapshot") or data


def main() -> int:
    c = connect(NODES["es01"][0])
    auth = curl_elastic_auth(get_elastic_password(c))

    print("delete old:", OLD, flush=True)
    print(es(c, auth, "DELETE", f"/_snapshot/{REPO}/{OLD}?pretty"), flush=True)

    # Elastic-recommended feature snapshot + explicit system indices (.*)
    # Two-step merge into one snap: feature_states=* AND indices .* like upgrade guides.
    body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": [
            "security",
            "kibana",
            "fleet",
            "async_search",
            "transform",
            "machine_learning",
            "watcher",
            "enrich",
            "geoip",
            "logstash_management",
            "synonyms",
            "tasks",
            "ent_search",
            "searchable_snapshots",
            "inference_plugin",
        ],
        "metadata": {
            "taken_by": "recreate_preupgrade_snap",
            "taken_because": "pre-upgrade: system indices + all feature states + cluster state",
        },
    }
    print("create:", NEW, flush=True)
    print(
        es(
            c,
            auth,
            "PUT",
            f"/_snapshot/{REPO}/{NEW}?wait_for_completion=false&pretty",
            body,
        ),
        flush=True,
    )

    for i in range(120):
        out = es(c, auth, "GET", f"/_snapshot/{REPO}/{NEW}?pretty")
        s = parse_snap(out)
        state = s.get("state")
        shards = s.get("shards") or {}
        print(
            f"  poll {i}: state={state} shards={shards} features="
            f"{[f.get('feature_name') for f in s.get('feature_states') or []]}",
            flush=True,
        )
        if state in ("SUCCESS", "FAILED", "PARTIAL"):
            print(json.dumps({
                "snapshot": s.get("snapshot"),
                "state": state,
                "include_global_state": s.get("include_global_state"),
                "indices_count": len(s.get("indices") or []),
                "feature_states": [f.get("feature_name") for f in s.get("feature_states") or []],
                "shards": shards,
                "failures": s.get("failures"),
                "duration_in_millis": s.get("duration_in_millis"),
            }, indent=2), flush=True)
            break
        time.sleep(10)
    else:
        print("timeout", flush=True)
        c.close()
        return 1

    print(es(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state,total"), flush=True)
    c.close()
    print("DONE name=", NEW, flush=True)
    return 0 if state == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
