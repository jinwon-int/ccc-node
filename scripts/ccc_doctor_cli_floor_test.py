#!/usr/bin/env python3
"""Hermetic verdict tests for doctor's worker Claude CLI floor check (a2a-nexus#2275).

A worker pin moved to a model that needs a newer Claude Code than both the host
CLI and the docker runner image carried; every task then failed with an API 400
and nobody noticed for a day. These tests pin the verdicts with fixture env
files and stub ``claude``/``docker`` binaries — no real CLI, docker daemon or
provider is touched — and pin that nothing outside the key allowlist is read
out of the (secret-bearing) worker env file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ccc_doctor  # noqa: E402
from ccc_doctor import Doctor, Row, normalize_model_id, parse_cli_version  # noqa: E402

LABEL = ccc_doctor.RUNNER_CLI_PACKAGE_LABEL
SECRET = "SENSITIVE_WORKER_TOKEN_MARKER"
HOST = "worker claude cli floor (host)"
RUNNER = "worker claude cli floor (runner image)"
BASE = "worker claude cli floor"


class CliFloorCheck(unittest.TestCase):
    def setUp(self) -> None:
        # Some hardened runners mount /tmp noexec; the stubs must execute.
        base = os.environ.get("TMPDIR") or str(Path(__file__).resolve().parents[2])
        self._tmp = tempfile.TemporaryDirectory(dir=base)
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.docker_log = self.tmp / "docker.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- fixtures ------------------------------------------------------------

    def stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def claude_stub(self, version: str) -> Path:
        return self.stub("claude", f'[ "$1" = --version ] && echo "{version} (Claude Code)"\n')

    def docker_stub(self, *, labels: dict[str, str] | None = None, missing: bool = False,
                    run_version: str = "") -> Path:
        label_json = json.dumps(labels) if labels is not None else "null"
        inspect = (
            'echo "Error: No such image: $6" >&2; exit 1' if missing else f"echo '{label_json}'"
        )
        run = f'echo "{run_version} (Claude Code)"' if run_version else "exit 125"
        return self.stub("docker", f"""\
            echo "$*" >> {self.docker_log}
            case "$1:$2" in
              image:inspect) {inspect} ;;
              run:*) {run} ;;
              *) exit 2 ;;
            esac
            """)

    def env_file(self, **values: str) -> Path:
        path = self.tmp / "a2a-hermes-worker"
        lines = [
            "# worker env fixture",
            f"A2A_BROKER_TOKEN={SECRET}",
            f"GITHUB_TOKEN={SECRET}",
        ]
        lines += [f"{key}={value}" for key, value in values.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def run_check(self, env_path: Path | None, **env: str) -> Doctor:
        doctor = Doctor(Path.cwd(), self.tmp / ".claude", "settings")
        # The stub dir leads PATH; docker is pinned to the stub path so a node's
        # real docker is never consulted (a missing stub reads as docker absent).
        overrides = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "CCC_DOCTOR_WORKER_ENV_FILE": str(env_path or self.tmp / "absent-env"),
            "CCC_DOCTOR_DOCKER_BIN": str(self.bin / "docker"),
            **env,
        }
        with patch.dict("os.environ", overrides, clear=True):
            doctor.check_worker_claude_cli_floor()
        rendered = json.dumps([row.__dict__ for row in doctor.rows], ensure_ascii=False)
        self.assertNotIn(SECRET, rendered, "a non-allowlisted worker env value leaked")
        return doctor

    def row(self, doctor: Doctor, item: str) -> Row:
        rows = [row for row in doctor.rows if row.item == item]
        self.assertEqual(len(rows), 1, [r.item for r in doctor.rows])
        return rows[0]

    def docker_runs(self) -> list[str]:
        if not self.docker_log.exists():
            return []
        return [ln for ln in self.docker_log.read_text().splitlines() if ln.startswith("run ")]

    def pinned(self, image: str = "", **extra: str) -> Path:
        values = {"A2A_CLAUDE_MODEL": "claude-opus-5-5", "A2A_OPENCLAW_MODEL": "claude-opus-5-5"}
        if image:
            values["A2A_DOCKER_RUNNER_IMAGE"] = image
        values.update(extra)
        return self.env_file(**values)

    # --- pure helpers --------------------------------------------------------

    def test_semver_compares_numerically_not_lexically(self) -> None:
        self.assertGreater(parse_cli_version("2.1.280 (Claude Code)"), parse_cli_version("2.1.28"))
        self.assertLess(parse_cli_version("2.1.274"), parse_cli_version("2.1.280"))
        self.assertLess(parse_cli_version("2.1.99"), parse_cli_version("2.1.280"))
        self.assertIsNone(parse_cli_version("claude: command failed"))

    def test_model_ids_normalize_provider_prefix_and_suffix(self) -> None:
        self.assertEqual(normalize_model_id(' "anthropic/Claude-Opus-5-5[1m]" '), "claude-opus-5-5")

    def test_seeded_table_carries_the_incident_floor(self) -> None:
        table = ccc_doctor.load_cli_floor_table(ccc_doctor.CLI_FLOOR_TABLE_PATH)
        self.assertEqual(table["claude-opus-5-5"], (2, 1, 280))

    # --- not applicable ------------------------------------------------------

    def test_no_worker_env_file_is_not_applicable(self) -> None:
        doctor = self.run_check(None)
        row = self.row(doctor, BASE)
        self.assertEqual(row.klass, "정상")
        self.assertIn("해당 없음", row.status)
        self.assertEqual(doctor.counts["경고"], 0)

    def test_unknown_model_is_never_warned_but_named(self) -> None:
        self.claude_stub("1.0.0")
        doctor = self.run_check(self.env_file(A2A_CLAUDE_MODEL="claude-future-9"))
        row = self.row(doctor, BASE)
        self.assertEqual(row.klass, "정상")
        self.assertIn("floor unknown: claude-future-9", row.status)
        self.assertEqual(doctor.counts["경고"], 0)

    # --- host CLI ------------------------------------------------------------

    def test_floor_met_on_host_and_runner_label(self) -> None:
        self.claude_stub("2.1.280")
        self.docker_stub(labels={LABEL: "@anthropic-ai/claude-code@2.1.281"})
        doctor = self.run_check(self.pinned(image="a2a-docker-runner-claude:abc1234-build"))
        self.assertEqual(self.row(doctor, HOST).klass, "정상")
        runner = self.row(doctor, RUNNER)
        self.assertEqual(runner.klass, "정상")
        self.assertIn("runner=2.1.281 via label", runner.status)
        self.assertFalse(self.docker_runs(), "label read must not start a container")

    def test_host_below_floor_warns_with_npm_hint(self) -> None:
        self.claude_stub("2.1.274")
        doctor = self.run_check(self.pinned())
        row = self.row(doctor, HOST)
        self.assertEqual(row.klass, "경고")
        self.assertIn("host=2.1.274", row.status)
        self.assertIn("needs >= 2.1.280", row.status)
        self.assertIn("npm i -g @anthropic-ai/claude-code@2.1.280", row.action)
        self.assertEqual(doctor.report_exit_code(), 0, "경고 must not flip the exit code")

    def test_two_digit_patch_is_below_three_digit_floor(self) -> None:
        self.claude_stub("2.1.28")
        doctor = self.run_check(self.pinned())
        self.assertEqual(self.row(doctor, HOST).klass, "경고")

    def test_worker_configured_binary_wins_over_path(self) -> None:
        self.claude_stub("2.1.280")
        old = self.tmp / "old-claude"
        old.write_text('#!/usr/bin/env bash\necho "2.1.236 (Claude Code)"\n', encoding="utf-8")
        old.chmod(0o755)
        doctor = self.run_check(self.pinned(A2A_CLAUDE_CODE_BIN=str(old)))
        row = self.row(doctor, HOST)
        self.assertEqual(row.klass, "경고")
        self.assertIn("host=2.1.236", row.status)

    def test_inline_comment_breaks_the_configured_binary(self) -> None:
        """systemd keeps an inline '#' in the value, so the worker gets a bad path."""
        self.claude_stub("2.1.280")
        doctor = self.run_check(self.pinned(A2A_CLAUDE_CODE_BIN="/usr/bin/claude# note"))
        row = self.row(doctor, HOST)
        self.assertEqual(row.klass, "경고")
        self.assertIn("확인 불가", row.status)
        self.assertIn("A2A_CLAUDE_CODE_BIN=/usr/bin/claude# note", row.status)

    # --- runner image --------------------------------------------------------

    def test_runner_tag_below_floor_warns_without_docker(self) -> None:
        self.claude_stub("2.1.280")
        doctor = self.run_check(
            self.pinned(image="registry.local:5000/a2a-docker-runner-claude:cf2c218-claude-2.1.236"),
        )
        row = self.row(doctor, RUNNER)
        self.assertEqual(row.klass, "경고")
        self.assertIn("runner=2.1.236 via tag", row.status)
        self.assertIn(">= 2.1.280", row.action)

    def test_label_outranks_a_stale_tag(self) -> None:
        self.claude_stub("2.1.280")
        self.docker_stub(labels={LABEL: "@anthropic-ai/claude-code@2.1.236"})
        doctor = self.run_check(self.pinned(image="a2a-docker-runner-claude:x-claude-2.1.280"))
        row = self.row(doctor, RUNNER)
        self.assertEqual(row.klass, "경고")
        self.assertIn("runner=2.1.236 via label", row.status)

    def test_docker_absent_and_untagged_image_is_unverifiable_not_failure(self) -> None:
        self.claude_stub("2.1.280")
        doctor = self.run_check(self.pinned(image="a2a-docker-runner-claude-code:0dca088"))
        row = self.row(doctor, RUNNER)
        self.assertEqual(row.klass, "정상")
        self.assertIn("확인 불가 (docker CLI not found", row.status)
        self.assertEqual(doctor.counts["경고"], 0)

    def test_unlabeled_untagged_image_asks_for_the_probe_and_runs_nothing(self) -> None:
        self.claude_stub("2.1.280")
        self.docker_stub(labels={})
        doctor = self.run_check(self.pinned(image="a2a-docker-runner-claude-code:0dca088"))
        row = self.row(doctor, RUNNER)
        self.assertEqual(row.klass, "경고")
        self.assertIn("CCC_DOCTOR_RUNNER_CLI_PROBE=1", row.action)
        self.assertFalse(self.docker_runs(), "no container without opt-in")

    def test_opt_in_probe_runs_a_bounded_offline_container(self) -> None:
        self.claude_stub("2.1.280")
        self.docker_stub(labels={}, run_version="2.1.274")
        doctor = self.run_check(
            self.pinned(image="a2a-docker-runner-claude-code:0dca088"),
            CCC_DOCTOR_RUNNER_CLI_PROBE="1",
        )
        row = self.row(doctor, RUNNER)
        self.assertEqual(row.klass, "경고")
        self.assertIn("runner=2.1.274 via run", row.status)
        run_line = self.docker_runs()[0]
        for flag in ("--rm", "--pull=never", "--network=none", "--entrypoint /usr/local/bin/claude"):
            self.assertIn(flag, run_line)

    def test_missing_runner_image_warns(self) -> None:
        self.claude_stub("2.1.280")
        self.docker_stub(missing=True)
        doctor = self.run_check(self.pinned(image="a2a-docker-runner-claude:x-claude-2.1.280"))
        row = self.row(doctor, RUNNER)
        self.assertEqual(row.klass, "경고")
        self.assertIn("not present locally", row.status)

    def test_disabled_runner_is_not_applicable(self) -> None:
        self.claude_stub("2.1.280")
        doctor = self.run_check(
            self.pinned(image="a2a-docker-runner-claude:x-claude-2.1.236", A2A_DOCKER_RUNNER_ENABLED="0")
        )
        self.assertIn("해당 없음", self.row(doctor, RUNNER).status)


if __name__ == "__main__":
    unittest.main()
