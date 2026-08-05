"""Resolve and compose the extractor backend independently of the contract."""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any, Literal, cast

from .codex_exec_backend import CodexExecDistillBackend
from .distill_extraction import DistillBackend
from .runtime_cli_backend import RuntimeCliDistillBackend

DistillProvider = Literal["claude", "codex", "piri"]


def resolve_distill_provider(
    main_provider: str,
    configured_provider: str,
) -> DistillProvider | None:
    """Resolve ``auto`` to the main runtime; unsupported runtimes stay off."""

    main = str(main_provider or "").strip().lower()
    configured = str(configured_provider or "").strip().lower()
    if configured == "off":
        return None
    if configured == "auto":
        return (
            cast(DistillProvider, main)
            if main in {"claude", "codex", "piri"}
            else None
        )
    if configured not in {"claude", "codex", "piri"}:
        raise ValueError("unsupported distill provider")
    return cast(DistillProvider, configured)


def build_distill_backend(
    settings: Any,
    *,
    provider: DistillProvider,
    wiki_enabled: bool,
    codex_environment: Mapping[str, str] | None = None,
) -> DistillBackend:
    """Build one isolated backend behind the shared ``DistillBackend`` seam."""

    model, timeout = resolve_distill_model_timeout(settings, provider)
    if provider == "codex":
        return CodexExecDistillBackend(
            executable=settings.codex_cli_path,
            wiki_enabled=wiki_enabled,
            environment=codex_environment,
            model=model,
            timeout_seconds=timeout,
        )
    if provider == "claude":
        executable = (
            str(settings.claude_cli_path)
            if settings.claude_cli_path
            else "claude"
        )
    else:
        executable = settings.piri_cli_path
    return RuntimeCliDistillBackend(
        provider,
        executable=executable,
        wiki_enabled=wiki_enabled,
        environment=os.environ,
        model=model,
        timeout_seconds=timeout,
    )


def resolve_distill_model_timeout(
    settings: Any,
    provider: DistillProvider,
) -> tuple[str, float]:
    """Resolve generic settings while honoring legacy Codex-only overrides."""

    model = settings.memory_distill_model
    timeout = settings.memory_distill_timeout_seconds
    if provider == "codex":
        if (
            model == "provider-default"
            and settings.codex_distill_model != "provider-default"
        ):
            model = settings.codex_distill_model
        if timeout == 120.0 and settings.codex_distill_timeout_seconds != 120.0:
            timeout = settings.codex_distill_timeout_seconds
    return model, timeout


__all__ = [
    "DistillProvider",
    "build_distill_backend",
    "resolve_distill_model_timeout",
    "resolve_distill_provider",
]
