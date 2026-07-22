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
    distill_extraction_worker: Any
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

        agent_runtime = CodexRuntime(
            cli_path=settings.codex_cli_path,
            memory_materializer_path=settings.codex_memory_materializer_path,
            memory_bootstrap_timeout_seconds=(settings.codex_memory_bootstrap_timeout_seconds),
        )
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

    wiki_enabled = (
        settings.node_isolation_profile != "external" and settings.wiki_memory_enabled
    )
    distill_extraction_worker = project_chat.build_distill_extraction_worker(
        distill_journal,
        CodexExecDistillBackend(wiki_enabled=wiki_enabled),
        wiki_enabled=wiki_enabled,
    )
    return AppContext(
        settings=settings,
        session_store=store,
        session_manager=session_manager,
        distill_journal=distill_journal,
        distill_extraction_worker=distill_extraction_worker,
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
        distill_extraction_worker=context.distill_extraction_worker,
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
