#!/usr/bin/env python3
"""Refresh the bounded shared-audience memory snapshot for A2A Piri lanes.

Produces the host-side snapshot consumed by the a2a-docker-runner piri memory
extension (jinwon-int/a2a-nexus#1797 item 3a): the runner bind-mounts
/var/lib/a2a-runner/piri-memory read-only at /run/secrets/piri-memory and the
baked extension injects MEMORY.md into the piri system prompt when the lane
opts in.

Privacy boundary: the snapshot is materialized through the bridge's
memory_audience stack with the exact SHARED audience route — the same scope
group/channel conversations receive. Private DM stores and private-only
legacy inputs are never read by this route, so the published file is safe to
enter A2A task containers.

Fail-closed: any materializer/validation failure leaves the previously
published snapshot untouched and exits non-zero.

Run standalone: python3 scripts/a2a_piri_memory_snapshot.py --json
Requires the bridge package importable (e.g. PYTHONPATH=.github/pythonpath).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".github" / "pythonpath"))

DEFAULT_AUDIENCE_ROOT = Path.home() / ".telegram_bot" / "memory-audiences"
DEFAULT_OUTPUT = Path("/var/lib/a2a-runner/piri-memory/MEMORY.md")
DEFAULT_MATERIALIZER = REPO_ROOT / "scripts" / "ccc_codex_memory.py"
DEFAULT_MAX_BYTES = 32768
MATERIALIZER_TIMEOUT_SECONDS = 120.0


class _SharedSettings:
    """Duck-typed settings shim for MemoryAudience.hook_environment.

    Only the attributes read for a SHARED audience are provided; legacy
    private reads stay disabled by construction.
    """

    def __init__(self, *, audience_root: Path, claude_settings_path: Path) -> None:
        self.bridge_memory_audience_root = str(audience_root)
        self.claude_settings_path = str(claude_settings_path)
        self.honcho_memory_enabled = os.environ.get("CCC_HONCHO_MEMORY_ENABLED", "1") == "1"
        self.honcho_config_path = Path(
            os.environ.get("CCC_HONCHO_CFG", str(Path.home() / ".hermes" / "honcho.json"))
        )

    def hook_policy_environment(self) -> dict[str, str]:
        return {}


def build_shared_memory_environment(
    *, audience_root: Path, claude_settings_path: Path, max_bytes: int
) -> dict[str, str]:
    """Return the materializer environment for the exact shared piri route.

    The materializer fails closed (codex_audience_scoped_blocked) unless the
    scoped piri route is canonical, so CODEX_HOME / bootstrap / session paths
    all point at the audience tree — the same file the bridge materializes
    for group chats. The producer publishes a bounded copy from there.
    """

    from telegram_bot.core.memory_audience import (
        AUDIENCE_SHARED,
        MemoryAudience,
    )

    audience = MemoryAudience(AUDIENCE_SHARED, AUDIENCE_SHARED, audience_root)
    env = audience.hook_environment(
        _SharedSettings(
            audience_root=audience_root,
            claude_settings_path=claude_settings_path,
        )
    )
    env["CODEX_HOME"] = str(audience.piri_bootstrap_home)
    env["CODEX_SQLITE_HOME"] = str(audience.piri_bootstrap_home)
    env["CCC_PIRI_BOOTSTRAP_HOME"] = str(audience.piri_bootstrap_home)
    env["PIRI_CODING_AGENT_SESSION_DIR"] = str(audience.piri_session_dir)
    env["CCC_PIRI_BOOTSTRAP_CONTEXT_FILE"] = str(audience.piri_bootstrap_home / "AGENTS.md")
    env["CCC_MEMORY_MATERIALIZER_PROVIDER"] = "piri"
    env["CCC_CODEX_MEMORY_MAX_BYTES"] = str(max_bytes)
    return env


def publish_snapshot(staging_file: Path, output: Path, *, max_bytes: int) -> int:
    """Atomically publish a validated snapshot; returns the byte count."""

    if not staging_file.is_file() or staging_file.is_symlink():
        raise RuntimeError("snapshot_missing")
    raw = staging_file.read_bytes()
    if not raw:
        raise RuntimeError("snapshot_empty")
    if len(raw) > max_bytes:
        raise RuntimeError("snapshot_oversized")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=output.parent, prefix=".MEMORY.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, output)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return len(raw)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audience-root", default=str(DEFAULT_AUDIENCE_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--materializer", default=str(DEFAULT_MATERIALIZER))
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--claude-settings",
        default=str(Path.home() / ".claude" / "settings.json"),
        help="Drives only inert legacy path strings for the shared route.",
    )
    parser.add_argument("--json", action="store_true", help="Print a body-free status line.")
    options = parser.parse_args(argv)

    if options.max_bytes <= 0 or options.max_bytes > 131072:
        print("error=max_bytes_out_of_range", file=sys.stderr)
        return 2
    materializer = Path(options.materializer)
    if not materializer.is_file():
        print("error=materializer_missing", file=sys.stderr)
        return 2

    audience_root = Path(options.audience_root).expanduser()
    output = Path(options.output)

    env = dict(os.environ)
    shared_env = build_shared_memory_environment(
        audience_root=audience_root,
        claude_settings_path=Path(options.claude_settings).expanduser(),
        max_bytes=options.max_bytes,
    )
    env.update(shared_env)
    try:
        completed = subprocess.run(
            [sys.executable, str(materializer), "materialize", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=MATERIALIZER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("error=materializer_timeout", file=sys.stderr)
        return 78
    if completed.returncode != 0:
        print("error=materializer_failed", file=sys.stderr)
        return 78

    try:
        byte_count = publish_snapshot(
            Path(shared_env["CCC_PIRI_BOOTSTRAP_CONTEXT_FILE"]),
            output,
            max_bytes=options.max_bytes,
        )
    except RuntimeError as exc:
        print(f"error={exc}", file=sys.stderr)
        return 78

    if options.json:
        print(json.dumps({"status": "ok", "bytes": byte_count, "audience": "shared"}))
    return 0


if __name__ == "__main__":
    sys.exit(run())
