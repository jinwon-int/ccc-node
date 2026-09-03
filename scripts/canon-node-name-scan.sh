#!/usr/bin/env bash
# Canon node-name scan — the deployment canon (skills/ and codex/skills/) is
# the public, fleet-universal jurisdiction (#1446): fleet node identifiers must
# not appear there. GitHub account names (jinon86, seoseo-ai, including the
# gh-seoseo-ai config token) are public identities and allowed. Fail-closed:
# exits 1 and lists every offending file:line when a node name is found.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: canon-node-name-scan.sh [--root REPO_ROOT]

Scans REPO_ROOT/skills and REPO_ROOT/codex/skills for fleet node names.
Exit codes: 0 clean, 1 violations found, 2 usage/path error.
EOF
}

root="$(pwd)"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) root="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[ -d "$root/skills" ] && [ -d "$root/codex/skills" ] || {
  echo "canon-node-name-scan: expected $root/skills and $root/codex/skills" >&2
  exit 2
}

# Account-derived tokens are stripped before matching so that allowed
# identities (seoseo-ai, gh-seoseo-ai) neither trigger nor mask findings:
# a line carrying both "seoseo-ai" and a bare "seoseo" is still flagged on
# the bare occurrence.
#
# grep -n output is "path:lineno:content". The match is applied to the
# CONTENT ONLY: the path prefix (which can itself contain a node name —
# e.g. a node named home directory) must neither trigger nor mask findings.
# awk is used because the content pattern needs word-ish boundaries without
# \b (not portable across awk variants); matching is line-level detection.
filter_hits() {
  PAT="$pattern" awk '{
    c1 = index($0, ":");
    rest = substr($0, c1 + 1);
    c2 = index(rest, ":");
    content = tolower(substr($0, c1 + c2 + 1));
    gsub(/seoseo-ai/, "", content);
    if (content ~ ENVIRON["PAT"]) print $0;
  }'
}

pattern='(^|[^a-z0-9_])(seoseo|gwakga|jingun|dungae|yukson|nosuk|soonwook|gongmyoung|gongyung|sogyo|bangtong|daegyo|vps[0-9]+|racknerd[a-z0-9-]+)([^a-z0-9_]|$)'

hits="$(grep -rIniE "$pattern" "$root/skills" "$root/codex/skills" 2>/dev/null \
  | filter_hits || true)"

if [ -n "$hits" ]; then
  {
    echo "canon-node-name-scan: fleet node names found in the canon skill sets:"
    printf '%s\n' "$hits"
    echo "The deployment canon must be public-safe and fleet-universal (#1446)."
    echo "Generalize node identifiers to roles (relay node, broker host, node-a)."
  } >&2
  exit 1
fi
echo "canon-node-name-scan: ok — no fleet node names in the canon skill sets"
