#!/usr/bin/env sh
# Example provider for the memory semantic-retrieval lane.
#
# Contract (CCC_MEMORY_EMBED_CMD): read text on stdin, print ONE embedding as a
# JSON float array on stdout. ccc-node ships NO provider and NO key — this is a
# non-secret template. Wire it per node:
#
#   cp docs/examples/memory-embed-openai.example.sh ~/.claude/bin/ccc-embed.sh
#   chmod +x ~/.claude/bin/ccc-embed.sh
#   export OPENAI_API_KEY=...                       # node-local secret, never committed
#   export CCC_MEMORY_EMBED_CMD="$HOME/.claude/bin/ccc-embed.sh"
#   export CCC_MEMORY_EMBED_MODEL=text-embedding-3-small
#
# Doc vectors are precomputed during the background memory refresh (network is
# allowed there); only the query is embedded at search time, with a tight
# timeout and fail-open, so SessionStart stays no-network when this is unset.
# Swap the curl block for Voyage / Cohere / a local server to use another
# provider — only the stdin->JSON-array contract matters.
set -eu
text="$(cat)"
model="${CCC_MEMORY_EMBED_MODEL:-text-embedding-3-small}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY in the node environment}"

curl -sS --fail https://api.openai.com/v1/embeddings \
  -H "Authorization: Bearer ${OPENAI_API_KEY}" \
  -H "Content-Type: application/json" \
  --data "$(jq -n --arg in "$text" --arg model "$model" '{input:$in, model:$model}')" \
  | jq -c '.data[0].embedding'
