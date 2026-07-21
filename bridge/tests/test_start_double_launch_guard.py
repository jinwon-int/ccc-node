"""find_project_bot_pids literal-match regression (#446).

start.sh's shared "is this project's bot running?" oracle used to splice
PROJECT_ROOT raw into a pgrep ERE (`--path ${PROJECT_ROOT}( |$)`). Paths with
regex metacharacters (`.`, space, `(`, `+`, …) were then mis-judged: a false
negative spawns a second instance and self-inflicts a Telegram getUpdates
Conflict; a false positive lets `--stop` target another process. These tests
pin exact-argv matching via the real `--status` unmanaged-PID fallback, which
shares find_project_bot_pids with --stop and the double-launch guard.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform.startswith("linux"), "uses /proc + pgrep")
@unittest.skipIf(shutil.which("pgrep") is None, "pgrep unavailable")
class DoubleLaunchGuardTests(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[1]
        self.start_script = self.repo_root / "start.sh"
        self._procs: list[subprocess.Popen] = []
        self._dirs: list[str] = []

    def tearDown(self):
        for p in self._procs:
            p.kill()
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mkdir(self, prefix: str) -> str:
        d = tempfile.mkdtemp(prefix=prefix)
        self._dirs.append(d)
        return str(Path(d).resolve())

    def _decoy(self, path_arg: str) -> subprocess.Popen:
        # /proc/<pid>/cmdline == python3 -c <sleep> -m telegram_bot --path <path_arg>
        p = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                "-m",
                "telegram_bot",
                "--path",
                path_arg,
            ]
        )
        self._procs.append(p)
        return p

    def _unmanaged_pids(self, project_root: str) -> set[str]:
        env = dict(os.environ)
        env.pop("PROJECT_ROOT", None)
        env.pop("CCC_AGENT_PROVIDER", None)
        r = subprocess.run(
            ["bash", str(self.start_script), project_root, "--status"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        pids: set[str] = set()
        for line in (r.stdout + r.stderr).splitlines():
            if "unmanaged PID(s):" in line:
                seg = line.split("unmanaged PID(s):", 1)[1]
                for tok in seg.replace("(", " ").replace(")", " ").split():
                    if tok.isdigit():
                        pids.add(tok)
        return pids

    def test_metacharacter_path_is_detected(self):
        # Regex-metacharacter + space path must still match its own instance.
        root = self._mkdir("ccc.test(a+b) ")
        p = self._decoy(root)
        time.sleep(0.5)
        self.assertIn(str(p.pid), self._unmanaged_pids(root))

    def test_false_positive_avoided_on_metacharacter_path(self):
        # root has a literal '.'; the old ERE '.' would also match 'X'. A decoy
        # on the '.'-substituted sibling path must NOT be attributed to root.
        base = self._mkdir("ccc-guard-")
        root = base + "/pre.fix"
        sibling = base + "/preXfix"
        os.makedirs(root, exist_ok=True)
        exact = self._decoy(root)
        other = self._decoy(sibling)
        time.sleep(0.5)
        detected = self._unmanaged_pids(root)
        self.assertIn(str(exact.pid), detected)  # true positive kept
        self.assertNotIn(str(other.pid), detected)  # false positive gone

    def test_superstring_path_not_matched(self):
        # `/root` must not match `/rootX` — exact argv comparison anchors the end.
        base = self._mkdir("ccc-guard-")
        root = base + "/proj"
        os.makedirs(root, exist_ok=True)
        exact = self._decoy(root)
        longer = self._decoy(root + "X")
        time.sleep(0.5)
        detected = self._unmanaged_pids(root)
        self.assertIn(str(exact.pid), detected)
        self.assertNotIn(str(longer.pid), detected)

    def test_no_instance_reports_none(self):
        root = self._mkdir("ccc-guard-")
        self.assertEqual(self._unmanaged_pids(root), set())

    def _reap(self, project_root: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("PROJECT_ROOT", None)
        env.pop("CCC_AGENT_PROVIDER", None)
        return subprocess.run(
            ["bash", str(self.start_script), project_root, "--_reap-competing-pollers"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_reap_clears_competing_poller(self):
        # A stray poller for this project root (the 409-conflict token holder)
        # must be terminated by the supervisor's pre-restart reap so the crash
        # loop self-heals instead of leaving the bot down.
        root = self._mkdir("ccc-reap-")
        decoy = self._decoy(root)
        time.sleep(0.5)
        self.assertIn(str(decoy.pid), self._unmanaged_pids(root))  # sanity
        r = self._reap(root)
        self.assertEqual(r.returncode, 0, r.stderr)
        for _ in range(50):
            if decoy.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(decoy.poll(), "competing poller was not terminated")

    def test_reap_no_competitor_is_noop(self):
        # No competing poller → exit 0, nothing killed, no cleanup log line.
        root = self._mkdir("ccc-reap-")
        r = self._reap(root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Clearing competing bot poller", r.stdout)

    def test_reap_leaves_other_project_poller(self):
        # A poller for a DIFFERENT project root must not be touched.
        base = self._mkdir("ccc-reap-")
        root = base + "/proj"
        other = base + "/proj-other"
        os.makedirs(root, exist_ok=True)
        os.makedirs(other, exist_ok=True)
        keep = self._decoy(other)
        time.sleep(0.5)
        r = self._reap(root)
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(0.5)
        self.assertIsNone(keep.poll(), "unrelated project poller was killed")


if __name__ == "__main__":
    unittest.main()
