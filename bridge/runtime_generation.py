"""Capture startup source/interpreter provenance without asserting readiness."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import sys

from .prepared_runtime import bounded_read, source_seal
from .runtime_readiness import git_identity


def capture_runtime_generation() -> dict:
    """Observe this process, not launcher environment variables or .env files.

    The dependency value is the last bootstrap receipt, not a new verification
    of installed package contents. Preserve this snapshot across heartbeats;
    source edits after startup must not relabel an already running process.
    """
    source = Path(__file__).resolve().parent
    prefix = Path(sys.prefix).resolve()
    result = {
        "schema": "ccc.runtime-generation.v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source),
        "source_git": git_identity(source, timeout=1.0),
        "source_seal": None,
        "python_executable": sys.executable,
        "python_prefix": str(prefix),
        "dependency_fingerprint": None,
        "collection_errors": [],
    }
    try:
        result["source_seal"] = source_seal(source)
    except (OSError, ValueError):
        result["collection_errors"].append("source_seal_unavailable")
    try:
        value = bounded_read(prefix / ".req_hash", 256).decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("invalid_fingerprint")
        result["dependency_fingerprint"] = value
    except (OSError, ValueError, UnicodeError):
        result["collection_errors"].append("dependency_fingerprint_unavailable")
    return result
