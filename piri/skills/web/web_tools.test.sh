#!/usr/bin/env bash
# Hermetic tests for piri/skills/web helpers — stub SearXNG + HTML server,
# no network beyond loopback.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SEARCH="$ROOT/piri/skills/web/web_search.py"
FETCH="$ROOT/piri/skills/web/web_fetch.py"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [ -n "${STUB_PID:-}" ] && kill "$STUB_PID" 2>/dev/null' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

cat > "$TMP/stub.py" <<'PY'
import json, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

class Healthy(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/search"):
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            body = json.dumps({"results": [
                {"title": f"Result for {q}", "url": "https://example.org/a", "content": "snippet text", "engine": "stub"}
            ], "unresponsive_engines": []}).encode()
        else:
            body = b"{}"
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

class Blocked(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b"<html><head><style>x{}</style><script>evil()</script></head>"
                b"<body><h1>Stub Title</h1><p>Hello   readable text</p></body></html>")
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

import os, sys
healthy = HTTPServer(("127.0.0.1", 0), Healthy)
blocked = HTTPServer(("127.0.0.1", 0), Blocked)
page = HTTPServer(("127.0.0.1", 0), Page)
for srv in (healthy, blocked, page):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
with open(os.environ["PORT_FILE"], "w") as fh:
    fh.write(f"{healthy.server_port} {blocked.server_port} {page.server_port}")
threading.Event().wait()
PY

PORT_FILE="$TMP/ports" python3 "$TMP/stub.py" &
STUB_PID=$!
for _ in $(seq 1 50); do [ -s "$TMP/ports" ] && break; sleep 0.1; done
read -r healthy_port blocked_port page_port < "$TMP/ports"

out="$(SEARXNG_URL="http://127.0.0.1:$healthy_port" python3 "$SEARCH" "hello world" --limit 3 2>/dev/null)"
ok "search prints the stub result for the query" 'grep -q "Result for hello world" <<<"$out" && grep -q "https://example.org/a" <<<"$out"'
ok "search marks results as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

out="$(SEARXNG_URL="http://127.0.0.1:$blocked_port,http://127.0.0.1:$healthy_port" python3 "$SEARCH" "fallback" 2>/dev/null)"
ok "search falls through an engine-blocked instance to the next base URL" 'grep -q "Result for fallback" <<<"$out"'

set +e
SEARXNG_URL="http://127.0.0.1:$blocked_port" python3 "$SEARCH" "empty" >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "search treats a solely engine-blocked instance as degraded (69), not as real zero hits" '[ "$rc" = 69 ] && grep -q "engines-unresponsive" "$TMP/err"'

set +e
SEARXNG_URL="http://127.0.0.1:1" python3 "$SEARCH" "down" >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "search exits 69 with a body-free diagnostic when every instance is down" '[ "$rc" = 69 ] && [ "$(wc -c < "$TMP/err")" -lt 200 ]'

out="$(python3 "$FETCH" "http://127.0.0.1:$page_port/page" 2>/dev/null)"
ok "fetch extracts readable text and strips script/style" 'grep -q "Stub Title" <<<"$out" && grep -q "Hello readable text" <<<"$out" && ! grep -q "evil()" <<<"$out" && ! grep -q "x{}" <<<"$out"'
ok "fetch marks page content as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

out="$(python3 "$FETCH" "http://127.0.0.1:$page_port/page" --max-chars 250 2>/dev/null)"
ok "fetch honours the output cap" '[ "$(printf "%s" "$out" | wc -c)" -lt 400 ]'

set +e
python3 "$FETCH" "file:///etc/passwd" >/dev/null 2>"$TMP/err2"; rc=$?
set -e
ok "fetch rejects non-http(s) URLs" '[ "$rc" = 65 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
