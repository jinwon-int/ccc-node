#!/usr/bin/env python3
"""Tests for scripts/env-file-guard.py (#2571).

The motivating outage: on gwakga a model-switch apply.sh moved
matrix-opus55-high.env into retired/ while ccc-hande-bridge still listed it as
a required EnvironmentFile, so the bridge's next restart failed and it stayed
down 14.7 h. These tests drive the CLI end-to-end against a fake systemctl
(SYSTEMCTL override) so no real manager is touched.

Run: python -m pytest -q scripts/env_file_guard_test.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import pwd
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "env-file-guard.py"

_spec = importlib.util.spec_from_file_location("env_file_guard", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
sys.modules["env_file_guard"] = guard  # dataclasses resolve cls.__module__
_spec.loader.exec_module(guard)

FAKE_SYSTEMCTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import fnmatch, json, os, sys
    argv = sys.argv[1:]
    with open(os.environ["FAKE_SYSTEMCTL_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\\n")
    fixture = json.load(open(os.environ["FAKE_SYSTEMCTL_FIXTURE"]))
    manager = "system"
    if "--user" in argv:
        manager = "user:self"
        if "-M" in argv:
            manager = "user:" + argv[argv.index("-M") + 1].rstrip("@")
    units = fixture.get(manager)
    if units is None:
        print("Failed to connect to bus: No medium found", file=sys.stderr)
        sys.exit(1)
    if "list-units" in argv:
        globs = [a for a in argv[argv.index("list-units") + 1:] if not a.startswith("-")]
        for name in units:
            if not globs or any(fnmatch.fnmatch(name, g) for g in globs):
                print(f"{name} loaded active running fake unit")
        sys.exit(0)
    if "show" in argv:
        print(units.get(argv[-1], ""))
        sys.exit(0)
    sys.exit(3)
    """
)


class EnvFileGuardCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fake = self.tmp / "systemctl"
        self.fake.write_text(FAKE_SYSTEMCTL, encoding="utf-8")
        self.fake.chmod(0o755)
        self.log = self.tmp / "calls.jsonl"
        self.fixture = self.tmp / "fixture.json"
        self.present = self.tmp / "present.env"
        self.present.write_text("SECRET_TOKEN=do-not-print\n", encoding="utf-8")
        self.gone = self.tmp / "retired-away.env"  # never created

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fixture(self, data: dict) -> None:
        self.fixture.write_text(json.dumps(data), encoding="utf-8")

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "SYSTEMCTL": str(self.fake),
            "FAKE_SYSTEMCTL_LOG": str(self.log),
            "FAKE_SYSTEMCTL_FIXTURE": str(self.fixture),
        }
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )

    def _calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_required_reference_blocks_retirement(self) -> None:
        self._fixture(
            {
                "system": {
                    "ccc-matrix-bridge.service": f"{self.present} (ignore_errors=no)",
                    "ccc-hande-bridge.service": f"{self.present} (ignore_errors=no)",
                    "ccc-agent-cron.service": "",
                }
            }
        )
        proc = self._run("users", str(self.present), "--json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertFalse(out["ok"])
        self.assertEqual(
            sorted(h["unit"] for h in out["hits"]),
            ["ccc-hande-bridge.service", "ccc-matrix-bridge.service"],
        )
        self.assertTrue(all(h["kind"] == "required" for h in out["hits"]))
        self.assertEqual(len(out["units_inspected"]), 3)
        self.assertNotIn("do-not-print", proc.stdout + proc.stderr)

    def test_optional_reference_is_reported_as_optional(self) -> None:
        self._fixture(
            {"system": {"ccc-matrix-bridge.service": f"{self.present} (ignore_errors=yes)"}}
        )
        proc = self._run("users", str(self.present))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("\toptional\t", proc.stdout)
        self.assertIn(str(self.present), proc.stdout)
        self.assertNotIn("do-not-print", proc.stdout + proc.stderr)

    def test_reference_matches_on_resolved_path(self) -> None:
        self._fixture(
            {"system": {"ccc-matrix-bridge.service": f"{self.present} (ignore_errors=no)"}}
        )
        link = self.tmp / "alias.env"
        link.symlink_to(self.present)
        indirect = f"{self.tmp}/sub/../{self.present.name}"
        self.assertEqual(self._run("users", str(link)).returncode, 1)
        self.assertEqual(self._run("users", indirect).returncode, 1)

    def test_unrelated_path_is_clean(self) -> None:
        self._fixture(
            {"system": {"ccc-matrix-bridge.service": f"{self.present} (ignore_errors=no)"}}
        )
        proc = self._run("users", str(self.tmp / "other.env"), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["hits"], [])

    def test_check_flags_missing_required_file(self) -> None:
        self._fixture(
            {
                "system": {
                    "ccc-hande-bridge.service": (
                        f"{self.present} (ignore_errors=no)\n{self.gone} (ignore_errors=no)"
                    )
                }
            }
        )
        proc = self._run("check", "--json")
        self.assertEqual(proc.returncode, 1)
        hits = json.loads(proc.stdout)["hits"]
        self.assertEqual([(h["unit"], h["path"]) for h in hits],
                         [("ccc-hande-bridge.service", str(self.gone))])
        self.assertFalse(hits[0]["exists"])

    def test_check_ignores_missing_optional_file(self) -> None:
        self._fixture(
            {
                "system": {
                    "ccc-matrix-bridge.service": (
                        f"{self.gone} (ignore_errors=yes) {self.present} (ignore_errors=no)"
                    )
                }
            }
        )
        proc = self._run("check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_user_manager_arguments(self) -> None:
        me = pwd.getpwuid(os.geteuid()).pw_name
        other = "envguard-nobody-user"  # not a real account -> -M <name>@
        self._fixture(
            {
                "system": {},
                "user:self": {"ccc-user-a.service": f"{self.gone} (ignore_errors=no)"},
                f"user:{other}": {"ccc-user-b.service": f"{self.present} (ignore_errors=no)"},
            }
        )
        proc = self._run("check", "--user", me, "--user", other, "--json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["managers"], ["system", f"user:{me}", f"user:{other}"])
        self.assertEqual([h["unit"] for h in out["hits"]], ["ccc-user-a.service"])
        calls = self._calls()
        self.assertIn(["--user", "-M", f"{other}@"], [c[:3] for c in calls])
        self.assertTrue(any(c[0] == "--user" and "-M" not in c for c in calls))
        self.assertTrue(any(c[0] == "list-units" for c in calls))

    def test_unreachable_manager_fails_closed(self) -> None:
        self._fixture({"system": {}})
        proc = self._run("check", "--user", "envguard-absent-user")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("systemctl failed", proc.stderr)

    def test_unit_globs_and_all(self) -> None:
        self._fixture(
            {
                "system": {
                    "ccc-matrix-bridge.service": f"{self.gone} (ignore_errors=no)",
                    "other-thing.service": f"{self.gone} (ignore_errors=no)",
                }
            }
        )
        default = json.loads(self._run("check", "--json").stdout)
        self.assertEqual([h["unit"] for h in default["hits"]], ["ccc-matrix-bridge.service"])
        everything = json.loads(self._run("check", "--all", "--json").stdout)
        self.assertEqual(len(everything["hits"]), 2)
        only = json.loads(self._run("check", "--unit-glob", "other-*", "--json").stdout)
        self.assertEqual([h["unit"] for h in only["hits"]], ["other-thing.service"])
        list_calls = [c for c in self._calls() if "list-units" in c]
        self.assertEqual(list_calls[0][-2:], ["ccc-*", "native-relay-*"])
        self.assertEqual(list_calls[1][-1], "--no-legend")


class ParseTests(unittest.TestCase):
    def test_parses_newline_and_space_separated_entries(self) -> None:
        value = (
            "/root/.config/ccc-node/matrix-nudge.env (ignore_errors=yes)\n"
            "/root/.config/ccc-main/matrix-danso.env (ignore_errors=no) "
            "/root/.config/ccc-main/matrix-opus55-high.env (ignore_errors=no)\n"
        )
        self.assertEqual(
            guard.parse_environment_files(value),
            [
                ("/root/.config/ccc-node/matrix-nudge.env", True),
                ("/root/.config/ccc-main/matrix-danso.env", False),
                ("/root/.config/ccc-main/matrix-opus55-high.env", False),
            ],
        )

    def test_empty_value(self) -> None:
        self.assertEqual(guard.parse_environment_files(""), [])
        self.assertEqual(guard.parse_environment_files("\n"), [])


if __name__ == "__main__":
    unittest.main()
