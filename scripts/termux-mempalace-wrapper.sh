#!/usr/bin/env bash
# ccc-node:termux-mempalace-wrapper:v1
# Run the Linux ARM64 MemPalace build in its dedicated PRoot container while
# preserving argv exactly. The container receives only the host home bind and
# a minimal, non-secret runtime environment.
set -euo pipefail

container="${CCC_TERMUX_MEMPALACE_CONTAINER:-ccc-mempalace}"
case "$container" in
  ''|-*|*[!A-Za-z0-9_.-]*) echo "invalid MemPalace container name" >&2; exit 2 ;;
esac

prefix="${PREFIX:-/data/data/com.termux/files/usr}"
proot_cli="${CCC_TERMUX_MEMPALACE_PROOT_CLI:-$prefix/bin/proot-distro}"
[ -f "$proot_cli" ] && [ -x "$proot_cli" ] \
  || { echo "proot-distro is unavailable" >&2; exit 2; }
case "$HOME" in
  /*) ;;
  *) echo "HOME must be absolute" >&2; exit 2 ;;
esac
case "$HOME" in
  *:*) echo "HOME containing ':' cannot be bound safely" >&2; exit 2 ;;
esac

exec "$proot_cli" login --isolated --bind "$HOME:$HOME" "$container" -- \
  env -i \
    HOME=/opt/ccc-mempalace/home \
    PATH=/opt/ccc-mempalace/venv/bin:/usr/bin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    XDG_CACHE_HOME=/opt/ccc-mempalace/cache \
    MEMPALACE_PALACE_PATH=/opt/ccc-mempalace/palace \
    MEMPALACE_BACKEND=sqlite_exact \
    MEMPALACE_BACKEND_EXPLICIT=sqlite_exact \
    MEMPALACE_EMBEDDING_MODEL=minilm \
    MEMPALACE_EMBEDDING_DEVICE=cpu \
    MEMPALACE_EMBEDDING_THREADS=1 \
    OMP_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
    /opt/ccc-mempalace/venv/bin/mempalace "$@"
