#!/usr/bin/env python3
"""Local dev server + API proxy for the EQ Auction Forge webapp.

WHY: tlp-auctions now sends CORS headers for the PRODUCTION origin
(https://wangel.github.io), so the deployed app on GitHub Pages calls the API
directly -- no proxy. But localhost is NOT a whitelisted origin, so a direct
call from a local dev server would still be CORS-blocked. This little server
lets you test price-checking locally WITHOUT bugging the API owner:

  * It serves docs/ (the GitHub Pages root) over http://localhost:8000, so the
    app at /app/ can fetch /items.txt.gz one level up -- exactly like prod.
  * It proxies /api/* to https://tlp-auctions.com/api/* *server-side*. Server-to-
    server requests don't involve CORS at all, and the browser talks to this
    server same-origin -- so CORS never enters the picture. (Tick "Use local
    proxy" in the app; the checkbox is hidden on Pages.)

This is a DEV CRUTCH ONLY -- production is fully static and proxy-free.

Run:   python docs/app/dev-proxy.py
Open:  http://localhost:8000/app/index.html   (or /app/probe.html)
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

# Port: CLI arg wins, then $PORT, else 8000.  e.g.  python docs/app/dev-proxy.py 8899
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8000"))
TARGET = "https://tlp-auctions.com"          # endpoints already include /api
# Serve docs/ (two levels up from this file) -- the GitHub Pages root, so /app
# and /items.txt.gz resolve in dev exactly as they do in production.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ctx = ssl.create_default_context()          # cert is valid -> verify properly


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static file server (rooted at the repo) that intercepts /api/* and
    forwards it upstream, tacking on permissive CORS headers."""

    def end_headers(self):
        # DEV ONLY: force the browser to revalidate every asset each load, so you
        # never test a stale app.js/index.html/style.css from the HTTP cache.
        # "no-cache" = store but revalidate -> unchanged returns a cheap 304,
        # changed returns 200 with the new file. (Static hosting/Pages is
        # unaffected; this proxy is dev-only.)
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, X-Client-App")

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
        # Forward the app's identifying header upstream so dev test traffic shows
        # up to the API owner exactly like the real webapp will.
        client = self.headers.get("X-Client-App")
        if client:
            req.add_header("X-Client-App", client)
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
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    print(f"Serving {ROOT}")
    print(f"  App:   http://localhost:{PORT}/app/index.html")
    print(f"  Probe: http://localhost:{PORT}/app/probe.html")
    print(f"  /api/* -> {TARGET}   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
