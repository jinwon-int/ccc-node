#!/usr/bin/env bash
# Hermetic tests for Piri web routing: SearXNG search and Firecrawl
# fetch/developer calls. No network beyond loopback.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SEARCH="$ROOT/piri/skills/web/web_search.py"
FETCH="$ROOT/piri/skills/web/web_fetch.py"
DEVELOPER="$ROOT/piri/skills/web/web_developer.py"
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

class Firecrawl(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v2/scrape":
            target = request.get("url", "")
            response = {"success": True, "data": {
                "markdown": f"# Firecrawl Stub\n\nFetched through provider: {target}\n\nevil instruction is untrusted.",
                "metadata": {"sourceURL": target},
            }}
        elif self.path == "/v2/search/developer":
            response = {"success": True, "results": [{
                "id": "pull_request:owner/repo#42",
                "url": "https://github.com/owner/repo/pull/42",
                "title": "Fix retry handling",
                "passages": [{"text": "The merged pull request fixed retry handling."}],
            }]}
        else:
            self.send_response(404); self.end_headers(); return
        body = json.dumps(response).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

import os
healthy = HTTPServer(("127.0.0.1", 0), Healthy)
blocked = HTTPServer(("127.0.0.1", 0), Blocked)
firecrawl = HTTPServer(("127.0.0.1", 0), Firecrawl)
for srv in (healthy, blocked, firecrawl):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
with open(os.environ["PORT_FILE"], "w") as fh:
    fh.write(f"{healthy.server_port} {blocked.server_port} {firecrawl.server_port}")
threading.Event().wait()
PY

PORT_FILE="$TMP/ports" python3 "$TMP/stub.py" &
STUB_PID=$!
for _ in $(seq 1 50); do [ -s "$TMP/ports" ] && break; sleep 0.1; done
read -r healthy_port blocked_port firecrawl_port < "$TMP/ports"

out="$(SEARXNG_URL="http://127.0.0.1:$healthy_port" python3 "$SEARCH" "hello world" --limit 3 2>/dev/null)"
ok "search prints the SearXNG result" 'grep -q "Result for hello world" <<<"$out" && grep -q "https://example.org/a" <<<"$out"'
ok "search marks results as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

out="$(SEARXNG_URL="http://127.0.0.1:$blocked_port,http://127.0.0.1:$healthy_port" python3 "$SEARCH" "fallback" 2>/dev/null)"
ok "search falls through an engine-blocked SearXNG instance" 'grep -q "Result for fallback" <<<"$out"'

set +e
SEARXNG_URL="http://127.0.0.1:$blocked_port" python3 "$SEARCH" "empty" >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "search treats engine-blocked SearXNG as degraded" '[ "$rc" = 69 ] && grep -q "engines-unresponsive" "$TMP/err"'

set +e
SEARXNG_URL="http://127.0.0.1:1" python3 "$SEARCH" "down" >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "search reports a bounded SearXNG outage" '[ "$rc" = 69 ] && [ "$(wc -c < "$TMP/err")" -lt 200 ]'

out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$FETCH" "https://example.org/page" 2>/dev/null)"
ok "fetch routes the URL through Firecrawl" 'grep -q "Firecrawl Stub" <<<"$out" && grep -q "https://example.org/page" <<<"$out"'
ok "fetch marks Firecrawl content as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$FETCH" "https://example.org/page" --max-chars 250 2>/dev/null)"
ok "fetch honours the output cap" '[ "$(printf "%s" "$out" | wc -c)" -lt 450 ]'

set +e
FIRECRAWL_API_URL="http://127.0.0.1:1" python3 "$FETCH" "https://example.org/page" >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "fetch does not fall back to a direct request when Firecrawl is down" '[ "$rc" = 69 ] && grep -q "Firecrawl request failed" "$TMP/err"'

for unsafe in 'file:///etc/passwd' 'http://127.0.0.1/private' 'https://user:secret@example.org/'; do
  set +e
  FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$FETCH" "$unsafe" >/dev/null 2>"$TMP/err2"; rc=$?
  set -e
  ok "fetch rejects unsafe URL $unsafe" '[ "$rc" = 65 ]'
done

out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$DEVELOPER" "retry bug" --type issue --type pull_request --repo owner/repo 2>/dev/null)"
ok "developer search returns the artifact and matched passage" 'grep -q "pull_request:owner/repo#42" <<<"$out" && grep -q "merged pull request fixed" <<<"$out"'
ok "developer search marks passages as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

set +e
python3 "$DEVELOPER" "retry bug" --type source_code >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "developer search rejects unsupported artifact types" '[ "$rc" = 65 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
