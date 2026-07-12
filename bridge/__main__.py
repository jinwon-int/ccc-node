import argparse
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from telegram_bot.utils.config import Settings, bind_config, setup_logging


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
    from telegram_bot.core.bot import bot

    setup_logging(settings)
    logger = logging.getLogger(__name__)
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
