"""Cron adapter for the existing node-local Danso auth, with no agent tools."""

import asyncio
from io import StringIO
import json
import os
from pathlib import Path
import sys
from dotenv import dotenv_values

from telegram_bot.memory.danso_backend import DansoDistillBackend
from telegram_bot.utils.config import Config
from telegram_bot.utils.secure_fs import ensure_private_directory, read_owner_only_bytes


def load_settings():
    # The managed bridge service's protected EnvironmentFile is the credential
    # owner. Cron does not inherit systemd's environment. Read that same file
    # locally; never scrape /proc or copy credentials into another account.
    config = Path.home() / ".config/ccc-node/nunchi-judge.json"
    if config.exists() or config.is_symlink():
        ensure_private_directory(config.parent)
        raw, _ = read_owner_only_bytes(config, max_bytes=16384, unsafe_mode_mask=0o077)
        manifest = json.loads(raw)
        sources = manifest.get("environment_files") if isinstance(manifest, dict) else None
        if not isinstance(sources, list) or not 1 <= len(sources) <= 8 or any(
            not isinstance(p, str) or not Path(p).is_absolute() for p in sources
        ):
            raise ValueError("invalid judge environment manifest")
    else:
        sources = [os.environ.get("CCC_NUNCHI_JUDGE_ENV_FILE") or
                   str(Path.home() / ".config/ccc-node/bridge.env")]
    values = {}
    for name in sources:
        source = Path(name)
        ensure_private_directory(source.parent)
        raw, _ = read_owner_only_bytes(source, max_bytes=262144, unsafe_mode_mask=0o077)
        values.update({key: value for key, value in dotenv_values(stream=StringIO(raw.decode()), interpolate=False).items()
                       if value is not None and (key.startswith(("CCC_DANSO_", "DANSO_")) or
                          key in {"ZAI_API_KEY", "OPENAI_API_KEY", "PROJECT_ROOT"})})
    values.update(os.environ)
    return Config.load(project_root=values.get("PROJECT_ROOT") or Path.home(), environ=values)


async def judge(prompt: bytes, settings, *, timeout: float = 120):
    if not prompt or len(prompt) > 32768:
        raise ValueError("judge input outside bounds")
    backend = DansoDistillBackend(settings, wiki_enabled=False, model="provider-default",
                                 timeout_seconds=timeout)
    return await backend.generate(prompt, "Judge only the supplied reference. Return only verdict JSON.")


def main():
    try:
        prompt = sys.stdin.buffer.read(32769)
        settings = load_settings()
        timeout = min(600, max(10, int(os.environ.get("NUNCHI_JUDGE_TIMEOUT_SEC", "120"))))
        payload = asyncio.run(judge(prompt, settings, timeout=timeout))
        sys.stdout.buffer.write(payload)
        return 0
    except Exception:
        # Config validation and provider diagnostics may contain credential
        # values. Only a fixed body-free failure code crosses this boundary.
        print("nunchi_danso_judge_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
