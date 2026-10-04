"""Repository adapter; setup installs the canonical module under this name."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "bridge/utils/report_style.py"
_SPEC = importlib.util.spec_from_file_location("_ccc_report_style", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("report-style module is unavailable")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
read_report_style = _MODULE.read_report_style
