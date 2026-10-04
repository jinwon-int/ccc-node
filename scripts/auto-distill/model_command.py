"""Resolve the TM-2380 auto-distill model command without silent provider drift.

The 30-minute auto-distill cron does not inherit the bridge service environment.
Piri nodes therefore need a bounded, inspectable lookup that can recover the
configured launcher from systemd and that never turns a missing Piri launcher
into an unannounced Claude invocation (#1257).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import IO


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
# Codex exec reads the prompt from stdin (trailing "-") and persists rollout
# sessions under $CODEX_HOME. Persistence cannot be disabled on the CLI, so the
# engine instead redirects CODEX_HOME to an isolated scratch home (see
# codex_scratch_home) — the extractor's own rollouts then never land in the
# operator's ~/.codex/sessions, making the self-call remix structurally
# impossible (#1295, live-proven on seoseo 2026-08-26).
#
# Usage accounting (#1857): plain ``codex exec`` prints only the final answer,
# so the codex lane recorded ``usage_missing`` for 100% of extractions. The
# lane therefore runs codex through this module's ``--codex-usage-wrapper``
# entry point: codex streams ``--json`` events, the wrapper prints only the
# final answer on stdout (unchanged stdout contract) and re-emits the
# ``turn.completed`` token usage as a ``PIRI_USAGE=`` line, which
# auto-distill.py already parses. auto-distill.py itself stays byte-identical
# to the evaluated receipt.
CODEX_ARGS = (
    "exec",
    "--json",
    "--skip-git-repo-check",
)
CODEX_WRAPPER_FLAG = "--codex-usage-wrapper"
USAGE_LINE_PREFIX = "PIRI_USAGE="
PR_SET_PDEATHSIG = 1
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
# Launcher aliases the provider resolves server-side. A receipt must never
# record one of these as the "resolved" model id (#1521, #1514 finding).
BARE_MODEL_ALIASES = frozenset({"haiku", "sonnet", "opus", "default"})


def claude_model_alias(argv: Sequence[str]) -> str | None:
    """Return the ``--model`` value a Claude argv sends (alias or concrete id)."""

    for index, arg in enumerate(argv):
        if arg == "--model" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--model="):
            return arg.partition("=")[2]
    return None


def _envelope_model_ids(payload: object) -> set[str]:
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return set()
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict):
        return set()
    return {key for key in usage if isinstance(key, str) and key.strip()}


def resolved_model_ids(text: str) -> list[str]:
    """Collect concrete model ids from Claude ``--output-format json`` envelopes.

    ``claude --version`` prints only the CLI version and the CLI exposes no
    offline alias table, so the only model-call-free source of the id an alias
    resolved to is the ``modelUsage`` block of envelopes the evaluation already
    produced (auto-distill.py reads the same block for cost accounting).
    Accepts one envelope or a log with one JSON document per line; anything
    that is not a result envelope is ignored. Never invokes a model.
    """

    ids: set[str] = set()
    try:
        ids |= _envelope_model_ids(json.loads(text))
        return sorted(ids)
    except ValueError:
        pass
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            ids |= _envelope_model_ids(json.loads(stripped))
        except ValueError:
            continue
    return sorted(ids)


class ModelCommandError(RuntimeError):
    """The configured provider has no safe runnable extraction command."""


@dataclass(frozen=True)
class ModelCommand:
    """One resolved command plus body-free provenance for console/audit output."""

    argv: tuple[str, ...]
    engine: str
    source: str
    reason: str | None = None
    # Engine-specific environment overrides merged into the child environment
    # by the caller (extract_json). Codex uses this for CODEX_HOME isolation.
    env_overrides: tuple[tuple[str, str], ...] = ()


def codex_scratch_home(home: Path | None = None) -> Path:
    """Prepare an isolated CODEX_HOME and return it (idempotent, per-node).

    Auth/config are *symlinked* from the real ~/.codex (no copies, no secret
    material is duplicated); only rollout sessions are written under the
    scratch home. Live-proven on seoseo: with this redirect the extractor's
    session lands in the scratch tree and ~/.codex/sessions stays untouched
    (119 → 119 files across a successful "OK" completion). Raises
    ModelCommandError when the real auth is missing so a codex-lane node fails
    closed instead of silently issuing unauthenticated calls.
    """

    real_home = (Path.home() if home is None else home) / ".codex"
    auth = real_home / "auth.json"
    if not auth.is_file():
        raise ModelCommandError(
            "codex engine selected but ~/.codex/auth.json is missing"
        )
    scratch = real_home.parent / ".codex-auto-distill-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "config.toml"):
        target = real_home / name
        link = scratch / name
        if link.is_symlink() or link.exists():
            link.unlink()
        if target.is_file():
            link.symlink_to(target)
    return scratch


def codex_wrapper_argv(codex_executable: str) -> tuple[str, ...]:
    """Argv that runs codex through the usage-accounting wrapper (#1857)."""

    python = sys.executable or shutil.which("python3") or "python3"
    return (python, str(Path(__file__).resolve()), CODEX_WRAPPER_FLAG, codex_executable)


def _token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0
    return int(value)


def normalize_codex_usage(raw: Mapping[str, object]) -> dict[str, object]:
    """Map codex ``turn.completed`` usage onto the Piri/Claude accounting keys.

    Codex reports ``input_tokens`` as the whole prompt, with cached reads (and
    cache writes, when reported) as subsets of it. Claude/Piri keys keep the
    uncached input separate, so the subsets are subtracted; ``totalTokens``
    therefore never exceeds what codex itself counted. ``output_tokens``
    already includes reasoning tokens, which are kept only as a detail field.

    Codex subscription calls carry no per-call price, so no ``costUsd`` is
    invented here — ``costBasis`` records that the dollar cost is unpriced
    instead of reporting a false 0.
    """

    total_input = _token_count(raw.get("input_tokens"))
    cache_read = _token_count(raw.get("cached_input_tokens"))
    cache_write = _token_count(raw.get("cache_write_input_tokens"))
    output = _token_count(raw.get("output_tokens"))
    uncached = max(total_input - cache_read - cache_write, 0)
    return {
        "requests": 1,
        "inputTokens": uncached,
        "outputTokens": output,
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": cache_write,
        "totalTokens": uncached + cache_read + cache_write + output,
        "reasoningOutputTokens": _token_count(raw.get("reasoning_output_tokens")),
        "costBasis": "codex-unpriced",
    }


def parse_codex_events(stream: str) -> tuple[dict[str, object] | None, str | None, list[str]]:
    """Read a ``codex exec --json`` event stream.

    Returns (normalized usage or None, last agent message, error messages).
    Usage is summed over every ``turn.completed`` event; malformed lines are
    skipped. Error text is returned so the caller can surface it on stderr —
    in ``--json`` mode codex reports failures (usage limits, auth) as events
    on stdout, and auto-distill classifies transport failures from stderr.
    """

    totals: dict[str, int] = {}
    seen_usage = False
    last_message: str | None = None
    errors: list[str] = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            seen_usage = True
            for key, value in event["usage"].items():
                totals[key] = totals.get(key, 0) + _token_count(value)
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            text = item.get("text") if item.get("type") == "agent_message" else None
            if isinstance(text, str):
                last_message = text
            elif item.get("type") == "error" and isinstance(item.get("message"), str):
                errors.append(item["message"])
        elif kind == "error" and isinstance(event.get("message"), str):
            errors.append(event["message"])
        elif kind == "turn.failed" and isinstance(event.get("error"), dict):
            message = event["error"].get("message")
            if isinstance(message, str):
                errors.append(message)
    usage = normalize_codex_usage(totals) if seen_usage else None
    return usage, last_message, errors


def _die_with_parent(parent_pid: int) -> Callable[[], None]:
    """preexec hook: kill codex when the wrapper is killed (Linux only).

    auto-distill's per-call timeout SIGKILLs its direct child — now the
    wrapper — so without this the codex grandchild would outlive the timeout.
    """

    def hook() -> None:
        if not sys.platform.startswith("linux"):
            return
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        except (OSError, AttributeError):
            return
        if os.getppid() != parent_pid:
            os._exit(1)

    return hook


def run_codex_usage_wrapper(
    argv: Sequence[str],
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Run ``codex exec --json`` and translate it for auto-distill (#1857).

    stdout receives only the final answer — the same contract as plain
    ``codex exec -`` — and stderr receives codex's own stderr, any error
    events, and finally one ``PIRI_USAGE=`` line when codex reported usage.
    A run without a usage event emits no line, so auto-distill still records
    ``usage_missing`` instead of a silent zero. The exit code is codex's.
    """

    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if len(argv) != 1 or not argv[0]:
        print(f"usage: model_command.py {CODEX_WRAPPER_FLAG} CODEX_EXECUTABLE", file=stderr)
        return 2
    prompt = stdin.read()
    with tempfile.TemporaryDirectory(prefix="ccc-auto-distill-codex-") as scratch:
        last_path = Path(scratch) / "last-message.txt"
        command = [argv[0], *CODEX_ARGS, "--output-last-message", str(last_path), "-"]
        try:
            completed = runner(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                check=False,
                preexec_fn=_die_with_parent(os.getpid()),
            )
        except OSError as exc:
            print(f"codex-usage-wrapper: cannot spawn codex ({type(exc).__name__})", file=stderr)
            return 127
        usage, fallback, errors = parse_codex_events(completed.stdout or "")
        final = ""
        if last_path.is_file():
            final = last_path.read_text(encoding="utf-8", errors="replace")
        if not final.strip():
            final = fallback or ""
    # Error events first: auto-distill keeps only the first 200 stderr chars
    # as the failure reason.
    for message in errors:
        print(f"codex error: {message}", file=stderr)
    if completed.stderr:
        stderr.write(completed.stderr if completed.stderr.endswith("\n") else completed.stderr + "\n")
    if final:
        stdout.write(final if final.endswith("\n") else final + "\n")
    if usage is not None:
        print(USAGE_LINE_PREFIX + json.dumps(usage, sort_keys=True), file=stderr)
    stdout.flush()
    stderr.flush()
    return completed.returncode


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
        if dedicated not in {"auto", "piri", "claude", "codex"}:
            raise ModelCommandError(
                "CCC_AUTO_DISTILL_PROVIDER must be auto, piri, claude, or codex"
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
    codex_executable, codex_source = _first_runnable(
        (("PATH:codex", "codex"),), which=which
    )
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
    if provider == "codex":
        # A node explicitly identified as codex never falls back to Piri or
        # Claude when the codex CLI is missing (fail-closed, #1295).
        if not codex_executable:
            raise ModelCommandError(
                "provider=codex but no runnable codex CLI was found; "
                "refusing Piri/Claude fallback"
            )
        scratch = codex_scratch_home()
        return ModelCommand(
            codex_wrapper_argv(codex_executable),
            "codex",
            str(codex_source),
            env_overrides=(("CODEX_HOME", str(scratch)),),
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


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [CODEX_WRAPPER_FLAG]:
        return run_codex_usage_wrapper(args[1:])
    print(f"usage: model_command.py {CODEX_WRAPPER_FLAG} CODEX_EXECUTABLE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
