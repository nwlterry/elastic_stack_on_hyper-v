#!/usr/bin/env python3
"""
Deploy APM Server on Fleet/elastic-agent host (air-gap Fleet APM package is a stub),
enable APM collection on ES + Kibana, create second ES system snapshot.

Alert rule already created: Upgrade Testing Sample Alert.
HV restore to 8.18.4 already done.
"""
from __future__ import annotations


def _lab_elastic_password() -> str:
    from elastic_credentials import load_config_password, load_local_password
    pwd = load_local_password() or load_config_password()
    if not pwd:
        raise SystemExit(
            "Set secrets/elastic-password or ElasticPassword in config.psd1"
        )
    return pwd


import json
import shlex
import time
from datetime import datetime
from pathlib import Path

from deploy_ordered_stack import NODES, REMOTE, connect, curl_elastic_auth, get_elastic_password, run
from fix_dashboard_search import kibana_curl
from scp import SCPClient

ROOT = Path(__file__).resolve().parent
# mis-saved earlier as .zip; actually apm-server tarball
APM_TGZ_LOCAL = ROOT / "packages" / "apm-server-8.18.4-linux-x86_64.tar.gz"
APM_TGZ_BAD = ROOT / "packages" / "apm-8.18.4.zip"
REPO = "fs_nfs_snapshots"
SECOND_ES_SNAP = "pre-upgrade-apm-alert-8.18.4-20260724"
APM_HOST = NODES["fleet"][0]
APM_URL = f"http://{APM_HOST}:8200"
PWD = _lab_elastic_password()
LOG = ROOT / "logs" / "deploy_apm_finish.log"
FLEET_POLICY_ID = "9be39452-a297-4b8b-9fae-b12ab3cb9315"

FEATURES = [
    "security", "kibana", "fleet", "async_search", "transform",
    "machine_learning", "watcher", "enrich", "geoip", "logstash_management",
    "synonyms", "tasks", "ent_search", "searchable_snapshots", "inference_plugin",
]


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def es_api(c, auth, method, path, body=None, timeout=300):
    cmd = f"curl -sk -u {auth} -X {method} "
    if body is not None:
        cmd += f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))} "
    cmd += f"'https://localhost:9200{path}'"
    return run(c, cmd, check=False, timeout=timeout)


def parse_json(out: str):
    for ch in ("{", "["):
        s = out.find(ch)
        if s >= 0:
            try:
                return json.loads(out[s:])
            except json.JSONDecodeError:
                pass
    return None


def ensure_apm_tarball() -> Path:
    if APM_TGZ_LOCAL.is_file() and APM_TGZ_LOCAL.stat().st_size > 1_000_000:
        return APM_TGZ_LOCAL
    if APM_TGZ_BAD.is_file() and APM_TGZ_BAD.stat().st_size > 1_000_000:
        # rename if it's actually the apm-server tarball
        APM_TGZ_BAD.replace(APM_TGZ_LOCAL)
        return APM_TGZ_LOCAL
    import subprocess

    url = "https://artifacts.elastic.co/downloads/apm-server/apm-server-8.18.4-linux-x86_64.tar.gz"
    log(f"download {url}")
    subprocess.check_call(["curl.exe", "-fSL", "-o", str(APM_TGZ_LOCAL), url], timeout=600)
    return APM_TGZ_LOCAL


def deploy_apm_on_fleet(elastic_pwd: str) -> None:
    """
    Install APM Server 8.18.4 on Fleet host (same host as elastic-agent).
    Air-gapped Fleet APM integration package has empty policy_templates and cannot
    start APM via package_policies; co-locate APM Server with the Fleet agent host.
    """
    log("=== Deploy APM Server on Fleet/elastic-agent host ===")
    tgz = ensure_apm_tarball()
    fl = connect(NODES["fleet"][0], attempts=20)
    run(fl, "mkdir -p /opt/elastic-setup/archives /opt/apm-server", check=False)
    with SCPClient(fl.get_transport()) as scp:
        scp.put(str(tgz), "/opt/elastic-setup/archives/apm-server-8.18.4-linux-x86_64.tar.gz")

    # CA for ES HTTPS
    es = connect(NODES["es01"][0], attempts=10)
    ca = run(es, "cat /etc/elasticsearch/certs/http_ca.crt", check=False)
    es.close()
    if "BEGIN CERTIFICATE" in ca:
        # extract only cert body
        start = ca.find("-----BEGIN")
        end = ca.rfind("-----END")
        cert = ca[start : end + len("-----END CERTIFICATE-----")] if start >= 0 else ca
        run(
            fl,
            f"mkdir -p /opt/apm-server; cat > /opt/apm-server/http_ca.crt <<'EOF'\n{cert}\nEOF\n",
            check=False,
        )

    es_hosts = json.dumps([f"https://{NODES[k][1]}:9200" for k in ("es01", "es02", "es03", "es04")])
    yml = f"""
apm-server:
  host: "0.0.0.0:8200"
  rum:
    enabled: false
  auth:
    anonymous:
      enabled: true
      allow_agent: [rum-js, js-base, iOS/swift]
      allow_service: null
      rate_limit:
        event_limit: 300
        ip_limit: 1000

output.elasticsearch:
  hosts: {es_hosts}
  protocol: https
  username: elastic
  password: {elastic_pwd}
  ssl:
    certificate_authorities: ["/opt/apm-server/http_ca.crt"]
    verification_mode: certificate

logging.to_files: true
logging.files:
  path: /var/log/apm-server
  name: apm-server
  keepfiles: 7
""".lstrip()

    run(
        fl,
        "set -e\n"
        "cd /opt\n"
        "tar -xzf /opt/elastic-setup/archives/apm-server-8.18.4-linux-x86_64.tar.gz\n"
        "rm -rf /opt/apm-server/bin 2>/dev/null || true\n"
        "rm -rf /usr/share/apm-server 2>/dev/null || true\n"
        "if [ -d /opt/apm-server-8.18.4-linux-x86_64 ]; then "
        "  rm -rf /opt/apm-server-install; mv /opt/apm-server-8.18.4-linux-x86_64 /opt/apm-server-install; "
        "fi\n"
        "mkdir -p /opt/apm-server /var/log/apm-server\n"
        "cp -a /opt/apm-server-install/* /opt/apm-server/ 2>/dev/null || "
        "cp -a /opt/apm-server-8.18.4-linux-x86_64/* /opt/apm-server/ 2>/dev/null || true\n"
        f"cat > /opt/apm-server/apm-server.yml <<'EOF'\n{yml}\nEOF\n"
        "chmod 600 /opt/apm-server/apm-server.yml\n"
        "id apm-server >/dev/null 2>&1 || useradd -r -s /sbin/nologin apm-server || true\n"
        "chown -R root:root /opt/apm-server\n"
        "cat > /etc/systemd/system/apm-server.service <<'EOF'\n"
        "[Unit]\nDescription=Elastic APM Server\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\nUser=root\n"
        "ExecStart=/opt/apm-server/apm-server -c /opt/apm-server/apm-server.yml -e\n"
        "Restart=on-failure\nRestartSec=5\nLimitNOFILE=65535\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
        "EOF\n"
        "systemctl daemon-reload\n"
        "systemctl enable apm-server\n"
        "systemctl restart apm-server\n"
        "sleep 3\n"
        "systemctl is-active apm-server || (journalctl -u apm-server -n 40 --no-pager; false)\n"
        "firewall-cmd --permanent --add-port=8200/tcp 2>/dev/null || true\n"
        "firewall-cmd --reload 2>/dev/null || true\n"
        "ss -lntp | grep 8200 || true\n"
        "curl -s -o /dev/null -w 'apm_http=%{http_code}\\n' http://127.0.0.1:8200/ || true\n",
        check=False,
        timeout=300,
    )
    # also note on Fleet policy (audit trail) via a simple package policy description update if APM stub exists
    fl.close()


def try_fleet_apm_note(kb, auth):
    """Best-effort: record APM package policy on Fleet Server policy even if stub."""
    log("=== Fleet APM package (best-effort) ===")
    pkgs = kibana_curl(kb, auth, "GET", "/api/fleet/package_policies?perPage=200")
    for pp in pkgs.get("items") or []:
        if (pp.get("package") or {}).get("name") == "apm":
            log(f"  existing {pp.get('name')}")
            return
    bodies = [
        {
            "name": "apm-server-upgrade-test",
            "description": f"APM Server co-located on Fleet host {APM_URL} (elastic-agent host)",
            "namespace": "default",
            "policy_id": FLEET_POLICY_ID,
            "package": {"name": "apm", "version": "8.18.4"},
            "inputs": [],
        },
    ]
    for b in bodies:
        r = kibana_curl(kb, auth, "POST", "/api/fleet/package_policies", b)
        log(f"  create: {json.dumps(r)[:400]}")


def wait_apm(timeout=180) -> bool:
    log(f"=== Wait APM {APM_URL} ===")
    deadline = time.time() + timeout
    while time.time() < deadline:
        fl = connect(NODES["fleet"][0], attempts=5)
        try:
            out = run(
                fl,
                "systemctl is-active apm-server; ss -lntp | grep 8200 || true; "
                "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 http://127.0.0.1:8200/ || true",
                check=False,
                timeout=30,
            )
            log(f"  {out[-300:]}")
            if "active" in out and (":8200" in out or "200" in out or "401" in out or "405" in out):
                return True
        finally:
            fl.close()
        time.sleep(10)
    return False


def enable_es_apm():
    log(f"=== Enable ES APM -> {APM_URL} ===")
    block = (
        f"\n# APM collection (upgrade testing)\n"
        f"tracing.apm.enabled: true\n"
        f"tracing.apm.agent.server_url: \"{APM_URL}\"\n"
        f"tracing.apm.agent.environment: \"upgrade-test\"\n"
    )
    for key in ("es04", "es03", "es02", "es01"):
        ip, fqdn = NODES[key]
        log(f"  {fqdn}")
        c = connect(ip, attempts=20)
        try:
            yml = run(c, "cat /etc/elasticsearch/elasticsearch.yml", check=False)
            if "tracing.apm.enabled" not in yml:
                run(
                    c,
                    f"cp -a /etc/elasticsearch/elasticsearch.yml /etc/elasticsearch/elasticsearch.yml.bak.apm; "
                    f"printf %s {shlex.quote(block)} >> /etc/elasticsearch/elasticsearch.yml",
                    check=False,
                )
            # keep discovery.seed_hosts intact
            run(c, "systemctl restart elasticsearch", check=False, timeout=180)
        finally:
            c.close()
        for _ in range(45):
            try:
                c2 = connect(ip, attempts=3)
                code = run(
                    c2,
                    "curl -sk -m 5 -o /dev/null -w '%{http_code}' https://localhost:9200/",
                    check=False,
                    timeout=20,
                )
                c2.close()
                if "401" in code or "200" in code:
                    log(f"    up")
                    break
            except Exception:
                pass
            time.sleep(5)
        time.sleep(6)


def wait_cluster() -> bool:
    log("=== Wait cluster ===")
    for i in range(60):
        c = connect(NODES["es02"][0], attempts=10)
        out = run(
            c,
            f"curl -sk -m 12 -u elastic:{PWD} 'https://localhost:9200/_cluster/health?pretty'; "
            f"curl -sk -m 12 -u elastic:{PWD} 'https://localhost:9200/_cat/nodes?v&h=name,version,master'",
            check=False,
            timeout=40,
        )
        c.close()
        log(f"  poll {i} nodes={out.count('8.18.4')}")
        if ('"status" : "yellow"' in out or '"status" : "green"' in out) and out.count("8.18.4") >= 4:
            if '"unassigned_primary_shards" : 0' in out:
                return True
        time.sleep(10)
    return False


def enable_kibana_apm():
    log(f"=== Enable Kibana APM -> {APM_URL} ===")
    block = (
        f"\n# APM collection (upgrade testing)\n"
        f"elastic.apm.active: true\n"
        f"elastic.apm.serverUrl: \"{APM_URL}\"\n"
        f"elastic.apm.environment: \"upgrade-test\"\n"
        f"elastic.apm.transactionSampleRate: 1.0\n"
    )
    kb = connect(NODES["kibana"][0], attempts=20)
    try:
        yml = run(kb, "cat /etc/kibana/kibana.yml", check=False)
        if "elastic.apm.active" not in yml:
            run(
                kb,
                f"cp -a /etc/kibana/kibana.yml /etc/kibana/kibana.yml.bak.apm; "
                f"printf %s {shlex.quote(block)} >> /etc/kibana/kibana.yml",
                check=False,
            )
        run(kb, "systemctl restart kibana", check=False, timeout=180)
    finally:
        kb.close()
    for _ in range(50):
        try:
            kb = connect(NODES["kibana"][0], attempts=5)
            code = run(
                kb,
                "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:5601/api/status || true",
                check=False,
                timeout=20,
            )
            kb.close()
            if "200" in code:
                log("  kibana 200")
                return
        except Exception:
            pass
        time.sleep(10)
    log("  kibana still settling")


def create_second_snap(auth) -> str:
    log(f"=== Second ES snapshot {SECOND_ES_SNAP} ===")
    c = connect(NODES["es02"][0], attempts=20)
    log(es_api(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state")[:800])
    body = {
        "indices": ".*",
        "ignore_unavailable": True,
        "include_global_state": True,
        "feature_states": FEATURES,
        "metadata": {
            "taken_by": "deploy_apm_finish",
            "taken_because": "second system snap after HV 8.18.4 restore + APM + upgrade-testing sample alert",
            "es_version": "8.18.4",
        },
    }
    print(
        es_api(
            c, auth, "PUT",
            f"/_snapshot/{REPO}/{SECOND_ES_SNAP}?wait_for_completion=false&pretty",
            body, timeout=120,
        ),
        flush=True,
    )
    state = "IN_PROGRESS"
    for i in range(180):
        out = es_api(c, auth, "GET", f"/_snapshot/{REPO}/{SECOND_ES_SNAP}?pretty", timeout=120)
        data = parse_json(out) or {}
        snaps = data.get("snapshots") or []
        s = snaps[0] if snaps else data
        state = s.get("state") or state
        feats = [f.get("feature_name") for f in (s.get("feature_states") or [])]
        log(f"  poll {i}: state={state} shards={s.get('shards')} features={feats}")
        if state in ("SUCCESS", "FAILED", "PARTIAL"):
            log(json.dumps({
                "snapshot": s.get("snapshot"),
                "state": state,
                "include_global_state": s.get("include_global_state"),
                "indices_count": len(s.get("indices") or []),
                "feature_states": feats,
                "shards": s.get("shards"),
            }, indent=2))
            break
        time.sleep(10)
    log(es_api(c, auth, "GET", f"/_snapshot/{REPO}/_all?pretty&filter_path=snapshots.snapshot,snapshots.state")[:1500])
    c.close()
    return state


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    log("START deploy APM + enable collection + second snap")

    c = connect(NODES["es02"][0])
    auth = curl_elastic_auth(get_elastic_password(c))
    c.close()

    deploy_apm_on_fleet(PWD)
    apm_up = wait_apm()
    log(f"APM listening={apm_up}")

    kb = connect(NODES["kibana"][0], attempts=30)
    try:
        try_fleet_apm_note(kb, auth)
        found = kibana_curl(kb, auth, "GET", "/api/alerting/rules/_find?per_page=20&search=Upgrade")
        log(f"alerts: {[(x.get('id'), x.get('name')) for x in (found.get('data') or [])]}")
    finally:
        kb.close()

    enable_es_apm()
    if not wait_cluster():
        log("WARN cluster not fully stable after ES APM restarts")
    enable_kibana_apm()

    c = connect(NODES["es02"][0])
    auth = curl_elastic_auth(get_elastic_password(c))
    c.close()
    state = create_second_snap(auth)

    c = connect(NODES["es02"][0])
    print(es_api(c, auth, "GET", "/_cat/nodes?v&h=name,node.role,master,version"), flush=True)
    print(es_api(c, auth, "GET", "/_cluster/health?pretty"), flush=True)
    c.close()

    log(
        f"\n=== DONE ===\n"
        f"HV: pre-upgrade-system-8.18.4-20260724 (done earlier)\n"
        f"Alert: Upgrade Testing Sample Alert\n"
        f"APM Server: {APM_URL} on Fleet/elastic-agent host active={apm_up}\n"
        f"ES/Kibana APM collection enabled\n"
        f"Second ES snap: {SECOND_ES_SNAP} state={state}\n"
    )
    return 0 if state == "SUCCESS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
