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

SecureFsError = _MODULE.SecureFsError
acquire_flock = _MODULE.acquire_flock
append_jsonl_line = _MODULE.append_jsonl_line
atomic_write_bytes = _MODULE.atomic_write_bytes
atomic_write_bytes_at = _MODULE.atomic_write_bytes_at
atomic_write_text = _MODULE.atomic_write_text
bounded_int_env = _MODULE.bounded_int_env
flock_guard = _MODULE.flock_guard
fsync_directory_fd = _MODULE.fsync_directory_fd
json_line = _MODULE.json_line
open_lock_descriptor = _MODULE.open_lock_descriptor
owner_only_regular_violation = _MODULE.owner_only_regular_violation
parse_jsonl_rows = _MODULE.parse_jsonl_rows
read_jsonl_rows = _MODULE.read_jsonl_rows
read_owner_only_bytes = _MODULE.read_owner_only_bytes
utc_now_iso = _MODULE.utc_now_iso
