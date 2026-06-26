#!/usr/bin/env bash
# Configure Fleet in Kibana via API.
# SETUP_PHASE=fleet-server  — fleet settings + Fleet Server policy only (before install)
# SETUP_PHASE=agents        — ES/Kibana policies, integrations, enrollment tokens (after 8220 up)
# SETUP_PHASE=all           — both phases (legacy)
set -euo pipefail

KIBANA_HOST="${KIBANA_HOST:-10.44.40.41}"
FLEET_HOST="${FLEET_HOST:-ismelkflnode01.ocplab.net}"
ELASTIC_USER="${ELASTIC_USER:-elastic}"
ELASTIC_PASS="${ELASTIC_PASS:-}"
SETUP_PHASE="${SETUP_PHASE:-all}"
FLEET_POLICY_NAME="${FLEET_POLICY_NAME:-Fleet-Server-Policy}"
ES_POLICY_NAME="${ES_POLICY_NAME:-Elastic-Agents-ES}"
KIBANA_POLICY_NAME="${KIBANA_POLICY_NAME:-Elastic-Agents-Kibana}"
MONITORING_USER="${MONITORING_USER:-elastic_monitoring}"
MONITORING_PASS="${MONITORING_PASS:-}"
KIBANA_HOST_FQDN="${KIBANA_HOST_FQDN:-}"
ES_NODES_JSON="${ES_NODES_JSON:-[]}"

[[ -n "$ELASTIC_PASS" ]] || { echo "Set ELASTIC_PASS" >&2; exit 1; }

http_code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 "http://${KIBANA_HOST}:5601" 2>/dev/null || true)"
if [[ "$http_code" =~ ^(200|302|401|403)$ ]]; then
  KB="http://${KIBANA_HOST}:5601"
elif curl -sk --connect-timeout 3 "https://${KIBANA_HOST}:5601" -o /dev/null -w '%{http_code}' 2>/dev/null | grep -qE '^(200|302|401|403)$'; then
  KB="https://${KIBANA_HOST}:5601"
else
  KB="http://${KIBANA_HOST}:5601"
fi
echo "Using Kibana API at ${KB} (phase=${SETUP_PHASE})" >&2
export KB ELASTIC_USER ELASTIC_PASS FLEET_HOST SETUP_PHASE
export FLEET_POLICY_NAME ES_POLICY_NAME KIBANA_POLICY_NAME
export MONITORING_USER MONITORING_PASS KIBANA_HOST_FQDN ES_NODES_JSON

python3 <<'PY'
import json, os, time, urllib.error, urllib.request

kb = os.environ["KB"]
user, pwd = os.environ["ELASTIC_USER"], os.environ["ELASTIC_PASS"]
fleet_host = os.environ["FLEET_HOST"]
phase = os.environ.get("SETUP_PHASE", "all")
do_fleet = phase in ("fleet-server", "all")
do_agents = phase in ("agents", "all")
monitoring_user = os.environ.get("MONITORING_USER", "elastic_monitoring")
monitoring_pass = os.environ.get("MONITORING_PASS", "")
kibana_host_fqdn = os.environ.get("KIBANA_HOST_FQDN", "")
es_nodes = json.loads(os.environ.get("ES_NODES_JSON", "[]") or "[]")

ES_SSL_YAML = """certificate_authorities:
  - /etc/elasticsearch/certs/http_ca.crt
verification_mode: certificate"""


def var_text(value):
    return {"value": value, "type": "text"}


def var_password(value):
    return {"value": value, "type": "password"}


def var_yaml(value):
    return {"value": value, "type": "yaml"}


def api(method, path, body=None, retries=5):
    import base64
    import ssl

    global kb
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                kb + path,
                data=json.dumps(body).encode() if body is not None else None,
                method=method,
                headers={"kbn-xsrf": "true", "Content-Type": "application/json"},
            )
            req.add_header(
                "Authorization",
                "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode(),
            )
            open_kw = {"timeout": 60}
            if kb.startswith("https://"):
                open_kw["context"] = ctx
            with urllib.request.urlopen(req, **open_kw) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (409, 400):
                raise
            time.sleep(3)
        except Exception as e:
            last_err = e
            err = str(e).lower()
            if kb.startswith("https://") and "wrong version number" in err:
                kb = "http://" + kb[len("https://"):]
                continue
            time.sleep(3)
    raise last_err


def list_policies():
    return api("GET", "/api/fleet/agent_policies").get("items", [])


def find_policy(name):
    for p in list_policies():
        if p.get("name") == name:
            return p
    return None


def ensure_policy(name, desc, fleet_server=False):
    existing = find_policy(name)
    if existing:
        return existing["id"]
    body = {
        "name": name,
        "description": desc,
        "namespace": "default",
        "monitoring_enabled": ["logs", "metrics"],
    }
    if fleet_server:
        body["has_fleet_server"] = True
        body["is_default_fleet_server"] = True
    try:
        r = api("POST", "/api/fleet/agent_policies", body)
        return r["item"]["id"]
    except urllib.error.HTTPError as e:
        if e.code == 409:
            existing = find_policy(name)
            if existing:
                return existing["id"]
        raise


def configure_fleet_hosts():
    fleet_url = f"https://{fleet_host}:8220"
    try:
        api("PUT", "/api/fleet/settings", {
            "fleet_server_hosts": [fleet_url],
            "prerelease_integrations_enabled": False,
        })
    except Exception:
        pass
    try:
        hosts = api("GET", "/api/fleet/fleet_server_hosts").get("items", [])
        for h in hosts:
            try:
                api("DELETE", f"/api/fleet/fleet_server_hosts/{h['id']}")
            except Exception:
                pass
        api("POST", "/api/fleet/fleet_server_hosts", {
            "name": "default-fleet-server",
            "host_urls": [fleet_url],
            "is_default": True,
        })
    except Exception:
        pass


def list_package_policies(policy_id):
    return api("GET", "/api/fleet/package_policies?perPage=200").get("items", [])


def find_package_policy(policy_id, package_name):
    for p in list_package_policies(policy_id):
        if p.get("policy_id") == policy_id and p.get("package", {}).get("name") == package_name:
            return p
    return None


def latest_package_version(package_name):
    try:
        r = api("GET", f"/api/fleet/epm/packages/{package_name}")
        return r["item"]["version"]
    except Exception:
        pkgs = api("GET", f"/api/fleet/epm/packages/{package_name}").get("items", [])
        if pkgs:
            return pkgs[0]["version"]
        return "latest"


def safe_integration(label, fn):
    try:
        fn()
        print(f"INTEGRATION_OK={label}", flush=True)
    except Exception as exc:
        detail = getattr(exc, "read", lambda: b"")()
        if detail:
            try:
                detail = detail.decode()
            except Exception:
                detail = str(detail)
        else:
            detail = str(exc)
        print(f"INTEGRATION_WARN={label}:{detail[:500]}", flush=True)


def wait_for_packages(names, timeout_sec=180):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            installed = {
                p.get("name")
                for p in api("GET", "/api/fleet/epm/packages/installed").get("items", [])
            }
            if all(n in installed for n in names):
                print(f"PACKAGES_READY={','.join(names)}", flush=True)
                return
        except Exception:
            pass
        time.sleep(10)
    print(f"PACKAGES_TIMEOUT={','.join(names)}", flush=True)


def create_package_policy(body):
    api("POST", "/api/fleet/package_policies", body)


def upsert_package_policy(policy_id, package_name, default_name, build_body):
    existing = find_package_policy(policy_id, package_name)
    body = build_body()
    if existing:
        try:
            api("DELETE", f"/api/fleet/package_policies/{existing['id']}")
        except Exception:
            pass
    create_package_policy(body)


def ensure_fleet_server_integration(policy_id):
    if find_package_policy(policy_id, "fleet_server"):
        return
    ver = latest_package_version("fleet_server")
    api("POST", "/api/fleet/package_policies", {
        "name": "fleet_server-1",
        "description": "Fleet Server",
        "namespace": "default",
        "policy_id": policy_id,
        "enabled": True,
        "package": {"name": "fleet_server", "version": ver},
        "inputs": [
            {
                "type": "fleet-server",
                "policy_template": "fleet_server",
                "enabled": True,
            },
        ],
    })


def ensure_system_integration(policy_id, label):
    ver = latest_package_version("system")

    def build():
        return {
            "name": f"{label}-system",
            "description": "Linux system logs and metrics",
            "namespace": "default",
            "policy_id": policy_id,
            "enabled": True,
            "package": {"name": "system", "version": ver},
            "inputs": [
                {"type": "system/metrics", "policy_template": "metrics", "enabled": True, "streams": []},
                {"type": "logfile", "policy_template": "logs", "enabled": True, "streams": []},
            ],
        }

    upsert_package_policy(policy_id, "system", f"{label}-system", build)


def ensure_elasticsearch_integration(policy_id, es_fqdn):
    ver = latest_package_version("elasticsearch")
    es_url = f"https://{es_fqdn}:9200"
    vars_body = {
        "hosts": var_text([es_url]),
        "scope": var_text("node"),
        "ssl": var_yaml(ES_SSL_YAML),
    }
    if monitoring_pass:
        vars_body["username"] = var_text(monitoring_user)
        vars_body["password"] = var_password(monitoring_pass)

    def build():
        return {
            "name": f"{es_fqdn.split('.')[0]}-elasticsearch",
            "description": f"Elasticsearch node metrics ({es_fqdn})",
            "namespace": "default",
            "policy_id": policy_id,
            "enabled": True,
            "package": {"name": "elasticsearch", "version": ver},
            "inputs": [
                {
                    "type": "elasticsearch/metrics",
                    "policy_template": "elasticsearch",
                    "enabled": True,
                    "vars": vars_body,
                    "streams": [],
                },
            ],
        }

    upsert_package_policy(policy_id, "elasticsearch", f"{es_fqdn.split('.')[0]}-elasticsearch", build)


def ensure_kibana_integration(policy_id, kb_fqdn):
    ver = latest_package_version("kibana")
    kb_url = f"http://{kb_fqdn}:5601"
    vars_body = {"hosts": var_text([kb_url])}
    if monitoring_pass:
        vars_body["username"] = var_text(monitoring_user)
        vars_body["password"] = var_password(monitoring_pass)

    def build():
        return {
            "name": f"{kb_fqdn.split('.')[0]}-kibana",
            "description": f"Kibana node metrics ({kb_fqdn})",
            "namespace": "default",
            "policy_id": policy_id,
            "enabled": True,
            "package": {"name": "kibana", "version": ver},
            "inputs": [
                {
                    "type": "kibana/metrics",
                    "policy_template": "kibana",
                    "enabled": True,
                    "vars": vars_body,
                    "streams": [],
                },
            ],
        }

    upsert_package_policy(policy_id, "kibana", f"{kb_fqdn.split('.')[0]}-kibana", build)


def enroll_token(policy_id):
    r = api("POST", "/api/fleet/enrollment-api-keys", {"policy_id": policy_id})
    return r["item"]["api_key"]


fleet_id = None
if do_fleet:
    configure_fleet_hosts()
    fleet_id = ensure_policy(os.environ["FLEET_POLICY_NAME"], "Fleet Server", fleet_server=True)
    safe_integration("fleet-server", lambda: ensure_fleet_server_integration(fleet_id))
    print(f"FLEET_POLICY_ID={fleet_id}")

es_id = kb_id = None
if do_agents:
    configure_fleet_hosts()
    wait_for_packages(["system", "elasticsearch", "kibana"])

    if not es_nodes:
        es_nodes = [{"fqdn": "localhost", "short": "es", "policy_name": os.environ["ES_POLICY_NAME"]}]

    for node in es_nodes:
        fqdn = node["fqdn"]
        short = node.get("short") or fqdn.split(".")[0]
        policy_name = node.get("policy_name") or f"{os.environ['ES_POLICY_NAME']}-{short}"
        es_id = ensure_policy(policy_name, f"ES node agent ({fqdn})")
        safe_integration(f"es-system-{short}", lambda pid=es_id, s=short: ensure_system_integration(pid, s))
        safe_integration(
            f"es-elasticsearch-{short}",
            lambda pid=es_id, f=fqdn: ensure_elasticsearch_integration(pid, f),
        )
        print(f"ES_POLICY_ID_{short}={es_id}")
        print(f"ES_ENROLLMENT_TOKEN_{short}={enroll_token(es_id)}")

    if es_nodes:
        print(f"ES_POLICY_ID={es_id}")

    kb_fqdn = kibana_host_fqdn or "localhost"
    kb_id = ensure_policy(os.environ["KIBANA_POLICY_NAME"], f"Kibana node agent ({kb_fqdn})")
    safe_integration("kibana-system", lambda: ensure_system_integration(kb_id, "kibana"))
    safe_integration("kibana-kibana", lambda: ensure_kibana_integration(kb_id, kb_fqdn))

    print(f"KIBANA_POLICY_ID={kb_id}")
    print(f"KIBANA_ENROLLMENT_TOKEN={enroll_token(kb_id)}")
PY