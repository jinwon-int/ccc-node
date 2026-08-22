#!/usr/bin/env python3
"""Hermetic tests for the executable architecture contract (#872)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("ccc_architecture_contract.py")
SPEC = importlib.util.spec_from_file_location("ccc_architecture_contract", MODULE_PATH)
assert SPEC and SPEC.loader
ARCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ARCH
SPEC.loader.exec_module(ARCH)


def _contract() -> dict[str, object]:
    return {
        "schema_version": 1,
        "python_roots": [{"path": "pkg", "package": "pkg"}],
        "layers": [
            {"name": "presentation", "module_patterns": ["pkg.ui"]},
            {"name": "provider_adapters", "module_patterns": ["pkg.provider"]},
            {"name": "gateways_sinks", "module_patterns": ["pkg.store"]},
        ],
        "rules": [
            {
                "name": "provider-no-ui",
                "from_layers": ["provider_adapters"],
                "forbid_layers": ["presentation"],
                "reason": "typed events cross this boundary",
            }
        ],
    }


class ArchitectureContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pkg = self.root / "pkg"
        self.pkg.mkdir()
        (self.pkg / "__init__.py").write_text("", encoding="utf-8")
        (self.pkg / "ui.py").write_text("def render(): pass\n", encoding="utf-8")
        (self.pkg / "store.py").write_text("def read(): pass\n", encoding="utf-8")
        self.contract_path = self.root / "contract.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_contract(self, contract: dict[str, object] | None = None) -> None:
        self.contract_path.write_text(json.dumps(contract or _contract()), encoding="utf-8")

    def test_valid_declared_dependency_passes(self) -> None:
        (self.pkg / "provider.py").write_text("from .store import read\n", encoding="utf-8")
        self.write_contract()
        result = ARCH.validate(self.root, self.contract_path)
        self.assertEqual(result.violations, ())
        self.assertEqual(result.classified_count, 3)

    def test_relative_import_violation_names_rule_path_and_target(self) -> None:
        (self.pkg / "provider.py").write_text("from . import ui\n", encoding="utf-8")
        self.write_contract()
        result = ARCH.validate(self.root, self.contract_path)
        self.assertEqual(len(result.violations), 1)
        self.assertEqual(
            result.violations[0].render(),
            "provider-no-ui: pkg/provider.py:1: pkg.provider imports pkg.ui "
            "(provider_adapters -> presentation)",
        )

    def test_absolute_from_import_violation_resolves_child_module(self) -> None:
        (self.pkg / "provider.py").write_text("from pkg import ui\n", encoding="utf-8")
        self.write_contract()
        result = ARCH.validate(self.root, self.contract_path)
        self.assertEqual([item.target_module for item in result.violations], ["pkg.ui"])

    def test_package_init_relative_import_uses_package_itself(self) -> None:
        sub = self.pkg / "provider"
        sub.mkdir()
        (sub / "__init__.py").write_text("from . import ui\n", encoding="utf-8")
        (sub / "ui.py").write_text("def render(): pass\n", encoding="utf-8")
        contract = _contract()
        layers = contract["layers"]
        assert isinstance(layers, list)
        layers[0]["module_patterns"] = ["pkg.provider.ui"]
        layers[1]["module_patterns"] = ["pkg.provider"]
        self.write_contract(contract)
        result = ARCH.validate(self.root, self.contract_path)
        self.assertEqual([item.target_module for item in result.violations], ["pkg.provider.ui"])

    def test_cli_fails_without_printing_source_body(self) -> None:
        marker = "PRIVATE_BODY_MUST_NOT_APPEAR"
        (self.pkg / "provider.py").write_text(
            f"# {marker}\nfrom . import ui\n", encoding="utf-8"
        )
        self.write_contract()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = ARCH.main(
                ["--repo-root", str(self.root), "--contract", str(self.contract_path)]
            )
        self.assertEqual(rc, 1)
        self.assertIn("provider-no-ui", stderr.getvalue())
        self.assertNotIn(marker, stderr.getvalue())

    def test_overlapping_layer_patterns_fail_closed(self) -> None:
        (self.pkg / "provider.py").write_text("", encoding="utf-8")
        contract = _contract()
        layers = contract["layers"]
        assert isinstance(layers, list)
        layers[0]["module_patterns"].append("pkg.provider")
        self.write_contract(contract)
        with self.assertRaisesRegex(ARCH.ContractError, "matches multiple layers"):
            ARCH.validate(self.root, self.contract_path)

    def test_unknown_rule_layer_is_rejected(self) -> None:
        contract = _contract()
        rules = contract["rules"]
        assert isinstance(rules, list)
        rules[0]["forbid_layers"] = ["not-a-layer"]
        self.write_contract(contract)
        with self.assertRaisesRegex(ARCH.ContractError, "unknown layers"):
            ARCH.load_contract(self.contract_path)

    def test_python_root_escape_is_rejected(self) -> None:
        contract = _contract()
        roots = contract["python_roots"]
        assert isinstance(roots, list)
        roots[0]["path"] = "../outside"
        self.write_contract(contract)
        with self.assertRaisesRegex(ARCH.ContractError, "repository-relative"):
            ARCH.load_contract(self.contract_path)


if __name__ == "__main__":
    unittest.main()
