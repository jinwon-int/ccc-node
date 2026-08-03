"""Keep operator configuration discovery aligned with ``Settings`` aliases."""

import ast
import re
from pathlib import Path


BRIDGE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BRIDGE_DIR / "utils" / "config.py"
ENV_EXAMPLE_PATH = BRIDGE_DIR / ".env.example"
SETUP_PATH = BRIDGE_DIR / "setup.sh"
CCC_NAME = re.compile(r"\bCCC_[A-Z0-9_]+\b")


def _ccc_field_aliases(source: str) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        is_field = (
            isinstance(node.func, ast.Name)
            and node.func.id == "Field"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "Field"
        )
        if not is_field:
            continue
        for keyword in node.keywords:
            value = keyword.value
            if (
                keyword.arg == "alias"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and CCC_NAME.fullmatch(value.value)
            ):
                aliases.add(value.value)
    return aliases


def test_every_ccc_settings_alias_is_documented_in_env_example():
    aliases = _ccc_field_aliases(CONFIG_PATH.read_text(encoding="utf-8"))
    documented = set(
        CCC_NAME.findall(ENV_EXAMPLE_PATH.read_text(encoding="utf-8"))
    )

    assert aliases, "no CCC_* Field aliases found; config drift check is not effective"
    missing = sorted(aliases - documented)
    assert not missing, (
        "bridge/.env.example is missing Settings aliases:\n"
        + "\n".join(f"- {name}" for name in missing)
    )


def test_timeout_examples_preserve_the_runtime_invariant():
    env_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    setup = SETUP_PATH.read_text(encoding="utf-8")

    for source in (env_example, setup):
        assert "CLAUDE_PROCESS_TIMEOUT=21600" in source
        assert "CCC_DELEGATED_TASK_STALL_SECONDS=7200" in source
