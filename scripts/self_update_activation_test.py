"""Adversarial persistence and startup-provenance regressions for #1527."""

import copy
import errno
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import time
from unittest.mock import patch

import pytest

PATH = Path(__file__).parent / "lib/self-update-activation.py"
spec = importlib.util.spec_from_file_location("activation", PATH)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


@pytest.fixture
def state(tmp_path):
    tmp_path.chmod(0o700)
    return tmp_path


def write(fd):
    a.write(fd, "a" * 40, "b" * 40, "pending", "[]", "/private/recovery")


def test_roundtrip_terminal_receipt(state):
    with a.directory(state) as fd:
        write(fd)
        pending = a.load(fd)
        assert pending["snapshot"] == "/private/recovery"
        assert (state / a.NAME).stat().st_mode & 0o777 == 0o600
        a.clear(fd, "a" * 40)
        assert a.load(fd)["outcome"] == "activated"
        assert a.load(fd)["started_at"] == pending["started_at"]
    assert a.main(["load", str(state), "a" * 40]) == 1
    assert a.main(["load", str(state), "c" * 40]) == 2


@pytest.mark.parametrize(
    "kind", ["dangling", "hardlink", "mode", "partial", "oversized", "tmp", "hidden-tmp", "intent"]
)
def test_unsafe_state_never_overwritten_or_cleared(state, kind):
    destination = state / a.NAME
    sentinel = state / "sentinel"
    sentinel.write_text("KEEP")
    if kind == "dangling":
        destination.symlink_to(state / "absent")
    elif kind == "hardlink":
        os.link(sentinel, destination)
    elif kind == "mode":
        destination.write_text("{}")
        destination.chmod(0o644)
    elif kind == "partial":
        destination.write_text("{")
        destination.chmod(0o600)
    elif kind == "oversized":
        destination.write_bytes(b"x" * (a.LIMIT + 1))
        destination.chmod(0o600)
    elif kind == "tmp":
        (state / (a.NAME + ".tmp.123")).symlink_to(sentinel)
    elif kind == "hidden-tmp":
        (state / ("." + a.NAME + ".tmp.123")).write_text("")
    else:
        (state / (a.NAME + ".intent")).symlink_to(state / "absent")
    with a.directory(state) as fd:
        with pytest.raises((ValueError, OSError, a.SecureFsError)):
            write(fd)
        with pytest.raises((ValueError, OSError, a.SecureFsError)):
            a.clear(fd, "a" * 40)
    assert sentinel.read_text() == "KEEP"


@pytest.mark.parametrize(
    "stage",
    [
        "write",
        "intent-file-sync",
        "intent-directory-sync",
        "file-sync",
        "replace",
        "directory-sync",
        "unsupported-sync",
        "unlink",
        "final-sync",
    ],
)
def test_persistence_failure_preserves_uncertainty(state, stage):
    with a.directory(state) as fd:
        write(fd)
        original_fsync = os.fsync
        count = 0

        def sync(descriptor):
            nonlocal count
            count += 1
            if count == {
                "intent-file-sync": 1,
                "intent-directory-sync": 2,
                "file-sync": 3,
                "directory-sync": 4,
                "unsupported-sync": 4,
                "final-sync": 5,
            }.get(stage):
                raise OSError(
                    errno.EINVAL if stage == "unsupported-sync" else errno.EIO,
                    "injected sync failure",
                )
            return original_fsync(descriptor)

        target = {"write": "os.write", "replace": "os.replace", "unlink": "os.unlink"}.get(stage)
        with patch("os.fsync", side_effect=sync):
            if target:
                with patch(target, side_effect=OSError("injected")):
                    with pytest.raises(OSError):
                        a.clear(fd, "a" * 40)
            else:
                with pytest.raises(OSError):
                    a.clear(fd, "a" * 40)
        with pytest.raises(ValueError, match="interrupted-write"):
            a.load(fd)
        assert (state / (a.NAME + ".intent")).exists()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def health_fixture(repo, now):
    return dict(
        schema_version=1,
        service=dict(state="available"),
        telegram=dict(state="healthy"),
        agent=dict(state="healthy"),
        updated_at=iso(now),
        process=dict(pid=os.getpid(), started_at=iso(now - 2)),
        runtime_generation=dict(
            schema="ccc.runtime-generation.v1",
            observed_at=iso(now - 1),
            source_dir=str(repo / "bridge"),
            source_git=dict(head="a" * 40, tracked_changes=False),
            collection_errors=[],
        ),
    )


@pytest.mark.parametrize(
    "bad",
    ["old-start", "short", "live-head-only", "dirty", "source-path", "stale", "dead-pid", "schema"],
)
def test_startup_identity_rejects_legacy_or_mismatch(state, bad):
    now = round(time.time(), 3)
    record = dict(target_sha="a" * 40, started_at=iso(now - 3))
    health = health_fixture(state, now)
    assert a.serving(record, state, health, now) == "a" * 40
    if bad == "old-start":
        health["process"]["started_at"] = iso(now - 4)
    elif bad == "short":
        health["runtime_generation"]["source_git"]["head"] = "a" * 7
    elif bad == "live-head-only":
        health = {"source_git": {"head": "a" * 40}}
    elif bad == "dirty":
        health["runtime_generation"]["source_git"]["tracked_changes"] = True
    elif bad == "source-path":
        health["runtime_generation"]["source_dir"] += "-lookalike"
    elif bad == "stale":
        now += 151
    elif bad == "dead-pid":
        health["process"]["pid"] = 2147483647
    else:
        health["runtime_generation"]["schema"] = "invented"
    with pytest.raises((ValueError, KeyError, OSError)):
        a.serving(record, state, health, now)


def test_probe_bounded_descendant_and_output(state):
    with a.directory(state) as fd:
        write(fd)
        started = time.monotonic()
        with pytest.raises(ValueError, match="probe-timeout"):
            a.probe(fd, str(state), "sleep 30 & exit 0", "1")
        assert time.monotonic() - started < 3
        with pytest.raises(ValueError, match="too-large"):
            a.probe(fd, str(state), "head -c 100000 /dev/zero", "1")
        with pytest.raises(ValueError):
            a.probe(fd, str(state), "printf aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "1")


def test_current_bridge_producer_supplies_frozen_full_head(state, monkeypatch):
    # Exercise the real producer, not an invented test-only wire contract.
    import sys

    sys.path.insert(0, str(PATH.parents[2]))
    from bridge import runtime_generation

    snapshot = runtime_generation.capture_runtime_generation()
    assert snapshot["schema"] == "ccc.runtime-generation.v1"
    assert len(snapshot["source_git"]["head"]) == 40
    old = copy.deepcopy(snapshot)
    monkeypatch.setattr(
        runtime_generation,
        "git_identity",
        lambda *args, **kwargs: dict(head="c" * 40, tracked_changes=False),
    )
    assert snapshot == old  # Existing object cannot be relabeled by later source reads.
    assert runtime_generation.capture_runtime_generation()["source_git"]["head"] == "c" * 40


def test_real_health_wire_reconciles_and_preserves_old_generation(state, monkeypatch):
    import json
    import subprocess
    import sys

    sys.path.insert(0, str(PATH.parents[2] / ".github/pythonpath"))
    from telegram_bot import runtime_generation as generation
    from telegram_bot.utils.health import RuntimeHealthReporter

    repo = state / "repo with spaces"
    source = repo / "bridge"
    source.mkdir(parents=True)
    (source / "runtime_generation.py").write_text("# fixture startup\n")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "startup",
    )
    target = git("rev-parse", "HEAD")
    prefix = state / "venv"
    prefix.mkdir()
    (prefix / ".req_hash").write_text("a" * 64)
    monkeypatch.setattr(generation, "__file__", str(source / "runtime_generation.py"))
    monkeypatch.setattr(generation.sys, "prefix", str(prefix))
    monkeypatch.setattr(generation.sys, "executable", str(prefix / "bin/python"))
    pending = dict(target_sha=target, started_at=iso(time.time() - 1))
    reporter = RuntimeHealthReporter(state / "project")
    reporter.initialize_process()
    reporter.record_telegram_ok()
    reporter.record_agent_ok()
    health = json.loads(reporter.health_file.read_text())
    assert a.serving(pending, repo, health) == target
    (source / "runtime_generation.py").write_text("# later checkout\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "later",
    )
    reporter.initialize_process()
    reporter.record_agent_ok()
    later_health = json.loads(reporter.health_file.read_text())
    assert later_health["runtime_generation"] == health["runtime_generation"]
    pending["target_sha"] = git("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="startup-generation-mismatch"):
        a.serving(pending, repo, later_health)


@pytest.mark.parametrize("later_snapshot", ["", "/private/new-forced-snapshot"])
def test_failed_same_target_recovery_preserves_original_provenance(state, later_snapshot):
    with a.directory(state) as fd:
        write(fd)
        original = a.load(fd)
        a.write(fd, "a" * 40, "a" * 40, "recovery-restart-failed", "[]", later_snapshot)
        retry = a.load(fd)
        assert retry["outcome"] == "recovery-restart-failed"
        for key in ("target_sha", "previous_sha", "started_at", "snapshot"):
            assert retry[key] == original[key]
