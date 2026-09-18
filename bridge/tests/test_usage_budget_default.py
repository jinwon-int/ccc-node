"""The doctor must judge the same default budget the bridge runs with."""

from __future__ import annotations

import ast
from pathlib import Path

from utils import config as bridge_config


def _doctor_constant() -> int:
    doctor = Path(__file__).resolve().parents[2] / "scripts" / "ccc_doctor.py"
    tree = ast.parse(doctor.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "USAGE_BUDGET_TOKENS_DEFAULT":
                    return int(ast.literal_eval(node.value))
    raise AssertionError("scripts/ccc_doctor.py lost USAGE_BUDGET_TOKENS_DEFAULT")


def _doctor_piri_constant() -> int:
    doctor = Path(__file__).resolve().parents[2] / "scripts" / "ccc_doctor.py"
    tree = ast.parse(doctor.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "USAGE_BUDGET_TOKENS_PIRI_DEFAULT":
                    return int(ast.literal_eval(node.value))
    raise AssertionError("scripts/ccc_doctor.py lost USAGE_BUDGET_TOKENS_PIRI_DEFAULT")


def test_bridge_and_doctor_share_the_default_budget():
    assert bridge_config.USAGE_BUDGET_TOKENS_DEFAULT == 2_000_000
    assert _doctor_constant() == bridge_config.USAGE_BUDGET_TOKENS_DEFAULT


def test_settings_defaults_carry_the_fleet_budget():
    fields = bridge_config.Config.model_fields
    for name in ("usage_budget_tokens_claude", "usage_budget_tokens_codex"):
        assert fields[name].default == bridge_config.USAGE_BUDGET_TOKENS_DEFAULT, name


def test_piri_default_is_uncapped_and_shared_with_the_doctor():
    """Piri's fleet default is 0 (cap disabled, metering on) since 2026-09-18."""

    assert bridge_config.Config.model_fields["usage_budget_tokens_piri"].default == 0
    assert _doctor_piri_constant() == 0
