#!/usr/bin/env python3
"""Local dev server + API proxy for the EQ Auction Forge webapp.

WHY: in the browser, calling tlp-auctions.com directly is blocked by CORS (the
API doesn't yet send Access-Control-Allow-Origin for our origin). This little
server lets you test price-checking locally WITHOUT bugging the API owner:

  * It serves the repo ROOT over http://localhost:8000, so the app at /web/ can
    still fetch /items.txt.gz one level up.
  * It proxies /api/* to https://tlp-auctions.com/api/* *server-side*. Server-to-
    server requests don't involve CORS at all, and the browser talks to this
    server same-origin -- so CORS never enters the picture.

This is a DEV CRUTCH ONLY. Production is meant to be fully static: once the API
sends CORS headers, the webapp calls it directly and this proxy is not needed.

Run:   python web/dev-proxy.py
Open:  http://localhost:8000/web/index.html   (or /web/probe.html)
Stop:  Ctrl+C
"""
import functools
import http.server
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

# Port: CLI arg wins, then $PORT, else 8000.  e.g.  python web/dev-proxy.py 8899
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
TARGET = "https://tlp-auctions.com"          # endpoints already include /api
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
_ctx = ssl.create_default_context()          # cert is valid -> verify properly


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static file server (rooted at the repo) that intercepts /api/* and
    forwards it upstream, tacking on permissive CORS headers."""

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")

    def do_OPTIONS(self):
        # CORS preflight — answer it so cross-origin callers work too.
        if self.path.startswith("/api/"):
            self.send_response(204)
            self._send_cors()
            self.end_headers()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy("GET")
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._proxy("POST")
        else:
            self.send_error(405, "POST only supported for /api/*")

    def _proxy(self, method):
        url = TARGET + self.path                       # /api/... -> tlp-auctions.com/api/...
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        req.add_header("Accept", "application/json")
        # Mirror the desktop app's _API_HEADERS UA so test traffic is identifiable
        # to the API owner. (A browser can't set this itself — the real webapp will
        # be recognized by Origin instead, since UA is a forbidden fetch header.)
        req.add_header("User-Agent",
                       "EQ-Auction_Forge/1.4.5 (+https://github.com/wangel/EQ_Auction_Forge)")
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
                data, status = r.read(), r.status
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:           # forward upstream 4xx/5xx as-is
            data, status = e.read(), e.code
            ctype = e.headers.get("Content-Type", "application/json")
        except Exception as e:                         # network/TLS failure
            data = json.dumps({"proxyError": str(e)}).encode()
            status, ctype = 502, "application/json"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(data)
        print(f"  [proxy] {method} {self.path} -> {status}")


if __name__ == "__main__":
    handler = functools.partial(Handler, directory=ROOT)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    print(f"Serving {ROOT}")
    print(f"  App:   http://localhost:{PORT}/web/index.html")
    print(f"  Probe: http://localhost:{PORT}/web/probe.html")
    print(f"  /api/* -> {TARGET}   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
