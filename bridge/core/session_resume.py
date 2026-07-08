"""Persisted-session resume policy after a bridge restart.

After a bridge restart the in-memory ``_runtime_active_sessions`` set is
empty, so historically every persisted session_id was rejected and each
conversation started blank ("memory loss", diagnosed as PATH1). This module
holds the pure decision helpers: a persisted session_id may be auto-resumed
when its SDK conversation transcript still exists on disk. An environment
off-switch (``CCC_RESUME_PERSISTED_SESSIONS=false``) restores the old
never-resume behavior.
"""

import os
from pathlib import Path
from typing import Mapping, Optional

from telegram_bot.core.conversation_paths import resolve_conversation_file

_FALSE_VALUES = {"false", "0", "no", "off"}

ENV_FLAG = "CCC_RESUME_PERSISTED_SESSIONS"


def resume_persisted_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """True unless CCC_RESUME_PERSISTED_SESSIONS is set to a false-y value."""
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_FLAG, "true")).strip().lower()
    return raw not in _FALSE_VALUES


def persisted_transcript_exists(
    conversations_dir: Optional[Path], session_id: Optional[str]
) -> bool:
    """True when the SDK JSONL transcript for session_id exists under conversations_dir.

    Uses resolve_conversation_file so a malicious/corrupt session_id cannot
    escape the conversations directory. Any filesystem error means "no".
    """
    if not conversations_dir or not session_id:
        return False
    try:
        filepath = resolve_conversation_file(conversations_dir, str(session_id))
        return bool(filepath and filepath.is_file())
    except OSError:
        return False
