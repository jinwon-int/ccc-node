#!/usr/bin/env python3
"""Tests for scripts/main-model-switch.py (#2557).

Drives the CLI end-to-end in temp dirs against a fake systemctl (SYSTEMCTL
override) that answers from the temp drop-ins, so the real
``env-file-guard.py`` and ``bridge-idle-gate.py`` run unmodified and no real
manager, unit file or env file is touched.

Run: python -m pytest -q scripts/main_model_switch_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "main-model-switch.py"
MAIN = "zzzzzzzzzzzzzz-main-"
SENTINEL = "sentinel-value-7f3a-do-not-print"
OLD_MODEL = "claude-old-model-1"
NEW_MODEL = "claude-new-model-2"

# Answers the calls main-model-switch.py, env-file-guard.py make, reading the
# temp systemd dir live (so daemon-reload is a logged no-op). State (reload
# count, MainPIDs) lives in a JSON file; restart writes /proc/<pid>/environ
# from the unit's referenced env files, like systemd would.
FAKE_SYSTEMCTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import fnmatch, json, os, re, sys
    from pathlib import Path
    argv = sys.argv[1:]
    E = os.environ
    with open(E["FAKE_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\\n")
    sysd = Path(E["FAKE_SYSTEMD_DIR"])
    fixture = json.loads(Path(E["FAKE_FIXTURE"]).read_text())
    state_path = Path(E["FAKE_STATE"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state.setdefault("reloads", 0)
    state.setdefault("pids", dict(fixture.get("pids", {})))

    def save():
        state_path.write_text(json.dumps(state))

    def units():
        names = [d.name[:-2] for d in sorted(sysd.glob("*.service.d"))]
        return names + [u for u in fixture.get("extra", {}) if u not in names]

    def env_files(unit):
        out = []
        for conf in sorted((sysd / (unit + ".d")).glob("*.conf")):
            for line in conf.read_text().splitlines():
                m = re.match(r"\\s*EnvironmentFile\\s*=\\s*(-?)(\\S+)", line)
                if m:
                    out.append((m.group(2), m.group(1) == "-"))
        for path in fixture.get("extra", {}).get(unit, []):
            out.append((path, False))
        if state["reloads"] == int(E.get("FAKE_BREAK_ON_RELOAD", "-1")):
            out.append((E["FAKE_BREAK_PATH"], False))
        return out

    def write_proc(unit, pid):
        model = ""
        for path, _ in env_files(unit):
            try:
                text = Path(path).read_text()
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith("ANTHROPIC_MODEL="):
                    model = line.split("=", 1)[1].strip('"')
        if unit in fixture.get("stale_restart", []):
            model = fixture["old_model"]
        d = Path(E["FAKE_PROC"]) / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "environ").write_bytes(("ANTHROPIC_MODEL=" + model + "\\0").encode())
        (d / "stat").write_text(f"{pid} (python3) S 1 " + "0 " * 20 + "\\n")

    if argv[:1] == ["daemon-reload"]:
        state["reloads"] += 1
        save()
        sys.exit(int(E.get("FAKE_RELOAD_EXIT", "0") or 0))
    if argv[:1] == ["list-units"]:
        globs = [a for a in argv[1:] if not a.startswith("-")]
        for name in units():
            if not globs or any(fnmatch.fnmatch(name, g) for g in globs):
                print(f"{name} loaded active running fake unit")
        sys.exit(0)
    if argv[:3] == ["show", "-p", "EnvironmentFiles"]:
        print(" ".join(f"{p} (ignore_errors={'yes' if o else 'no'})"
                       for p, o in env_files(argv[-1])))
        sys.exit(0)
    if argv[:1] == ["show"]:
        unit = argv[1]
        print(state["pids"].get(unit, 0))
        print(fixture.get("environment", {}).get(unit, ""))
        sys.exit(0)
    if argv[:1] == ["is-active"]:
        active = argv[1] in fixture.get("active", [])
        print("active" if active else "inactive")
        sys.exit(0 if active else 3)
    if argv[:1] == ["restart"]:
        unit = argv[1]
        pid = int(state["pids"].get(unit, 100)) + 1000
        state["pids"][unit] = pid
        save()
        write_proc(unit, pid)
        sys.exit(0)
    sys.exit(3)
    """
)


class SwitchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.sysd = self.tmp / "systemd"
        self.envd = self.tmp / "ccc-main"
        self.claude = self.tmp / "claude"
        self.backups = self.tmp / "backups"
        self.proc = self.tmp / "proc"
        self.cgroup = self.tmp / "cgroup"
        self.cgroup.write_text("0::/user.slice/test.scope\n")
        for d in (self.sysd, self.envd, self.claude, self.proc):
            d.mkdir()
        self.fake = self.tmp / "systemctl"
        self.fake.write_text(FAKE_SYSTEMCTL, encoding="utf-8")
        self.fake.chmod(0o755)
        self.log = self.tmp / "calls.jsonl"
        self.state = self.tmp / "state.json"
        self.fixture_path = self.tmp / "fixture.json"
        self.fixture: dict = {"old_model": OLD_MODEL}
        self.extra_env: dict[str, str] = {}
        self.data_dirs = {ch: self.tmp / f"data-{ch}" for ch in ("telegram", "matrix", "hande")}
        self._layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- fixture layout: telegram + matrix, hande cloned from matrix ------------
    def _env(self, channel: str) -> Path:
        path = self.envd / f"{channel}-old-high.env"
        path.write_text(
            "CCC_AGENT_PROVIDER=claude\n"
            f"ANTHROPIC_MODEL={OLD_MODEL}\n"
            "CLAUDE_CODE_EFFORT_LEVEL=medium\n"
            f"CLAUDE_CLI_PATH=/opt/fake/{SENTINEL}/claude\n"
            'CCC_MODEL_LABEL="Old Model"\n',
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _dropin(self, unit: str, env: Path, slug: str = "old-high") -> Path:
        d = self.sysd / f"{unit}.d"
        d.mkdir(exist_ok=True)
        path = d / f"{MAIN}{slug}.conf"
        path.write_text(f"# old switch\n[Service]\nEnvironmentFile={env}\n", encoding="utf-8")
        (d / "provider.conf").write_text("[Service]\nEnvironment=CCC_PROVIDER=claude\n")
        return path

    def _layout(self) -> None:
        self.tg_env = self._env("telegram")
        self.mx_env = self._env("matrix")
        self.tg_drop = self._dropin("ccc-telegram-bridge.service", self.tg_env)
        self.mx_drop = self._dropin("ccc-matrix-bridge.service", self.mx_env)
        self.hd_drop = self._dropin("ccc-hande-bridge.service", self.mx_env)
        (self.claude / "settings.json").write_text(
            json.dumps({"model": OLD_MODEL, "env": {"X": SENTINEL}}, indent=2) + "\n"
        )
        (self.claude / "settings.local.json").write_text(json.dumps({"theme": "dark"}) + "\n")
        sessions = {
            "telegram_session:1": {"model": OLD_MODEL, "effort": "medium"},
            "telegram_session:2": {"model": OLD_MODEL, "provider": "claude"},
            "telegram_session:3": {"model": "gpt-x", "provider": "codex", "effort": "low"},
            "telegram_session:4": {"model": "claude-y", "provider": "codex"},
            "telegram_session:5": {"session_id": "abc"},
        }
        for d in self.data_dirs.values():
            d.mkdir()
            (d / "sessions.json").write_text(json.dumps(sessions, indent=2) + "\n")
            (d / "sessions.json").chmod(0o600)

    def _write_fixture(self) -> None:
        self.fixture_path.write_text(json.dumps(self.fixture), encoding="utf-8")

    def _run(self, *args: str, switch: bool = True) -> subprocess.CompletedProcess:
        self._write_fixture()
        env = {
            **os.environ,
            "SYSTEMCTL": str(self.fake),
            "FAKE_LOG": str(self.log),
            "FAKE_SYSTEMD_DIR": str(self.sysd),
            "FAKE_FIXTURE": str(self.fixture_path),
            "FAKE_STATE": str(self.state),
            "FAKE_PROC": str(self.proc),
            "FAKE_BREAK_PATH": str(self.tmp / "never-there.env"),
            **self.extra_env,
        }
        base = [
            "--systemd-dir", str(self.sysd),
            "--env-dir", str(self.envd),
            "--claude-dir", str(self.claude),
            "--bridge-data-dirs", ",".join(str(d) for d in self.data_dirs.values()),
            "--backup-root", str(self.backups),
            "--node", "testnode",
            "--proc", str(self.proc),
            "--self-cgroup", str(self.cgroup),
        ]
        target = ["--model", NEW_MODEL, "--effort", "high", "--label", "New Model 2",
                  "--slug", "new-high"] if switch else []
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *base, *target, *args],
            capture_output=True, text=True, env=env, timeout=120, check=False,
        )
        self.assertNotIn(SENTINEL, proc.stdout + proc.stderr)
        return proc

    def _snapshot(self) -> dict[str, bytes]:
        roots = (self.sysd, self.envd, self.claude, *self.data_dirs.values())
        return {
            str(p): p.read_bytes() for r in roots for p in sorted(r.rglob("*")) if p.is_file()
        }

    def _calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def _backup_dir(self) -> Path:
        dirs = sorted(self.backups.iterdir())
        self.assertEqual(len(dirs), 1)
        return dirs[0]

    @staticmethod
    def _env_value(path: Path, key: str) -> str:
        for line in path.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        raise AssertionError(f"{key} not in {path}")


class SwitchTests(SwitchTestBase):
    def test_switch_repoints_all_three_units_and_retires_old_envs(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        new_tg, new_mx = self.envd / "telegram-new-high.env", self.envd / "matrix-new-high.env"
        for unit, env in (
            ("ccc-telegram-bridge.service", new_tg),
            ("ccc-matrix-bridge.service", new_mx),
            ("ccc-hande-bridge.service", new_mx),
        ):
            d = self.sysd / f"{unit}.d"
            self.assertEqual(sorted(p.name for p in d.iterdir()),
                             ["provider.conf", f"{MAIN}new-high.conf"])
            text = (d / f"{MAIN}new-high.conf").read_text()
            self.assertIn(f"EnvironmentFile={env}\n", text)
            self.assertIn("--rollback", text)
            self.assertEqual((d / f"{MAIN}new-high.conf").stat().st_mode & 0o777, 0o644)
            self.assertEqual((d / "provider.conf").read_text(),
                             "[Service]\nEnvironment=CCC_PROVIDER=claude\n")
        for env in (new_tg, new_mx):
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self._env_value(env, "ANTHROPIC_MODEL"), NEW_MODEL)
            self.assertEqual(self._env_value(env, "CLAUDE_CODE_EFFORT_LEVEL"), "high")
            self.assertEqual(self._env_value(env, "CCC_MODEL_LABEL"), '"New Model 2"')
            self.assertEqual(self._env_value(env, "CLAUDE_CLI_PATH"),
                             f"/opt/fake/{SENTINEL}/claude")
        self.assertFalse(self.tg_env.exists())
        self.assertFalse(self.mx_env.exists())
        backup = self._backup_dir()
        self.assertTrue(backup.name.startswith("main-new-high-testnode-"))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertTrue((backup / "retired" / self.mx_env.name).exists())
        self.assertTrue((backup / "retired" / "ccc-hande-bridge.service.d" /
                         self.hd_drop.name).exists())
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "applied")
        paths = {e["path"] for e in manifest["entries"]}
        self.assertIn(str(self.hd_drop), paths)
        self.assertIn(str(self.mx_env), paths)
        self.assertIn(str(self.claude / "settings.json"), paths)
        self.assertNotIn(SENTINEL, (backup / "manifest.json").read_text())
        calls = self._calls()
        self.assertIn(["daemon-reload"], calls)
        self.assertFalse(any(c[:1] == ["restart"] for c in calls))

    def test_settings_and_sessions_only_claude_entries(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        settings = json.loads((self.claude / "settings.json").read_text())
        self.assertEqual(settings, {"model": NEW_MODEL, "env": {"X": SENTINEL}})
        self.assertEqual(json.loads((self.claude / "settings.local.json").read_text()),
                         {"theme": "dark"})
        for d in self.data_dirs.values():
            path = d / "sessions.json"
            data = json.loads(path.read_text())
            self.assertEqual(data["telegram_session:1"], {"model": NEW_MODEL, "effort": "high"})
            self.assertEqual(data["telegram_session:2"],
                             {"model": NEW_MODEL, "provider": "claude"})
            self.assertEqual(data["telegram_session:3"]["model"], "gpt-x")
            self.assertEqual(data["telegram_session:3"]["effort"], "low")
            self.assertEqual(data["telegram_session:4"]["model"], "claude-y")
            self.assertEqual(data["telegram_session:5"], {"session_id": "abc"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_dry_run_writes_nothing(self) -> None:
        before = self._snapshot()
        proc = self._run("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self.backups.exists())
        self.assertIn("dry-run", proc.stdout)
        self.assertIn("ccc-hande-bridge.service", proc.stdout)
        self.assertNotIn(["daemon-reload"], self._calls())

    def test_env_still_referenced_elsewhere_is_kept(self) -> None:
        self.fixture["extra"] = {"ccc-agent-cron.service": [str(self.tg_env)]}
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.tg_env.exists())
        self.assertFalse(self.mx_env.exists())
        self.assertIn("kept", proc.stderr)
        self.assertIn("ccc-agent-cron.service", proc.stdout)
        manifest = json.loads((self._backup_dir() / "manifest.json").read_text())
        kept = [e for e in manifest["entries"] if e["path"] == str(self.tg_env)]
        self.assertEqual(kept[0]["action"], "kept")

    def test_check_failure_rolls_back_everything(self) -> None:
        before = self._snapshot()
        self.extra_env["FAKE_BREAK_ON_RELOAD"] = "1"  # broken view after the 1st reload
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("rolling back", proc.stdout)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.mx_env.stat().st_mode & 0o777, 0o600)
        manifest = json.loads((self._backup_dir() / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "rolled-back")
        self.assertEqual(self._calls().count(["daemon-reload"]), 2)

    def test_preflight_check_failure_changes_nothing(self) -> None:
        before = self._snapshot()
        self.extra_env["FAKE_BREAK_ON_RELOAD"] = "0"  # broken from the start
        proc = self._run()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self.backups.exists())

    def test_rerun_is_idempotent(self) -> None:
        self.assertEqual(self._run().returncode, 0)
        after_first = self._snapshot()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("nothing to change", proc.stdout)
        self.assertEqual(self._snapshot(), after_first)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_rollback_command_restores_originals(self) -> None:
        before = self._snapshot()
        self.assertEqual(self._run().returncode, 0)
        self.assertNotEqual(self._snapshot(), before)
        backup = self._backup_dir()
        proc = self._run("--rollback", str(backup), switch=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.tg_env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((backup / "manifest.json").read_text())["status"],
                         "rolled-back")

    def test_restart_commands_printed_for_active_units_only(self) -> None:
        self.fixture["active"] = ["ccc-matrix-bridge.service", "ccc-hande-bridge.service"]
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("systemctl restart ccc-matrix-bridge.service", proc.stdout)
        self.assertIn("systemctl restart ccc-hande-bridge.service", proc.stdout)
        self.assertNotIn("systemctl restart ccc-telegram-bridge.service", proc.stdout)
        self.assertFalse(any(c[:1] == ["restart"] for c in self._calls()))

    def test_bad_arguments_are_rejected(self) -> None:
        proc = self._run("--slug", "../x")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(self.backups.exists())


class RestartTests(SwitchTestBase):
    def _running(self, unit: str, pid: int, busy: bool = False) -> None:
        channel = unit.split("-")[1]
        data = self.data_dirs[channel]
        self.fixture.setdefault("active", []).append(unit)
        self.fixture.setdefault("pids", {})[unit] = pid
        self.fixture.setdefault("environment", {})[unit] = f"BOT_DATA_DIR={data}"
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} (python3) S 1 " + "0 " * 20 + "\n")
        (d / "environ").write_bytes(f"ANTHROPIC_MODEL={OLD_MODEL}\0".encode())
        stamp = datetime.now(timezone.utc).isoformat()
        (data / "health.json").write_text(json.dumps({
            "updated_at": stamp,
            "process": {"pid": pid},
            "workload": {"active_requests": 1 if busy else 0},
            "requests": {"delegated_tasks_active": 0},
        }))

    def test_restart_waits_idle_and_verifies_environ(self) -> None:
        self._running("ccc-matrix-bridge.service", 200)
        self._running("ccc-hande-bridge.service", 300)
        proc = self._run("--restart", "--verify-timeout", "5")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        restarts = [c[1] for c in self._calls() if c[:1] == ["restart"]]
        self.assertEqual(sorted(restarts),
                         ["ccc-hande-bridge.service", "ccc-matrix-bridge.service"])
        self.assertIn(f"ccc-hande-bridge.service: ANTHROPIC_MODEL={NEW_MODEL}", proc.stdout)
        self.assertIn("restart verified", proc.stdout)
        # a second run sees the live model already switched: no further restart
        rerun = self._run("--restart")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("already running", rerun.stdout)
        self.assertEqual(len([c for c in self._calls() if c[:1] == ["restart"]]), 2)

    def test_busy_unit_is_not_restarted(self) -> None:
        self._running("ccc-matrix-bridge.service", 200, busy=True)
        started = time.monotonic()
        proc = self._run("--restart", "--idle-timeout", "0")
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("not idle", proc.stdout)
        self.assertIn("--rollback", proc.stdout)
        self.assertFalse(any(c[:1] == ["restart"] for c in self._calls()))
        # the switch itself stays applied (no automatic rollback after restart failure)
        self.assertTrue((self.envd / "matrix-new-high.env").exists())

    def test_stale_restart_is_reported(self) -> None:
        self._running("ccc-matrix-bridge.service", 200)
        self.fixture["stale_restart"] = ["ccc-matrix-bridge.service"]
        proc = self._run("--restart", "--verify-timeout", "1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"ccc-matrix-bridge.service: ANTHROPIC_MODEL={OLD_MODEL}", proc.stdout)
        self.assertIn("restart not verified", proc.stdout)

    def test_refuses_to_restart_own_unit(self) -> None:
        self._running("ccc-matrix-bridge.service", 200)
        self.cgroup.write_text("0::/system.slice/ccc-matrix-bridge.service\n")
        proc = self._run("--restart")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("systemd-run", proc.stdout)
        self.assertFalse(any(c[:1] == ["restart"] for c in self._calls()))


if __name__ == "__main__":
    unittest.main()
