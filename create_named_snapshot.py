#!/usr/bin/env python3
"""Create an NFS snapshot in fs_nfs_snapshots without deleting existing snaps."""
from __future__ import annotations

import argparse
import json
import shlex
import time
from datetime import datetime, timezone

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run

REPO = "fs_nfs_snapshots"
FEATURE_STATES = [
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
]


def es(c, auth: str, method: str, path: str, body=None, timeout: int = 600) -> str:
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--name",
        default=f"pre-upgrade-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}",
    )
    p.add_argument("--because", default="pre-upgrade checkpoint")
    p.add_argument(
        "--reregister",
        action="store_true",
        help="Re-PUT fs_nfs_snapshots (location=/mnt/es-snapshots) if ES disabled the repo",
    )
    args = p.parse_args()

    c = connect(NODES["es01"][0])
    auth = curl_elastic_auth(get_elastic_password(c))
    if args.reregister:
        put = es(
            c,
            auth,
            "PUT",
            f"/_snapshot/{REPO}?verify=true",
            {"type": "fs", "settings": {"location": "/mnt/es-snapshots", "compress": True}},
        )
        print("reregister:", put[:1500], flush=True)
        if '"error"' in put and '"acknowledged":true' not in put.replace(" ", ""):
            c.close()
            return 1
    body = {
        "indices": "*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": FEATURE_STATES,
        "metadata": {
            "taken_by": "create_named_snapshot",
            "taken_because": args.because,
        },
    }
    print(f"create {REPO}/{args.name}", flush=True)
    created = es(
        c,
        auth,
        "PUT",
        f"/_snapshot/{REPO}/{args.name}?wait_for_completion=false",
        body,
    )
    print(created, flush=True)
    if '"error"' in created:
        print("SNAPSHOT CREATE FAILED", flush=True)
        c.close()
        return 1
    deadline = time.time() + 3600
    state = ""
    while time.time() < deadline:
        out = es(c, auth, "GET", f"/_snapshot/{REPO}/{args.name}")
        start = out.find("{")
        try:
            data = json.loads(out[start:]) if start >= 0 else {}
        except json.JSONDecodeError:
            data = {}
        snaps = data.get("snapshots") or []
        state = (snaps[0].get("state") if snaps else data.get("state")) or ""
        print(f"  state={state}", flush=True)
        if state in ("SUCCESS", "FAILED", "PARTIAL"):
            break
        time.sleep(15)
    c.close()
    print(f"RESULT {args.name} {state}", flush=True)
    return 0 if state == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
