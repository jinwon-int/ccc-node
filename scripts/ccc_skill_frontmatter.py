"""Repository adapter for the canonical bridge skill-frontmatter helper (#2032).

Production setup installs the canonical module itself under this filename.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_CANONICAL_PATH = Path(__file__).resolve().parents[1] / "bridge" / "utils" / "skill_frontmatter.py"
_SPEC = importlib.util.spec_from_file_location("_ccc_canonical_skill_frontmatter", _CANONICAL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("canonical skill-frontmatter module is unavailable")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

FrontmatterError = _MODULE.FrontmatterError
collapse = _MODULE.collapse
double_quote = _MODULE.double_quote
is_block_scalar = _MODULE.is_block_scalar
is_yaml_safe = _MODULE.is_yaml_safe
needs_quoting = _MODULE.needs_quoting
normalize_skill_md = _MODULE.normalize_skill_md
parse_scalar = _MODULE.parse_scalar
render_line = _MODULE.render_line
render_scalar = _MODULE.render_scalar
strict_frontmatter_fields = _MODULE.strict_frontmatter_fields
unquote_scalar = _MODULE.unquote_scalar
