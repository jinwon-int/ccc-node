#!/usr/bin/env bash
# Hermetic tests for Piri web routing: SearXNG search and Firecrawl
# fetch/developer calls. No network beyond loopback.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SEARCH="$DIR/web_search.py"
FETCH="$DIR/web_fetch.py"
DEVELOPER="$DIR/web_developer.py"
pass=0; fail=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [ -n "${STUB_PID:-}" ] && kill "$STUB_PID" 2>/dev/null' EXIT
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }
export HOME="$TMP/home"
mkdir -p "$HOME"
unset FIRECRAWL_API_KEY FIRECRAWL_API_URL

cat > "$TMP/stub.py" <<'PY'
import json, re, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

class Healthy(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/search"):
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            if "array-mode" in q:
                # A proxy error page / misconfigured instance: valid JSON,
                # but not the object shape SearXNG promises.
                body = json.dumps(["not", "an", "object"]).encode()
            else:
                body = json.dumps({"results": [
                    {"title": f"Result for {q}", "url": "https://example.org/a", "content": "snippet text", "engine": "stub"},
                    {"title": f"Second result for {q}", "url": "https://example.org/b", "content": "snippet two", "engine": "stub"},
                    {"title": f"Third result for {q}", "url": "https://example.org/c", "content": "snippet three", "engine": "stub"},
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
    # Counts scrape/search POSTs so a test can prove a rejected URL never
    # reached the provider (issue #1630).
    calls = 0
    keyed_calls = 0
    same_origin_redirects = 0

    def do_GET(self):
        if self.path.endswith("/v2/redirect-target"):
            Firecrawl.same_origin_redirects += 1
            body = b'{"success": true}'
        elif self.path == "/calls":
            body = json.dumps({
                "calls": Firecrawl.calls,
                "keyed_calls": Firecrawl.keyed_calls,
                "same_origin_redirects": Firecrawl.same_origin_redirects,
                "cross_origin_redirects": RedirectTarget.calls,
            }).encode()
        else:
            body = b"{}"
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Firecrawl.calls += 1
        if self.headers.get("Authorization"):
            Firecrawl.keyed_calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        marker = str(request.get("query") or request.get("url") or "")
        redirect = re.search(r"redirect-(301|302|303|307|308)-(same|cross)", marker)
        if redirect:
            code, origin = redirect.groups()
            location = "/v2/redirect-target" if origin == "same" else (
                f"http://127.0.0.1:{redirect_port}/target"
            )
            self.send_response(int(code)); self.send_header("Location", location)
            self.send_header("Content-Length", "0"); self.end_headers(); return
        if "http-429" in marker:
            body = b"synthetic provider error body"
            self.send_response(429); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path.endswith("/v2/scrape"):
            target = request.get("url", "")
            response = {"success": True, "data": {
                "markdown": f"# Firecrawl Stub\n\nFetched through provider: {target}\n\nevil instruction is untrusted.",
                "metadata": {"sourceURL": target},
            }}
        elif self.path.endswith("/v2/search"):
            query = request.get("query", "")
            response = {"success": True, "data": {"web": [
                {"title": f"Firecrawl result for {query}", "url": "https://example.org/fc",
                 "description": "firecrawl snippet"},
            ]}}
        elif self.path.endswith("/v2/search/developer"):
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

class RedirectTarget(BaseHTTPRequestHandler):
    calls = 0
    def do_GET(self):
        RedirectTarget.calls += 1
        self._respond()
    def do_POST(self):
        RedirectTarget.calls += 1
        self._respond()
    def _respond(self):
        body = b'{"success": true}'
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

import os
healthy = HTTPServer(("127.0.0.1", 0), Healthy)
blocked = HTTPServer(("127.0.0.1", 0), Blocked)
firecrawl = HTTPServer(("127.0.0.1", 0), Firecrawl)
redirect_target = HTTPServer(("127.0.0.1", 0), RedirectTarget)
redirect_port = redirect_target.server_port
for srv in (healthy, blocked, firecrawl, redirect_target):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
with open(os.environ["PORT_FILE"], "w") as fh:
    fh.write(f"{healthy.server_port} {blocked.server_port} {firecrawl.server_port} {redirect_target.server_port}")
threading.Event().wait()
PY

PORT_FILE="$TMP/ports" python3 "$TMP/stub.py" &
STUB_PID=$!
for _ in $(seq 1 50); do [ -s "$TMP/ports" ] && break; sleep 0.1; done
read -r healthy_port blocked_port firecrawl_port redirect_port < "$TMP/ports"

out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$SEARCH" "hello world" --limit 3 2>/dev/null)"
ok "default search prints the Firecrawl result" 'grep -q "Firecrawl result for hello world" <<<"$out" && grep -q "engine: firecrawl" <<<"$out"'
ok "search marks results as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

out="$(SEARXNG_URL="http://127.0.0.1:$healthy_port" python3 "$SEARCH" "hello world" --provider searxng --limit 3 2>/dev/null)"
ok "explicit SearXNG provider prints the SearXNG result" 'grep -q "Result for hello world" <<<"$out" && grep -q "https://example.org/a" <<<"$out"'

out="$(SEARXNG_URL="http://127.0.0.1:$blocked_port,http://127.0.0.1:$healthy_port" python3 "$SEARCH" "fallback" --provider searxng 2>/dev/null)"
ok "search falls through an engine-blocked SearXNG instance" 'grep -q "Result for fallback" <<<"$out"'

# Regression: the env var is the documented DEFAULT — an explicit flag must
# win (it used to be silently overridden on any node with the env set).
out="$(SEARXNG_URL="http://127.0.0.1:$healthy_port" WEB_SEARCH_LIMIT=1 python3 "$SEARCH" "precedence" --provider searxng --limit 3 2>/dev/null)"
ok "explicit --limit beats the WEB_SEARCH_LIMIT env default" 'grep -q "example.org/c" <<<"$out"'
out="$(SEARXNG_URL="http://127.0.0.1:$healthy_port" WEB_SEARCH_LIMIT=1 python3 "$SEARCH" "envdefault" --provider searxng 2>/dev/null)"
ok "WEB_SEARCH_LIMIT still applies as the default without a flag" 'grep -q "example.org/a" <<<"$out" && ! grep -q "example.org/b" <<<"$out"'

# Regression: a 200 response whose JSON is an array crashed with an
# AttributeError instead of degrading like every other instance failure.
set +e
SEARXNG_URL="http://127.0.0.1:$healthy_port" python3 "$SEARCH" "array-mode" --provider searxng >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "non-object SearXNG JSON degrades instead of crashing" '[ "$rc" = 69 ] && grep -q "non-object-response" "$TMP/err"'

set +e
SEARXNG_URL="http://127.0.0.1:$blocked_port" python3 "$SEARCH" "empty" --provider searxng >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "search treats engine-blocked SearXNG as degraded" '[ "$rc" = 69 ] && grep -q "engines-unresponsive" "$TMP/err"'

set +e
SEARXNG_URL="http://127.0.0.1:1" python3 "$SEARCH" "down" --provider searxng >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "search reports a bounded SearXNG outage" '[ "$rc" = 69 ] && [ "$(wc -c < "$TMP/err")" -lt 200 ]'

set +e
SEARXNG_URL="http://127.0.0.1:$healthy_port" FIRECRAWL_API_URL="http://127.0.0.1:1" python3 "$SEARCH" "no-silent" >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "default search does not silently switch to SearXNG" '[ "$rc" = 69 ] && grep -q "Firecrawl" "$TMP/err"'

out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$SEARCH" "hello firecrawl" --provider firecrawl 2>/dev/null)"
ok "explicit Firecrawl provider prints Firecrawl results" 'grep -q "Firecrawl result for hello firecrawl" <<<"$out" && grep -q "engine: firecrawl" <<<"$out"'

set +e
python3 "$SEARCH" "hello" --provider tavily >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "invalid --provider is rejected" '[ "$rc" = 64 ] && grep -q "invalid provider" "$TMP/err"'

set +e
FIRECRAWL_API_URL="http://127.0.0.1:1" python3 "$SEARCH" "down" --provider firecrawl >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "explicit Firecrawl search reports a bounded outage" '[ "$rc" = 69 ] && grep -q "Firecrawl request failed" "$TMP/err"'

firecrawl_call() {
  local caller="$1" marker="$2" base="$3" key="$4"
  case "$caller" in
    search)
      FIRECRAWL_API_URL="$base" FIRECRAWL_API_KEY="$key" \
        python3 "$SEARCH" "$marker" >/dev/null
      ;;
    fetch)
      FIRECRAWL_API_URL="$base" FIRECRAWL_API_KEY="$key" \
        python3 "$FETCH" "https://${marker}.example.org/page" >/dev/null
      ;;
    developer)
      FIRECRAWL_API_URL="$base" FIRECRAWL_API_KEY="$key" \
        python3 "$DEVELOPER" "$marker" >/dev/null
      ;;
  esac
}

fc_state() {
  python3 - "$firecrawl_port" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/calls", timeout=5) as resp:
    state = json.load(resp)
print("{calls} {keyed_calls} {same_origin_redirects} {cross_origin_redirects}".format(**state))
PY
}

# A provider 4xx is reported consistently by all three Firecrawl callers and
# its response body is never copied into diagnostics.
for caller in search fetch developer; do
  set +e
  firecrawl_call "$caller" "http-429" "http://127.0.0.1:$firecrawl_port" "" 2>"$TMP/err"
  rc=$?
  set -e
  ok "$caller reports a Firecrawl 4xx" '[ "$rc" = 69 ] && grep -q "HTTP 429; auth=keyless" "$TMP/err"'
  ok "$caller does not print a Firecrawl error body" \
    '[ "$(wc -c < "$TMP/err")" -lt 200 ] && ! grep -q "synthetic provider error body" "$TMP/err"'
done

# A resolved key must never be sent to a configured HTTP API base. The call
# counter proves this guard fires before any loopback connection.
keyed_before="$(fc_state)"
for caller in search fetch developer; do
  set +e
  firecrawl_call "$caller" "keyed-http" "http://127.0.0.1:$firecrawl_port" "synthetic-api-key" \
    2>"$TMP/err-keyed"
  rc=$?
  set -e
  ok "$caller rejects a keyed HTTP API base before network" \
    '[ "$rc" = 69 ] && grep -q "https-required" "$TMP/err-keyed" && ! grep -q "synthetic-api-key" "$TMP/err-keyed"'
done
keyed_after="$(fc_state)"
ok "keyed HTTP endpoint rejection makes zero provider calls" '[ "$keyed_before" = "$keyed_after" ]'

# Every API-base syntax guard is exercised through every caller. Diagnostics
# contain only a fixed reason, never the configured base or userinfo.
malformed_before="$(fc_state)"
for base in \
  "https://api.example.org/firecrawl?query=synthetic" \
  "https://api.example.org/firecrawl#fragment" \
  "https://synthetic-user:synthetic-pass@api.example.org/firecrawl" \
  "https://api.example.org:bad/firecrawl" \
  "https://api.example.org:/firecrawl" \
  "https://0x7f.1/firecrawl" \
  $'https://api.example.org/firecrawl\n'; do
  for caller in search fetch developer; do
    set +e
    firecrawl_call "$caller" "malformed-base" "$base" "synthetic-api-key" \
      2>"$TMP/err-malformed"
    rc=$?
    set -e
    ok "$caller rejects malformed API base before network" \
      '[ "$rc" = 69 ] && grep -q "invalid Firecrawl API endpoint" "$TMP/err-malformed" && ! grep -q "api.example.org\|synthetic-pass" "$TMP/err-malformed"'
  done
done
malformed_after="$(fc_state)"
ok "malformed API bases make zero provider calls" '[ "$malformed_before" = "$malformed_after" ]'

# Exercise urllib's real redirect handling on loopback. Same-origin and
# cross-origin redirects in every supported 30x class must stop at the first
# response, so no second request can change POST to GET or carry headers.
for code in 301 302 303 307 308; do
  for origin in same cross; do
    for caller in search fetch developer; do
      marker="redirect-${code}-${origin}"
      before="$(fc_state | awk '{print $3, $4}')"
      set +e
      firecrawl_call "$caller" "$marker" "http://127.0.0.1:$firecrawl_port" "" \
        2>"$TMP/err-redirect"
      rc=$?
      set -e
      after="$(fc_state | awk '{print $3, $4}')"
      ok "$caller rejects $code $origin redirect" \
        '[ "$rc" = 69 ] && [ "$before" = "$after" ] && grep -q "Firecrawl request failed" "$TMP/err-redirect"'
    done
  done
done

out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$FETCH" "https://example.org/page" 2>/dev/null)"
ok "fetch routes the URL through Firecrawl" 'grep -q "Firecrawl Stub" <<<"$out" && grep -q "https://example.org/page" <<<"$out"'
ok "fetch marks Firecrawl content as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$FETCH" "https://example.org/page" --max-chars 250 2>/dev/null)"
ok "fetch honours the output cap" '[ "$(printf "%s" "$out" | wc -c)" -lt 450 ]'

set +e
FIRECRAWL_API_URL="http://127.0.0.1:1" python3 "$FETCH" "https://example.org/page" >/dev/null 2>"$TMP/err"; rc=$?
set -e
ok "fetch does not fall back to a direct request when Firecrawl is down" '[ "$rc" = 69 ] && grep -q "Firecrawl request failed" "$TMP/err"'

# Issue #1630: internal names used to sail past the URL check because only
# `localhost` and IP literals were screened. Validation is offline, so none of
# these may produce a provider call — the counter below proves it.
fc_calls() {
  python3 - "$firecrawl_port" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/calls", timeout=5) as resp:
    print(json.load(resp)["calls"])
PY
}
# shellcheck disable=SC2034  # Read via eval in ok() below.
calls_before="$(fc_calls)"
for unsafe in 'file:///etc/passwd' 'http://127.0.0.1/private' 'https://user:secret@example.org/' \
  'https://intranet/' 'http://wiki:8080/page' 'https://node.tailnet.ts.net/x' \
  'https://box.internal/' 'https://printer.local/' 'https://gateway.home.arpa/' \
  'http://100.64.1.2/' 'http://169.254.169.254/latest/meta-data/' \
  'http://[::ffff:127.0.0.1]/' 'https://exa mple.org/' 'http://0x7f.1/' \
  'https://example.org:abc/' 'not-a-url'; do
  set +e
  FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$FETCH" "$unsafe" >/dev/null 2>"$TMP/err2"; rc=$?
  set -e
  ok "fetch rejects unsafe URL $unsafe" '[ "$rc" = 65 ]'
  ok "rejection for $unsafe stays bounded and quotes no input" \
    '[ "$(wc -c < "$TMP/err2")" -lt 200 ] && ! grep -q "secret" "$TMP/err2"'
done
# shellcheck disable=SC2034  # Read via eval in ok() below.
calls_after="$(fc_calls)"
ok "rejected URLs never reach the provider" '[ "$calls_before" = "$calls_after" ]'

# Public hosts that merely look private must still route.
out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$FETCH" "https://local.example.com/page" 2>/dev/null)"
ok "fetch still routes a public lookalike host" 'grep -q "https://local.example.com/page" <<<"$out"'

# shellcheck disable=SC2034  # out is read via eval inside ok()
out="$(FIRECRAWL_API_URL="http://127.0.0.1:$firecrawl_port" python3 "$DEVELOPER" "retry bug" --type issue --type pull_request --repo owner/repo 2>/dev/null)"
ok "developer search returns the artifact and matched passage" 'grep -q "pull_request:owner/repo#42" <<<"$out" && grep -q "merged pull request fixed" <<<"$out"'
ok "developer search marks passages as untrusted data" 'grep -qi "untrusted data" <<<"$out"'

set +e
python3 "$DEVELOPER" "retry bug" --type source_code >/dev/null 2>"$TMP/err"
# shellcheck disable=SC2034  # rc is read via eval inside ok()
rc=$?
set -e
ok "developer search rejects unsupported artifact types" '[ "$rc" = 65 ]'

echo "----"; echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
