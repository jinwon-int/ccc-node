#!/usr/bin/env bash
# Install the repo-managed TM-2380 auto-distill source transactionally (#1257, #1262).
#
# This deliberately does not create/modify cron entries or start services.
# Fleet rollout remains a separately approved operation.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/auto-distill"
RECEIPT_VERIFIER="$SCRIPT_DIR/verify-auto-distill-receipt.py"
RECEIPT_NAME=evaluation-receipt.json
ACTION=preview
TARGET_HOME="${CCC_AUTO_DISTILL_TARGET_HOME:-${HOME:?HOME must be set}}"
FILES=(auto-distill.py metrics.py model_command.py "$RECEIPT_NAME")

usage() {
  printf '%s\n' \
    "Usage: $0 [--preview|--check|--apply] [--target-home ABSOLUTE_PATH]" \
    "" \
    "Default is --preview. --apply installs source only; cron/services are untouched."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --preview) ACTION=preview ;;
    --check) ACTION=check ;;
    --apply) ACTION=apply ;;
    --target-home)
      shift
      [ "$#" -gt 0 ] || { echo "--target-home requires a value" >&2; exit 2; }
      TARGET_HOME="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$TARGET_HOME" in
  /*) ;;
  *) echo "target home must be an absolute path" >&2; exit 2 ;;
esac
[ -d "$TARGET_HOME" ] && [ ! -L "$TARGET_HOME" ] \
  || { echo "target home is missing or unsafe" >&2; exit 2; }
[ "$(stat -c %u -- "$TARGET_HOME")" = "$(id -u)" ] \
  || { echo "target home owner is unsafe" >&2; exit 2; }

for name in "${FILES[@]}"; do
  if [ ! -f "$SOURCE_DIR/$name" ] || [ -L "$SOURCE_DIR/$name" ]; then
    if [ "$name" = "$RECEIPT_NAME" ]; then
      echo "evaluation receipt invalid: missing or unsafe" >&2
      exit 3
    fi
    echo "managed source missing or unsafe: $name" >&2
    exit 2
  fi
done
if [ ! -f "$RECEIPT_VERIFIER" ] || [ -L "$RECEIPT_VERIFIER" ]; then
  echo "receipt verifier missing or unsafe" >&2
  exit 2
fi

if ! receipt_summary="$(python3 "$RECEIPT_VERIFIER" \
    --source "$SOURCE_DIR/auto-distill.py" \
    --receipt "$SOURCE_DIR/$RECEIPT_NAME" 2>&1)"; then
  printf '%s\n' "$receipt_summary" >&2
  exit 3
fi
printf '%s\n' "$receipt_summary"

DEST_DIR="$TARGET_HOME/.hermes/auto-distill"
BACKUP_ROOT="$TARGET_HOME/.hermes/backups/auto-distill"

safe_existing_dir() {
  local path="$1"
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ -d "$path" ] && [ ! -L "$path" ] \
      || { echo "unsafe directory: $path" >&2; return 1; }
    [ "$(stat -c %u -- "$path")" = "$(id -u)" ] \
      || { echo "unsafe directory owner: $path" >&2; return 1; }
  fi
}

safe_target_file() {
  local path="$1"
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ -f "$path" ] && [ ! -L "$path" ] \
      || { echo "unsafe target file: $path" >&2; return 1; }
    [ "$(stat -c %u -- "$path")" = "$(id -u)" ] \
      || { echo "unsafe target owner: $path" >&2; return 1; }
  fi
}

file_mode() {
  case "$1" in
    "$RECEIPT_NAME") printf '600\n' ;;
    *) printf '700\n' ;;
  esac
}

safe_existing_dir "$TARGET_HOME/.hermes" || exit 2
safe_existing_dir "$DEST_DIR" || exit 2
for name in "${FILES[@]}"; do safe_target_file "$DEST_DIR/$name" || exit 2; done

if [ "$ACTION" = preview ]; then
  printf 'auto-distill install preview: target=%s\n' "$DEST_DIR"
  for name in "${FILES[@]}"; do
    if [ -f "$DEST_DIR/$name" ] && cmp -s "$SOURCE_DIR/$name" "$DEST_DIR/$name"; then
      printf '  unchanged %s\n' "$name"
    elif [ -e "$DEST_DIR/$name" ]; then
      printf '  update %s (backup before replace)\n' "$name"
    else
      printf '  install %s\n' "$name"
    fi
  done
  printf '%s\n' 'cron/services: unchanged'
  exit 0
fi

if [ "$ACTION" = check ]; then
  drift=0
  for name in "${FILES[@]}"; do
    if [ -f "$DEST_DIR/$name" ] && [ ! -L "$DEST_DIR/$name" ] \
       && cmp -s "$SOURCE_DIR/$name" "$DEST_DIR/$name"; then
      printf 'ok %s\n' "$name"
    else
      printf 'drift %s\n' "$name"
      drift=1
    fi
  done
  exit "$drift"
fi

mkdir -p "$TARGET_HOME/.hermes"
chmod 700 "$TARGET_HOME/.hermes"
mkdir -p "$DEST_DIR"
chmod 700 "$DEST_DIR"

stage_dir="$(mktemp -d "$DEST_DIR/.ccc-stage.XXXXXX")"
backup_dir=""
changed=()
installed=()

cleanup() {
  if [ -n "${stage_dir:-}" ] && [ -d "$stage_dir" ]; then
    rm -rf -- "$stage_dir"
  fi
  return 0
}

rollback() {
  local rc="${1:-1}"
  trap - ERR
  for name in "${installed[@]}"; do
    if [ -n "$backup_dir" ] && [ -f "$backup_dir/$name" ]; then
      install -m "$(file_mode "$name")" -- "$backup_dir/$name" "$DEST_DIR/$name"
    else
      rm -f -- "$DEST_DIR/$name"
    fi
  done
  cleanup
  echo "auto-distill install rolled back (rc=$rc)" >&2
  exit "$rc"
}

trap cleanup EXIT
trap 'rollback $?' ERR

for name in "${FILES[@]}"; do
  install -m "$(file_mode "$name")" -- "$SOURCE_DIR/$name" "$stage_dir/$name"
  if [ ! -f "$DEST_DIR/$name" ] || ! cmp -s "$stage_dir/$name" "$DEST_DIR/$name"; then
    changed+=("$name")
  fi
done

if [ "${#changed[@]}" -eq 0 ]; then
  printf 'auto-distill already current: %s\n' "$DEST_DIR"
  exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
for name in "${changed[@]}"; do
  if [ -f "$DEST_DIR/$name" ]; then
    if [ -z "$backup_dir" ]; then
      backup_dir="$BACKUP_ROOT/$stamp"
      mkdir -p "$backup_dir"
      chmod 700 "$TARGET_HOME/.hermes/backups" "$BACKUP_ROOT" "$backup_dir"
    fi
    install -m 600 -- "$DEST_DIR/$name" "$backup_dir/$name"
  fi
done

for name in "${changed[@]}"; do
  mv -f -- "$stage_dir/$name" "$DEST_DIR/$name"
  installed+=("$name")
done

trap - ERR
printf 'auto-distill installed: %s\n' "$DEST_DIR"
if [ -n "$backup_dir" ]; then
  printf 'backup: %s\n' "$backup_dir"
fi
printf '%s\n' 'cron/services: unchanged'
