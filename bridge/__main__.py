import argparse
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_bot.utils.config import Settings, bind_config
from telegram_bot.utils.logging_setup import setup_logging

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


@dataclass(frozen=True)
class AppContext:
    """Validated runtime dependencies shared by one bridge application."""

    settings: Settings
    session_store: Any
    session_manager: Any
    distill_journal: Any
    distill_snapshot_worker: Any
    distill_extraction_worker: Any
    distill_local_sink_worker: Any
    distill_wiki_sink_worker: Any
    distill_honcho_sink_worker: Any
    project_chat: Any
    agent_runtime: Any
    sdk_factory: Any
    telegram_port: Any
    clock: Any


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
    from telegram.ext import Application
    from telegram_bot.core.project_chat import ProjectChatHandler
    from telegram_bot.memory.distill_journal import DistillJournal
    from telegram_bot.session.manager import SessionManager
    from telegram_bot.session.store import SessionStore
    from telegram_bot.utils.chat_logger import bind_logs_dir
    from telegram_bot.utils.health import health_reporter

    if settings.agent_provider == "codex" and agent_runtime is None:
        from telegram_bot.core.codex_runtime import CodexRuntime
        from telegram_bot.utils.memory_policy import MEMORY_MODE_AUDIENCE_SCOPED

        def build_codex_runtime(process_environment=None):
            if process_environment is None:
                return CodexRuntime(
                    cli_path=settings.codex_cli_path,
                    memory_materializer_path=settings.codex_memory_materializer_path,
                    memory_bootstrap_timeout_seconds=(
                        settings.codex_memory_bootstrap_timeout_seconds
                    ),
                )
            from telegram_bot.utils.secure_fs import ensure_private_directory

            ensure_private_directory(Path(process_environment["CODEX_HOME"]))
            ensure_private_directory(Path(process_environment["CODEX_SQLITE_HOME"]))
            return CodexRuntime(
                cli_path=settings.codex_cli_path,
                process_environment=process_environment,
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
    telegram_port = telegram_port or Application.builder
    clock = clock or time
    bind_logs_dir(settings.logs_dir)
    health_reporter.bind(settings.bot_data_dir, settings.agent_provider)
    store = SessionStore(settings.session_store_path)
    session_manager = SessionManager(store=store, settings=settings)
    distill_journal = DistillJournal(settings.bot_data_dir / "distill-journal")
    project_chat = ProjectChatHandler(
        settings=settings,
        agent_runtime=agent_runtime,
        clock=clock,
    )
    # Production distill extraction composition (#465 scheduling consumes
    # this): the worker is built only through the handler factory so its
    # autonomous spend is always gated by the shared usage meter (#388).
    from telegram_bot.memory.codex_exec_backend import CodexExecDistillBackend

    audience_scoped = settings.bridge_memory_mode == "audience-scoped"
    wiki_enabled = (
        not audience_scoped
        and settings.node_isolation_profile != "external"
        and settings.wiki_memory_enabled
    )
    honcho_enabled = (
        not audience_scoped
        and settings.honcho_memory_enabled
    )
    distill_environment = None
    if (
        settings.agent_provider == "codex"
        and settings.bridge_memory_mode == "audience-scoped"
    ):
        from telegram_bot.core.memory_audience import shared_memory_audience

        distill_environment = dict(os.environ)
        distill_environment.update(
            shared_memory_audience(settings).codex_environment(settings)
        )
        from telegram_bot.utils.secure_fs import ensure_private_directory

        ensure_private_directory(Path(distill_environment["CODEX_HOME"]))
        ensure_private_directory(Path(distill_environment["CODEX_SQLITE_HOME"]))
    distill_extraction_worker = project_chat.build_distill_extraction_worker(
        distill_journal,
        CodexExecDistillBackend(
            wiki_enabled=wiki_enabled,
            environment=distill_environment,
            model=settings.codex_distill_model,
            timeout_seconds=settings.codex_distill_timeout_seconds,
        ),
        wiki_enabled=wiki_enabled,
        honcho_enabled=honcho_enabled,
        model=settings.codex_distill_model,
    )
    distill_snapshot_worker = None
    if settings.agent_provider == "codex":
        from telegram_bot.memory.codex_snapshot import CodexThreadSnapshotter

        distill_snapshot_worker = CodexThreadSnapshotter(
            distill_journal,
            agent_runtime,
        )
    distill_local_sink_worker = None
    if settings.bridge_memory_mode == "audience-scoped":
        from telegram_bot.core.memory_audience import shared_memory_audience
        from telegram_bot.memory.distill_local_worker import (
            CodexDistillLocalSinkWorker,
        )

        distill_local_sink_worker = CodexDistillLocalSinkWorker(
            distill_journal,
            audience_root=shared_memory_audience(settings).root,
            indexer_path=(
                Path(settings.codex_memory_materializer_path).expanduser().parent
                / "ccc-memory-index.sh"
            ),
        )
    distill_wiki_sink_worker = None
    if settings.agent_provider == "codex" and wiki_enabled:
        from telegram_bot.memory.distill_wiki_worker import (
            CodexDistillWikiSinkWorker,
        )

        distill_wiki_sink_worker = CodexDistillWikiSinkWorker(
            distill_journal,
            queue_dir=settings.bot_data_dir / "wiki-candidates",
        )
    distill_honcho_sink_worker = None
    if settings.agent_provider == "codex" and honcho_enabled:
        from telegram_bot.memory.distill_honcho_worker import (
            CodexDistillHonchoSinkWorker,
            HonchoHttpSender,
        )

        distill_honcho_sink_worker = CodexDistillHonchoSinkWorker(
            distill_journal,
            outbox_dir=settings.bot_data_dir / "honcho-outbox",
            sender=HonchoHttpSender(
                settings.honcho_config_path,
                node_label=os.environ.get("CCC_NODE", "ccc-node"),
            ),
        )

    return AppContext(
        settings=settings,
        session_store=store,
        session_manager=session_manager,
        distill_journal=distill_journal,
        distill_snapshot_worker=distill_snapshot_worker,
        distill_extraction_worker=distill_extraction_worker,
        distill_local_sink_worker=distill_local_sink_worker,
        distill_wiki_sink_worker=distill_wiki_sink_worker,
        distill_honcho_sink_worker=distill_honcho_sink_worker,
        project_chat=project_chat,
        agent_runtime=agent_runtime,
        sdk_factory=sdk_factory,
        telegram_port=telegram_port,
        clock=clock,
    )


def create_app(context: AppContext):
    """Create the Telegram adapter from an already-built application context."""
    from telegram_bot.core.bot import TelegramBot

    return TelegramBot(
        settings=context.settings,
        session_manager=context.session_manager,
        project_chat=context.project_chat,
        distill_journal=context.distill_journal,
        distill_snapshot_worker=context.distill_snapshot_worker,
        distill_extraction_worker=context.distill_extraction_worker,
        distill_local_sink_worker=context.distill_local_sink_worker,
        distill_wiki_sink_worker=context.distill_wiki_sink_worker,
        distill_honcho_sink_worker=context.distill_honcho_sink_worker,
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
    try:
        bot.run()
    except SystemExit as exc:
        if exc.code and str(exc.code) != "0":
            logger.error(str(exc.code))
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
