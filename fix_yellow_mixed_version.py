#!/usr/bin/env python3
"""
Reduce yellow health in a mixed-version Elasticsearch cluster.

Lab context: masters on 8.18.4 + one hot node on 8.19.x leaves replica shards
unassigned when primaries live on the newer node (node_version decider).

What this script does
---------------------
1. Sets ``number_of_replicas: 0`` and ``auto_expand_replicas: false`` on every
   non-restricted index that still has unassigned replicas.
2. Reports remaining unassigned shards (typically system indices that refuse
   settings overrides: ``.security-*``, ``.fleet-actions-results``).
3. Explains that full green requires version alignment (all nodes same major.minor
   path) so replicas of primaries on the newer node can land elsewhere.

What it does *not* do
---------------------
- Override system-index replica settings (Elastic blocks this).
- Upgrade or downgrade nodes (use upgrade_es_to_8_19_18.py / Hyper-V restore).

Usage
-----
    python fix_yellow_mixed_version.py
    python fix_yellow_mixed_version.py --es-url https://10.44.40.31:9200
"""
from __future__ import annotations

import argparse
import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from elastic_credentials import load_config_password, load_local_password


def resolve_password() -> str:
    pwd = load_local_password() or load_config_password()
    if not pwd:
        raise SystemExit(
            "No elastic password found. Write secrets/elastic-password or set "
            "ElasticPassword in config.psd1 (gitignored)."
        )
    return pwd


def make_client(es_url: str, password: str):
    ctx = ssl._create_unverified_context()
    auth = base64.b64encode(f"elastic:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }

    def request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            es_url.rstrip("/") + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                raw = resp.read().decode() or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw
            return e.code, parsed
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    return request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--es-url",
        default="https://10.44.40.31:9200",
        help="Elasticsearch HTTPS URL (default: es01)",
    )
    args = parser.parse_args()

    request = make_client(args.es_url, resolve_password())
    code, health = request("GET", "/_cluster/health")
    if code != 200 or not isinstance(health, dict):
        print(f"health check failed: {code} {health}", file=sys.stderr)
        return 1

    print(
        f"before: status={health.get('status')} "
        f"unassigned={health.get('unassigned_shards')} "
        f"active_pct={health.get('active_shards_percent_as_number')}"
    )

    code, shards = request(
        "GET",
        "/_cat/shards?h=index,shard,prirep,state,node&s=state&format=json",
    )
    if code != 200 or not isinstance(shards, list):
        print(f"shard listing failed: {code} {shards}", file=sys.stderr)
        return 1

    unassigned = sorted(
        {s["index"] for s in shards if s.get("state") == "UNASSIGNED"}
    )
    if not unassigned:
        print("no unassigned shards")
        return 0

    settings_body = {
        "index": {
            "number_of_replicas": 0,
            "auto_expand_replicas": "false",
        }
    }
    # auto_expand_replicas: "0-1" reverts number_of_replicas to 1 — must disable it.
    for index in unassigned:
        code, body = request("PUT", f"/{index}/_settings", settings_body)
        summary = body if isinstance(body, str) else json.dumps(body)[:180]
        print(f"  settings {index}: {code} {summary}")

    time.sleep(2)
    code, health = request("GET", "/_cluster/health")
    code2, shards = request(
        "GET",
        "/_cat/shards?h=index,shard,prirep,state,node&s=state&format=json",
    )
    remaining = [
        s for s in (shards if isinstance(shards, list) else [])
        if s.get("state") == "UNASSIGNED"
    ]
    if isinstance(health, dict):
        print(
            f"after: status={health.get('status')} "
            f"unassigned={health.get('unassigned_shards')} "
            f"active_pct={health.get('active_shards_percent_as_number')}"
        )
    if remaining:
        print("\nRemaining unassigned (expected for mixed-version + system indices):")
        for s in remaining:
            print(f"  {s.get('index')} {s.get('prirep')} {s.get('state')}")
        print(
            "\nFull green requires all Elasticsearch nodes on a compatible version "
            "so replicas of primaries on the newest node can allocate "
            "(e.g. rolling upgrade es01–es03 to match es04, or Hyper-V restore "
            "of all ES nodes to the same snap)."
        )
    else:
        print("cluster has no unassigned shards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
