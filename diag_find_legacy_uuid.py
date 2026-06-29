#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, run

LEGACY = "befe6dd7-ec0b-4cb7-aa59-e4d5e6f39ae9"
IDS = [
    "elasticsearch-b1399af0-628c-11ee-9c63-732d7f759a7a",
    "elasticsearch-ea888f80-61e4-11ee-b5a1-0d1803efe5cf",
]
auth = curl_elastic_auth((Path(__file__).parent / "secrets" / "elastic-password").read_text().strip())
kb = connect(NODES["kibana"][0])

for did in IDS:
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/saved_objects/dashboard/{did}'",
        check=False,
    )
    dash = json.loads(out.strip().splitlines()[-1])
    s = json.dumps(dash)
    idx = 0
    print(f"\n=== {dash.get('attributes', {}).get('title', did)} ===")
    while True:
        pos = s.find(LEGACY, idx)
        if pos < 0:
            break
        print(f"  at {pos}: ...{s[max(0, pos - 60):pos + len(LEGACY) + 60]}...")
        idx = pos + 1

kb.close()