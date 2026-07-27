#!/usr/bin/env python3
"""
After restoring es01-03 to 8.18.4, monitor whether 9.4.1 es04 can rejoin.
Capture all join-related failures from es04 (+ masters) into logs/.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from deploy_ordered_stack import NODES, connect, curl_elastic_auth, get_elastic_password, run

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs" / "downgrade-join-test"
ES01, ES02, ES03 = NODES["es01"], NODES["es02"], NODES["es03"]
ES04_IP, ES04_FQDN = NODES["es04"]

JOIN_PATTERNS = re.compile(
    r"join|version|incompatible|failed to join|NodeDisconnected|"
    r"handshake|transport|remote_transport|security|certificate|"
    r"discovery|seed|master|coordination|reject|illegal|mismatch",
    re.I,
)


def wait_ssh(ip: str, label: str, timeout: int = 900) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            c = connect(ip, attempts=2)
            out = run(c, "hostname; uptime", check=False)
            c.close()
            print(f"  {label} up: {out.strip()[:120]}", flush=True)
            return True
        except Exception as e:
            print(f"  waiting {label}: {e}", flush=True)
            time.sleep(15)
    return False


def es_version(ip: str) -> str:
    c = connect(ip)
    # try with elastic pwd from es01 if available; else unauthenticated banner
    out = run(
        c,
        "curl -sk https://localhost:9200 2>/dev/null | head -c 400; "
        "rpm -q elasticsearch 2>/dev/null; "
        "cat /etc/elasticsearch/elasticsearch.yml | grep -E '^(cluster|node)\\.name' || true",
        check=False,
    )
    c.close()
    return out


def collect_es04_logs(minutes: int = 20) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = LOG_DIR / f"es04-join-capture-{ts}.log"

    lines: list[str] = []
    lines.append(f"=== capture start {ts} ===\n")

    # live cluster from es01 if up
    try:
        es = connect(ES01[0])
        try:
            pwd = get_elastic_password(es)
            auth = curl_elastic_auth(pwd)
            lines.append("=== es01 cluster health (if 8.18.4) ===\n")
            lines.append(
                run(
                    es,
                    f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'; "
                    f"curl -sk -u {auth} 'https://localhost:9200'; "
                    f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v'",
                    check=False,
                )
            )
        except Exception as e:
            lines.append(f"es01 auth probe failed: {e}\n")
            lines.append(run(es, "curl -sk https://localhost:9200 2>/dev/null | head -c 500", check=False))
        es.close()
    except Exception as e:
        lines.append(f"es01 unreachable: {e}\n")

    # versions
    for label, ip in (("es01", ES01[0]), ("es02", ES02[0]), ("es03", ES03[0]), ("es04", ES04_IP)):
        try:
            lines.append(f"\n=== version {label} ({ip}) ===\n")
            lines.append(es_version(ip))
        except Exception as e:
            lines.append(f"{label} version fail: {e}\n")

    # restart es04 elasticsearch to force fresh join attempts
    try:
        c4 = connect(ES04_IP)
        lines.append("\n=== restart elasticsearch on es04 to force join ===\n")
        lines.append(
            run(
                c4,
                "systemctl restart elasticsearch; sleep 5; systemctl is-active elasticsearch; "
                "rpm -q elasticsearch",
                check=False,
                timeout=180,
            )
        )

        # poll logs for join failures
        end = time.time() + minutes * 60
        poll = 0
        while time.time() < end:
            poll += 1
            print(f"  es04 log poll {poll}/{minutes*6}", flush=True)
            chunk = run(
                c4,
                "journalctl -u elasticsearch -n 200 --no-pager 2>/dev/null; "
                "echo '--- log file ---'; "
                "ls -1t /var/log/elasticsearch/*.log 2>/dev/null | head -3; "
                "for f in $(ls -1t /var/log/elasticsearch/*.log 2>/dev/null | head -2); do "
                "  echo \"=== $f ===\"; tail -n 150 \"$f\"; "
                "done",
                check=False,
                timeout=120,
            )
            matched = [ln for ln in chunk.splitlines() if JOIN_PATTERNS.search(ln)]
            header = f"\n=== poll {poll} {datetime.now(timezone.utc).isoformat()} matched={len(matched)} ===\n"
            lines.append(header)
            # keep full tail for context + filtered highlights
            lines.append("--- FILTERED JOIN-RELATED ---\n")
            lines.append("\n".join(matched[-200:]))
            lines.append("\n--- RAW TAIL (last 80 lines) ---\n")
            lines.append("\n".join(chunk.splitlines()[-80:]))
            # also try local API
            lines.append(
                "\n"
                + run(
                    c4,
                    "curl -sk https://localhost:9200 2>/dev/null | head -c 300; echo; "
                    "curl -sk https://localhost:9200/_cluster/health?pretty 2>/dev/null | head -c 400",
                    check=False,
                )
            )
            out_file.write_text("\n".join(lines), encoding="utf-8", errors="replace")
            time.sleep(10)

        # masters' perspective
        for label, ip in (("es01", ES01[0]), ("es02", ES02[0]), ("es03", ES03[0])):
            try:
                cm = connect(ip)
                lines.append(f"\n=== {label} journal join/version rejects ===\n")
                raw = run(
                    cm,
                    "journalctl -u elasticsearch -n 300 --no-pager 2>/dev/null | "
                    "grep -iE 'join|version|incompatible|reject|handshake|security' | tail -n 100; "
                    "ls -1t /var/log/elasticsearch/*.log 2>/dev/null | head -1 | "
                    "xargs -I{} sh -c 'grep -iE \"join|version|incompatible|reject|handshake\" {} | tail -n 80'",
                    check=False,
                    timeout=120,
                )
                lines.append(raw)
                cm.close()
            except Exception as e:
                lines.append(f"{label} log fail: {e}\n")

        c4.close()
    except Exception as e:
        lines.append(f"es04 capture fail: {e}\n")

    lines.append(f"\n=== capture end {datetime.now(timezone.utc).isoformat()} ===\n")
    out_file.write_text("\n".join(lines), encoding="utf-8", errors="replace")
    # also write filtered-only summary
    summary = LOG_DIR / f"es04-join-FILTERED-{ts}.log"
    filt = [ln for ln in lines if JOIN_PATTERNS.search(ln) or ln.startswith("===")]
    summary.write_text("\n".join(filt), encoding="utf-8", errors="replace")
    print(f"Wrote {out_file}", flush=True)
    print(f"Wrote {summary}", flush=True)
    return out_file


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("=== Wait for es01-03 after restore ===", flush=True)
    for label, node in (("es01", ES01), ("es02", ES02), ("es03", ES03), ("es04", NODES["es04"])):
        if not wait_ssh(node[0], label, timeout=900):
            print(f"WARN {label} never came up", flush=True)

    print("=== Capture join failures (20 minutes of es04 restarts/logs) ===", flush=True)
    # shorter default for first pass; user can re-run longer
    path = collect_es04_logs(minutes=8)
    print(f"\nCapture complete: {path}", flush=True)
    # print last filtered highlights
    text = path.read_text(errors="replace")
    hits = [ln for ln in text.splitlines() if JOIN_PATTERNS.search(ln)]
    print("\n=== Sample join-related lines (last 40) ===", flush=True)
    for ln in hits[-40:]:
        print(ln, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
