#!/usr/bin/env python3
"""Serve Elastic Agent upgrade artifacts (artifacts.elastic.co layout) for air-gap Fleet."""
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.environ.get("AGENT_ARTIFACT_ROOT", "/opt/elastic-artifacts/downloads")
HOST = os.environ.get("AGENT_ARTIFACT_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGENT_ARTIFACT_PORT", "8081"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("{} - {}".format(self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        rel = parsed.path.lstrip("/")
        if rel.startswith("downloads/"):
            rel = rel[len("downloads/") :]
        fpath = os.path.join(ROOT, rel)
        if not os.path.isfile(fpath):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
        with open(fpath, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    os.makedirs(ROOT, exist_ok=True)
    httpd = HTTPServer((HOST, PORT), Handler)
    print("Agent artifacts at http://{}:{}/downloads/ (root={})".format(HOST, PORT, ROOT), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()