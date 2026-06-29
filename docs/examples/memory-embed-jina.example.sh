#!/usr/bin/env sh
# Example Jina provider for the memory semantic-retrieval lane.
#
# Contract (CCC_MEMORY_EMBED_CMD): read text on stdin, print ONE embedding as a
# JSON float array on stdout. ccc-node ships NO provider and NO key — this is a
# non-secret template. Wire it per node:
#
#   cp docs/examples/memory-embed-jina.example.sh ~/.claude/bin/ccc-embed-jina.sh
#   chmod +x ~/.claude/bin/ccc-embed-jina.sh
#   install -m 700 -d ~/.claude/secrets
#   printf '%s\n' "JINA_API_KEY='...redacted...'" > ~/.claude/secrets/memory-embed.env
#   chmod 600 ~/.claude/secrets/memory-embed.env
#   export CCC_MEMORY_EMBED_CMD="$HOME/.claude/bin/ccc-embed-jina.sh"
#   export CCC_MEMORY_EMBED_MODEL=jina-embeddings-v3
#
# The key is sourced from a node-local 0600 secret file by default so settings
# files can contain only non-secret env. Set CCC_MEMORY_EMBED_SECRET_FILE to use
# another location.
#
# Doc vectors are precomputed during the background memory refresh (network is
# allowed there); only the query is embedded at search time, with a tight timeout
# and fail-open, so SessionStart stays no-network when this is unset.
set -eu

secret_file="${CCC_MEMORY_EMBED_SECRET_FILE:-$HOME/.claude/secrets/memory-embed.env}"
if [ ! -r "$secret_file" ]; then
  echo "missing embedding secret file: $secret_file" >&2
  exit 2
fi

# shellcheck disable=SC1090
. "$secret_file"
: "${JINA_API_KEY:?set JINA_API_KEY in $secret_file}"

text="$(cat)"
model="${CCC_MEMORY_EMBED_MODEL:-jina-embeddings-v3}"

# Use Python stdlib HTTPS rather than curl so this example is robust on nodes
# where curl may be wrapped/stubbed by a test harness. Print only the vector.
JINA_API_KEY="$JINA_API_KEY" JINA_EMBED_MODEL="$model" JINA_EMBED_TEXT="$text" python3 - <<'PY'
import json
import os
import sys
import urllib.request

key = os.environ["JINA_API_KEY"]
model = os.environ.get("JINA_EMBED_MODEL") or "jina-embeddings-v3"
text = os.environ.get("JINA_EMBED_TEXT") or ""
timeout = float(os.environ.get("CCC_MEMORY_EMBED_TIMEOUT", "20") or 20)

payload = json.dumps({"model": model, "input": text}).encode("utf-8")
req = urllib.request.Request(
    "https://api.jina.ai/v1/embeddings",
    data=payload,
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "curl/8.5.0",
    },
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    print(f"jina embedding request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)

vec = data.get("data", [{}])[0].get("embedding")
if not isinstance(vec, list):
    print("jina embedding response missing data[0].embedding", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps(vec, separators=(",", ":")))
PY
