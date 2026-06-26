#!/usr/bin/env python3
"""Review cluster/Kibana/agent logs and dashboards; print issues for fixing."""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from deploy_ordered_stack import ES_NODES, NODES, connect, curl_elastic_auth, get_elastic_password, run
from monitoring_credentials import ensure_monitoring_user

ROOT = Path(__file__).parent
ISSUES: list[str] = []
WARNINGS: list[str] = []


def note(msg: str) -> None:
    ISSUES.append(msg)
    print(f"ISSUE: {msg}", flush=True)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARN: {msg}", flush=True)


def check_es_cluster(es, elastic_pwd: str) -> None:
    auth = curl_elastic_auth(elastic_pwd)
    print("\n=== ES cluster health ===", flush=True)
    health = run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cluster/health?pretty'", check=False)
    print(health, flush=True)
    if '"status" : "red"' in health or '"status":"red"' in health:
        note("Elasticsearch cluster status is RED")
    nodes = run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cat/nodes?v&h=name,node.role,master,heap.percent,cpu,load_1m'", check=False)
    print(nodes, flush=True)
    unassigned = run(
        es,
        f"curl -sk -u {auth} 'https://localhost:9200/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason' | grep UNASSIGNED || echo NONE",
        check=False,
    )
    print("Unassigned shards:\n", unassigned, flush=True)
    if "UNASSIGNED" in unassigned and "NONE" not in unassigned.splitlines()[0]:
        note("Elasticsearch has unassigned shards")


def check_es_yml_bootstrap(ip: str, fqdn: str) -> None:
    c = connect(ip)
    yml = run(c, "grep -E '^(cluster.initial_master_nodes|discovery.seed_hosts)' /etc/elasticsearch/elasticsearch.yml || echo NONE", check=False)
    print(f"\n=== ES yml bootstrap {fqdn} ===\n{yml}", flush=True)
    if yml and "NONE" not in yml:
        masters = sum(1 for ln in yml.splitlines() if ln.startswith("cluster.initial_master_nodes"))
        seeds = sum(1 for ln in yml.splitlines() if ln.startswith("discovery.seed_hosts"))
        if masters > 1 or seeds > 1:
            note(f"Duplicate bootstrap stanzas in elasticsearch.yml on {fqdn}")
        if masters > 0:
            note(f"cluster.initial_master_nodes still present on {fqdn} (should be removed after bootstrap)")
        if seeds == 0:
            note(f"discovery.seed_hosts missing on {fqdn}")
    c.close()


def check_es_logs(ip: str, fqdn: str) -> None:
    print(f"\n=== ES logs {fqdn} (recent) ===", flush=True)
    c = connect(ip)
    out = run(
        c,
        "journalctl -u elasticsearch --since '6 hours ago' --no-pager 2>/dev/null | "
        "grep -iE 'error|exception|fatal' | grep -viE 'deprecation|ssl handshake|received plaintext' | tail -15 || echo CLEAN",
        check=False,
        timeout=60,
    )
    print(out[-2000:] if out else "CLEAN", flush=True)
    if out and "CLEAN" not in out and re.search(r"(?i)(out_of_memory|corrupt|failed to start|fatal)", out):
        note(f"Recent ES log errors on {fqdn}")
    c.close()


def check_kibana_logs() -> None:
    kb_ip = NODES["kibana"][0]
    print("\n=== Kibana server logs (recent) ===", flush=True)
    c = connect(kb_ip)
    out = run(
        c,
        "journalctl -u kibana --since '2 hours ago' --no-pager | "
        "grep -iE 'error|exception|fatal|FATAL|failed' | "
        "grep -viE 'deprecation|BrowserType|legacy endpoint|monitoring_bulk|NoLivingConnections|telemetry_events' | tail -25 || echo CLEAN",
        check=False,
        timeout=60,
    )
    print(out[-3000:] if out else "CLEAN", flush=True)
    if out and "CLEAN" not in out:
        if re.search(r"(?i)(crash|cannot start|out of memory)", out):
            note("Kibana server log shows critical errors")
        elif re.search(r"ECONNREFUSED.*9200", out) and not re.search(
            r"(?i)(ProductDocBase|kibana-knowledge-base-artifacts)", out
        ):
            warn("Kibana transient ES connection errors in recent logs (check cluster uptime)")
        if "synthetics from registry" in out:
            warn("Kibana Fleet synthetics registry errors (air-gap EPR — expected if synthetics not staged)")
        if "kibana-knowledge-base-artifacts" in out:
            warn("Kibana external artifact fetch failures (air-gap — non-critical ProductDocBase)")
    telem = run(c, "grep -E '^(telemetry\\.|newsfeed\\.)' /etc/kibana/kibana.yml || echo MISSING", check=False)
    print("\n=== Kibana telemetry settings ===\n", telem, flush=True)
    if "telemetry.enabled: false" not in telem:
        note("Kibana telemetry not disabled for air-gap")
    epr = run(c, "journalctl -u local-epr -n 30 --no-pager | grep -iE 'error|exception|traceback' | tail -10 || echo CLEAN", check=False)
    print("\n=== local-epr ===\n", epr, flush=True)
    if epr and "CLEAN" not in epr and "error" in epr.lower():
        note("local-epr service has errors")
    c.close()


def check_agent_logs() -> None:
    targets = list(ES_NODES) + [NODES["kibana"], NODES["fleet"]]
    for ip, fqdn in targets:
        print(f"\n=== elastic-agent {fqdn} ===", flush=True)
        c = connect(ip)
        status = run(c, "elastic-agent status 2>&1 | head -8", check=False, timeout=45)
        print(status, flush=True)
        if "(STARTING)" in status:
            warn(f"elastic-agent still starting on {fqdn}")
        elif "(DEGRADED)" in status and "missed" in status:
            warn(f"elastic-agent degraded on {fqdn} (may be recovering after restart)")
        elif "(DEGRADED)" in status:
            note(f"elastic-agent degraded on {fqdn}")
        elif "FAILED" in status and "503" in status:
            warn(f"elastic-agent fleet 503 on {fqdn} (Fleet/ES may be recovering)")
        elif "FAILED" in status and "403" in status and "ErrAgentIdentity" in status:
            warn(f"elastic-agent identity mismatch on {fqdn} (restart usually clears)")
        elif "FAILED" in status:
            note(f"elastic-agent unhealthy on {fqdn}")
        logs = run(
            c,
            "journalctl -u elastic-agent -n 40 --no-pager | "
            "grep -iE 'error|failed|refused|401|403' | grep -viE 'deprecation' | tail -12 || echo CLEAN",
            check=False,
            timeout=45,
        )
        print(logs[-1500:] if logs else "CLEAN", flush=True)
        c.close()


def check_metrics_data(es, elastic_pwd: str) -> None:
    auth = curl_elastic_auth(elastic_pwd)
    print("\n=== Metrics data streams ===", flush=True)
    py = (
        "import sys,json; d=json.load(sys.stdin); "
        "names=sorted(x['name'] for x in d.get('data_streams',[])); "
        "print(chr(10).join(names))"
    )
    out = run(es, f"curl -sk -u {auth} 'https://localhost:9200/_data_stream/metrics-*?pretty' | python3 -c {shlex.quote(py)}", check=False)
    print(out, flush=True)
    for pattern in ("metrics-system.", "metrics-elasticsearch.", "metrics-kibana."):
        if pattern not in out:
            note(f"No {pattern}* metrics data streams — integration streams may be empty")
    mon = run(es, f"curl -sk -u {auth} 'https://localhost:9200/_cat/indices/.monitoring-*?v&h=index,docs.count' 2>/dev/null | tail -5", check=False)
    print("\n=== Legacy stack monitoring indices ===\n", mon, flush=True)


def check_dashboards(kb, elastic_pwd: str) -> None:
    auth = curl_elastic_auth(elastic_pwd)
    print("\n=== Kibana dashboards ===", flush=True)
    out = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/saved_objects/_find?type=dashboard&per_page=50&fields=title'",
        check=False,
        timeout=60,
    )
    try:
        data = json.loads(out)
        dashboards = data.get("saved_objects", [])
        print(f"Found {len(dashboards)} dashboards", flush=True)
        linux_relevant = [
            d.get("attributes", {}).get("title", "")
            for d in dashboards
            if any(
                tag in d.get("attributes", {}).get("title", "")
                for tag in ("[Metrics System]", "[Elastic Agent]", "[Elasticsearch]", "[Logs System]")
            )
        ]
        for title in linux_relevant[:20]:
            print(f"  - {title}", flush=True)
        if len(dashboards) == 0:
            note("No Kibana dashboards found")
        windows_only = sum(1 for d in dashboards if "Windows" in d.get("attributes", {}).get("title", ""))
        if windows_only > 5:
            print(f"  ({windows_only} Windows-specific dashboards present — expected on Linux fleet packages)", flush=True)
    except json.JSONDecodeError:
        print(out[:500], flush=True)
        note("Could not list Kibana dashboards")

    for path, label in (
        ("/api/fleet/agents?perPage=20", "fleet agents"),
        ("/api/fleet/package_policies?perPage=30", "package policies"),
        ("/api/status", "kibana status"),
    ):
        r = run(kb, f"curl -s -o /dev/null -w '%{{http_code}}' -u {auth} -H 'kbn-xsrf:true' 'http://127.0.0.1:5601{path}'", check=False)
        code = (r or "").strip()[-3:]
        print(f"  {label}: HTTP {code}", flush=True)
        if code not in ("200", "302"):
            note(f"Kibana API {label} returned HTTP {code}")


def check_integrations(kb, elastic_pwd: str) -> None:
    auth = curl_elastic_auth(elastic_pwd)
    print("\n=== Integration endpoints ===", flush=True)
    py = (
        "import sys,json; d=json.load(sys.stdin); "
        "[print(p.get('package',{}).get('name'), "
        "len((p.get('inputs',[{}])[0].get('streams') or [])), "
        "(p.get('inputs',[{}])[0].get('vars',{}).get('hosts',{}).get('value') or ['?'])[0], "
        "p.get('inputs',[{}])[0].get('vars',{}).get('username',{}).get('value','-')) "
        "for p in d.get('items',[]) if p.get('package',{}).get('name') in ('elasticsearch','kibana','system')]"
    )
    print(
        run(
            kb,
            f"curl -s -u {auth} -H 'kbn-xsrf:true' "
            f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=50' | "
            f"python3 -c {shlex.quote(py)}",
            check=False,
        ),
        flush=True,
    )
    empty = run(
        kb,
        f"curl -s -u {auth} -H 'kbn-xsrf:true' "
        f"'http://127.0.0.1:5601/api/fleet/package_policies?perPage=100' | "
        f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
        f"bad=[p.get('name') for p in d.get('items',[]) "
        f"if p.get('package',{{}}).get('name') in ('system','elasticsearch','kibana') "
        f"and any(not (i.get('streams') or []) for i in p.get('inputs',[]) if i.get('enabled',True))]; "
        f"print(','.join(bad) if bad else 'OK')\"",
        check=False,
    ).strip()
    if empty and empty != "OK":
        note(f"Package policies with empty streams: {empty}")


def main() -> int:
    es = connect(NODES["es01"][0])
    elastic_pwd = get_elastic_password(es)
    ensure_monitoring_user(es, run, elastic_pwd)
    check_es_cluster(es, elastic_pwd)
    check_metrics_data(es, elastic_pwd)
    es.close()

    for ip, fqdn in ES_NODES:
        check_es_yml_bootstrap(ip, fqdn)
        check_es_logs(ip, fqdn)

    check_kibana_logs()
    check_agent_logs()

    kb = connect(NODES["kibana"][0])
    check_dashboards(kb, elastic_pwd)
    check_integrations(kb, elastic_pwd)
    kb.close()

    print(f"\n=== SUMMARY: {len(ISSUES)} issue(s), {len(WARNINGS)} warning(s) ===", flush=True)
    for i in ISSUES:
        print(f"  ISSUE: {i}", flush=True)
    for w in WARNINGS:
        print(f"  WARN:  {w}", flush=True)
    lines = []
    if ISSUES:
        lines.append("Issues:")
        lines.extend(f"  - {i}" for i in ISSUES)
    if WARNINGS:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in WARNINGS)
    Path(ROOT / "cluster_review_issues.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(ISSUES)


if __name__ == "__main__":
    raise SystemExit(main())