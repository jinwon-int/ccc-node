#!/usr/bin/env python3
"""Installed/source-checkout entry point for the canonical pending journal."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_canonical_module():
    installed = Path(__file__).resolve().parents[1] / "ccc_distill_pending_journal.py"
    source = Path(__file__).resolve().parents[3] / "bridge/memory/distill_pending_journal.py"
    module_path = installed if installed.is_file() else source
    if module_path == installed:
        # The canonical module's colocated secure-fs dependency lives one level
        # above this entry point's script directory in an installed hook tree.
        sys.path.insert(0, str(installed.parent))
    spec = importlib.util.spec_from_file_location("ccc_distill_pending_journal", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("pending distill journal module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(_load_canonical_module().main())
