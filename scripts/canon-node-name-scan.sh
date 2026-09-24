#!/usr/bin/env bash
# Canon node-name scan — the deployment canon (skills/ and codex/skills/) is
# the public, fleet-universal jurisdiction (#1446): fleet node identifiers must
# not appear there. GitHub account names (jinon86, seoseo-ai, including the
# gh-seoseo-ai config token) are public identities and allowed. Fail-closed:
# exits 1 and lists every offending file:line when a node name is found.
#
# --repo-wide (#1451 P4): the same pattern over EVERY tracked file, ratcheted
# against scripts/canon-node-name-baseline.txt (one "<hits> <path>" line per
# file that still carries node names). The canon rule is zero; the rest of
# the repo converges to zero one PR at a time:
#   - a tracked file NOT in the baseline that gains a node name  -> fail (new)
#   - a baseline file whose hit count went UP                     -> fail (grew)
#   - a baseline file whose hit count went DOWN (or to zero)      -> fail until
#     the baseline is regenerated, so the progress is locked in   (shrank)
# `--update-baseline` regenerates the file from the current tree; it is the
# only sanctioned way to edit it. Allowed outside the scan: CHANGELOG.md and
# git history (mitigation, not rewrite — #1451), the hashed CI requirement
# pins, and the baseline file itself.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: canon-node-name-scan.sh [--root REPO_ROOT]
       canon-node-name-scan.sh --repo-wide [--root REPO_ROOT] [--baseline FILE] [--update-baseline]

Default: scans REPO_ROOT/skills and REPO_ROOT/codex/skills for fleet node
names (must be zero). --repo-wide: scans every git-tracked file under
REPO_ROOT and compares per-file hit counts against the baseline
(default REPO_ROOT/scripts/canon-node-name-baseline.txt); any difference
fails. --update-baseline rewrites the baseline from the current tree.
Exit codes: 0 clean, 1 violations found, 2 usage/path error.
EOF
}

root="$(pwd)"
mode=canon
baseline=""
update=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) root="${2:-}"; shift 2 ;;
    --repo-wide) mode=repo; shift ;;
    --baseline) baseline="${2:-}"; shift 2 ;;
    --update-baseline) update=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

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

# ---------------------------------------------------------------- canon mode
if [ "$mode" = canon ]; then
  [ -d "$root/skills" ] && [ -d "$root/codex/skills" ] || {
    echo "canon-node-name-scan: expected $root/skills and $root/codex/skills" >&2
    exit 2
  }
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
  exit 0
fi

# ------------------------------------------------------------ repo-wide mode
[ -n "$baseline" ] || baseline="$root/scripts/canon-node-name-baseline.txt"
git -C "$root" rev-parse --show-toplevel >/dev/null 2>&1 || {
  echo "canon-node-name-scan: --repo-wide needs a git checkout at $root" >&2
  exit 2
}
case "$baseline" in
  /*) baseline_rel="${baseline#"$root"/}" ;;
  *) baseline_rel="$baseline"; baseline="$root/$baseline" ;;
esac

# Per-file hit counts over the tracked tree, "<hits> <path>" sorted by path.
# Paths come out of grep relative to $root because grep runs there.
current="$(
  cd "$root" && git ls-files -z \
    | grep -zvE "^((.*/)?CHANGELOG\.md|\.github/requirements/.*|${baseline_rel})$" \
    | xargs -0 grep -IniE "$pattern" -- 2>/dev/null \
    | filter_hits \
    | awk '{ c1 = index($0, ":"); p = substr($0, 1, c1 - 1); n[p]++ }
           END { for (p in n) printf "%d %s\n", n[p], p }' \
    | LC_ALL=C sort -k2 || true
)"

if [ "$update" -eq 1 ]; then
  {
    echo "# canon-node-name-scan --repo-wide baseline (#1451 P4). Generated —"
    echo "# regenerate with: bash scripts/canon-node-name-scan.sh --repo-wide --update-baseline"
    echo "# One '<hits> <path>' per tracked file still carrying fleet node names."
    echo "# Ratchet: new files and higher counts fail CI; lower counts fail until"
    echo "# this file is regenerated, so progress is locked in. Never hand-edit."
    [ -n "$current" ] && printf '%s\n' "$current"
  } > "$baseline"
  files=$(printf '%s\n' "$current" | grep -c . || true)
  total=$(printf '%s\n' "$current" | awk '{ s += $1 } END { print s + 0 }')
  echo "canon-node-name-scan: baseline written — $files files, $total hits ($baseline_rel)"
  exit 0
fi

[ -f "$baseline" ] || {
  echo "canon-node-name-scan: baseline not found: $baseline (run --update-baseline)" >&2
  exit 2
}
expected="$(grep -vE '^\s*(#|$)' "$baseline" | LC_ALL=C sort -k2 || true)"

report="$(
  awk '
    FNR == NR { base[$2] = $1; next }
    { cur[$2] = $1 }
    END {
      for (p in cur) {
        if (!(p in base))        printf "NEW    %4d  %s\n", cur[p], p
        else if (cur[p] > base[p]) printf "GREW   %4d -> %4d  %s\n", base[p], cur[p], p
        else if (cur[p] < base[p]) printf "SHRANK %4d -> %4d  %s\n", base[p], cur[p], p
      }
      for (p in base) if (!(p in cur)) printf "SHRANK %4d -> %4d  %s\n", base[p], 0, p
    }' <(printf '%s\n' "$expected") <(printf '%s\n' "$current") | LC_ALL=C sort
)"

if [ -n "$report" ]; then
  {
    echo "canon-node-name-scan (repo-wide): tracked tree differs from $baseline_rel:"
    printf '%s\n' "$report"
    if printf '%s\n' "$report" | grep -qE '^(NEW|GREW)'; then
      echo "NEW/GREW: fleet node names may not spread (#1451). Generalize to roles"
      echo "or read them from the node-local topology config; do not extend the baseline."
    fi
    if printf '%s\n' "$report" | grep -q '^SHRANK'; then
      echo "SHRANK: lock the progress in — rerun with --update-baseline and commit the file."
    fi
  } >&2
  exit 1
fi
files=$(printf '%s\n' "$current" | grep -c . || true)
total=$(printf '%s\n' "$current" | awk '{ s += $1 } END { print s + 0 }')
echo "canon-node-name-scan (repo-wide): ok — matches baseline ($files files, $total hits)"
