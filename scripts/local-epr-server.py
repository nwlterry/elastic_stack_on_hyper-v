#!/usr/bin/env python3
"""Minimal Elastic Package Registry for air-gapped Kibana (Python 3.6+)."""
import json
import mimetypes
import os
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

EPR_DIR = os.environ.get("EPR_PACKAGES", "/opt/elastic-setup/epr-packages")
BUNDLED = "/usr/share/kibana/node_modules/@kbn/fleet-plugin/target/bundled_packages"
PORT = int(os.environ.get("EPR_PORT", "8080"))

PACKAGES = {
    "fleet_server": {
        "title": "Fleet Server",
        "version": "1.6.0",
        "description": "Centrally manage Elastic Agents with the Fleet Server integration.",
        "policy_templates": [{"name": "fleet_server", "title": "Fleet Server", "description": "Fleet Server setup"}],
        "categories": ["elastic_stack"],
    },
    "elastic_agent": {
        "title": "Elastic Agent",
        "version": "2.3.0",
        "description": "Collect logs and metrics from Elastic Agents.",
        "policy_templates": [],
        "categories": ["elastic_stack"],
    },
    "system": {
        "title": "System",
        "version": "1.60.0",
        "description": "System logs and metrics.",
        "policy_templates": [
            {"name": "metrics", "title": "Metrics", "description": "System metrics"},
            {"name": "logs", "title": "Logs", "description": "System logs"},
        ],
        "categories": ["observability"],
    },
    "elasticsearch": {
        "title": "Elasticsearch",
        "version": "1.12.0",
        "description": "Elasticsearch metrics.",
        "policy_templates": [{"name": "elasticsearch", "title": "Elasticsearch", "description": "Elasticsearch"}],
        "categories": ["observability"],
    },
    "kibana": {
        "title": "Kibana",
        "version": "2.3.1",
        "description": "Kibana logs and metrics.",
        "policy_templates": [{"name": "kibana", "title": "Kibana", "description": "Kibana"}],
        "categories": ["observability"],
    },
}


def package_entry(name, meta):
    ver = meta["version"]
    return {
        "name": name,
        "title": meta["title"],
        "version": ver,
        "release": "ga",
        "description": meta["description"],
        "type": "integration",
        "download": "/epr/{}/{}-{}.zip".format(name, name, ver),
        "path": "/package/{}/{}".format(name, ver),
        "policy_templates": meta.get("policy_templates", []),
        "conditions": {"kibana": {"version": "^8.12.0 || ^9.0.0"}, "elastic": {"subscription": "basic"}},
        "owner": {"type": "elastic", "github": "elastic/fleet"},
        "categories": meta.get("categories", ["elastic_stack"]),
    }


def resolve_zip(rel_path):
    base = os.path.basename(rel_path)
    for directory in (EPR_DIR, BUNDLED):
        if not directory:
            continue
        fpath = os.path.join(directory, base)
        if os.path.isfile(fpath):
            return fpath
    return None


def resolve_package_zip(name, version):
    for directory in (EPR_DIR, BUNDLED):
        fpath = os.path.join(directory, "{}-{}.zip".format(name, version))
        if os.path.isfile(fpath):
            return fpath
    return None


def read_from_zip(zip_path, name, version, inner_path):
    prefix = "{}-{}/".format(name, version)
    wanted = prefix + inner_path.lstrip("/")
    with zipfile.ZipFile(zip_path, "r") as zf:
        if wanted in zf.namelist():
            return zf.read(wanted)
        for entry in zf.namelist():
            if entry == prefix + "manifest.yml":
                return zf.read(entry)
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("{} - {}".format(self.address_string(), fmt % args), flush=True)

    def do_HEAD(self):
        if self._dispatch(self.path, head_only=True):
            return
        self.send_error(404)

    def do_GET(self):
        if self._dispatch(self.path, head_only=False):
            return
        self.send_error(404)

    def _dispatch(self, path, head_only=False):
        parsed = urllib.parse.urlparse(path)
        if parsed.path in ("/", "/health"):
            if not head_only:
                self._json(200, {"status": "ok"})
            else:
                self.send_response(200)
                self.end_headers()
            return True
        if parsed.path == "/search":
            if not head_only:
                qs = urllib.parse.parse_qs(parsed.query)
                pkg = (qs.get("package") or [""])[0]
                if pkg in PACKAGES:
                    self._json(200, [package_entry(pkg, PACKAGES[pkg])])
                else:
                    self._json(200, [])
            else:
                self.send_response(200)
                self.end_headers()
            return True
        if parsed.path.startswith("/package/"):
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 3:
                name, version = parts[1], parts[2]
                zpath = resolve_package_zip(name, version)
                if not zpath:
                    return False
                # Registry index: /package/{name}/{version}/ -> JSON (not YAML)
                if len(parts) == 3:
                    if not head_only:
                        entry = package_entry(name, PACKAGES.get(name, {"version": version, "title": name, "description": name}))
                        prefix = "{}-{}/".format(name, version)
                        with zipfile.ZipFile(zpath, "r") as zf:
                            entry["assets"] = ["/package/{}/{}/{}".format(name, version, n[len(prefix):]) for n in zf.namelist() if n.startswith(prefix) and not n.endswith("/")]
                            entry["format_version"] = "3.0.2"
                        self._json(200, entry)
                    else:
                        self.send_response(200)
                        self.end_headers()
                    return True
                rel = "/".join(parts[3:])
                data = read_from_zip(zpath, name, version, rel)
                if data is not None:
                    ctype = "application/octet-stream"
                    if rel.endswith(".yml") or rel.endswith(".yaml"):
                        ctype = "application/x-yaml"
                    elif rel.endswith(".json"):
                        ctype = "application/json"
                    elif rel.endswith(".svg"):
                        ctype = "image/svg+xml"
                    elif rel.endswith(".png"):
                        ctype = "image/png"
                    if not head_only:
                        self._bytes(200, data, ctype)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                    return True
        if parsed.path.startswith("/epr/") or parsed.path.startswith("/download/"):
            if "/epr/" in parsed.path:
                rel = parsed.path.split("/epr/", 1)[-1]
            else:
                rel = parsed.path.split("/download/", 1)[-1]
            fpath = resolve_zip(rel)
            if fpath:
                if head_only:
                    size = os.path.getsize(fpath)
                    self.send_response(200)
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                else:
                    self._file(200, fpath)
                return True
        return False

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, code, fpath):
        ctype = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
        with open(fpath, "rb") as fh:
            data = fh.read()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    os.makedirs(EPR_DIR, exist_ok=True)
    httpd = HTTPServer(("127.0.0.1", PORT), Handler)
    print("Local EPR listening on http://127.0.0.1:{} (packages={})".format(PORT, EPR_DIR), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()