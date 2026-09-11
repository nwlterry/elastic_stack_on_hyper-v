#!/usr/bin/env python3
"""Delete every NFS snapshot in fs_nfs_snapshots except the named keep snap."""
from __future__ import annotations

import argparse
import json

from create_named_snapshot import REPO, es
from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password


def _snaps(c, auth: str) -> list[tuple[str, str]]:
    out = es(
        c,
        auth,
        "GET",
        f"/_snapshot/{REPO}/_all?filter_path=snapshots.snapshot,snapshots.state",
        timeout=180,
    )
    start = out.find("{")
    if start < 0:
        return []
    try:
        data = json.loads(out[start:])
    except json.JSONDecodeError:
        return []
    rows = []
    for s in data.get("snapshots") or []:
        name = s.get("snapshot")
        if name:
            rows.append((name, s.get("state") or ""))
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keep", default="pre-upgrade-to-9.5.3")
    args = p.parse_args()
    keep = args.keep

    c = connect(NODES["es01"][0])
    auth = curl_elastic_auth(get_elastic_password(c))
    rows = _snaps(c, auth)
    print("before:", rows, flush=True)
    names = [n for n, _ in rows]
    if keep not in names:
        print(f"ERROR: keep snapshot {keep} not found", flush=True)
        c.close()
        return 1
    keep_state = dict(rows).get(keep)
    if keep_state != "SUCCESS":
        print(f"ERROR: keep snapshot {keep} state={keep_state}", flush=True)
        c.close()
        return 1

    to_delete = [n for n in names if n != keep]
    for name in to_delete:
        print(f"DELETE {REPO}/{name}", flush=True)
        out = es(c, auth, "DELETE", f"/_snapshot/{REPO}/{name}", timeout=300)
        print(out[-400:], flush=True)
        if '"error"' in out and '"acknowledged":true' not in out.replace(" ", ""):
            print(f"WARN delete {name} may have failed", flush=True)

    after = _snaps(c, auth)
    print("after:", after, flush=True)
    c.close()
    left = [n for n, _ in after]
    if left != [keep]:
        print(f"ERROR remaining snapshots {left}, expected only {keep}", flush=True)
        return 1
    print(f"OK only {keep} remains", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
