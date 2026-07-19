#!/usr/bin/env bash
# ccc-bridge-locate.sh — which checkout actually serves this node's Telegram
# bridge, and how do I safely operate it?
#
# Nodes often carry multiple ccc-node checkouts (/opt/ccc-node, /root/ccc-node,
# $HOME/ccc-node). Invoking the WRONG checkout's bridge/start.sh caused two
# outages on 2026-07-19. This tool answers, read-only:
#   - is a bridge running, and from WHICH checkout (resolved from the venv
#     python path of the `python -m telegram_bot` process)?
#   - what `--path <projectPath>` does it serve?
#   - what is the exact safe restart command for that serving checkout?
#
# Self-contained: no repo-relative sourcing — pipe it to any node:
#   ssh node 'bash -s' < scripts/ccc-bridge-locate.sh
#
# Usage: ccc-bridge-locate.sh [--json] [--all]
#   --json   single-line JSON: {running, multi, bridges:[...], candidates:[...], restartCmd}
#   --all    include the candidate-checkout scan even when a bridge is running
#
# Exit codes: 0 = exactly one serving bridge found
#             2 = no running bridge (candidates scanned)
#             4 = multiple running bridges (ambiguous — inspect before acting)
#
# READ-ONLY always: never touches processes or files.
#
# Test seams:
#   CCC_BRIDGE_LOCATE_PS          command whose stdout replaces the process
#                                 table ("<pid> <cmdline...>" per line)
#   CCC_BRIDGE_LOCATE_CANDIDATES  colon-separated candidate checkout roots
set -uo pipefail

JSON=0
ALL=0
for arg in "$@"; do
    case "$arg" in
        --json) JSON=1 ;;
        --all)  ALL=1 ;;
        -h|--help)
            sed -n '2,26p' "$0" 2>/dev/null || true
            exit 0 ;;
        *) echo "ccc-bridge-locate: unknown argument: $arg (try --help)" >&2; exit 1 ;;
    esac
done

# ── process table ────────────────────────────────────────────────────────────
# One line per process: "<pid> <full cmdline>". Primary source is portable ps
# (works on VPS and Termux); /proc scan is the fallback for stripped-down ps.
ps_lines() {
    if [ -n "${CCC_BRIDGE_LOCATE_PS:-}" ]; then
        sh -c "$CCC_BRIDGE_LOCATE_PS" 2>/dev/null || true
        return 0
    fi
    local out
    if out="$(ps -eo pid=,args= 2>/dev/null)" && [ -n "$out" ]; then
        printf '%s\n' "$out"
        return 0
    fi
    local d pid
    for d in /proc/[0-9]*; do
        pid="${d#/proc/}"
        [ -r "$d/cmdline" ] || continue
        printf '%s %s\n' "$pid" "$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)"
    done
    return 0
}

# ── git info ─────────────────────────────────────────────────────────────────
GI_HEAD=""; GI_BRANCH=""; GI_DIRTY=""
git_info() { # <dir> → GI_HEAD GI_BRANCH GI_DIRTY
    local dir="$1"
    GI_HEAD="$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    GI_BRANCH="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    GI_DIRTY="$(git -C "$dir" status --porcelain 2>/dev/null | wc -l | tr -d '[:space:] ')"
    case "$GI_DIRTY" in ''|*[!0-9]*) GI_DIRTY=0 ;; esac
}

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

# ── detect running bridge(s) ─────────────────────────────────────────────────
# Match: an interpreter token ending in python(3[.x]) followed by
# `-m telegram_bot`. The serving checkout is resolved from the venv path
# `<checkout>/bridge/venv/bin/python` when present.
BRIDGE_JSON=()      # per-bridge JSON objects
BRIDGE_HUMAN=()     # per-bridge human lines
BRIDGE_CHECKOUT=()  # per-bridge checkout ("" if unresolvable)
BRIDGE_PPATH=()     # per-bridge --path value ("" if absent)

while IFS= read -r line; do
    case "$line" in
        *python*" -m telegram_bot"*) ;;
        *) continue ;;
    esac
    case "$line" in *ccc-bridge-locate*) continue ;; esac
    read -r -a tok <<< "$line"
    pid="${tok[0]:-}"
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    exe="${tok[1]:-}"
    checkout=""
    case "$exe" in
        */bridge/venv/bin/python*) checkout="${exe%/bridge/venv/bin/python*}" ;;
    esac
    ppath=""
    i=2
    while [ "$i" -lt "${#tok[@]}" ]; do
        if [ "${tok[$i]}" = "--path" ]; then
            ppath="${tok[$((i+1))]:-}"
            break
        fi
        i=$((i+1))
    done
    if [ -n "$checkout" ] && [ -d "$checkout" ]; then
        git_info "$checkout"
    else
        GI_HEAD="unknown"; GI_BRANCH="unknown"; GI_DIRTY=0
    fi
    BRIDGE_CHECKOUT+=("$checkout")
    BRIDGE_PPATH+=("$ppath")
    BRIDGE_JSON+=("{\"pid\":$pid,\"checkout\":\"$(json_escape "$checkout")\",\"projectPath\":\"$(json_escape "$ppath")\",\"head\":\"$(json_escape "$GI_HEAD")\",\"branch\":\"$(json_escape "$GI_BRANCH")\",\"dirty\":$GI_DIRTY}")
    BRIDGE_HUMAN+=("pid=$pid checkout=${checkout:-?} projectPath=${ppath:-?} head=$GI_HEAD branch=$GI_BRANCH dirty=$GI_DIRTY")
done < <(ps_lines)

N=${#BRIDGE_JSON[@]}
RUNNING=false; MULTI=false; RC=2
if [ "$N" -eq 1 ]; then RUNNING=true; RC=0
elif [ "$N" -gt 1 ]; then RUNNING=true; MULTI=true; RC=4
fi

# restartCmd: the exact safe command for the serving checkout — only when a
# single unambiguous serving bridge with a resolved checkout + projectPath.
RESTART_CMD=""
if [ "$N" -eq 1 ] && [ -n "${BRIDGE_CHECKOUT[0]}" ] && [ -n "${BRIDGE_PPATH[0]}" ]; then
    RESTART_CMD="${BRIDGE_CHECKOUT[0]}/bridge/start.sh --path ${BRIDGE_PPATH[0]} --restart -d"
fi

# ── candidate scan ───────────────────────────────────────────────────────────
# Probe well-known checkout roots for `.git` + `bridge/start.sh`; runs when no
# bridge is running, or always with --all.
CAND_JSON=()
CAND_HUMAN=()
if [ "$N" -eq 0 ] || [ "$ALL" -eq 1 ]; then
    if [ -n "${CCC_BRIDGE_LOCATE_CANDIDATES:-}" ]; then
        IFS=':' read -r -a roots <<< "$CCC_BRIDGE_LOCATE_CANDIDATES"
    else
        roots=(/opt/ccc-node /root/ccc-node "${HOME:-/root}/ccc-node")
    fi
    seen=" "
    for root in "${roots[@]}"; do
        [ -n "$root" ] || continue
        case "$seen" in *" $root "*) continue ;; esac
        seen="$seen$root "
        [ -e "$root/.git" ] || continue
        [ -f "$root/bridge/start.sh" ] || continue
        git_info "$root"
        CAND_JSON+=("{\"checkout\":\"$(json_escape "$root")\",\"head\":\"$(json_escape "$GI_HEAD")\",\"branch\":\"$(json_escape "$GI_BRANCH")\",\"dirty\":$GI_DIRTY}")
        CAND_HUMAN+=("checkout=$root head=$GI_HEAD branch=$GI_BRANCH dirty=$GI_DIRTY")
    done
fi

# ── output ───────────────────────────────────────────────────────────────────
if [ "$JSON" -eq 1 ]; then
    bridges=""
    for b in ${BRIDGE_JSON[@]+"${BRIDGE_JSON[@]}"}; do bridges="$bridges${bridges:+,}$b"; done
    cands=""
    for c in ${CAND_JSON[@]+"${CAND_JSON[@]}"}; do cands="$cands${cands:+,}$c"; done
    if [ -n "$RESTART_CMD" ]; then
        restart_json="\"$(json_escape "$RESTART_CMD")\""
    else
        restart_json="null"
    fi
    printf '{"running":%s,"multi":%s,"bridges":[%s],"candidates":[%s],"restartCmd":%s}\n' \
        "$RUNNING" "$MULTI" "$bridges" "$cands" "$restart_json"
else
    if [ "$N" -eq 0 ]; then
        echo "ccc-bridge-locate: no running bridge (python -m telegram_bot not found)"
    elif [ "$N" -eq 1 ]; then
        echo "ccc-bridge-locate: 1 serving bridge"
    else
        echo "ccc-bridge-locate: $N running bridges — AMBIGUOUS, inspect before acting"
    fi
    for h in ${BRIDGE_HUMAN[@]+"${BRIDGE_HUMAN[@]}"}; do echo "  bridge $h"; done
    if [ -n "$RESTART_CMD" ]; then
        echo "restart (safe, serving checkout): $RESTART_CMD"
    fi
    if [ "$N" -eq 0 ] || [ "$ALL" -eq 1 ]; then
        if [ "${#CAND_HUMAN[@]}" -eq 0 ]; then
            echo "candidates: none (probed /opt/ccc-node, /root/ccc-node, \$HOME/ccc-node)"
        else
            echo "candidates:"
            for h in ${CAND_HUMAN[@]+"${CAND_HUMAN[@]}"}; do echo "  $h"; done
        fi
    fi
fi

exit "$RC"
