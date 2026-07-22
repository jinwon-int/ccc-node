"""Repository adapter for the canonical bridge secure-fs implementation.

Production setup installs the canonical module itself under this filename.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_CANONICAL_PATH = Path(__file__).resolve().parents[1] / "bridge" / "utils" / "secure_fs.py"
_SPEC = importlib.util.spec_from_file_location("_ccc_canonical_secure_fs", _CANONICAL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("canonical secure-fs module is unavailable")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

atomic_write_bytes_at = _MODULE.atomic_write_bytes_at
fsync_directory_fd = _MODULE.fsync_directory_fd
owner_only_regular_violation = _MODULE.owner_only_regular_violation
