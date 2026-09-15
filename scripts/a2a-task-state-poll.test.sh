#!/usr/bin/env bash
# Synthetic payloads only; no real broker, GitHub, or live task execution.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
python3 - "$HERE/a2a-task-state-poll.sh" <<'PY'
import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

POLLER = sys.argv.pop()


def body_succeeded_flat(pr_url="https://github.com/o/r/pull/115"):
    return {"status": "succeeded",
            "result": {"output": {"repo": "o/r", "prUrl": pr_url}}}


def body_succeeded_nested():
    return {"status": "succeeded",
            "result": {"output": {"github": {"prUrl": "https://github.com/o/r/pull/116"}}}}


def body_succeeded_no_pr():
    return {"status": "succeeded", "result": {"output": {"summary": "no patch"}}}


def body_failed_with_pr():
    return {"status": "failed",
            "result": {"output": {"prUrl": "https://github.com/o/r/pull/117"}}}


class FakeBroker:
    """Route /tasks/<encoded-id> to fixture bodies; record observed requests."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.lock = threading.Lock()
        self._server = None

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                with outer.lock:
                    outer.requests.append(
                        {"path": self.path,
                         "secret": self.headers.get("x-a2a-edge-secret")})
                body = outer.routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="a2a-task-state-poll-test-")
        self.addCleanup(self.fixture.cleanup)
        self.root = self.fixture.name
        self.broker = FakeBroker()
        self.base = self.broker.start()
        self.addCleanup(self.broker.stop)
        self.secret_file = os.path.join(self.root, "broker.env")
        with open(self.secret_file, "w", encoding="utf-8") as fh:
            fh.write('BROKER_EDGE_SECRET="test-secret-value-xyz"\n')

    def log_path(self, name="task-state.log"):
        path = os.path.join(self.root, name)
        return path

    def run_watch(self, pairs, extra=None, expect=0):
        cmd = [POLLER, "--broker", self.base,
               "--secret-file", f"{self.secret_file}:BROKER_EDGE_SECRET"]
        for task_id, log in pairs:
            cmd += ["--task", task_id, "--log", log]
        cmd += list(extra or [])
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=dict(os.environ, TMPDIR=self.root))
        self.assertEqual(proc.returncode, expect,
                         f"stdout={proc.stdout} stderr={proc.stderr}")
        return proc

    def lines(self, log):
        with open(log, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()

    # -- watch mode: terminal evidence comes from the broker record --------

    def test_succeeded_with_flat_pr_url_logs_the_url(self):
        log = self.log_path()
        self.broker.routes["/tasks/t-flat"] = body_succeeded_flat()
        self.run_watch([("t-flat", log)])
        self.assertTrue(any("state=succeeded" in line for line in self.lines(log)))
        self.assertTrue(any("pr=https://github.com/o/r/pull/115" in line
                            for line in self.lines(log)))

    def test_succeeded_with_nested_github_pr_url_logs_the_url(self):
        log = self.log_path()
        self.broker.routes["/tasks/t-nested"] = body_succeeded_nested()
        self.run_watch([("t-nested", log)])
        self.assertTrue(any("pr=https://github.com/o/r/pull/116" in line
                            for line in self.lines(log)))

    def test_succeeded_without_pr_logs_explicit_none_marker(self):
        # The #1760 defect shape: a bare empty `prs=` read like "no PR".
        # The replacement marker must be explicit and non-empty.
        log = self.log_path()
        self.broker.routes["/tasks/t-nopr"] = body_succeeded_no_pr()
        self.run_watch([("t-nopr", log)])
        self.assertTrue(any("pr=none-in-broker-result" in line
                            for line in self.lines(log)))
        for line in self.lines(log):
            self.assertFalse(line.startswith(("state=",)) and "pr= " in line)

    def test_failed_task_still_reports_its_pr_evidence(self):
        log = self.log_path()
        self.broker.routes["/tasks/t-fail"] = body_failed_with_pr()
        self.run_watch([("t-fail", log)])
        self.assertTrue(any("state=failed" in line for line in self.lines(log)))
        self.assertTrue(any("pr=https://github.com/o/r/pull/117" in line
                            for line in self.lines(log)))

    def test_non_terminal_status_has_no_pr_line(self):
        log = self.log_path()
        self.broker.routes["/tasks/t-run"] = {"status": "running"}
        self.run_watch([("t-run", log)])
        lines = self.lines(log)
        self.assertTrue(any("state=running" in line for line in lines))
        self.assertFalse(any("pr=" in line for line in lines))

    def test_unparseable_body_is_recorded_without_fabrication(self):
        log = self.log_path()
        self.broker.routes["/tasks/t-junk"] = {"not": "a task"}
        self.run_watch([("t-junk", log)])
        self.assertTrue(any("state=unparseable" in line for line in self.lines(log)))
        self.assertFalse(any("pr=https" in line for line in self.lines(log)))

    def test_http_404_is_recorded_as_http_state(self):
        log = self.log_path()
        # A single task that 404s means every pair failed → exit 1, and the
        # log still records the observable fact for the next invocation.
        self.run_watch([("t-missing", log)], expect=1)
        self.assertTrue(any("state=http-404" in line for line in self.lines(log)))

    def test_unreachable_broker_is_recorded_as_fetch_failed(self):
        log = self.log_path()
        cmd = [POLLER, "--broker", "http://127.0.0.1:1", "--task", "t", "--log", log]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=dict(os.environ, TMPDIR=self.root))
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(any("state=fetch-failed" in line for line in self.lines(log)))

    # -- operational contracts ----------------------------------------------

    def test_task_ids_with_colons_are_percent_encoded(self):
        log = self.log_path()
        raw_id = "lane:RNM-1:terminology_bilingual"
        encoded = urllib.parse.quote(raw_id, safe="")
        self.broker.routes[f"/tasks/{encoded}"] = body_succeeded_flat()
        self.run_watch([(raw_id, log)])
        self.assertTrue(any("state=succeeded" in line for line in self.lines(log)))
        with self.broker.lock:
            served = [r["path"] for r in self.broker.requests]
        self.assertIn(f"/tasks/{encoded}", served)

    def test_secret_never_reaches_the_log(self):
        log = self.log_path()
        self.broker.routes["/tasks/t-sec"] = body_succeeded_flat()
        self.run_watch([("t-sec", log)])
        with open(log, "r", encoding="utf-8") as fh:
            self.assertNotIn("test-secret-value-xyz", fh.read())

    def test_secret_reaches_the_broker_via_header(self):
        log = self.log_path()
        self.broker.routes["/tasks/t-hdr"] = body_succeeded_flat()
        self.run_watch([("t-hdr", log)])
        with self.broker.lock:
            secrets = {r["secret"] for r in self.broker.requests}
        self.assertEqual(secrets, {"test-secret-value-xyz"})

    def test_log_file_is_private_and_append_only(self):
        log = self.log_path()
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("2026-09-15T23:33:32+0900 state=running\n")
        self.broker.routes["/tasks/t-app"] = body_succeeded_flat()
        self.run_watch([("t-app", log)])
        lines = self.lines(log)
        self.assertEqual(lines[0], "2026-09-15T23:33:32+0900 state=running")
        mode = stat.S_IMODE(os.stat(log).st_mode)
        self.assertEqual(mode, 0o600)

    def test_all_pairs_fail_exits_nonzero_but_still_logs(self):
        log = self.log_path()
        self.run_watch([("t-absent", log)], expect=1)
        self.assertTrue(any("state=http-404" in line for line in self.lines(log)))

    # -- usage contract ------------------------------------------------------

    def test_missing_broker_is_usage_error(self):
        proc = subprocess.run([POLLER, "--task", "t", "--log", "/tmp/x.log"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 64)

    def test_task_log_count_mismatch_is_usage_error(self):
        proc = subprocess.run(
            [POLLER, "--broker", self.base, "--task", "t"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 64)

    def test_no_pairs_is_usage_error(self):
        proc = subprocess.run([POLLER, "--broker", self.base],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 64)

    # -- offline analysis mode ----------------------------------------------

    def run_from_file(self, body):
        path = os.path.join(self.root, "body.json")
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(body, str):
                fh.write(body)
            else:
                json.dump(body, fh)
        proc = subprocess.run([POLLER, "--from-file", path],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = dict(line.split("=", 1) for line in proc.stdout.splitlines())
        return fields

    def test_from_file_flat_nested_and_none(self):
        self.assertEqual(
            self.run_from_file(body_succeeded_flat()),
            {"status": "succeeded", "pr_url": "https://github.com/o/r/pull/115"})
        self.assertEqual(
            self.run_from_file(body_succeeded_nested()),
            {"status": "succeeded", "pr_url": "https://github.com/o/r/pull/116"})
        self.assertEqual(
            self.run_from_file(body_succeeded_no_pr()),
            {"status": "succeeded", "pr_url": "none-in-broker-result"})

    def test_from_file_invalid_json_is_unparseable(self):
        self.assertEqual(
            self.run_from_file("{not json"),
            {"status": "unparseable", "pr_url": "none-in-broker-result"})


if __name__ == "__main__":
    unittest.main()
PY
