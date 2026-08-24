#!/usr/bin/env python3
"""Hermetic regressions for managed TM-2380 auto-distill (#1257)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from model_command import (  # noqa: E402
    CLAUDE_ARGS,
    PIRI_ARGS,
    ModelCommandError,
    parse_systemd_environment,
    read_bridge_unit_environment,
    resolve_explicit_model_command,
    resolve_model_command,
)


def _load_auto_distill():
    spec = importlib.util.spec_from_file_location(
        "managed_auto_distill", HERE / "auto-distill.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load managed auto-distill")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUTO_DISTILL = _load_auto_distill()


class ModelCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def executable(self, name: str) -> Path:
        path = self.root / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    @staticmethod
    def no_which(_name: str) -> None:
        return None

    def test_systemd_parser_returns_only_allowlisted_values(self) -> None:
        parsed = parse_systemd_environment(
            'CCC_AGENT_PROVIDER=piri '
            '"CCC_PIRI_REAL_CLI_PATH=/opt/piri path/piri-ccc.sh" '
            'UNRELATED_SECRET=do-not-return'
        )
        self.assertEqual(parsed["CCC_AGENT_PROVIDER"], "piri")
        self.assertEqual(
            parsed["CCC_PIRI_REAL_CLI_PATH"], "/opt/piri path/piri-ccc.sh"
        )
        self.assertNotIn("UNRELATED_SECRET", parsed)

    def test_systemd_reader_prefers_complete_system_unit(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    "CCC_AGENT_PROVIDER=piri "
                    "CCC_PIRI_REAL_CLI_PATH=/opt/piri/piri-ccc.sh"
                ),
                stderr="",
            )

        values = read_bridge_unit_environment(runner=runner)
        self.assertEqual(values["CCC_AGENT_PROVIDER"], "piri")
        self.assertEqual(values["CCC_PIRI_REAL_CLI_PATH"], "/opt/piri/piri-ccc.sh")
        self.assertEqual(len(calls), 1)

    def test_systemd_reader_does_not_mix_two_units(self) -> None:
        calls = 0

        def runner(argv, **_kwargs):
            nonlocal calls
            calls += 1
            stdout = (
                "CCC_AGENT_PROVIDER=piri"
                if calls == 1
                else "CCC_PIRI_CLI_PATH=/stale/user-wrapper"
            )
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        values = read_bridge_unit_environment(runner=runner)
        self.assertEqual(values, {"CCC_AGENT_PROVIDER": "piri"})
        self.assertEqual(calls, 1)

    def test_process_real_path_wins(self) -> None:
        process_real = self.executable("process-real")
        unit_real = self.executable("unit-real")
        wrapper = self.executable("wrapper")
        selected = resolve_model_command(
            process_environment={
                "CCC_AGENT_PROVIDER": "piri",
                "CCC_PIRI_REAL_CLI_PATH": str(process_real),
                "CCC_PIRI_CLI_PATH": str(wrapper),
            },
            unit_environment={"CCC_PIRI_REAL_CLI_PATH": str(unit_real)},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "piri")
        self.assertEqual(selected.argv, (str(process_real), *PIRI_ARGS))
        self.assertEqual(selected.source, "process:CCC_PIRI_REAL_CLI_PATH")

    def test_systemd_real_path_beats_process_wrapper(self) -> None:
        unit_real = self.executable("unit-real")
        process_wrapper = self.executable("process-wrapper")
        selected = resolve_model_command(
            process_environment={
                "CCC_AGENT_PROVIDER": "piri",
                "CCC_PIRI_CLI_PATH": str(process_wrapper),
            },
            unit_environment={"CCC_PIRI_REAL_CLI_PATH": str(unit_real)},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.argv[0], str(unit_real))
        self.assertEqual(selected.source, "systemd:CCC_PIRI_REAL_CLI_PATH")

    def test_standard_opt_like_path_beats_home_path(self) -> None:
        opt_path = self.executable("opt-piri")
        home_path = self.executable("home-piri")
        selected = resolve_model_command(
            process_environment={"CCC_AGENT_PROVIDER": "piri"},
            unit_environment={},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(opt_path, home_path),
        )
        self.assertEqual(selected.argv[0], str(opt_path))
        self.assertEqual(selected.source, f"standard:{opt_path}")

    def test_piri_provider_refuses_claude_fallback(self) -> None:
        claude = self.executable("claude")

        def which(name: str) -> str | None:
            return str(claude) if name == "claude" else None

        with self.assertRaisesRegex(ModelCommandError, "refusing Claude fallback"):
            resolve_model_command(
                process_environment={"CCC_AGENT_PROVIDER": "piri"},
                unit_environment={},
                home=self.home,
                which=which,
                piri_default_paths=(),
            )

    def test_claude_provider_does_not_switch_to_available_piri(self) -> None:
        piri = self.executable("piri")
        claude = self.executable("claude")

        def which(name: str) -> str | None:
            return {"piri": str(piri), "claude": str(claude)}.get(name)

        selected = resolve_model_command(
            process_environment={"CCC_AGENT_PROVIDER": "claude"},
            unit_environment={},
            home=self.home,
            which=which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "claude")
        self.assertEqual(selected.argv, (str(claude), *CLAUDE_ARGS))

    def test_codex_runtime_hint_keeps_auto_piri_lane(self) -> None:
        piri = self.executable("piri")
        selected = resolve_model_command(
            process_environment={"CCC_AGENT_PROVIDER": "codex"},
            unit_environment={"CCC_PIRI_REAL_CLI_PATH": str(piri)},
            home=self.home,
            which=self.no_which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "piri")

    def test_auto_claude_fallback_is_explicit(self) -> None:
        claude = self.executable("claude")

        def which(name: str) -> str | None:
            return str(claude) if name == "claude" else None

        selected = resolve_model_command(
            process_environment={},
            unit_environment={},
            home=self.home,
            which=which,
            piri_default_paths=(),
        )
        self.assertEqual(selected.engine, "claude")
        self.assertEqual(selected.reason, "no-runnable-piri")

    def test_invalid_dedicated_provider_fails_closed(self) -> None:
        with self.assertRaisesRegex(ModelCommandError, "must be auto, piri, or claude"):
            resolve_model_command(
                process_environment={"CCC_AUTO_DISTILL_PROVIDER": "codex"},
                unit_environment={},
                home=self.home,
                which=self.no_which,
                piri_default_paths=(),
            )

    def test_explicit_command_preserves_quoted_argument(self) -> None:
        custom = self.executable("custom")
        selected = resolve_explicit_model_command(
            f'{custom} --label "two words"', which=self.no_which
        )
        self.assertEqual(selected.argv, (str(custom), "--label", "two words"))

    def test_extractor_child_always_receives_inflight_guards(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"items": []}),
            stderr="",
        )
        with patch.object(AUTO_DISTILL.subprocess, "run", return_value=completed) as run:
            data, (error, _usage, _raw) = AUTO_DISTILL.extract_json(
                "prompt", ["model"], 10
            )
        self.assertEqual(data, {"items": []})
        self.assertIsNone(error)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["CLAUDE_DISTILL_INFLIGHT"], "1")
        self.assertEqual(child_env["CCC_AUTO_DISTILL_INFLIGHT"], "1")

    def test_spawn_failure_is_body_and_path_free(self) -> None:
        with patch.object(AUTO_DISTILL.subprocess, "run", side_effect=FileNotFoundError):
            _data, (error, _usage, raw) = AUTO_DISTILL.extract_json(
                "secret prompt", ["/private/path/model"], 10
            )
        self.assertEqual(error, "model_spawn_error:FileNotFoundError")
        self.assertEqual(raw, "")
        self.assertNotIn("/private/path", error)

    def test_main_reports_custom_engine_and_writes_body_free_audit(self) -> None:
        custom = self.executable("custom")
        session_dir = self.home / ".piri/agent/sessions"
        session_dir.mkdir(parents=True)
        environment = os.environ.copy()
        environment.update({"HOME": str(self.home), "PATH": "/usr/bin:/bin"})
        completed = subprocess.run(
            [
                sys.executable,
                str(HERE / "auto-distill.py"),
                "--dry-run",
                "--cap",
                "0",
                "--no-cache-sync",
                "--model-cmd",
                str(custom),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("engine=custom source=--model-cmd", completed.stdout)
        audit_path = self.home / ".hermes/logs/auto-distill-audit.jsonl"
        audit = [json.loads(line) for line in audit_path.read_text().splitlines()]
        self.assertEqual(audit[0]["event"], "engine_selected")
        self.assertEqual(audit[0]["engine"], "custom")
        self.assertNotIn("argv", audit[0])


if __name__ == "__main__":
    unittest.main()
