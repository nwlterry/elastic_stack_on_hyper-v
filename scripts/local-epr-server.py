#!/usr/bin/env python3
"""Minimal Elastic Package Registry for air-gapped Kibana (bundled zips)."""
import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

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
        "description": "System logs and metrics (stub for deploy).",
        "policy_templates": [{"name": "system", "title": "System", "description": "System"}],
        "categories": ["observability"],
    },
    "elasticsearch": {
        "title": "Elasticsearch",
        "version": "1.12.0",
        "description": "Elasticsearch metrics (stub for deploy).",
        "policy_templates": [],
        "categories": ["observability"],
    },
    "kibana": {
        "title": "Kibana",
        "version": "1.11.0",
        "description": "Kibana metrics (stub for deploy).",
        "policy_templates": [],
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
        "download": f"/epr/{name}/{name}-{ver}.zip",
        "path": f"/package/{name}/{ver}",
        "policy_templates": meta.get("policy_templates", []),
        "conditions": {"kibana": {"version": "^8.12.0 || ^9.0.0"}, "elastic": {"subscription": "basic"}},
        "owner": {"type": "elastic", "github": "elastic/fleet"},
        "categories": meta.get("categories", ["elastic_stack"]),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/health"):
            self._json(200, {"status": "ok"})
            return
        if parsed.path == "/search":
            qs = urllib.parse.parse_qs(parsed.query)
            pkg = (qs.get("package") or [""])[0]
            if pkg in PACKAGES:
                self._json(200, [package_entry(pkg, PACKAGES[pkg])])
            else:
                self._json(200, [])
            return
        if parsed.path.startswith("/epr/"):
            rel = parsed.path[len("/epr/") :]
            fpath = os.path.join(BUNDLED, os.path.basename(rel))
            if os.path.isfile(fpath):
                self._file(200, fpath)
                return
        self.send_error(404)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    httpd = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Local EPR listening on http://127.0.0.1:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()