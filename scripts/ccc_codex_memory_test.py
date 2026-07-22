"""Hermetic RED-first tests for the Codex memory materializer (#419)."""

from __future__ import annotations

import ast
import importlib.util
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "ccc_codex_memory.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ccc_codex_memory_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load materializer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def process_materialize(home: str, snapshot: str, queue) -> None:
    try:
        module = load_module()
        options = module.MaterializeOptions.from_environ(
            {
                "HOME": str(Path(home).parent),
                "CODEX_HOME": home,
                "CCC_CODEX_MEMORY_MAX_BYTES": "512",
            }
        )
        result = module.materialize_snapshot(snapshot, options)
        queue.put((True, result.status))
    except BaseException as exc:  # pragma: no cover - surfaced to parent
        queue.put((False, type(exc).__name__))


class CodexMemoryMaterializerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ccc419-materializer-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.home.mkdir()
        self.module = load_module()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def options(self, **extra: str):
        env = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
            "CCC_CODEX_MEMORY_MAX_BYTES": "512",
            "CCC_CODEX_AGENTS_BUDGET_BYTES": "4096",
            **extra,
        }
        return self.module.MaterializeOptions.from_environ(env)

    def test_atomic_write_delegates_to_shared_secure_fs(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertNotIn("_fsync_directory", functions)

        atomic_write = functions["_atomic_write"]
        calls = {
            (node.func.value.id, node.func.attr)
            for node in ast.walk(atomic_write)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        }
        self.assertIn(("_secure_fs", "atomic_write_bytes_at"), calls)
        self.assertTrue(
            {("os", name) for name in ("open", "write", "fsync", "replace", "unlink")}
            .isdisjoint(calls)
        )

        validator_calls = {
            (node.func.value.id, node.func.attr)
            for node in ast.walk(functions["validate_owned_regular"])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        }
        self.assertIn(("_secure_fs", "owner_only_regular_violation"), validator_calls)

    def test_shared_atomic_write_error_keeps_body_free_materializer_code(self) -> None:
        with mock.patch.object(
            self.module._secure_fs,
            "atomic_write_bytes_at",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(self.module.MaterializeError) as caught:
                self.module._atomic_write(123, "AGENTS.md", b"secret")
        self.assertEqual(caught.exception.code, "codex_io_failed")
        self.assertNotIn("secret", str(caught.exception))

    def test_creates_private_base_file_and_body_free_metadata(self) -> None:
        result = self.module.materialize_snapshot("NODE_SECRET_SENTINEL", self.options())

        target = self.codex_home / "AGENTS.md"
        metadata = self.codex_home / ".ccc-codex-memory.json"
        text = target.read_text(encoding="utf-8")
        meta_text = metadata.read_text(encoding="utf-8")
        meta = json.loads(meta_text)
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.active_kind, "base")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
        self.assertIn(self.module.BEGIN_MARKER, text)
        self.assertIn(self.module.END_MARKER, text)
        self.assertIn(
            f"- github-policy: `{self.module.GITHUB_POLICY_VERSION}`", text
        )
        self.assertIn(self.module.GITHUB_POLICY_BLOCK.strip(), text)
        self.assertLess(
            text.index(self.module.GITHUB_POLICY_BLOCK.strip()),
            text.index(self.module.SNAPSHOT_DELIMITER),
        )
        self.assertIn("NODE_SECRET_SENTINEL", text)
        self.assertNotIn("NODE_SECRET_SENTINEL", meta_text)
        self.assertEqual(meta["snapshot_sha256"], result.snapshot_sha256)
        self.assertNotIn("active_path", meta)

    def test_status_is_body_free_and_detects_missing_or_tampered_snapshot(self) -> None:
        options = self.options()
        missing = self.module.snapshot_status(options)
        self.assertEqual(missing.status, "missing")
        self.assertFalse(missing.is_ready)

        self.module.materialize_snapshot("STATUS_SECRET_SENTINEL", options)
        ready = self.module.snapshot_status(options)
        self.assertEqual(ready.status, "ready")
        self.assertTrue(ready.is_ready)
        self.assertEqual(ready.metadata_status, "ok")
        payload = json.dumps(ready.body_free_json(), sort_keys=True)
        self.assertNotIn("STATUS_SECRET_SENTINEL", payload)
        self.assertEqual(len(ready.snapshot_sha256 or ""), 64)

        agents = self.codex_home / "AGENTS.md"
        agents.write_text(
            agents.read_text(encoding="utf-8").replace(
                "STATUS_SECRET_SENTINEL", "TAMPERED_SECRET_SENTINEL"
            ),
            encoding="utf-8",
        )
        tampered = self.module.snapshot_status(options)
        self.assertEqual(tampered.status, "unsafe")
        self.assertFalse(tampered.is_ready)
        self.assertNotIn("TAMPERED_SECRET_SENTINEL", json.dumps(tampered.body_free_json()))

    def test_cli_status_exit_code_and_json_are_body_free(self) -> None:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex_home),
        }
        missing = subprocess.run(
            [sys.executable, str(MODULE_PATH), "status", "--json"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(json.loads(missing.stdout)["status"], "missing")
        self.module.materialize_snapshot("CLI_STATUS_SECRET", self.options())
        ready = subprocess.run(
            [sys.executable, str(MODULE_PATH), "status", "--json"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(ready.returncode, 0)
        self.assertEqual(json.loads(ready.stdout)["status"], "ready")
        self.assertNotIn("CLI_STATUS_SECRET", ready.stdout + ready.stderr)

    def test_preserves_user_bytes_and_replaces_exactly_one_block(self) -> None:
        self.codex_home.mkdir(mode=0o700)
        target = self.codex_home / "AGENTS.md"
        user = "# user guidance\nkeep-this-byte-for-byte"
        target.write_text(user, encoding="utf-8")
        target.chmod(0o600)

        self.module.materialize_snapshot("first", self.options())
        first = target.read_text(encoding="utf-8")
        self.module.materialize_snapshot("second", self.options())
        second = target.read_text(encoding="utf-8")

        self.assertTrue(first.startswith(user))
        self.assertTrue(second.startswith(user))
        self.assertEqual(second.count(self.module.BEGIN_MARKER), 1)
        self.assertEqual(second.count(self.module.END_MARKER), 1)
        self.assertNotIn("\nfirst\n", second)
        self.assertIn("\nsecond\n", second)

    def test_legacy_block_without_github_policy_is_not_reused(self) -> None:
        options = self.options()
        self.module.materialize_snapshot("same-snapshot", options)
        target = self.codex_home / "AGENTS.md"
        legacy = target.read_text(encoding="utf-8")
        legacy = legacy.replace(
            f"- github-policy: `{self.module.GITHUB_POLICY_VERSION}`\n\n", ""
        ).replace(f"{self.module.GITHUB_POLICY_BLOCK}\n", "")
        target.write_text(legacy, encoding="utf-8")
        target.chmod(0o600)

        status = self.module.snapshot_status(options)
        self.assertEqual(status.status, "missing")
        result = self.module.materialize_snapshot("same-snapshot", options)
        refreshed = target.read_text(encoding="utf-8")

        self.assertEqual(result.status, "updated")
        self.assertIn(self.module.GITHUB_POLICY_BLOCK.strip(), refreshed)
        self.assertEqual(refreshed.count(self.module.BEGIN_MARKER), 1)

    def test_nonempty_override_is_active_and_empty_override_falls_back(self) -> None:
        self.codex_home.mkdir(mode=0o700)
        base = self.codex_home / "AGENTS.md"
        override = self.codex_home / "AGENTS.override.md"
        base.write_text("base-user\n", encoding="utf-8")
        override.write_text("  \n", encoding="utf-8")
        base.chmod(0o600)
        override.chmod(0o600)

        result = self.module.materialize_snapshot("base-snapshot", self.options())
        self.assertEqual(result.active_kind, "base")
        self.assertIn("base-snapshot", base.read_text(encoding="utf-8"))
        self.assertNotIn(self.module.BEGIN_MARKER, override.read_text(encoding="utf-8"))

        override.write_text("override-user\n", encoding="utf-8")
        override.chmod(0o600)
        result = self.module.materialize_snapshot("override-snapshot", self.options())
        self.assertEqual(result.active_kind, "override")
        self.assertIn("override-user", override.read_text(encoding="utf-8"))
        self.assertIn("override-snapshot", override.read_text(encoding="utf-8"))

    def test_malformed_or_duplicate_markers_preserve_last_file(self) -> None:
        self.codex_home.mkdir(mode=0o700)
        target = self.codex_home / "AGENTS.md"
        malformed = f"user\n{self.module.BEGIN_MARKER}\npartial"
        target.write_text(malformed, encoding="utf-8")
        target.chmod(0o600)

        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot("new", self.options())

        self.assertEqual(caught.exception.code, "codex_markers_malformed")
        self.assertEqual(target.read_text(encoding="utf-8"), malformed)

        for broken in (
            f"{self.module.BEGIN_MARKER}\n{self.module.BEGIN_MARKER}\n{self.module.END_MARKER}",
            f"{self.module.END_MARKER}\n{self.module.BEGIN_MARKER}",
            f"{self.module.BEGIN_MARKER}\n{self.module.END_MARKER}\n{self.module.END_MARKER}",
        ):
            target.write_text(broken, encoding="utf-8")
            target.chmod(0o600)
            with self.assertRaises(self.module.MaterializeError) as nested:
                self.module.materialize_snapshot("new", self.options())
            self.assertEqual(nested.exception.code, "codex_markers_malformed")
            self.assertEqual(target.read_text(encoding="utf-8"), broken)

    def test_symlink_hardlink_fifo_and_foreign_owner_are_rejected(self) -> None:
        self.codex_home.mkdir(mode=0o700)
        target = self.codex_home / "AGENTS.md"
        outside = self.root / "outside"
        outside.write_text("outside", encoding="utf-8")
        target.symlink_to(outside)
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot("new", self.options())
        self.assertEqual(caught.exception.code, "codex_agents_unsafe")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        target.unlink()

        os.link(outside, target)
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot("new", self.options())
        self.assertEqual(caught.exception.code, "codex_agents_unsafe")
        target.unlink()

        os.mkfifo(target, 0o600)
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot("new", self.options())
        self.assertEqual(caught.exception.code, "codex_agents_unsafe")
        target.unlink()

        fake_stat = type(
            "FakeStat",
            (),
            {"st_mode": stat.S_IFREG | 0o600, "st_uid": os.geteuid() + 1, "st_nlink": 1},
        )()
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.validate_owned_regular(fake_stat)
        self.assertEqual(caught.exception.code, "codex_agents_unsafe")

    def test_control_file_symlinks_are_rejected_without_external_write(self) -> None:
        self.codex_home.mkdir(mode=0o700)
        outside = self.root / "control-outside"
        outside.write_text("outside", encoding="utf-8")
        lock_path = self.codex_home / ".ccc-codex-memory.lock"
        lock_path.symlink_to(outside)
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot("snapshot", self.options())
        self.assertEqual(caught.exception.code, "codex_agents_unsafe")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        lock_path.unlink()

        self.module.materialize_snapshot("last-good", self.options())
        target = self.codex_home / "AGENTS.md"
        before = target.read_bytes()
        metadata = self.codex_home / ".ccc-codex-memory.json"
        metadata.unlink()
        metadata.symlink_to(outside)
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot("new", self.options())
        self.assertEqual(caught.exception.code, "codex_agents_unsafe")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_marker_text_in_snapshot_is_rejected_without_rewrite(self) -> None:
        self.module.materialize_snapshot("last-good", self.options())
        target = self.codex_home / "AGENTS.md"
        before = target.read_bytes()
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot(f"attacker {self.module.BEGIN_MARKER}", self.options())
        self.assertEqual(caught.exception.code, "codex_snapshot_unsafe")
        self.assertEqual(target.read_bytes(), before)

    def test_utf8_truncation_is_valid_and_total_budget_preserves_user_content(self) -> None:
        result = self.module.materialize_snapshot(
            "한글🙂" * 200,
            self.options(
                CCC_CODEX_MEMORY_MAX_BYTES="129",
                CCC_CODEX_AGENTS_BUDGET_BYTES="1024",
            ),
        )
        text = (self.codex_home / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(result.truncated)
        self.assertNotIn("�", text)
        self.assertLessEqual(len(text.encode("utf-8")), 1024)

        target = self.codex_home / "AGENTS.md"
        target.write_text("u" * 1000, encoding="utf-8")
        target.chmod(0o600)
        before = target.read_bytes()
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot(
                "new", self.options(CCC_CODEX_AGENTS_BUDGET_BYTES="1024")
            )
        self.assertEqual(caught.exception.code, "codex_budget_exhausted")
        self.assertEqual(target.read_bytes(), before)

    def test_unchanged_snapshot_is_noop_without_content_or_mtime_change(self) -> None:
        first = self.module.materialize_snapshot("same", self.options())
        target = self.codex_home / "AGENTS.md"
        before = target.read_bytes()
        before_mtime = target.stat().st_mtime_ns
        time.sleep(0.01)
        second = self.module.materialize_snapshot("same", self.options())
        self.assertEqual(first.snapshot_sha256, second.snapshot_sha256)
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)

    def test_unchanged_snapshot_repairs_missing_metadata_and_private_mode(self) -> None:
        self.module.materialize_snapshot("same", self.options())
        target = self.codex_home / "AGENTS.md"
        metadata = self.codex_home / ".ccc-codex-memory.json"
        target.chmod(0o644)
        metadata.unlink()
        before = target.read_bytes()

        result = self.module.materialize_snapshot("same", self.options())

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(meta["snapshot_sha256"], result.snapshot_sha256)

    def test_world_writable_codex_home_is_rejected(self) -> None:
        self.codex_home.mkdir(mode=0o700)
        self.codex_home.chmod(0o777)
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.materialize_snapshot("snapshot", self.options())
        self.assertEqual(caught.exception.code, "codex_home_unsafe")

    def test_lock_contention_is_bounded_and_preserves_file(self) -> None:
        self.module.materialize_snapshot("last-good", self.options())
        target = self.codex_home / "AGENTS.md"
        before = target.read_bytes()
        lock_path = self.codex_home / ".ccc-codex-memory.lock"
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(self.module.MaterializeError) as caught:
                self.module.materialize_snapshot(
                    "new", self.options(CCC_CODEX_LOCK_TIMEOUT_SEC="0.05")
                )
            self.assertEqual(caught.exception.code, "codex_lock_timeout")
            self.assertEqual(target.read_bytes(), before)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_ten_processes_leave_one_valid_block(self) -> None:
        self.codex_home.mkdir(mode=0o700)
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        processes = [
            context.Process(
                target=process_materialize,
                args=(str(self.codex_home), f"snapshot-{index}", queue),
            )
            for index in range(10)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        outcomes = [queue.get(timeout=2) for _ in processes]
        self.assertTrue(all(ok for ok, _ in outcomes), outcomes)
        text = (self.codex_home / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text.count(self.module.BEGIN_MARKER), 1)
        self.assertEqual(text.count(self.module.END_MARKER), 1)
        self.assertEqual(stat.S_IMODE((self.codex_home / "AGENTS.md").stat().st_mode), 0o600)
        metadata = json.loads(
            (self.codex_home / ".ccc-codex-memory.json").read_text(encoding="utf-8")
        )
        block_hash = self.module._HASH_RE.search(text)
        assert block_hash is not None
        self.assertEqual(metadata["snapshot_sha256"], block_hash.group(1))

    def test_cli_reuses_loader_context_and_outputs_body_free_json(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        loader = hooks / "load-memory.sh"
        sentinel = "CLI_MEMORY_BODY_SENTINEL"
        loader.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' "
            + repr(json.dumps({"hookSpecificOutput": {"additionalContext": sentinel}}))
            + "\n",
            encoding="utf-8",
        )
        loader.chmod(0o700)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "CCC_CODEX_MEMORY_LOADER": str(loader),
                "CCC_MEMORY_NO_REFRESH": "1",
            }
        )
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "materialize", "--json"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIn(payload["status"], {"updated", "unchanged"})
        self.assertNotIn(sentinel, completed.stdout)
        self.assertNotIn(sentinel, completed.stderr)
        self.assertIn(sentinel, (self.codex_home / "AGENTS.md").read_text())

    def test_audience_scoped_env_blocks_materialize_and_status(self) -> None:
        # #581: the global CODEX_HOME/AGENTS.md store has no per-audience
        # separation; under audience-scoped memory both refreshing and reusing
        # the snapshot must fail closed, body-free.
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "CCC_MEMORY_AUDIENCE_SCOPED": "1",
            }
        )
        for command in ("materialize", "status"):
            completed = subprocess.run(
                [sys.executable, str(MODULE_PATH), command, "--json"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 70, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["code"], "codex_audience_scoped_blocked")
        self.assertFalse((self.codex_home / "AGENTS.md").exists())

    def test_audience_scoped_off_spellings_do_not_block(self) -> None:
        for value in ("", "0", "false", "off", "no", "OFF"):
            self.assertFalse(
                self.module._audience_scoped_blocked({"CCC_MEMORY_AUDIENCE_SCOPED": value})
            )
        for value in ("1", "true", "on", "yes"):
            self.assertTrue(
                self.module._audience_scoped_blocked({"CCC_MEMORY_AUDIENCE_SCOPED": value})
            )

    def test_loader_and_errors_are_bounded_body_free_codes(self) -> None:
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.load_snapshot(
                self.options(CCC_CODEX_MEMORY_LOADER=str(self.root / "missing"))
            )
        self.assertEqual(caught.exception.code, "codex_loader_unavailable")
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_loader_output_cap_terminates_before_long_loader_deadline(self) -> None:
        loader = self.root / "oversize-loader.sh"
        loader.write_text(
            "#!/usr/bin/env bash\n"
            "python3 -c 'import sys; sys.stdout.write(\"x\" * 1100000)'\n"
            "sleep 3\n",
            encoding="utf-8",
        )
        loader.chmod(0o700)
        started = time.monotonic()
        with self.assertRaises(self.module.MaterializeError) as caught:
            self.module.load_snapshot(
                self.options(
                    CCC_CODEX_MEMORY_LOADER=str(loader),
                    CCC_CODEX_LOADER_TIMEOUT_SEC="5",
                )
            )
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "codex_loader_failed")
        self.assertLess(elapsed, 1.5)
        self.assertFalse((self.codex_home / "AGENTS.md").exists())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CodexMemoryMaterializerTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    failed = len(result.failures) + len(result.errors)
    print(f"PASS={passed} FAIL={failed}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
