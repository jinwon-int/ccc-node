"""Resolve the TM-2380 auto-distill model command without silent provider drift.

The 30-minute auto-distill cron does not inherit the bridge service environment.
Piri nodes therefore need a bounded, inspectable lookup that can recover the
configured launcher from systemd and that never turns a missing Piri launcher
into an unannounced Claude invocation (#1257).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import subprocess


SYSTEMD_UNIT = "ccc-telegram-bridge.service"
UNIT_ENV_KEYS = (
    "CCC_AUTO_DISTILL_PROVIDER",
    "CCC_AGENT_PROVIDER",
    "CCC_PIRI_REAL_CLI_PATH",
    "CCC_PIRI_CLI_PATH",
    "CLAUDE_CLI_PATH",
)
PIRI_ARGS = (
    "-p",
    "--no-session",
    "--exclude-tools",
    "bash,edit,write,read,grep,find,ls,ask_question",
)
CLAUDE_ARGS = (
    "-p",
    "--no-session-persistence",
    "--output-format",
    "json",
    "--model",
    "haiku",
    "--disallowedTools",
    "Bash",
    "Edit",
    "Write",
    "Read",
    "Grep",
    "Glob",
    "Task",
    "WebFetch",
    "WebSearch",
)


class ModelCommandError(RuntimeError):
    """The configured provider has no safe runnable extraction command."""


@dataclass(frozen=True)
class ModelCommand:
    """One resolved command plus body-free provenance for console/audit output."""

    argv: tuple[str, ...]
    engine: str
    source: str
    reason: str | None = None


def parse_systemd_environment(raw: str, *, allowed: Sequence[str] = UNIT_ENV_KEYS) -> dict[str, str]:
    """Parse ``systemctl show -p Environment --value`` without exposing extras."""

    if not raw:
        return {}
    try:
        assignments = shlex.split(raw, posix=True)
    except ValueError:
        return {}
    allowed_names = set(allowed)
    result: dict[str, str] = {}
    for assignment in assignments:
        name, separator, value = assignment.partition("=")
        if separator and name in allowed_names:
            result[name] = value
    return result


def read_bridge_unit_environment(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Read only allowlisted values from the system or user bridge unit.

    The system unit is preferred. Missing systemd, an unavailable user bus, an
    inactive unit, malformed quoting, and timeouts all degrade to an empty
    mapping so Termux and non-systemd nodes continue through path discovery.
    """

    commands = (
        ("systemctl", "show", "--property=Environment", "--value", SYSTEMD_UNIT),
        ("systemctl", "--user", "show", "--property=Environment", "--value", SYSTEMD_UNIT),
    )
    for command in commands:
        try:
            completed = runner(
                list(command),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        parsed = parse_systemd_environment(completed.stdout)
        # Never combine a system unit's provider hint with a different user
        # unit's launcher path. One coherent unit wins; standard-path lookup
        # can still resolve a provider-only unit safely.
        if parsed:
            return parsed
    return {}


def _runnable(raw: str | None, *, which: Callable[[str], str | None]) -> str | None:
    if not raw or "\0" in raw or "\n" in raw or "\r" in raw:
        return None
    candidate = str(raw).strip()
    if not candidate:
        return None
    if "/" in candidate:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    resolved = which(candidate)
    if resolved and Path(resolved).is_file() and os.access(resolved, os.X_OK):
        return resolved
    return None


def _first_runnable(
    candidates: Sequence[tuple[str, str | None]],
    *,
    which: Callable[[str], str | None],
) -> tuple[str | None, str | None]:
    for source, candidate in candidates:
        executable = _runnable(candidate, which=which)
        if executable:
            return executable, source
    return None, None


def _provider_hint(process_env: Mapping[str, str], unit_env: Mapping[str, str]) -> str:
    dedicated = (
        process_env.get("CCC_AUTO_DISTILL_PROVIDER")
        or unit_env.get("CCC_AUTO_DISTILL_PROVIDER")
        or ""
    ).strip().lower()
    if dedicated:
        if dedicated not in {"auto", "piri", "claude"}:
            raise ModelCommandError(
                "CCC_AUTO_DISTILL_PROVIDER must be auto, piri, or claude"
            )
        return dedicated
    runtime = (
        process_env.get("CCC_AGENT_PROVIDER")
        or unit_env.get("CCC_AGENT_PROVIDER")
        or ""
    ).strip().lower()
    # Codex-primary Termux can intentionally use a local Piri extraction lane,
    # so unsupported runtime hints retain the established auto-discovery path.
    return runtime if runtime in {"piri", "claude"} else "auto"


def resolve_model_command(
    *,
    process_environment: Mapping[str, str] | None = None,
    unit_environment: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    piri_default_paths: Sequence[Path] | None = None,
) -> ModelCommand:
    """Resolve Piri/Claude with provider-aware fail-closed behavior.

    Piri priority is REAL before wrapper, then standard fleet paths, then PATH.
    Process values override systemd for the same variable. A node explicitly
    identified as Piri never falls back to Claude when Piri is missing.
    """

    process_env = dict(os.environ if process_environment is None else process_environment)
    unit_env = dict(
        read_bridge_unit_environment()
        if unit_environment is None
        else unit_environment
    )
    home_path = Path.home() if home is None else home
    standards = tuple(
        piri_default_paths
        if piri_default_paths is not None
        else (Path("/opt/piri/piri-ccc.sh"), home_path / "piri/piri-ccc.sh")
    )

    piri_candidates: list[tuple[str, str | None]] = [
        ("process:CCC_PIRI_REAL_CLI_PATH", process_env.get("CCC_PIRI_REAL_CLI_PATH")),
        ("systemd:CCC_PIRI_REAL_CLI_PATH", unit_env.get("CCC_PIRI_REAL_CLI_PATH")),
        ("process:CCC_PIRI_CLI_PATH", process_env.get("CCC_PIRI_CLI_PATH")),
        ("systemd:CCC_PIRI_CLI_PATH", unit_env.get("CCC_PIRI_CLI_PATH")),
    ]
    piri_candidates.extend((f"standard:{path}", str(path)) for path in standards)
    piri_candidates.append(("PATH:piri", "piri"))
    piri_executable, piri_source = _first_runnable(piri_candidates, which=which)

    claude_candidates = (
        ("process:CLAUDE_CLI_PATH", process_env.get("CLAUDE_CLI_PATH")),
        ("systemd:CLAUDE_CLI_PATH", unit_env.get("CLAUDE_CLI_PATH")),
        ("PATH:claude", "claude"),
    )
    claude_executable, claude_source = _first_runnable(claude_candidates, which=which)
    provider = _provider_hint(process_env, unit_env)

    if provider == "piri":
        if not piri_executable:
            raise ModelCommandError(
                "provider=piri but no runnable Piri CLI was found; refusing Claude fallback"
            )
        return ModelCommand(
            (piri_executable, *PIRI_ARGS), "piri", str(piri_source)
        )
    if provider == "claude":
        if not claude_executable:
            raise ModelCommandError("provider=claude but no runnable Claude CLI was found")
        return ModelCommand(
            (claude_executable, *CLAUDE_ARGS), "claude", str(claude_source)
        )
    if piri_executable:
        return ModelCommand((piri_executable, *PIRI_ARGS), "piri", str(piri_source))
    if claude_executable:
        return ModelCommand(
            (claude_executable, *CLAUDE_ARGS),
            "claude",
            str(claude_source),
            reason="no-runnable-piri",
        )
    raise ModelCommandError("no runnable Piri or Claude extraction CLI was found")


def resolve_explicit_model_command(
    raw: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> ModelCommand:
    """Validate a user-supplied command while preserving quoted arguments."""

    try:
        argv = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise ModelCommandError(f"invalid --model-cmd quoting: {exc}") from exc
    if not argv:
        raise ModelCommandError("--model-cmd must not be empty")
    executable = _runnable(argv[0], which=which)
    if not executable:
        raise ModelCommandError("--model-cmd executable is not runnable")
    return ModelCommand((executable, *argv[1:]), "custom", "--model-cmd")
