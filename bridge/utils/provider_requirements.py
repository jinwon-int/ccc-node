"""Startup required-environment check for the selected provider (#1771).

#2065 hands the wrapper-only settings (``WRAPPER_ENV_KEYS``) from the merged
config to the ccc-piri / ccc-codex child. Everything else a provider CLI reads
only from its own process environment — notably the Claude OAuth token — must
reach the bridge process itself, through the shared owner-only systemd
EnvironmentFile (``~/.config/ccc-node/bridge.env``) or ``start.sh``'s export.
Secrets are never injected at application level.

This module only CHECKS, at startup, that what the selected provider needs is
resolvable in the environment its child will actually get, and describes a
gap by key NAME. It never returns, logs or stores a value.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from telegram_bot.utils.wrapper_environment import with_wrapper_environment

# Environment-only Claude authentication sources, reported by name.
CLAUDE_AUTH_ENV_NAMES: Final = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)
# Also sufficient, but not suggested in the message.
_CLAUDE_ALTERNATE_AUTH_ENV: Final = (
    "ANTHROPIC_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

# provider -> (cli setting env name, wrapper basename, real-CLI env name,
# wrapper default real CLI). Mirrors ``real_candidate="${VAR:-default}"``.
_WRAPPED_PROVIDERS: Final = {
    "piri": ("CCC_PIRI_CLI_PATH", "ccc-piri", "CCC_PIRI_REAL_CLI_PATH", "piri"),
    "codex": ("CCC_CODEX_CLI_PATH", "ccc-codex", "CCC_CODEX_REAL_CLI_PATH", "codex"),
}
_CLI_SETTING_ATTRIBUTES: Final = {"piri": "piri_cli_path", "codex": "codex_cli_path"}


def _resolve_executable(value: str, environ: Mapping[str, str]) -> str | None:
    candidate = os.path.expanduser(str(value or "").strip())
    if not candidate or candidate.startswith("-"):
        return None
    return shutil.which(candidate, path=environ.get("PATH", os.defpath))


def _claude_has_non_env_auth(
    environ: Mapping[str, str],
    *,
    claude_settings_path: Path | str | None,
    platform: str,
) -> bool:
    if platform == "darwin":
        # The macOS login lives in the keychain, which cannot be probed without
        # the CLI; `claude auth status` stays authoritative there.
        return True
    config_dir = environ.get("CLAUDE_CONFIG_DIR") or (
        str(Path(environ["HOME"]) / ".claude") if environ.get("HOME") else ""
    )
    if config_dir and (Path(config_dir).expanduser() / ".credentials.json").is_file():
        return True
    if claude_settings_path:
        try:
            data = json.loads(Path(claude_settings_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("apiKeyHelper"):
            return True
    return False


def missing_provider_environment(
    provider: str,
    environ: Mapping[str, str],
    *,
    cli_path: str | None = None,
    claude_settings_path: Path | str | None = None,
    platform: str | None = None,
) -> tuple[str, ...]:
    """Names the provider needs but cannot resolve in ``environ``. Never values.

    Each entry is one requirement; ``A|B`` means any one name satisfies it.

    - piri/codex: the configured CLI must resolve on the child ``PATH``; when it
      is the ``ccc-*`` wrapper, the real CLI it execs must resolve too.
    - claude: an environment auth source, unless a non-environment login is
      present (``.credentials.json``, ``apiKeyHelper``, macOS keychain).
    - other providers: nothing (they have their own gates).
    """

    provider = str(provider or "claude").strip().lower()
    if provider in _WRAPPED_PROVIDERS:
        cli_name, wrapper, real_name, real_default = _WRAPPED_PROVIDERS[provider]
        resolved = _resolve_executable(
            cli_path if cli_path is not None else real_default, environ
        )
        if resolved is None:
            return (cli_name,)
        if Path(resolved).name != wrapper:
            return ()
        if _resolve_executable(environ.get(real_name) or real_default, environ) is None:
            return (real_name,)
        return ()
    if provider == "claude":
        if any(environ.get(name) for name in CLAUDE_AUTH_ENV_NAMES + _CLAUDE_ALTERNATE_AUTH_ENV):
            return ()
        if _claude_has_non_env_auth(
            environ,
            claude_settings_path=claude_settings_path,
            platform=sys.platform if platform is None else platform,
        ):
            return ()
        return ("|".join(CLAUDE_AUTH_ENV_NAMES),)
    return ()


def provider_child_environment(settings: Any, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a provider child gets: ``base`` + #2065 wrapper keys."""

    return with_wrapper_environment(os.environ if base is None else base, settings)


def provider_environment_problem(
    settings: Any,
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> str | None:
    """Secret-free, names-only description of a missing requirement, or None.

    ``environ`` defaults to the process environment plus the wrapper keys
    #2065 hands to the child. Never raises: a diagnostic must not take the
    bridge down.
    """

    try:
        provider = str(getattr(settings, "agent_provider", "claude") or "claude").lower()
        attribute = _CLI_SETTING_ATTRIBUTES.get(provider)
        cli_path = getattr(settings, attribute, None) if attribute else None
        missing = missing_provider_environment(
            provider,
            provider_child_environment(settings) if environ is None else environ,
            cli_path=None if cli_path is None else str(cli_path),
            claude_settings_path=getattr(settings, "claude_settings_path", None),
            platform=platform,
        )
    except Exception:
        return None
    if not missing:
        return None
    return (
        "required provider environment missing: "
        f"provider={provider} missing={','.join(missing)}"
    )
