#!/usr/bin/env bash
# self-update: detect drift between this node's ccc-node checkout and origin/main.
# READ-ONLY — fetches and reports; never pulls, installs, or restarts.
set -uo pipefail

REPO="${CCC_REPO_DIR:-$([ -d /opt/ccc-node/.git ] && echo /opt/ccc-node || echo "${HOME:-/root}/ccc-node")}"
if [ ! -d "$REPO/.git" ]; then
  echo "ccc-node repo not found at $REPO (set CCC_REPO_DIR)"; exit 1
fi
cd "$REPO" || exit 1

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if ! git fetch origin --quiet 2>/dev/null; then
  echo "git fetch failed (network/credentials?) — cannot check drift"; exit 1
fi

behind="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo '?')"
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
dirty="$(git status --porcelain 2>/dev/null | head -1)"

echo "repo:   $REPO"
echo "branch: $branch   (ahead $ahead / behind $behind of origin/main)"
[ -n "$dirty" ] && echo "WARNING: working tree has uncommitted changes — resolve before updating."

# Commit distance answers "is the checkout current", NOT "is the harness
# current". A `git pull` with no `setup.sh` leaves ~/.claude serving the old
# code while this script reported success (#1033). Ask ccc-doctor, which
# already compares each installed file against its template through setup.sh's
# canonical-path rewrite -- reimplementing that comparison here would reproduce
# the phantom-drift bug the shared transform exists to prevent.
#
# Read-only and advisory: any failure (no python3, unreadable doctor, broken
# JSON) leaves installed_state empty and the git verdict is reported alone,
# marked as unverified rather than silently claimed as clean.
installed_state=""
installed_detail=""
if command -v python3 >/dev/null 2>&1 && [ -f "$REPO/scripts/ccc_doctor.py" ]; then
  # doctor exits non-zero whenever it classifies anything as 교정가능, which is
  # precisely the drift case, so its status is captured as data and never used
  # as a success signal.
  doctor_json="$(python3 "$REPO/scripts/ccc_doctor.py" --json 2>/dev/null || true)"
  if [ -n "$doctor_json" ]; then
    installed_detail="$(printf '%s' "$doctor_json" | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin).get("rows", [])
except Exception:
    sys.exit(1)
bad = [r for r in rows if r.get("status") in {"drifted", "missing"}]
for r in bad[:20]:
    print("  {:8} {}".format(r.get("status", "?"), r.get("item", "?")))
if len(bad) > 20:
    print("  ... and {} more".format(len(bad) - 20))
sys.exit(3 if bad else 0)
' 2>/dev/null)"
    case $? in
      0) installed_state="clean" ;;
      3) installed_state="stale" ;;
      *) installed_state="" ;;
    esac
  fi
fi

report_installed() {
  case "$installed_state" in
    clean)
      echo "installed harness (~/.claude): matches this checkout."
      ;;
    stale)
      echo "installed harness (~/.claude): DOES NOT match this checkout —"
      [ -n "$installed_detail" ] && printf '%s\n' "$installed_detail"
      echo "  run setup.sh to install, or ccc-doctor --fix --apply --scope=files."
      ;;
    *)
      echo "installed harness (~/.claude): NOT CHECKED (ccc-doctor unavailable) — run /doctor."
      ;;
  esac
}

if [ "$behind" = "0" ]; then
  if [ "$installed_state" = "stale" ]; then
    echo "STATUS: checkout up to date, but the INSTALLED harness is stale — run setup.sh."
    report_installed
    exit 0
  fi
  echo "STATUS: checkout up to date — no repo update needed."
  report_installed
  exit 0
fi

echo "STATUS: $behind commit(s) behind origin/main — update available."
echo
echo "--- new commits ---"
git --no-pager log --oneline HEAD..origin/main 2>/dev/null | head -20
echo
echo "--- changed harness files (claude/ scripts/) ---"
git --no-pager diff --stat HEAD..origin/main -- claude scripts 2>/dev/null | head -40
echo
echo "--- CHANGELOG additions ---"
git --no-pager diff HEAD..origin/main -- CHANGELOG.md 2>/dev/null \
  | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//' | head -30
