#!/usr/bin/env bash
# PreToolUse guard — fail-closed enforcement of the "Fresh Approval Required" boundary.
#
# Reads the PreToolUse hook payload on stdin ({tool_name, tool_input:{command|file_path}}).
# Exit 0 = allow; exit 2 = deny (the harness aborts the tool call and shows stderr to Claude).
#
# Policy: separation of approval from execution. Gated actions are DENIED by default and
# require an explicit operator approval signal — CCC_ALLOW_GATED=1 set in the HARNESS
# process environment (the env this hook inherits), only after the operator has approved
# the specific action (bypass-by-operator). An agent cannot self-approve: env assignments
# inside the Bash tool command (e.g. `CCC_ALLOW_GATED=1 cmd`) never reach this hook,
# which runs before the command in its own environment.
#
# Risk-profile model (see RISK-PROFILES.md):
#   autonomous              — not matched here; proceeds silently.
#   operator_notify         — proceeds; captured by the PostToolUse audit log (audit.sh).
#   operator_approval_gated — DENIED until CCC_ALLOW_GATED=1 (operator approves the action).
#   operator_review_gated   — DENIED; history/published-state change needing review evidence too.
# guard.sh enforces the two *gated* profiles (deny). The other two are non-blocking.
#
# Secret policy (2026-07-06): local file reads (.env, credentials) are NOT gated — the node
# operator already has full shell access, so reading locally carries no marginal risk.
# Only EXTERNAL exfil (curl/wget/scp sending secret files to remote endpoints) stays gated.
#
# Design notes:
#   - No `set -e`: grep "no match" returns 1 and must not abort the script.
#   - Fail-OPEN only if jq/stdin is unavailable (jq is a harness dependency); everything else
#     fails CLOSED. Patterns favor precision to avoid blocking normal git/gh/npm/file work.
set -uo pipefail

input="$(cat 2>/dev/null)"
[ -n "$input" ] || exit 0

tool="$(printf '%s' "$input"  | jq -r '.tool_name // empty'          2>/dev/null)"
cmd="$(printf '%s' "$input"   | jq -r '.tool_input.command // empty' 2>/dev/null)"
fpath="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"

# deny <label> <profile> <detail>
deny() {
  local label="$1" profile="$2" detail="$3"
  # Observability: record the denial (risk label + profile + tool only — never the raw
  # command, which may carry secrets) so blocked gated actions surface as approval-needed.
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
  local approval_log="${CCC_APPROVAL_LOG:-${HOME:-/root}/.claude/state/approval-needed.log}"
  mkdir -p "$(dirname "$approval_log")" 2>/dev/null
  printf '%s\tDENY[%s]\tprofile=%s\ttool=%s\n' "$ts" "$label" "$profile" "${tool:-?}" >> "$approval_log" 2>/dev/null
  echo "BLOCKED by ccc-node guard [$label] (profile=$profile): ${detail}" >&2
  echo "→ Fresh Approval Required (CLAUDE.md). NOTE: CCC_ALLOW_GATED=1 only works when set in the HARNESS process environment by the operator (e.g. relaunch the session with it, or the operator runs the approved command in their own shell). A CCC_ALLOW_GATED=1 prefix inside an agent Bash command has NO effect — this hook runs first, in its own environment. Agents: prefer a non-gated alternative path, or ask the operator to execute." >&2
  exit 2
}

# --- Operator escape hatch: explicit, audited approval signal ---
if [ "${CCC_ALLOW_GATED:-0}" = "1" ]; then
  echo "ccc-node guard: CCC_ALLOW_GATED=1 set — gated action allowed by operator (audit: tool=$tool)." >&2
  exit 0
fi

# --- Self-update operator config: agents may READ but never write -------------
# ~/.claude/self-update.services / .repo define which services the pre-approved
# ccc-self-update.sh procedure may restart (and where the repo lives). They are
# the blast-radius boundary of that carve-out, so only the operator edits them.
case "$tool" in
  Edit|Write|NotebookEdit|MultiEdit)
    case "$fpath" in
      */self-update.services|*/self-update.repo)
        deny "self-update-config" "operator_approval_gated" "$tool on $fpath" ;;
    esac
    ;;
esac

# --- Bash command-content patterns ---
[ "$tool" = "Bash" ] || exit 0
[ -n "$cmd" ] || exit 0
c="$cmd"

g() { grep -Eq "$1" <<<"$c"; }   # case-sensitive
gi() { grep -Eiq "$1" <<<"$c"; } # case-insensitive

# Quote-stripped view: dangerous tokens must not be hidden behind quotes, e.g.
# `rm -rf "/root"` or `cat ".env"`. Matching the de-quoted form only ever ADDS
# denials (never removes them), so it cannot loosen the guard.
cn="${c//\"/}"; cn="${cn//\'/}"
gn() { grep -Eq "$1" <<<"$cn"; }    # case-sensitive, quote-stripped

# Public-key carve-out: `.pub.pem` is a PUBLIC key (safe to read), but the secret
# patterns below match any `.pem`. Neutralize `.pub.pem` tokens in a separate view
# so a public key alone is allowed, while any real secret in the SAME command still
# trips the deny (e.g. `cat a.pub.pem b.pem` → b.pem still matches).
cnp="${cn//.pub.pem/ }"
gnp() { grep -Eq "$1" <<<"$cnp"; }  # like gn, but with public keys neutralized

# force push / history rewrite
#
# Force-push is review-gated by default, BUT auto-allowed (operator-approved
# relaxation) for an *explicit single* force-push to a NON-protected feature
# branch. Protected branches (main/master/develop/release*/…), ambiguous or
# bare targets (no explicit dst, HEAD, current branch), multiple refspecs, and
# compound/chained commands ALL stay DENIED — fail-closed when uncertain.
# 0 = the command's git subcommand is `push`, tolerating global options that sit
# between `git` and the subcommand (e.g. `git -C <dir> push`, `git -c k=v push`).
# The old regex required `git` and `push` to be adjacent, so `git -C x push
# --force origin main` slipped past the review gate entirely.
cmd_is_git_push() {
  # Parse the quote-stripped view so a quoted subcommand/flag can't hide the push.
  local toks; read -ra toks <<<"$cn"
  local n=${#toks[@]} i=0
  while [ "$i" -lt "$n" ] && [ "${toks[$i]}" != "git" ]; do i=$((i+1)); done
  [ "$i" -lt "$n" ] || return 1
  i=$((i+1))
  while [ "$i" -lt "$n" ]; do
    case "${toks[$i]}" in
      -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix) i=$((i+2)) ;;  # opt + value
      push) return 0 ;;
      -*) i=$((i+1)) ;;        # value-less global flag
      *) return 1 ;;           # first non-option subcommand isn't push
    esac
  done
  return 1
}

is_forcepush() {
  cmd_is_git_push || return 1
  # Match against the quote-stripped view (gn/cn) so `git push "--force" …` and
  # `git push origin "+main"` can't slip the gate behind quotes. The short-flag
  # pattern allows `f` anywhere in a bundled cluster (`-fv`, `-vf`, `-fu`), not
  # only as the last letter.
  gn '([[:space:]]-[a-zA-Z]*f[a-zA-Z]*\b|--force-with-lease|--force([[:space:]=]|$))' && return 0
  gn '[[:space:]]\+[A-Za-z0-9_./-]+:'                                        && return 0  # +src:dst
  gn '[[:space:]]\+[A-Za-z0-9_./-]+([[:space:]]|$)'                          && return 0  # +branch
  return 1
}

# 0 = safe (single explicit force-push to a clear non-protected feature branch).
forcepush_to_feature_branch() {
  # Never safe if the command chains/embeds anything else.
  case "$c" in *';'*|*'&'*|*'|'*|*'`'*|*'$('*|*$'\n'*) return 1;; esac

  # Parse the quote-stripped view and locate the push subcommand, tolerating the
  # same global options as cmd_is_git_push (`git -C <dir> push`, `git -c k=v push`).
  # The old code counted adjacent `git push` with `wc -l`, which returned 0 for
  # `-C`/`-c` forms and wrongly denied every legitimate feature-branch force-push
  # made through them.
  local toks; read -ra toks <<<"$cn"
  local n=${#toks[@]} i=0
  while [ "$i" -lt "$n" ] && [ "${toks[$i]}" != "git" ]; do i=$((i+1)); done
  [ "$i" -lt "$n" ] || return 1
  i=$((i+1))
  local pi=-1
  while [ "$i" -lt "$n" ]; do
    case "${toks[$i]}" in
      -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix) i=$((i+2)) ;;
      push) pi=$i; break ;;
      -*) i=$((i+1)) ;;
      *) return 1 ;;   # first non-option subcommand isn't push
    esac
  done
  [ "$pi" -ge 0 ] || return 1

  # Collect positional args after `push`, skipping flags (and value-taking flags).
  local positionals=() j=$((pi+1)) t
  while [ "$j" -lt "$n" ]; do
    t="${toks[$j]}"
    case "$t" in
      -o|--push-option|--repo|--exec|--receive-pack) j=$((j+2)); continue ;;  # flag + its value
      -*) j=$((j+1)); continue ;;                                             # other flags
      *) positionals+=("$t") ;;
    esac
    j=$((j+1))
  done

  # Require exactly: <remote> <single-refspec>. Anything else is ambiguous/multi.
  [ "${#positionals[@]}" -eq 2 ] || return 1
  local refspec="${positionals[1]}"
  refspec="${refspec#+}"          # drop leading + (force refspec)
  local dst="${refspec##*:}"      # dst = after last ':' (whole token if no ':')
  # Normalize a fully-qualified ref down to its branch name so protected targets
  # written as `refs/heads/main` / `heads/main` are still recognized as `main`.
  dst="${dst#refs/heads/}"; dst="${dst#heads/}"
  [ -n "$dst" ] || return 1

  # Protected / ambiguous destinations stay gated.
  case "$dst" in
    main|master|develop|HEAD|@|prod|production|stable) return 1 ;;
    release|release/*|release-*|releases/*|hotfix/*)    return 1 ;;
  esac
  # dst must be a plain branch ref (no globs / refspec tricks).
  printf '%s' "$dst" | grep -Eq '^[A-Za-z0-9._/-]+$' || return 1
  return 0
}

if is_forcepush; then
  forcepush_to_feature_branch \
    || deny "force-push" "operator_review_gated" "$c"
  # else: operator-approved relaxation — single force-push to a non-protected feature branch.
fi
g 'git[[:space:]]+(filter-branch|filter-repo)([[:space:]]|$)|git-filter-repo'                               && deny "history-rewrite" "operator_review_gated" "$c"

# Service/host lifecycle is fail-closed.  guard.sh is only defense-in-depth: the
# actual privilege boundary is the root-owned ccc-service-control wrapper and its
# root-owned allowlist (docs/service-control.md).  Direct lifecycle commands need
# fresh approval, except for the exact local ccc-telegram-bridge restart carve-out.
is_direct_ccc_bridge_restart() {
  case "$cn" in
    'systemctl restart ccc-telegram-bridge'|'systemctl restart ccc-telegram-bridge.service'|\
    'sudo systemctl restart ccc-telegram-bridge'|'sudo systemctl restart ccc-telegram-bridge.service')
      return 0 ;;
  esac
  return 1
}

if ! is_direct_ccc_bridge_restart; then
  gn '\b(systemctl|service)\b[^;&|]*\b(start|restart|reload|stop|kill|disable|enable|mask|unmask|daemon-reload|daemon-reexec)\b' \
    && deny "service-lifecycle" "operator_approval_gated" "$c"
  gn '\bpm2\b[^;&|]*\b(start|restart|reload|stop|delete|kill)\b' \
    && deny "service-lifecycle" "operator_approval_gated" "$c"
  gn '\b(docker|podman)\b[^;&|]*\b(run|up|start|restart|stop|kill|rm|pause|unpause|down)\b' \
    && deny "service-lifecycle" "operator_approval_gated" "$c"
  gn '\bkubectl\b[^;&|]*\b(rollout[[:space:]]+restart|scale|delete|drain|cordon|uncordon)\b' \
    && deny "service-lifecycle" "operator_approval_gated" "$c"
  gn '(^|[[:space:];|&])(restart-worker|stop-broker)([[:space:];|&]|$)' \
    && deny "service-lifecycle" "operator_approval_gated" "$c"
fi

is_readonly_lifecycle_text_search() {
  printf '%s' "$cn" | grep -Eq \
    '^[[:space:]]*(grep|rg)([[:space:]]+[^;&|<>$`()]+)+[[:space:]]*$'
}
if ! is_readonly_lifecycle_text_search \
  && gn '\b(shutdown|reboot|poweroff|halt)\b'; then
  deny "host-lifecycle" "operator_approval_gated" "$c"
fi

# References to self-update operator config fail closed unless the whole command
# is a simple local read.  This deliberately catches interpreter-mediated writes
# (`python open(..., "w")`, Ruby File.write, etc.) without pretending to parse
# every language.  The root-owned deployment is the real protection layer.
is_readonly_self_update_config_command() {
  printf '%s' "$cn" | grep -Eq \
    '^[[:space:]]*(cat|grep|stat|test|wc|sha256sum)([[:space:]]+--?[A-Za-z0-9_-]+)*[[:space:]]+[^;&|<>$`()]*self-update\.(services|repo)([[:space:]]+[^;&|<>$`()]+)*[[:space:]]*$'
}
if gn 'self-update\.(services|repo)' && ! is_readonly_self_update_config_command; then
  deny "self-update-config" "operator_approval_gated" "$c"
fi

# DB destructive / migration / replay
gi '\b(DROP[[:space:]]+(TABLE|DATABASE)|TRUNCATE[[:space:]]|FLUSHALL|FLUSHDB)\b'                  && deny "db-destructive" "operator_approval_gated" "$c"
# `db:migrate` only as an actual run invocation (npm/yarn/pnpm/npx run, or make),
# not the bare token — `grep db:migrate Makefile` used to trip this.
gi '\b((npm|pnpm|yarn|npx)([[:space:]]+run)?[[:space:]]+db:migrate|make[[:space:]]+db:migrate|prisma[[:space:]]+migrate[[:space:]]+(deploy|dev)|alembic[[:space:]]+(upgrade|downgrade)|knex[[:space:]]+migrate)\b' && deny "db-migrate" "operator_approval_gated" "$c"
# `replay` only as a broker/worker/gateway subcommand — the bare word matched
# innocuous greps/filenames like `grep replay app.log` before.
g '\b(broker|worker|gateway|hermes|a2a|nexus|openclaw)[A-Za-z0-9_-]*[[:space:]]+replay([[:space:]]|$)' && deny "replay" "operator_approval_gated" "$c"

# release / publish / tag-push / repo visibility
g '\b(npm|yarn|pnpm)[[:space:]]+publish([[:space:]]|$)|gh[[:space:]]+release[[:space:]]+create([[:space:]]|$)' && deny "release/publish" "operator_review_gated" "$c"
# tag-push: detect the push subcommand through git global options (-C/-c) just
# like the force-push gate, then look for --tags/--follow-tags anywhere in it.
# The old adjacency regex let `git -C /repo push origin --tags` slip the gate.
if cmd_is_git_push && gn '[[:space:]]--(tags|follow-tags)([[:space:]=]|$)'; then
  deny "release/publish" "operator_review_gated" "$c"
fi
g 'gh[[:space:]]+repo[[:space:]]+edit([[:space:]]|$)[^|;&]*--visibility'                          && deny "repo-visibility" "operator_approval_gated" "$c"

# secret exfil — external transfer of credential/key FILES to a remote endpoint.
# Local reads (.env, credentials) are intentionally NOT gated: the operator already has
# full shell access to the node, so local reads carry no marginal risk. Only network
# exfil (curl/wget/nc/scp sending secret files to a remote) stays gated.
#
# Order-INDEPENDENT: a network tool anywhere in the command PLUS a credential-file
# reference anywhere trips the gate. The old single-regex form required the secret
# token to appear *after* the tool on the same segment, so `cat .env | curl @-`
# (read the secret, pipe it out) and `base64 key | nc host` slipped straight
# through. `secret`/`token` are no longer matched as bare words — that blocked
# ordinary API calls like `curl https://…/token` — only concrete credential files
# (.env, .credentials, SSH private keys) count. Public keys (…​.pub / …​.pub.pem)
# are neutralized first so deploying an authorized_keys public key is not gated.
cexf="$(printf '%s' "$cnp" | sed -E 's#[A-Za-z0-9_./~-]*\.pub(\.pem)?#PUBKEY#g' 2>/dev/null)"
[ -n "$cexf" ] || cexf="$cnp"
gexf() { grep -Eq "$1" <<<"$cexf"; }
_exfil_net='\b(curl|wget|nc|ncat|scp|sftp|ftp|rsync)\b'
_exfil_secret='(\.env([^A-Za-z0-9_.-]|$)|\.credentials|\bid_(rsa|dsa|ecdsa|ed25519)\b)'
if gexf "$_exfil_net" && gexf "$_exfil_secret"; then
  deny "secret-exfil" "operator_approval_gated" "$c"
fi

# catastrophic rm against absolute / home roots (quote-stripped; long flags too)
# The trailing anchor now also accepts `*` so `rm -rf /*` (glob-expands to every
# top-level dir — as catastrophic as `rm -rf /`) is caught, and `${HOME}` is
# matched alongside `$HOME`. Relative globs like `rm -rf foo/*` are unaffected:
# the token right after the flags must still BE a filesystem root.
gn '\brm\b([[:space:]]+--?[A-Za-z-]+)*[[:space:]]+(/|~|\$HOME|\$\{HOME\}|/root|/etc|/var|/usr|/bin|/lib)([[:space:]/*]|$)' && deny "rm-catastrophic" "operator_approval_gated" "$c"

exit 0
