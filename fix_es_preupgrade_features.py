#!/usr/bin/env python3
"""Delete and recreate pre-upgrade ES snap with explicit feature_states (non-empty)."""
from __future__ import annotations

import json
import shlex
import time

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run

REPO = "fs_nfs_snapshots"
SNAP = "pre-upgrade-system-8.18.4-20260724"

# Explicit list used successfully earlier; * left empty feature_states in this cluster.
FEATURES = [
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
]


def es(c, auth, method, path, body=None, timeout=600):
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def parse(out: str) -> dict:
    start = out.find("{")
    if start < 0:
        return {}
    data = json.loads(out[start:])
    snaps = data.get("snapshots") or []
    return snaps[0] if snaps else data


def main() -> int:
    c = connect(NODES["es02"][0])
    auth = curl_elastic_auth(get_elastic_password(c))

    print("DELETE", SNAP, flush=True)
    print(es(c, auth, "DELETE", f"/_snapshot/{REPO}/{SNAP}?pretty", timeout=600), flush=True)
    time.sleep(2)

    body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": FEATURES,
        "metadata": {
            "taken_by": "fix_es_preupgrade_features",
            "taken_because": "pre-upgrade system 8.18.4: system indices + feature states + cluster state",
            "es_version": "8.18.4",
        },
    }
    print("CREATE", SNAP, flush=True)
    print(
        es(
            c,
            auth,
            "PUT",
            f"/_snapshot/{REPO}/{SNAP}?wait_for_completion=false&pretty",
            body,
            timeout=120,
        ),
        flush=True,
    )

    state = "IN_PROGRESS"
    for i in range(180):
        out = es(c, auth, "GET", f"/_snapshot/{REPO}/{SNAP}?pretty", timeout=120)
        s = parse(out)
        state = s.get("state") or state
        feats = [f.get("feature_name") for f in (s.get("feature_states") or [])]
        shards = s.get("shards") or {}
        print(f"poll {i}: state={state} shards={shards} features={feats}", flush=True)
        if state in ("SUCCESS", "FAILED", "PARTIAL"):
            print(
                json.dumps(
                    {
                        "snapshot": s.get("snapshot"),
                        "state": state,
                        "include_global_state": s.get("include_global_state"),
                        "indices_count": len(s.get("indices") or []),
                        "feature_states": feats,
                        "shards": shards,
                        "failures": s.get("failures"),
                        "duration_in_millis": s.get("duration_in_millis"),
                    },
                    indent=2,
                ),
                flush=True,
            )
            break
        time.sleep(10)
    else:
        print("timeout", flush=True)
        c.close()
        return 1

    c.close()
    print("DONE", SNAP, state, flush=True)
    return 0 if state == "SUCCESS" and len(feats) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
