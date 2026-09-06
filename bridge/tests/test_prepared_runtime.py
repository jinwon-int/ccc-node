"""Prepared launch fails before lifecycle changes and preserves prior records."""
import json
import os
from pathlib import Path
import time

import pytest

from telegram_bot import prepared_runtime as prepared
from telegram_bot import dependency_bootstrap as bootstrap


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("__init__.py", "app.py", "requirements.txt", "requirements.lock.txt", "pyproject.toml"):
        (source / name).write_text("original\n")
    work = tmp_path / "job"
    work.mkdir(mode=0o700)
    runtime = work / "runtime"
    runtime.mkdir()
    paths = bootstrap.DependencyPaths.from_roots(source, runtime, work / "absent")
    paths.hash_cache.write_text(bootstrap.dependency_fingerprint(paths, bootstrap.InstallMode.LOCKED))
    report = dict(schema="ccc.termux-preparation.v1", status="ready", work_dir=str(work),
                  scenarios={"fresh_install": "pass"}, source_seal=prepared.source_seal(source))
    (work / "receipt.json").write_text(json.dumps(report))
    (work / "receipt.json").chmod(0o600)
    monkeypatch.setattr(prepared.sys, "prefix", str(runtime))
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: type("Package", (), {"origin": str(source / "__init__.py")})())
    return source, work, paths


def test_identity_accepts_sealed_source_and_locked_fingerprint(sealed):
    source, work, paths = sealed
    result = prepared.prepared_identity(source, work)
    assert result["runtime_dir"] == str(work / "runtime")
    assert result["dependency_fingerprint"] == paths.hash_cache.read_text()


@pytest.mark.parametrize("mutation", ["code", "new_code", "lock", "fingerprint", "receipt", "unsealed", "relocated", "prefix", "runtime_symlink"])
def test_invalid_preparations_rejected(sealed, mutation, monkeypatch):
    source, work, paths = sealed
    if mutation == "code":
        (source / "app.py").write_text("changed")
    elif mutation == "new_code":
        (source / "new.py").write_text("new")
    elif mutation == "lock":
        (source / "requirements.lock.txt").write_text("changed")
    elif mutation == "fingerprint":
        paths.hash_cache.write_text("stale")
    elif mutation == "prefix":
        monkeypatch.setattr(prepared.sys, "prefix", str(source))
    elif mutation == "runtime_symlink":
        (work / "runtime").rename(work / "old-runtime")
        (work / "runtime").symlink_to(work / "old-runtime", target_is_directory=True)
    else:
        p = work / "receipt.json"
        record = json.loads(p.read_text())
        if mutation == "receipt":
            record["status"] = "error"
        elif mutation == "unsealed":
            record.pop("source_seal")
        else:
            record["work_dir"] = "/another/job"
        p.write_text(json.dumps(record))
    with pytest.raises((ValueError, OSError)):
        prepared.prepared_identity(source, work)


def test_editable_source_must_match_selected_launcher(sealed, monkeypatch):
    import importlib.util
    source, work, _ = sealed
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: type("Package", (), {"origin": "/other/bridge/__init__.py"})())
    with pytest.raises(ValueError, match="editable_source"):
        prepared.prepared_identity(source, work)


def test_seal_ignores_secrets_and_generated_outputs(sealed):
    source, _, _ = sealed
    before = prepared.source_seal(source)
    (source / ".env").write_text("PRIVATE")
    (source / ".credentials.json").write_text("PRIVATE")
    for directory in ("__pycache__", "venv", "thing.egg-info", "tests"):
        (source / directory).mkdir()
        (source / directory / "generated.py").write_text("changed")
    assert prepared.source_seal(source) == before


def test_seal_detects_symlinks_and_bounds_file_reads(sealed):
    source, _, _ = sealed
    (source / "linked.py").symlink_to(source / "app.py")
    with pytest.raises(OSError):
        prepared.source_seal(source)
    with pytest.raises(ValueError, match="oversize"):
        prepared.bounded_read(source / "app.py", 2)


def test_launch_records_are_private_append_only(sealed, tmp_path):
    source, work, _ = sealed
    directory = tmp_path / "history"
    report = prepared.prepared_identity(source, work)
    old = os.umask(0o022)
    try:
        prepared.record_validation(directory, report, 123)
        first = list(directory.iterdir())[0]
        original = first.read_bytes()
        prepared.record_validation(directory, report, 124)
    finally:
        os.umask(old)
    assert len(list(directory.iterdir())) == 2
    assert first.read_bytes() == original
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    assert json.loads(original)["phase"] == "validated_before_launch"
    assert "serving" not in json.loads(original)


def test_record_directory_symlink_is_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        prepared.record_validation(link, {}, 1)
    assert not list(target.iterdir())


def test_probe_timeout_kills_owned_group(tmp_path):
    marker = tmp_path / "pid"
    command = ("import subprocess,time; from pathlib import Path; "
               "p=subprocess.Popen(['sleep','30']); "
               f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(30)")
    result = prepared.probe("fixture", ("-c", command), time.monotonic() + 0.3)
    assert result["status"] == "timeout"
    p = Path(f"/proc/{int(marker.read_text())}/stat")
    for _ in range(100):
        try:
            if p.read_text().split()[2] == "Z":
                return
        except (FileNotFoundError, ProcessLookupError):
            return
        time.sleep(0.01)
    pytest.fail("worker still runnable")


def test_identity_failure_never_probes_or_records(sealed, monkeypatch, tmp_path, capsys):
    source, work, _ = sealed
    (source / "app.py").write_text("drift")
    monkeypatch.setattr(prepared, "probe", lambda *a: pytest.fail("probe after invalid identity"))
    directory = tmp_path / "records"
    assert prepared.main(["--bridge-dir", str(source), "--prepared-dir", str(work), "--record-dir", str(directory)]) == 1
    assert not directory.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "error"
    assert "drift" not in json.dumps(report)


def test_record_rejects_symlink_parent_before_creating_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "parent-link"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        prepared.record_validation(link / "history", {}, 1)
    assert not list(real.iterdir())


def test_drift_during_probes_prevents_launch_record(sealed, monkeypatch, tmp_path, capsys):
    source, work, _ = sealed
    def mutate(name, command, deadline):
        (source / "app.py").write_text("changed during probe")
        return {"id": name, "status": "pass"}
    monkeypatch.setattr(prepared, "probe", mutate)
    directory = tmp_path / "records"
    assert prepared.main(["--bridge-dir", str(source), "--prepared-dir", str(work), "--record-dir", str(directory)]) == 1
    assert not directory.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "error"
