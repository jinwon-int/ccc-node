#!/usr/bin/env bash
# Tests for the standard rescreen rotation generator (#2028).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="$ROOT/scripts/rescreen-rotation.py"
PROMOTER="$ROOT/scripts/ccc-skill-promotion.py"
# shellcheck source=claude/hooks/lib/test-stub.sh
. "$ROOT/claude/hooks/lib/test-stub.sh"
ccc_test_reset_hook_env
pass=0
fail=0
TMP="$(ccc_test_tmpdir)" || exit 1
trap 'rm -rf "$TMP"' EXIT

ok() {
  if eval "$2"; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    echo "FAIL: $1"
  fi
}

HEAD_OK="$(printf 'a%.0s' $(seq 1 40))"
BIN="$TMP/bin"
mkdir -p "$BIN" "$TMP/home/.claude/state/skill-promotion"
chmod 700 "$BIN" "$TMP/home" "$TMP/home/.claude" "$TMP/home/.claude/state" "$TMP/home/.claude/state/skill-promotion"

# Broker stub: primary has author + two keyring reviewers online; one of them
# (failnode) carries a recent task.failed audit event. The remote broker has
# one eligible reviewer with a recorded provider (via implementationCapability).
cat > "$BIN/curl" <<STUB
#!/usr/bin/env bash
set -eu
url=""
for argument in "\$@"; do case "\$argument" in http*) url="\$argument";; esac; done
case "\$url" in
  */health) printf '{"brokerId":"primary-broker"}' ;;
  */workers*)
    printf '{"items":[{"nodeId":"authorx","status":"online","implementationCapability":{"providerId":"anthropic","modelTier":"claude-sonnet-5"}},
      {"nodeId":"alpharev","status":"online","implementationCapability":{"providerId":"anthropic","modelTier":"claude-sonnet-5"}},
      {"nodeId":"betarev","status":"online","implementationCapability":{"providerId":"xai","modelTier":"grok-4.6"}},
      {"nodeId":"failnode","status":"online"}]}'
    ;;
  */audit*) printf '{"items":[{"actorId":"failnode","action":"task.failed","createdAt":"%s"}]}' "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" ;;
  *) printf '{}\n' ;;
esac
STUB
chmod +x "$BIN/curl"

cat > "$BIN/ssh" <<'STUB'
#!/usr/bin/env bash
set -eu
case " $* " in
  *"a2a-dispatch-round.mjs"*) printf '{"results":[{"taskId":"remote-rescreen-task"}],"ok":true}' ;;
  *"/health"*) printf '{"brokerId":"t2-broker"}' ;;
  *"/workers"*) printf '{"items":[{"nodeId":"t2rev","status":"online","implementationCapability":{"providerId":"openai","modelTier":"gpt-5.6-sol"}}]}' ;;
  *"/audit"*) printf '{"items":[]}' ;;
  *) exit 8 ;;
esac
STUB
chmod +x "$BIN/ssh"

cat > "$BIN/scp" <<'STUB'
#!/usr/bin/env bash
set -eu
exit 0
STUB
chmod +x "$BIN/scp"

cat > "$BIN/node" <<'STUB'
#!/usr/bin/env bash
set -eu
printf '{"results":[{"taskId":"rescreen-task-%s","classification":"queued"}]}' "$RANDOM"
STUB
chmod +x "$BIN/node"

# cases: three candidates. case order = alphabetical name sort.
jq -n --arg head "$HEAD_OK" '
{
  "skill-a": {node:"authorx", provider:"claude", branch:"b-a", pr:1, skill_sha256:"s1",
    tree_sha256:"t1", files:[{path:"SKILL.md", content_b64:("I3Rlc3Q=")}]},
  "skill-b": {node:"authorx", provider:"claude", branch:"b-b", pr:2, skill_sha256:"s2",
    tree_sha256:"t2", files:[{path:"SKILL.md", content_b64:("I3Rlc3Q=")}]},
  "skill-c": {node:"alpharev", provider:"claude", branch:"b-c", pr:3, skill_sha256:"s3",
    tree_sha256:"t3", files:[{path:"SKILL.md", content_b64:("I3Rlc3Q=")}]}
}' > "$TMP/cases.json"

cat > "$TMP/keyring.json" <<'JSON'
{"keys":{"worker:alpharev:g2:v1":{},"worker:betarev:g2:v1":{},"worker:t2rev:g2:v1":{},"worker:failnode:g2:v1":{}}}
JSON

KR_B64="$(base64 -w0 "$TMP/keyring.json")"
HEAD40="$(printf 'a%.0s' $(seq 1 40))"
cat > "$BIN/gh" <<STUB
#!/usr/bin/env bash
set -eu
has_jq=0
for argument in "\$@"; do
  [ "\$argument" = "--jq" ] && has_jq=1
  case "\$argument" in
    *a2a-public-keyring*)
      printf '{"content":"%s"}' "$KR_B64"; exit 0 ;;
  esac
done
case " \$* " in
  *"/commits"*|*"git/trees"*)
    if [ "\$has_jq" = 1 ]; then printf '%s' "$HEAD40"; else printf '{"sha":"%s"}' "$HEAD40"; fi
    exit 0 ;;
esac
exit 1
STUB
chmod +x "$BIN/gh"

run_tool() { # $1 extra args...; outputs summary JSON
  env "${BASE_ENV[@]}" PATH="$BIN:$PATH" A2A_EDGE_SECRET=test-secret TMP="$TMP" HEAD_OK="$HEAD_OK" \
    CCC_SKILL_PROMOTION_REMOTE_BROKERS='[{"name":"t2","ssh_host":"t2stub","broker_url":"http://127.0.0.1:8787","nexus_dir":"/n","secret_cmd":"echo remote-secret"}]' \
    python3 "$TOOL" --cases "$TMP/cases.json" "$@"
}

export BASE_ENV=(
  "HOME=$TMP/home"
  "CCC_CLAUDE_DIR=$TMP/home/.claude"
  "CCC_STATE_DIR=$TMP/home/.claude/state"
  "CCC_SKILL_PROMOTION_REPO=test/repo"
  "CCC_NODE=testnode"
  "CCC_SKILL_PROMOTION_A2A_NEXUS_DIR=$TMP/nexus"
)
mkdir -p "$TMP/nexus/scripts" "$TMP/nexus/docs"
printf '#!/usr/bin/env bash\n' > "$TMP/nexus/scripts/a2a-dispatch-round.mjs"; chmod +x "$TMP/nexus/scripts/a2a-dispatch-round.mjs"
{ printf '## Worker procedure\n'; printf 'Apply the rubric step. %.0s' $(seq 1 20); printf '\n## Receipt projection\n'; } > "$TMP/nexus/docs/skills-intake-review.md"

# Determinism: two dry-runs over the same state produce identical assignment.
out1="$(run_tool --dry-run --names "skill-a,skill-b,skill-c,skill-orphan")"; rc1=$?
out2="$(run_tool --dry-run --names "skill-a,skill-b,skill-c,skill-orphan")"; rc2=$?
ok "dry-run succeeds and is deterministic (#2028)" \
  '[ "$rc1" = 0 ] && [ "$rc2" = 0 ] && [ "$(jq -c .results <<<"$out1")" = "$(jq -c .results <<<"$out2")" ]'
ok "pool excludes the recent-failure node with a recorded reason" \
  '[ "$(jq -r ".exclusions[] | select(.node==\"failnode\") | .reason" <<<"$out1" | grep -c "task.failed")" != "0" ]'
ok "pool records provider/model per reviewer (#2028)" \
  '[ "$(jq -r ".pool[] | select(.node==\"betarev\") | .provider" <<<"$out1")" = "xai" ]'

# Author exclusion: skill-a author (authorx) must not review its own skill,
# and the recent-failure node is never chosen.
ok "author node never reviews its own candidate; failed node never chosen (#2028)" \
  '[ "$(jq -r ".results[] | select(.name==\"skill-a\") | .reviewer" <<<"$out1")" != "authorx" ] \
     && [ "$(jq -r ".results[] | .reviewer" <<<"$out1")" != "failnode" ]'
ok "remote-broker reviewer is used and recorded with broker+provider (#2028)" \
  '[ "$(jq -r "[.results[] | select(.reviewer==\"t2rev\") | .broker] | length" <<<"$out1")" != "0" ] \
     && [ "$(jq -r ".results[] | select(.reviewer==\"t2rev\") | .review_provider" <<<"$out1")" = "openai" ]'
ok "no-case candidate is skipped with a reason" \
  '[ "$(jq -r ".results[] | select(.name==\"skill-orphan\") | .reason" <<<"$out1")" = "no-case" ]'

# Dispatch path: non-dry-run produces tasks via the stubbed dispatcher.
out3="$(run_tool)"; rc3=$?
ok "dispatch mode assigns tasks to every candidate (#2028)" \
  '[ "$rc3" = 0 ] && [ "$(jq -r "[.results[] | select(.task != null)] | length" <<<"$out3")" = 3 ]'
ok "remote dispatch records the broker name (#2028)" \
  '[ "$(jq -r ".results[] | select(.reviewer==\"t2rev\") | .broker" <<<"$out3")" = "t2" ]'

echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
