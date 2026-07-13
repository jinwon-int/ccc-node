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

# Service lifecycle.  guard.sh is only defense-in-depth: the actual privilege
# boundary is the unprivileged agent account plus the root-owned
# ccc-service-control wrapper and its root-owned allowlist (docs/service-control.md).
#
# Fleet-service relaxation (operator-approved; RISK-PROFILES.md): pure lifecycle
# verbs — start/restart/reload/stop/kill (and try-/or- variants) — on FLEET units
# (names carrying a2a / hermes / openclaw / broker / gateway / worker, or
# ccc-telegram-bridge) proceed autonomously.  The transport does not change the
# risk class: local systemctl, a peer-node restart via `ssh <node> systemctl …`,
# and `systemctl -H <node> …` are judged by the same unit check, so a node can
# update from GitHub and recover itself or a peer unattended.
#
# Everything else stays fail-closed (fresh approval): non-fleet units,
# config-changing verbs (enable/disable/mask/unmask/daemon-reload/daemon-reexec)
# even on fleet units, pm2 delete, docker/podman/kubectl lifecycle, host
# lifecycle (shutdown/reboot/poweroff/halt — local or remote), and any
# invocation whose verb/targets cannot be parsed unambiguously
# (interpreter-mediated forms fail closed).

fleet_unit() {  # 0 = token names a fleet service (systemd unit or pm2 name)
  printf '%s' "$1" | grep -Eiq \
    '(^|[^A-Za-z0-9])(a2a|hermes|openclaw|broker|gateway|worker)([^A-Za-z0-9]|$)|ccc-telegram-bridge'
}

_relax_verb() {  # lifecycle verbs covered by the fleet relaxation
  case "$1" in
    start|restart|reload|stop|kill|try-restart|reload-or-restart|try-reload-or-restart|force-reload) return 0 ;;
  esac
  return 1
}

# stdin = one candidate target token per line (everything after the verb).
# 0 = at least one positional target and EVERY positional target is a fleet
# unit.  Flags are skipped; redirections (`>f`, `2>&1`, `> f`) are skipped; a
# `#` token stops parsing (trailing comment).  Anything else must BE a fleet
# unit or the whole segment fails (fail-closed).
_targets_fleet() {
  local t found=0 skip=0
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$t" in '#'*) break ;; esac
    if printf '%s' "$t" | grep -Eq '^[0-9]*(>>?|<)'; then
      printf '%s' "$t" | grep -Eq '^[0-9]*(>>?|<)$' && skip=1   # bare op: value follows
      continue
    fi
    case "$t" in
      -s|--signal|--kill-who|--kill-whom) skip=1; continue ;;   # value-taking kill flags
      -*) continue ;;
    esac
    found=1
    fleet_unit "$t" || return 1
  done
  [ "$found" = 1 ]
}

_systemctl_seg_fleet() {  # `systemctl [flags] <verb> <unit…>`
  local -a t; read -ra t <<<"$1"
  local n=${#t[@]} i=1 verb=''
  while [ "$i" -lt "$n" ]; do
    case "${t[$i]}" in
      -H|--host|-M|--machine) i=$((i+2)) ;;   # value-taking transport flags
      -*) i=$((i+1)) ;;
      *) verb="${t[$i]}"; i=$((i+1)); break ;;
    esac
  done
  _relax_verb "$verb" || return 1
  printf '%s\n' "${t[@]:$i}" | _targets_fleet
}

_service_seg_fleet() {  # SysV order: `service <unit> <verb>`
  local -a t; read -ra t <<<"$1"
  [ "${#t[@]}" -ge 3 ] || return 1
  _relax_verb "${t[2]}" || return 1
  fleet_unit "${t[1]}"
}

_pm2_seg_fleet() {  # `pm2 <verb> <name…>`; delete is config-changing → not relaxed
  local -a t; read -ra t <<<"$1"
  local n=${#t[@]} i=1 verb=''
  while [ "$i" -lt "$n" ]; do
    case "${t[$i]}" in -*) i=$((i+1)) ;; *) verb="${t[$i]}"; i=$((i+1)); break ;; esac
  done
  case "$verb" in start|restart|reload|stop|kill) ;; *) return 1 ;; esac
  printf '%s\n' "${t[@]:$i}" | _targets_fleet
}

# 0 = every systemctl/service/pm2 lifecycle invocation in the command targets
# only fleet units with a relaxed verb.  One non-fleet target, config verb,
# missing target, or unparseable segment fails the whole command (fail-closed).
all_lifecycle_segments_fleet() {
  local seg decided=1
  while IFS= read -r seg; do
    [ -n "$seg" ] || continue
    # Segments without a gated verb (e.g. `systemctl status x` inside a
    # compound) did not trigger this gate and are not judged here.
    grep -Eq '\b(start|restart|reload|stop|kill|delete|disable|enable|mask|unmask|isolate|daemon-reload|daemon-reexec)\b' <<<"$seg" \
      || continue
    case "$seg" in
      systemctl*) _systemctl_seg_fleet "$seg" || return 1 ;;
      service*)   _service_seg_fleet "$seg"   || return 1 ;;
      pm2*)       _pm2_seg_fleet "$seg"       || return 1 ;;
      *) return 1 ;;
    esac
    decided=0
  done < <(grep -Eo '\b(systemctl|service|pm2)\b[^;&|]*' <<<"$cn")
  return "$decided"
}

if gn '\b(systemctl|service)\b[^;&|]*\b(start|restart|reload|stop|kill|disable|enable|mask|unmask|isolate|daemon-reload|daemon-reexec)\b' \
   || gn '\bpm2\b[^;&|]*\b(start|restart|reload|stop|delete|kill)\b'; then
  all_lifecycle_segments_fleet \
    || deny "service-lifecycle" "operator_approval_gated" "$c"
fi
gn '\b(docker|podman)\b[^;&|]*\b(run|up|start|restart|stop|kill|rm|pause|unpause|down)\b' \
  && deny "service-lifecycle" "operator_approval_gated" "$c"
gn '\bkubectl\b[^;&|]*\b(rollout[[:space:]]+restart|scale|delete|drain|cordon|uncordon)\b' \
  && deny "service-lifecycle" "operator_approval_gated" "$c"

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

# secret exfil — EGRESS of a credential/key FILE to a remote endpoint.
# Local reads (.env, credentials) are intentionally NOT gated: the operator already has
# full shell access to the node, so local reads carry no marginal risk. Only genuine
# network egress of a secret file stays gated.
#
# Precision (issue #399) over the old "net tool ANYWHERE + secret ANYWHERE" match,
# which over-blocked three non-exfil shapes. The refined rule keeps every real-exfil
# catch while allowing:
#   1) segment scope   — a net tool and a secret in DIFFERENT statements (`;`, `&&`,
#                        `||`, `&`) no longer cross-contaminate (they can't share data).
#   2) remote required — a purely LOCAL scp/sftp/rsync copy (no remote spec) is not
#                        exfil; rsync is the standard local-backup tool.
#   3) direction       — an INGRESS download whose only secret reference is an output
#                        sink (`-o FILE`, `> FILE`), a URL/remote resource, or a
#                        remote-source pull (`scp host:/x .`) is not egress.
# Preserved real-exfil catches: read-then-pipe (`cat .env | curl @-`,
# `base64 key | nc host`), direct uploads (`curl -T/-d @.env`, `scp .env host:`,
# `rsync .env host:`, `wget --post-file=.env`, `nc host < .env`). Public keys
# (…​.pub / …​.pub.pem) are neutralized first so deploying an authorized_keys key is fine.
_secret_in()   { grep -Eq '(\.env([^A-Za-z0-9_.-]|$)|\.credentials|\bid_(rsa|dsa|ecdsa|ed25519)\b)' <<<"$1"; }
_remote_in()   { grep -Eq '(^[A-Za-z][A-Za-z0-9+.-]*://|^([A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+:)' <<<"$1"; }
_net_http_in() { grep -Eqw '(curl|wget|nc|ncat|ftp)' <<<"$1"; }   # inherently-remote senders
_net_copy_in() { grep -Eqw '(scp|sftp|rsync)' <<<"$1"; }          # SRC->DEST copy tools (can be local)
_net_any_in()  { grep -Eqw '(curl|wget|nc|ncat|ftp|scp|sftp|rsync)' <<<"$1"; }

# 0 = the statement egresses a credential file to a remote endpoint.
exfil_stmt() {
  local stmt="$1"
  _secret_in "$stmt" || return 1
  _net_any_in "$stmt" || return 1

  local -a stages; IFS='|' read -ra stages <<<"$stmt"   # read (not glob) split on pipe
  local n=${#stages[@]} i j

  # (1) piped read-then-send: a secret in an UPSTREAM stage feeding a net tool downstream.
  for ((i=0; i<n; i++)); do
    _secret_in "${stages[$i]}" || continue
    for ((j=i+1; j<n; j++)); do
      _net_any_in "${stages[$j]}" && return 0
    done
  done

  # (2)/(3) direct: a net tool and a secret in the SAME stage.
  local stage k prev
  for stage in "${stages[@]}"; do
    _net_any_in "$stage" || continue
    local -a t; read -ra t <<<"$stage"
    local m=${#t[@]} http=0 copy=0 rlast=-1
    for ((k=0; k<m; k++)); do
      _net_http_in "${t[$k]}" && http=1
      _net_copy_in "${t[$k]}" && copy=1
      _remote_in  "${t[$k]}" && rlast=$k                 # last remote endpoint = the destination
    done

    # HTTP-ish sender: egress unless every secret token is an output sink or a remote resource.
    if [ "$http" -eq 1 ]; then
      for ((k=0; k<m; k++)); do
        _secret_in "${t[$k]}" || continue
        _remote_in "${t[$k]}" && continue                # a URL/remote resource, not a local secret
        prev="${t[$((k-1))]:-}"
        grep -Eq '^(-o|--output|-O|--output-document)$' <<<"$prev"      && continue   # -o FILE
        grep -Eq '^[0-9]*>>?$' <<<"$prev"                              && continue   # > FILE
        grep -Eq '^(--output|--output-document|-o)=' <<<"${t[$k]}"     && continue   # --output=FILE
        grep -Eq '^[0-9]*>>?' <<<"${t[$k]}"                            && continue   # >FILE (fused)
        return 0                                          # non-sink secret with an HTTP sender → egress
      done
    fi

    # COPY tool: needs a real remote spec; egress = a LOCAL secret source before the remote dest.
    if [ "$copy" -eq 1 ] && [ "$rlast" -ge 0 ]; then
      for ((k=0; k<m; k++)); do
        _secret_in "${t[$k]}" || continue
        _remote_in "${t[$k]}" && continue                # secret on the remote side (ingress pull)
        [ "$k" -lt "$rlast" ] && return 0                # local secret source precedes a remote dest
      done
    fi
  done
  return 1
}

# Quote-stripped + public-key-neutralized view, split into statements on `;`/`&&`/`||`/`&`.
base_exfil="$(printf '%s' "$cn" | sed -E 's#[A-Za-z0-9_./~-]*\.pub(\.pem)?#PUBKEY#g' 2>/dev/null)"
[ -n "$base_exfil" ] || base_exfil="$cn"
# `|| [ -n "$_stmt" ]` so a final statement with no trailing newline is still tested.
while IFS= read -r _stmt || [ -n "$_stmt" ]; do
  [ -n "$_stmt" ] || continue
  exfil_stmt "$_stmt" && deny "secret-exfil" "operator_approval_gated" "$c"
done < <(printf '%s\n' "$base_exfil" | sed -E 's/(&&|\|\||;|&)/\n/g')

# catastrophic rm against absolute / home roots (quote-stripped; long flags too)
# The trailing anchor now also accepts `*` so `rm -rf /*` (glob-expands to every
# top-level dir — as catastrophic as `rm -rf /`) is caught, and `${HOME}` is
# matched alongside `$HOME`. Relative globs like `rm -rf foo/*` are unaffected:
# the token right after the flags must still BE a filesystem root.
gn '\brm\b([[:space:]]+--?[A-Za-z-]+)*[[:space:]]+(/|~|\$HOME|\$\{HOME\}|/root|/etc|/var|/usr|/bin|/lib)([[:space:]/*]|$)' && deny "rm-catastrophic" "operator_approval_gated" "$c"

exit 0
