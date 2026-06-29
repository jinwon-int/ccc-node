#!/usr/bin/env python3
"""Safe launcher/checker for the Termux native A2A worker slice.

The mobile worker should run a2a-broker-worker/dist/worker.js under the
native glibc-runner Node wrapper, not under proot. This helper reads a
systemd-style environment file, validates the native-worker/Claude bridge
wiring, and either prints the exact command or execs it when explicitly asked.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
from urllib.parse import urlparse

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_KEYS = (
    "A2A_TERMUX_NATIVE",
    "A2A_NATIVE_NODE_BIN",
    "A2A_WORKER_ROOT",
    "A2A_CLAUDE_CODE_BIN",
    "OPENCLAW_BIN",
    "A2A_OPENCLAW_ANALYSIS_BIN",
    "BROKER_URL",
    "WORKER_MODE",
    "WORKER_METADATA_JSON",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
    "USE_BUILTIN_RIPGREP",
)
PATH_KEYS = (
    "A2A_NATIVE_NODE_BIN",
    "A2A_CLAUDE_CODE_BIN",
    "OPENCLAW_BIN",
    "A2A_OPENCLAW_ANALYSIS_BIN",
)
# These are exec()'d directly (the native Node wrapper and the Claude CLI
# wrapper), so they must be executable — caught at check time, not at exec time.
# The bridges are .mjs files passed to Node, so they only need to exist.
EXEC_KEYS = (
    "A2A_NATIVE_NODE_BIN",
    "A2A_CLAUDE_CODE_BIN",
)
REQUIRED_METADATA = {
    "runtime": "claude-code",
    "harness": "claude",
}
# Intent-aware drop-in bridges accepted for OPENCLAW_BIN. The patch bridge
# (a2a-nexus #1021, claude-a2a-patch-bridge.mjs) behaves IDENTICALLY to the
# analysis bridge for analysis-intent tasks and adds a deterministic
# single-shot PATCH path; it shares the same CLI contract, stdout envelope,
# and OPENCLAW_BIN/A2A_OPENCLAW_ANALYSIS_BIN env var, so it is a safe superset.
ANALYSIS_BRIDGE = "claude-a2a-analysis-bridge.mjs"
PATCH_BRIDGE = "claude-a2a-patch-bridge.mjs"
ALLOWED_BRIDGES = (ANALYSIS_BRIDGE, PATCH_BRIDGE)
# The adapter id the worker must register, keyed by the wired bridge file, so
# WORKER_METADATA_JSON stays honest about which bridge is actually spawned.
BRIDGE_ADAPTER = {
    ANALYSIS_BRIDGE: "claude-a2a-analysis-bridge",
    PATCH_BRIDGE: "claude-a2a-patch-bridge",
}
# Recognized values for the patch bridge's single-shot opt-in (mirrors
# isSingleShotPatchMode in claude-a2a-patch-bridge.mjs).
PATCH_MODE_VALUES = ("single-shot", "single_shot", "singleshot")
FORBIDDEN_CONTEXT_NAMES = {
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "TOOLS.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
}


class ConfigError(Exception):
    """A fail-closed configuration error safe to print."""


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            raise ConfigError(f"{path}:{lineno}: invalid environment key {key!r}")
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                parts = shlex.split(value, posix=True)
            except ValueError as exc:
                raise ConfigError(f"{path}:{lineno}: invalid quoted value for {key}: {exc}") from exc
            if len(parts) != 1:
                raise ConfigError(f"{path}:{lineno}: quoted value for {key} must be a single token")
            value = parts[0]
        elif any(ch.isspace() for ch in value):
            raise ConfigError(f"{path}:{lineno}: unquoted whitespace in value for {key}")
        if "\x00" in value:
            raise ConfigError(f"{path}:{lineno}: NUL byte refused in value for {key}")
        env[key] = value
    return env


def require_keys(env: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_KEYS if not env.get(key)]
    if missing:
        raise ConfigError("missing required env key(s): " + ", ".join(missing))


def existing_file(label: str, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise ConfigError(f"{label} must point to an existing file: {value}")
    return path


def executable_file(label: str, value: str) -> Path:
    path = existing_file(label, value)
    if not os.access(path, os.X_OK):
        raise ConfigError(f"{label} must be executable: {value}")
    return path


def existing_dir(label: str, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_dir():
        raise ConfigError(f"{label} must point to an existing directory: {value}")
    return path


def check_forbidden_context(path: Path, label: str) -> None:
    parts = set(path.parts)
    if ".openclaw" in parts or path.name in FORBIDDEN_CONTEXT_NAMES:
        raise ConfigError(f"{label} points at forbidden OpenClaw runtime/bootstrap context: {path}")


def validate_broker_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("BROKER_URL must be an http(s) URL")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigError("BROKER_URL must point at the local Termux tunnel host")
    if parsed.port != 18790:
        raise ConfigError("BROKER_URL must use local tunnel port 18790")


def validate_metadata(value: str, expected_adapter: str) -> dict[str, object]:
    try:
        metadata = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"WORKER_METADATA_JSON is invalid JSON: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ConfigError("WORKER_METADATA_JSON must be a JSON object")
    for key, expected in REQUIRED_METADATA.items():
        if metadata.get(key) != expected:
            raise ConfigError(f"WORKER_METADATA_JSON must set {key}={expected!r}")
    if metadata.get("adapter") != expected_adapter:
        raise ConfigError(
            f"WORKER_METADATA_JSON must set adapter={expected_adapter!r} "
            "to match the wired OPENCLAW_BIN bridge"
        )
    return metadata


def validate_env(env: dict[str, str]) -> tuple[Path, list[str], dict[str, object]]:
    require_keys(env)
    if env["A2A_TERMUX_NATIVE"] != "1":
        raise ConfigError("A2A_TERMUX_NATIVE must be 1 so proot/systemd configs fail closed")
    if env["WORKER_MODE"] != "persistent":
        raise ConfigError("WORKER_MODE must be persistent for the broker worker lane")
    if env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] != "1":
        raise ConfigError("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC must be 1")
    if env["DISABLE_GROWTHBOOK"] != "1":
        raise ConfigError("DISABLE_GROWTHBOOK must be 1")
    if env["USE_BUILTIN_RIPGREP"] != "0":
        raise ConfigError("USE_BUILTIN_RIPGREP must be 0 so Termux-native rg is used")

    worker_root = existing_dir("A2A_WORKER_ROOT", env["A2A_WORKER_ROOT"])
    worker_script = Path(env.get("A2A_WORKER_SCRIPT", str(worker_root / "dist" / "worker.js"))).expanduser()
    if not worker_script.is_file():
        raise ConfigError(f"worker script must exist: {worker_script}")
    if not worker_script.name == "worker.js":
        raise ConfigError("worker script must be the a2a-broker-worker worker.js entrypoint")
    # The script we exec must live under the declared worker root, so an override
    # can't point the launcher at a worker.js anywhere else on the device.
    try:
        worker_script.resolve().relative_to(worker_root.resolve())
    except ValueError:
        raise ConfigError(
            f"worker script must live under A2A_WORKER_ROOT ({worker_root}): {worker_script}"
        )

    checked_paths: dict[str, Path] = {}
    for key in PATH_KEYS:
        # Reject forbidden OpenClaw context targets first (regardless of mode/bits),
        # then assert existence and — for the exec'd wrappers — executability.
        check_forbidden_context(Path(env[key]).expanduser(), key)
        checked_paths[key] = (executable_file if key in EXEC_KEYS else existing_file)(key, env[key])
    check_forbidden_context(worker_script, "A2A_WORKER_SCRIPT")

    openclaw = checked_paths["OPENCLAW_BIN"].resolve()
    a2a_openclaw = checked_paths["A2A_OPENCLAW_ANALYSIS_BIN"].resolve()
    if openclaw != a2a_openclaw:
        raise ConfigError("OPENCLAW_BIN and A2A_OPENCLAW_ANALYSIS_BIN must point at the same bridge file")
    if openclaw.name not in ALLOWED_BRIDGES:
        raise ConfigError(
            "OpenClaw bridge must be one of: " + ", ".join(ALLOWED_BRIDGES)
        )

    patch_mode = env.get("A2A_CLAUDE_CODE_PATCH_MODE", "").strip().lower()
    if patch_mode:
        if patch_mode not in PATCH_MODE_VALUES:
            raise ConfigError(
                "A2A_CLAUDE_CODE_PATCH_MODE must be one of: " + ", ".join(PATCH_MODE_VALUES)
            )
        if openclaw.name != PATCH_BRIDGE:
            raise ConfigError(
                f"A2A_CLAUDE_CODE_PATCH_MODE requires OPENCLAW_BIN to be {PATCH_BRIDGE}"
            )

    validate_broker_url(env["BROKER_URL"])
    metadata = validate_metadata(env["WORKER_METADATA_JSON"], BRIDGE_ADAPTER[openclaw.name])

    args = [str(checked_paths["A2A_NATIVE_NODE_BIN"]), str(worker_script)]
    extra = env.get("A2A_WORKER_ARGS", "")
    if extra:
        try:
            args.extend(shlex.split(extra, posix=True))
        except ValueError as exc:
            raise ConfigError(f"A2A_WORKER_ARGS is invalid shell syntax: {exc}") from exc
    return worker_script, args, metadata


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def load_and_validate(env_file: str) -> tuple[dict[str, str], Path, list[str], dict[str, object]]:
    path = Path(env_file)
    if not path.is_file():
        raise ConfigError(f"env file not found: {env_file}")
    env = parse_env_file(path)
    worker_script, args, metadata = validate_env(env)
    return env, worker_script, args, metadata


def cmd_check(args: argparse.Namespace) -> int:
    env, worker_script, command, metadata = load_and_validate(args.env_file)
    print("OK: Termux native A2A worker env is safe to launch")
    print(f"workerScript={worker_script}")
    print(f"brokerUrl={env['BROKER_URL']}")
    print(
        "metadata="
        + ",".join(f"{key}={metadata.get(key)}" for key in ("runtime", "harness", "adapter"))
    )
    print("command=" + shell_join(command))
    return 0


def cmd_print_command(args: argparse.Namespace) -> int:
    _env, _worker_script, command, _metadata = load_and_validate(args.env_file)
    print(shell_join(command))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    env, _worker_script, command, _metadata = load_and_validate(args.env_file)
    child_env = os.environ.copy()
    child_env.update(env)
    try:
        os.execve(command[0], command, child_env)
    except OSError as exc:  # exec failed — stay fail-closed instead of a raw traceback
        raise ConfigError(f"failed to exec native worker {command[0]}: {exc}") from exc
    return 127


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or launch the native Termux A2A worker.js harness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text, func in (
        ("check", "validate the env file and print a bounded launch summary", cmd_check),
        ("print-command", "print the exact native node worker.js command", cmd_print_command),
        ("run", "validate, then exec native node worker.js in the current process", cmd_run),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--env-file", required=True, help="systemd-style KEY=VALUE env file")
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
