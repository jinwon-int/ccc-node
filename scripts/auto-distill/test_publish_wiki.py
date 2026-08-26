#!/usr/bin/env python3
"""Hermetic regressions for the 11-node AUTO.md Wiki batch publisher."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = ROOT / "scripts/auto-distill/publish_wiki.py"
ROSTER = (
    "seoseo",
    "dungae",
    "sogyo",
    "nosuk",
    "bangtong",
    "yukson",
    "soonwook",
    "gwakga",
    "jingun",
    "gongyung",
    "daegyo",
)


def item(key: str, *, status: str = "unverified", fact: str = "durable fact") -> str:
    return f"""\
### ✅ 승격 후보 — candidate {key}

- **사실**: {fact}
- **분류**: `decision` · **상태**: `{status}` · **키**: `{key}` · **파이프라인**: `v6`
- **출처**: 세션 `session-safe` · 근거 `message-1` · 2026-08-26 20:00 KST
  > body-free fixture
"""


def document(node: str, *items: str) -> str:
    return f"""\
# [DOC-auto-{node}] {node} AUTO — 자동 승격 후보 (auto-distill)

> **에이전트 다이제스트** — status: `unverified`
> - 정본이 아니다.

{"".join(items)}"""


class PublishWikiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.worktree = self.root / "wiki"
        (self.worktree / ".git").mkdir(parents=True)
        for node in ROSTER:
            (self.worktree / "pages/nodes" / node).mkdir(parents=True)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def source(self, node: str, text: str) -> Path:
        path = self.inputs / f"{node}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def full_args(
        self,
        *,
        local: dict[str, Path] | None = None,
        remote: dict[str, str] | None = None,
        omit: set[str] | None = None,
    ) -> list[str]:
        local = local or {}
        remote = remote or {}
        omit = omit or set()
        args = ["--wiki-worktree", str(self.worktree), "--json"]
        for node in ROSTER:
            if node in omit:
                continue
            if node in local:
                args += ["--local", f"{node}={local[node]}"]
            elif node in remote:
                args += ["--remote", f"{node}={remote[node]}"]
            else:
                args += ["--empty", node]
        return args

    def run_publisher(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(PUBLISHER), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_exact_roster_is_required(self) -> None:
        result = self.run_publisher(self.full_args(omit={"bangtong"}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing: bangtong", result.stderr)

    def test_preview_accepts_explicit_empty_without_mutation(self) -> None:
        before = sorted(str(path.relative_to(self.worktree)) for path in self.worktree.rglob("*"))
        result = self.run_publisher(self.full_args())
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["roster"], 11)
        self.assertEqual(report["declared_empty"], 11)
        self.assertEqual(report["changed_pages"], 0)
        after = sorted(str(path.relative_to(self.worktree)) for path in self.worktree.rglob("*"))
        self.assertEqual(after, before)

    def test_wrong_node_header_fails_before_any_write(self) -> None:
        source = self.source("gongyung", document("daegyo", item("a" * 12)))
        result = self.run_publisher([*self.full_args(local={"gongyung": source}), "--apply"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("expected=gongyung found=daegyo", result.stderr)
        self.assertFalse((self.worktree / "pages/nodes/gongyung/AUTO.md").exists())

    def test_apply_preserves_human_verdict_and_appends_only_unseen_key(self) -> None:
        target = self.worktree / "pages/nodes/gwakga/AUTO.md"
        target.write_text(document("gwakga", item("a" * 12, status="promoted")), encoding="utf-8")
        source = self.source(
            "gwakga",
            document("gwakga", item("a" * 12), item("b" * 12)),
        )
        args = [*self.full_args(local={"gwakga": source}), "--apply"]
        first = self.run_publisher(args)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(first.stdout)
        self.assertEqual(report["appended"], 1)
        body = target.read_text(encoding="utf-8")
        self.assertIn("**상태**: `promoted`", body)
        self.assertEqual(body.count("**키**: `aaaaaaaaaaaa`"), 1)
        self.assertEqual(body.count("**키**: `bbbbbbbbbbbb`"), 1)

        second = self.run_publisher(args)
        self.assertEqual(second.returncode, 0, second.stderr)
        report2 = json.loads(second.stdout)
        self.assertEqual(report2["appended"], 0)
        self.assertEqual(report2["changed_pages"], 0)
        self.assertEqual(target.read_text(encoding="utf-8"), body)

    def test_duplicate_key_fails_closed(self) -> None:
        source = self.source(
            "nosuk",
            document("nosuk", item("c" * 12), item("c" * 12)),
        )
        result = self.run_publisher(self.full_args(local={"nosuk": source}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate candidate key", result.stderr)

    def test_secret_and_conflict_markers_fail_before_write(self) -> None:
        secret = self.source(
            "seoseo",
            document("seoseo", item("d" * 12, fact="ghp_" + "A" * 24)),
        )
        secret_result = self.run_publisher([*self.full_args(local={"seoseo": secret}), "--apply"])
        self.assertEqual(secret_result.returncode, 2)
        self.assertIn("secret-like content", secret_result.stderr)

        conflict = self.source(
            "seoseo",
            document("seoseo", item("e" * 12)) + "<<<<<<< ours\n",
        )
        conflict_result = self.run_publisher(
            [*self.full_args(local={"seoseo": conflict}), "--apply"]
        )
        self.assertEqual(conflict_result.returncode, 2)
        self.assertIn("merge-conflict marker", conflict_result.stderr)
        self.assertFalse((self.worktree / "pages/nodes/seoseo/AUTO.md").exists())

    def test_remote_read_failure_is_not_silently_empty(self) -> None:
        fake_ssh = self.root / "ssh"
        fake_ssh.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
        fake_ssh.chmod(0o700)
        result = self.run_publisher(
            [
                *self.full_args(remote={"bangtong": "bangtong"}),
                "--ssh-bin",
                str(fake_ssh),
            ]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("node=bangtong host=bangtong rc=23", result.stderr)

    def test_submit_requires_apply(self) -> None:
        result = self.run_publisher([*self.full_args(), "--submit"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("--submit requires --apply", result.stderr)

    def test_apply_and_submit_invokes_one_pr_after_full_validation(self) -> None:
        source = self.source("jingun", document("jingun", item("f" * 12)))
        calls = self.root / "wiki-agent.calls"
        fake = self.root / "wiki-agent"
        fake.write_text(
            "#!/bin/sh\n"
            f'echo "$1" >> {calls}\n'
            f'[ "$1" = write-path ] && echo {self.worktree}\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o700)
        result = self.run_publisher(
            [
                *self.full_args(local={"jingun": source}),
                "--wiki-agent-bin",
                str(fake),
                "--apply",
                "--submit",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["write-path", "pr"])
        report = json.loads(result.stdout)
        self.assertTrue(report["submitted"])
        self.assertTrue((self.worktree / "pages/nodes/jingun/AUTO.md").exists())

    def test_one_bad_node_keeps_all_pages_untouched(self) -> None:
        good = self.source("yukson", document("yukson", item("1" * 12)))
        bad = self.source("soonwook", document("daegyo", item("2" * 12)))
        result = self.run_publisher(
            [
                *self.full_args(local={"yukson": good, "soonwook": bad}),
                "--apply",
            ]
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.worktree / "pages/nodes/yukson/AUTO.md").exists())


if __name__ == "__main__":
    unittest.main()
