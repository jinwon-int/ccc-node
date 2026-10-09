import argparse
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_bot.utils.channel_environment import (
    channel_selection_blank_overlay,
    without_channel_selection,
)
from telegram_bot.utils.config import Settings, bind_config
from telegram_bot.utils.logging_setup import setup_logging
from telegram_bot.utils.wrapper_environment import (
    missing_wrapper_environment,
    with_wrapper_environment,
)

logger = logging.getLogger(__name__)


def load_runtime_settings(
    *,
    project_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    bot_env_file: Path | str | None = None,
) -> Settings:
    """Load and bind validated settings before runtime modules are imported."""
    settings = Settings.load(
        project_root=project_root,
        environ=environ,
        bot_env_file=bot_env_file,
    )
    bind_config(settings)
    return settings


def report_provider_environment(
    settings: Any, environ: Mapping[str, str] | None = None
) -> str | None:
    """Startup required-env check for the selected provider, names only (#1771).

    Logged as one ERROR at startup instead of surfacing at the first user turn.
    The bridge keeps running degraded — the readiness probe (Telegram) and
    ``health.json`` (Matrix) carry the same text — because exiting would
    crash-loop the unit under ``Restart=always`` and stop push-spool delivery,
    the owner's alert channel. Returns the problem text (or None).
    """
    from telegram_bot.utils.provider_requirements import provider_environment_problem

    if getattr(settings, "agent_provider", None) is None:
        return None
    problem = provider_environment_problem(settings, environ)
    if problem:
        logger.error(
            "%s — put the key in the shared EnvironmentFile "
            "~/.config/ccc-node/bridge.env (secrets) or the project/package .env "
            "(wrapper paths); see docs/bridge-ops.md, provider environment contract",
            problem,
        )
    return problem


@dataclass(frozen=True)
class AppContext:
    """Validated runtime dependencies shared by one bridge application."""

    settings: Settings
    session_store: Any
    session_manager: Any
    distill_journal: Any
    skill_candidate_collector_worker: Any
    distill_snapshot_worker: Any
    distill_extraction_worker: Any
    distill_local_sink_worker: Any
    memory_promoter: Any
    distill_wiki_sink_worker: Any
    project_chat: Any
    agent_runtime: Any
    sdk_factory: Any
    telegram_port: Any
    clock: Any


def _build_piri_runtime(settings: Settings) -> Any:
    """Compose the Piri adapter and its fail-closed audience memory route."""

    from telegram_bot.core.memory_audience import (
        MemoryAudience,
        audience_from_piri_environment,
        shared_memory_audience,
    )
    from telegram_bot.core.piri_runtime import PiriRuntime
    from telegram_bot.memory.distill_types import validate_memory_route

    route_environment_factory: (
        Callable[[str, str], Mapping[str, str]] | None
    ) = None
    memory_environment_validator: (
        Callable[[Mapping[str, str]], object] | None
    ) = None
    if settings.bridge_memory_mode == "audience-scoped":
        shared = shared_memory_audience(settings)

        def build_piri_route_environment(audience: str, scope: str):
            validate_memory_route(audience, scope)
            return MemoryAudience(audience, scope, shared.root).piri_environment(
                settings
            )

        def validate_piri_memory_environment(environment: Mapping[str, str]):
            return audience_from_piri_environment(settings, environment)

        route_environment_factory = build_piri_route_environment
        memory_environment_validator = validate_piri_memory_environment

    logger.info("Piri provider routed through unrestricted PiriRuntime RPC adapter")
    return PiriRuntime(
        executable=settings.piri_cli_path,
        # #1771: the ccc-piri wrapper reads its real-CLI/memory keys only from
        # the process environment; hand over the ones set in the .env files.
        # #2177: the agent's tool shell must not inherit the frontend's
        # channel selection.
        process_environment=without_channel_selection(
            with_wrapper_environment(os.environ, settings)
        ),
        model_catalog_directory=str(Path(settings.project_root).resolve()),
        memory_materializer_path=settings.codex_memory_materializer_path,
        memory_bootstrap_timeout_seconds=(
            settings.codex_memory_bootstrap_timeout_seconds
        ),
        memory_environment_validator=memory_environment_validator,
        route_environment_factory=route_environment_factory,
    )


def _build_session_started_recorder(settings: Settings, manager: Any):
    if settings.agent_provider != "danso":
        return None
    from telegram_bot.core.session_scope import storage_key

    async def record(user_id: int, chat_id: int, ident: str, *, dispatch_guard=None) -> None:
        key = storage_key(settings.telegram_session_scope, user_id, chat_id)
        updates = {"provider": "danso", "session_id": ident, "new_session": False}
        if dispatch_guard is None:
            await manager.patch_session(key, updates=updates)
        elif not await manager.patch_session_if(key, expected={}, updates=updates, guard=dispatch_guard):
            raise ValueError("recovery dispatch expired before session persistence")

    return record


def _build_distill_environment(
    settings: Settings,
    provider: str,
) -> dict[str, str] | None:
    """Return the private Codex extraction environment when one is required."""

    if not (
        provider == "codex"
        and settings.bridge_memory_mode == "audience-scoped"
    ):
        return None
    from telegram_bot.core.memory_audience import shared_memory_audience
    from telegram_bot.utils.secure_fs import ensure_private_directory

    environment = dict(os.environ)
    environment.update(
        shared_memory_audience(settings).codex_environment(settings)
    )
    ensure_private_directory(Path(environment["CODEX_HOME"]))
    ensure_private_directory(Path(environment["CODEX_SQLITE_HOME"]))
    return environment


def _finite_autonomous_spend_gate(
    settings: Settings,
    usage_meter: Any,
    provider: str,
) -> bool:
    """True when ``provider``'s autonomous spend has a real finite budget."""

    provider_budget = int(
        getattr(settings, f"usage_budget_tokens_{provider}", 0) or 0
    )
    return (
        bool(getattr(settings, "usage_meter_enabled", True))
        and provider_budget > 0
        and usage_meter is not None
    )


def _distill_extraction_authorized(
    settings: Settings,
    project_chat: Any,
    provider: str | None,
) -> bool:
    """Fail closed unless autonomous extraction has a real finite spend gate."""

    if provider is None:
        return False
    if _finite_autonomous_spend_gate(
        settings, project_chat.usage_meter, provider
    ) or bool(settings.memory_distill_allow_unbounded):
        return True
    logger.warning(
        "Provider-neutral distill extraction disabled: provider=%s requires "
        "usage metering and a finite autonomous budget (or explicit "
        "CCC_MEMORY_DISTILL_ALLOW_UNBOUNDED=1)",
        provider,
    )
    return False


def _skill_candidate_collection_authorized(
    settings: Settings,
    usage_meter: Any,
    provider: str,
) -> bool:
    """Same fail-closed spend gate as distill extraction.

    Every provider-started collector attempt keeps its worst-case reservation,
    so without a finite budget a persistently failing backend charges the
    shared usage meter without bound. The distill escape hatch is reused: it
    is the operator's one opt-in for unbounded autonomous memory extraction.
    """

    if _finite_autonomous_spend_gate(settings, usage_meter, provider) or bool(
        getattr(settings, "memory_distill_allow_unbounded", False)
    ):
        return True
    logger.warning(
        "Skill-candidate collection disabled: provider=%s requires "
        "usage metering and a finite autonomous budget (or explicit "
        "CCC_MEMORY_DISTILL_ALLOW_UNBOUNDED=1)",
        provider,
    )
    return False


def _build_skill_candidate_collector(
    settings: Settings,
    *,
    distill_journal: Any,
    usage_meter: Any,
) -> Any | None:
    """Compose the Codex/Piri skill-candidate collector, or None (#749)."""

    collector_provider = (
        settings.agent_provider
        if settings.agent_provider in {"codex", "piri", "danso"}
        else None
    )
    if not (
        collector_provider
        and getattr(
            settings,
            f"{collector_provider}_skill_collector_enabled",
            True,
        )
        and distill_journal is not None
    ):
        return None
    if not _skill_candidate_collection_authorized(
        settings, usage_meter, collector_provider
    ):
        return None
    from telegram_bot.memory.skill_candidate import SkillCandidateSink
    from telegram_bot.memory.skill_candidate_backend import (
        CodexExecSkillCandidateBackend,
    )
    from telegram_bot.memory.skill_candidate_worker import (
        SkillCandidateCollectorWorker,
    )

    if collector_provider == "codex":
        backend = CodexExecSkillCandidateBackend(
            model=settings.codex_distill_model,
            timeout_seconds=settings.codex_distill_timeout_seconds,
            audience_auth_mode=settings.codex_audience_auth_mode,
        )
    elif collector_provider == "danso":
        from telegram_bot.memory.danso_backend import DansoSkillCandidateBackend
        from telegram_bot.memory.distill_backend_factory import (
            resolve_distill_model_timeout,
        )

        model, timeout = resolve_distill_model_timeout(settings, "danso")
        backend = DansoSkillCandidateBackend(
            settings,
            model=model,
            timeout_seconds=timeout,
        )
    else:
        from telegram_bot.memory.distill_backend_factory import (
            resolve_distill_model_timeout,
        )
        from telegram_bot.memory.runtime_cli_backend import (
            RuntimeCliSkillCandidateBackend,
        )

        model, timeout = resolve_distill_model_timeout(settings, "piri")
        backend = RuntimeCliSkillCandidateBackend(
            "piri",
            executable=settings.piri_cli_path,
            environment=with_wrapper_environment(os.environ, settings),
            model=model,
            timeout_seconds=timeout,
        )
    return SkillCandidateCollectorWorker(
        journal=distill_journal,
        backend=backend,
        sink=SkillCandidateSink(
            settings.bot_data_dir / "skill-candidates",
            settings.codex_skill_pending_dir,
        ),
        usage_meter=usage_meter,
        provider=collector_provider,
    )


def _build_grok_context(settings: Settings, agent_runtime: Any, telegram_port: Any, clock: Any) -> AppContext:
    from telegram.ext import Application
    from telegram_bot.core.grok_provider import build_grok_runtime, configured_route

    configured_route(settings)
    # No generic resets, prompt decorators, distillation or task runners.
    return AppContext(
        settings=settings, session_store=None, session_manager=None,
        distill_journal=None, skill_candidate_collector_worker=None,
        distill_snapshot_worker=None, distill_extraction_worker=None,
        distill_local_sink_worker=None, memory_promoter=None,
        distill_wiki_sink_worker=None, project_chat=None,
        agent_runtime=agent_runtime if agent_runtime is not None else build_grok_runtime(settings),
        sdk_factory=None, telegram_port=telegram_port or Application.builder,
        clock=clock or time,
    )


def build_context(
    settings: Settings,
    *,
    sdk_factory: Any = None,
    agent_runtime: Any = None,
    telegram_port: Any = None,
    clock: Any = None,
) -> AppContext:
    """Compose dependencies without performing filesystem initialization."""
    bind_config(settings)
    if settings.agent_provider == "grok":
        return _build_grok_context(settings, agent_runtime, telegram_port, clock)
    return _build_standard_context(settings, sdk_factory=sdk_factory, agent_runtime=agent_runtime,
                                   telegram_port=telegram_port, clock=clock)


def _bind_resume_diagnostics(agent_runtime: Any, health_reporter: Any) -> None:
    """Attach the optional body-free resume observer without provider RPCs."""
    setter = getattr(agent_runtime, "set_resume_diagnostics_observer", None)
    if callable(setter):
        setter(health_reporter.record_codex_resume_diagnostics)


def _build_standard_context(
    settings: Settings, *, sdk_factory: Any = None, agent_runtime: Any = None,
    telegram_port: Any = None, clock: Any = None,
) -> AppContext:
    from telegram.ext import Application
    from telegram_bot.core.project_chat import ProjectChatHandler
    from telegram_bot.memory.distill_journal import DistillJournal
    from telegram_bot.session.manager import SessionManager
    from telegram_bot.session.store import SessionStore
    from telegram_bot.utils.chat_logger import bind_logs_dir
    from telegram_bot.utils.health import health_reporter

    if settings.agent_provider == "crush" and agent_runtime is None:
        from telegram_bot.core.crush_runtime import CrushRuntime

        from telegram_bot.core import tool_policy

        # Mirror the Codex mapping: the same operator policy that gives Codex
        # approval=never pre-approves crush's tools, so crush does not
        # round-trip an approval the bridge would allow anyway (#940 follow-up).
        _profile = tool_policy.resolve_execution_profile(
            settings.execution_profile,
            allowed_user_ids=settings.allowed_user_ids,
            require_allowlist=settings.require_allowlist,
        )
        _bash = tool_policy.effective_bash_policy(
            tool_policy.resolve_bash_policy(settings.bash_policy), _profile
        )
        agent_runtime = CrushRuntime(
            executable=settings.crush_cli_path,
            config_path=settings.crush_config_path,
            preapprove_tools=_bash == tool_policy.BASH_AUTO_APPROVE,
        )
    elif settings.agent_provider == "codex" and agent_runtime is None:
        from telegram_bot.core.codex_runtime import CodexRuntime
        from telegram_bot.utils.memory_policy import MEMORY_MODE_AUDIENCE_SCOPED

        def build_codex_runtime(process_environment=None):
            # #1771: the ccc-codex wrapper reads its real-CLI/materializer keys
            # only from the process environment. Add the ones configured in the
            # .env files that os.environ lacks; an explicit value always wins.
            # #2177: CodexRuntime merges os.environ under this overlay, so the
            # frontend's channel selection is blanked rather than removed.
            wrapper_overlay = {
                **channel_selection_blank_overlay(os.environ),
                **missing_wrapper_environment(settings, os.environ),
            }
            if process_environment is None:
                # Without an overlay the runtime keeps inheriting os.environ
                # exactly as before; only a configured key opts into the
                # explicit child environment.
                overlay_kwargs = (
                    {"process_environment": wrapper_overlay} if wrapper_overlay else {}
                )
                return CodexRuntime(
                    cli_path=settings.codex_cli_path,
                    working_state_environment=os.environ,
                    memory_materializer_path=settings.codex_memory_materializer_path,
                    memory_bootstrap_timeout_seconds=(
                        settings.codex_memory_bootstrap_timeout_seconds
                    ),
                    **overlay_kwargs,
                )
            from telegram_bot.utils.secure_fs import ensure_private_directory

            ensure_private_directory(Path(process_environment["CODEX_HOME"]))
            ensure_private_directory(Path(process_environment["CODEX_SQLITE_HOME"]))
            return CodexRuntime(
                cli_path=settings.codex_cli_path,
                process_environment={**wrapper_overlay, **process_environment},
                memory_materializer_path=settings.codex_memory_materializer_path,
                memory_bootstrap_timeout_seconds=(
                    settings.codex_memory_bootstrap_timeout_seconds
                ),
            )

        if settings.bridge_memory_mode == MEMORY_MODE_AUDIENCE_SCOPED:
            from telegram_bot.core.codex_runtime_pool import CodexRuntimePool
            from telegram_bot.core.memory_audience import shared_memory_audience

            shared = shared_memory_audience(settings)

            def route_environment(audience: str, scope: str):
                from telegram_bot.core.memory_audience import MemoryAudience
                from telegram_bot.memory.distill_types import validate_memory_route

                validate_memory_route(audience, scope)
                return MemoryAudience(audience, scope, shared.root).codex_environment(
                    settings
                )

            agent_runtime = CodexRuntimePool(
                shared_environment=shared.codex_environment(settings),
                runtime_factory=build_codex_runtime,
                route_environment_factory=route_environment,
            )
        else:
            agent_runtime = build_codex_runtime()
    elif settings.agent_provider == "claude" and agent_runtime is None:
        # #346/#584 cutover complete (slice C-2): the Claude provider always
        # routes through the provider-neutral ClaudeRuntime adapter; the legacy
        # direct SDK stream path and its CCC_CLAUDE_RUNTIME_ADAPTER kill-switch
        # are gone (rollback = git revert). The transcripts browsing directory
        # matches ~/.claude/projects (ProjectChatHandler.conversations_dir).
        from telegram_bot.core.claude_runtime import ClaudeRuntime
        from telegram_bot.core.conversation_paths import claude_project_dir_name

        logger.info("Claude provider routed through ClaudeRuntime adapter (#346)")
        agent_runtime = ClaudeRuntime(
            sdk_client_factory=sdk_factory,
            settings=settings,
            transcripts_dir=Path.home()
            / ".claude"
            / "projects"
            / claude_project_dir_name(Path(settings.project_root).resolve()),
        )
    elif settings.agent_provider == "piri" and agent_runtime is None:
        agent_runtime = _build_piri_runtime(settings)
    elif settings.agent_provider == "danso" and agent_runtime is None:
        from telegram_bot.core.danso_runtime import build_danso_runtime

        agent_runtime = build_danso_runtime(settings)
    telegram_port = telegram_port or Application.builder
    clock = clock or time
    bind_logs_dir(settings.logs_dir)
    health_reporter.bind(
        settings.bot_data_dir,
        settings.agent_provider,
        settings.dead_session_wakeup,
    )
    _bind_resume_diagnostics(agent_runtime, health_reporter)
    store = SessionStore(settings.session_store_path)
    session_manager = SessionManager(store=store, settings=settings)
    # Enabling Danso must not consume or mutate another provider's backlog.
    journal_name = "danso-distill-journal" if settings.agent_provider == "danso" else "distill-journal"
    distill_journal = DistillJournal(settings.bot_data_dir / journal_name)
    project_chat = ProjectChatHandler(
        settings=settings,
        agent_runtime=agent_runtime,
        clock=clock,
        session_started_recorder=_build_session_started_recorder(settings, session_manager),
        # The frontend name namespaces private memory audiences (#1780);
        # "telegram" keeps the historical digest.
        memory_route=settings.channel,
    )
    # Production distill extraction composition (#465 scheduling consumes
    # this): the worker is built only through the handler factory so its
    # autonomous spend is always gated by the shared usage meter (#388).
    from telegram_bot.memory.distill_backend_factory import (
        build_distill_backend,
        resolve_distill_model_timeout,
        resolve_distill_provider,
    )

    audience_scoped = settings.bridge_memory_mode == "audience-scoped"
    wiki_enabled = (
        settings.node_isolation_profile != "external"
        and settings.wiki_memory_enabled
    )
    distill_provider = resolve_distill_provider(
        settings.agent_provider,
        settings.memory_distill_provider,
    )
    if settings.agent_provider == "danso" and settings.bridge_memory_mode != "audience-scoped":
        distill_provider = None
    distill_environment = (
        _build_distill_environment(settings, distill_provider)
        if distill_provider is not None
        else None
    )
    extraction_wiki_enabled = wiki_enabled and distill_provider != "danso"
    distill_extraction_worker = None
    if _distill_extraction_authorized(settings, project_chat, distill_provider):
        from telegram_bot.memory.distill_guard import DistillGuard

        assert distill_provider is not None
        distill_model, _distill_timeout = resolve_distill_model_timeout(
            settings, distill_provider
        )
        distill_extraction_worker = project_chat.build_distill_extraction_worker(
            distill_journal,
            build_distill_backend(
                settings,
                provider=distill_provider,
                wiki_enabled=extraction_wiki_enabled,
                codex_environment=distill_environment,
            ),
            wiki_enabled=extraction_wiki_enabled,
            extractor_provider=distill_provider,
            model=distill_model,
            guard=DistillGuard(),
            max_attempts=settings.memory_distill_max_attempts,
            provider_cooldown_seconds=(
                settings.memory_distill_provider_cooldown_seconds
            ),
            retry_backoff_base_seconds=(
                settings.memory_distill_retry_backoff_base_seconds
            ),
            retry_backoff_max_seconds=(
                settings.memory_distill_retry_backoff_max_seconds
            ),
        )
    distill_snapshot_worker = None
    if (
        distill_provider is not None
        and settings.agent_provider in {"claude", "codex", "piri", "danso"}
    ):
        from telegram_bot.memory.codex_snapshot import CodexThreadSnapshotter

        distill_snapshot_worker = CodexThreadSnapshotter(
            distill_journal,
            agent_runtime,
        )
    distill_local_sink_worker = None
    memory_promoter = None
    if settings.bridge_memory_mode == "audience-scoped":
        from telegram_bot.core.memory_audience import shared_memory_audience
        from telegram_bot.memory.distill_local_worker import (
            CodexDistillLocalSinkWorker,
        )

        distill_local_sink_worker = CodexDistillLocalSinkWorker(
            distill_journal,
            environment=with_wrapper_environment(os.environ, settings),
            audience_root=shared_memory_audience(settings).root,
            indexer_path=(
                Path(settings.codex_memory_materializer_path).expanduser().parent
                / "ccc-memory-index.sh"
            ),
            nunchi_feed_path=(
                Path(settings.codex_memory_materializer_path).expanduser().parent
                / "nunchi"
                / "piri-feed.sh"
                if settings.agent_provider == "piri"
                else None
            ),
        )
        from telegram_bot.memory.promotion import CodexMemoryPromoter

        memory_promoter = CodexMemoryPromoter(
            shared_memory_audience(settings).root,
        )
    distill_wiki_sink_worker = None
    if (
        distill_provider is not None
        and settings.agent_provider in {"claude", "codex", "piri"}
        and wiki_enabled
    ):
        from telegram_bot.memory.distill_wiki_worker import (
            CodexDistillWikiSinkWorker,
        )

        distill_wiki_sink_worker = CodexDistillWikiSinkWorker(
            distill_journal,
            queue_dir=settings.bot_data_dir / "wiki-candidates",
            require_memory_route=audience_scoped,
        )
    # Default-on Codex/Piri skill-candidate collector (#749, piri parity).
    # Three-guard per provider: matching node provider, no explicit opt-out, and
    # a distill journal to read snapshots from. Claude composition is unchanged
    # and installation remains approve-first.
    skill_candidate_collector_worker = _build_skill_candidate_collector(
        settings,
        distill_journal=distill_journal,
        usage_meter=project_chat.usage_meter,
    )

    return AppContext(
        settings=settings,
        session_store=store,
        session_manager=session_manager,
        distill_journal=distill_journal,
        skill_candidate_collector_worker=skill_candidate_collector_worker,
        distill_snapshot_worker=distill_snapshot_worker,
        distill_extraction_worker=distill_extraction_worker,
        distill_local_sink_worker=distill_local_sink_worker,
        memory_promoter=memory_promoter,
        distill_wiki_sink_worker=distill_wiki_sink_worker,
        project_chat=project_chat,
        agent_runtime=agent_runtime,
        sdk_factory=sdk_factory,
        telegram_port=telegram_port,
        clock=clock,
    )


def create_app(context: AppContext):
    """Create the chat frontend from an already-built application context."""
    if context.settings.agent_provider == "grok":
        if getattr(context.settings, "channel", "telegram") == "matrix":
            # Same restricted Grok contract (owner only, text only, one turn),
            # served through the E2EE Matrix transport in the owner's direct room.
            from telegram_bot.core.grok_matrix_bot import GrokMatrixBot

            return GrokMatrixBot(context.settings, context.agent_runtime)
        from telegram_bot.core.grok_bot import GrokTelegramBot

        return GrokTelegramBot(context.settings, context.agent_runtime, context.telegram_port)
    if getattr(context.settings, "channel", "telegram") == "matrix":
        # Same ProjectChatHandler, E2EE Matrix transport instead of Telegram (#1780).
        from telegram_bot.core.matrix.bot import MatrixBot

        return MatrixBot(
            context.settings,
            project_chat=context.project_chat,
            session_manager=context.session_manager,
            clock=context.clock,
            distill_journal=context.distill_journal,
            distill_snapshot_worker=context.distill_snapshot_worker,
            distill_extraction_worker=context.distill_extraction_worker,
            distill_local_sink_worker=context.distill_local_sink_worker,
            distill_wiki_sink_worker=context.distill_wiki_sink_worker,
            skill_candidate_collector_worker=context.skill_candidate_collector_worker,
            memory_promoter=context.memory_promoter,
        )
    from telegram_bot.core.bot import TelegramBot

    return TelegramBot(
        settings=context.settings,
        session_manager=context.session_manager,
        project_chat=context.project_chat,
        distill_journal=context.distill_journal,
        skill_candidate_collector_worker=context.skill_candidate_collector_worker,
        distill_snapshot_worker=context.distill_snapshot_worker,
        distill_extraction_worker=context.distill_extraction_worker,
        distill_local_sink_worker=context.distill_local_sink_worker,
        memory_promoter=context.memory_promoter,
        distill_wiki_sink_worker=context.distill_wiki_sink_worker,
        application_builder_factory=context.telegram_port,
        clock=context.clock,
    )


def create_bot(settings: Settings):
    """Compatibility entrypoint for validated Settings callers."""
    return create_app(build_context(settings))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", help="Project path")
    parser.add_argument("--path", dest="path_opt", help="Project path")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    if args.debug:
        os.environ["BOT_DEBUG"] = "1"

    path = args.path_opt or args.path
    if path:
        os.environ["PROJECT_ROOT"] = str(Path(path).expanduser().resolve())

    if "PROJECT_ROOT" not in os.environ:
        print(
            "Error: Please specify project path via argument or PROJECT_ROOT environment variable"
        )
        raise SystemExit(1)

    settings = load_runtime_settings()
    os.environ.update(settings.hook_policy_environment())
    bot = create_bot(settings)

    bot.validate_runtime_paths()
    setup_logging(settings)
    report_provider_environment(settings)
    try:
        bot.run()
    except SystemExit as exc:
        # A clean exit must stay clean. Rewriting every SystemExit to 1 told
        # systemd that an orderly shutdown had failed, so a Restart=on-failure
        # unit would bounce a bridge that meant to stop (and the operator saw a
        # failed unit for a successful stop). Only a genuinely non-zero /
        # message-bearing exit is an error.
        if exc.code is None or str(exc.code) == "0":
            raise
        logger.error(str(exc.code))
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
