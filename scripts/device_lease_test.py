"""Tests for scripts/device_lease.py (#2927)."""
import importlib.util
import json
import os
import shlex
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("device_lease", os.path.join(HERE, "device_lease.py"))
dl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dl)

T0 = 1_800_000_000


def st(tmp_path):
    return str(tmp_path / "d" / "device-lease.json")


def test_acquire_free_then_busy_for_other_node(tmp_path):
    rc, out = dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="Play 출시", now=T0)
    assert rc == 0 and out["lease"]["node"] == "dungae" and out["lease"]["expires_at_unix"] == T0 + 900
    rc, out = dl.apply("acquire", st(tmp_path), node="gongyung", session="g1", purpose="랜딩 확인", now=T0 + 60)
    assert rc == dl.EXIT_BUSY and out["lease"]["node"] == "dungae" and out["lease"]["purpose"] == "Play 출시"


def test_same_holder_reacquire_renews_and_keeps_acquired_at(tmp_path):
    dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="a", now=T0)
    rc, out = dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="a", now=T0 + 300)
    assert rc == 0 and out["renewed"] is True
    assert out["lease"]["acquired_at"] == dl._iso(T0) and out["lease"]["expires_at_unix"] == T0 + 1200


def test_same_node_other_session_is_busy(tmp_path):
    dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="a", now=T0)
    rc, _ = dl.apply("acquire", st(tmp_path), node="dungae", session="s2", purpose="b", now=T0 + 1)
    assert rc == dl.EXIT_BUSY


def test_expired_lease_is_free(tmp_path):
    dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="a", ttl=60, now=T0)
    rc, out = dl.apply("status", st(tmp_path), node="x", session="y", now=T0 + 61)
    assert rc == 0 and out["held"] is False and out["expired"]["node"] == "dungae"
    rc, out = dl.apply("acquire", st(tmp_path), node="gongyung", session="g1", purpose="b", now=T0 + 61)
    assert rc == 0 and out["lease"]["node"] == "gongyung"


def test_release_only_by_holder(tmp_path):
    dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="a", now=T0)
    rc, _ = dl.apply("release", st(tmp_path), node="gongyung", session="g1", now=T0 + 1)
    assert rc == dl.EXIT_NOT_HOLDER and os.path.exists(st(tmp_path))
    rc, out = dl.apply("release", st(tmp_path), node="dungae", session="s1", now=T0 + 2)
    assert rc == 0 and out["released"] is True and not os.path.exists(st(tmp_path))
    rc, out = dl.apply("release", st(tmp_path), node="dungae", session="s1", now=T0 + 3)
    assert rc == 0 and out["released"] is False


def test_renew_requires_holder(tmp_path):
    rc, _ = dl.apply("renew", st(tmp_path), node="dungae", session="s1", now=T0)
    assert rc == dl.EXIT_NOT_HOLDER
    dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="a", now=T0)
    rc, out = dl.apply("renew", st(tmp_path), node="dungae", session="s1", ttl=1800, now=T0 + 600)
    assert rc == 0 and out["lease"]["expires_at_unix"] == T0 + 2400


def test_corrupt_file_fails_closed(tmp_path):
    p = st(tmp_path)
    os.makedirs(os.path.dirname(p))
    with open(p, "w") as fh:
        fh.write("{not json")
    rc, out = dl.apply("acquire", p, node="dungae", session="s1", purpose="a", now=T0)
    assert rc == dl.EXIT_ERROR and "unreadable" in out["error"]
    with open(p) as fh:
        assert fh.read() == "{not json"


def test_lease_file_mode_is_private(tmp_path):
    dl.apply("acquire", st(tmp_path), node="dungae", session="s1", purpose="a", now=T0)
    assert os.stat(st(tmp_path)).st_mode & 0o077 == 0


@pytest.mark.parametrize("text,secs", [("900", 900), ("90s", 90), ("15m", 900), ("2h", 7200)])
def test_parse_ttl_ok(text, secs):
    assert dl.parse_ttl(text) == secs


@pytest.mark.parametrize("text", ["", "10", "3h", "15 min", "-5m"])
def test_parse_ttl_rejects(text):
    with pytest.raises(ValueError):
        dl.parse_ttl(text)


def _cli(tmp_path, *args, env=None):
    e = {**os.environ, "CCC_NODE": "dungae", "CCC_LEASE_SESSION": "cli-1", **(env or {})}
    return subprocess.run([sys.executable, os.path.join(HERE, "device_lease.py"), "--local",
                           "--state-file", st(tmp_path), *args], capture_output=True, text=True, env=e)


def test_cli_roundtrip_and_busy_exit(tmp_path):
    p = _cli(tmp_path, "acquire", "--purpose", "Play 출시 확인")
    assert p.returncode == 0 and json.loads(p.stdout)["lease"]["purpose"] == "Play 출시 확인"
    p = _cli(tmp_path, "acquire", "--purpose", "x", env={"CCC_NODE": "gongyung", "CCC_LEASE_SESSION": "g"})
    assert p.returncode == 3 and json.loads(p.stdout)["lease"]["node"] == "dungae"
    assert _cli(tmp_path, "release").returncode == 0


def test_cli_acquire_needs_purpose(tmp_path):
    assert _cli(tmp_path, "acquire").returncode == 2


def test_remote_command_quotes_every_word(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    monkeypatch.setenv("CCC_NODE", "dungae")
    monkeypatch.setenv("CCC_LEASE_SESSION", "sess 1")
    rc = dl.main(["--target", "daegyo", "acquire", "--purpose", "Play 출시; rm -rf ~"])
    assert rc == 0 and seen["cmd"][:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    assert seen["cmd"][5] == "daegyo"
    words = shlex.split(seen["cmd"][6])
    assert words[:3] == ["python3", "-", "--local"]
    assert words[words.index("--purpose") + 1] == "Play 출시; rm -rf ~"
    assert words[words.index("--session") + 1] == "sess 1" and words[-1] == "acquire"
    assert "def apply(" in seen["input"]


def test_remote_unexpected_exit_is_error(monkeypatch):
    monkeypatch.setattr(dl.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 255, "", "ssh: no route"))
    assert dl.main(["--target", "daegyo", "status"]) == dl.EXIT_ERROR
