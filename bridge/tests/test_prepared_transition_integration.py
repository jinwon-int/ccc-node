"""Opt-in controller exercises actual retained-pair restart and recovery."""
import json

import pytest

from test_prepared_recovery import Rehearsal, pytestmark as pytestmark
from telegram_bot.prepared_serving import process_alive


@pytest.fixture
def rehearsal(tmp_path):
    r = Rehearsal(tmp_path)
    try:
        r.prepare("previous")
        r.prepare("candidate")
        rc, output = r.command("previous")
        assert rc == 0, output
        yield r
    finally:
        r.close()


def controlled(r, daemon=False):
    source, work = r.generations["previous"]
    return r.command("candidate", extra_args=("--recovery-source", str(source),
                     "--recovery-runtime", str(work), *(("--daemon",) if daemon else ())), timeout=90)


def records(r):
    root = r.data / "runtime-transitions"
    assert not (root / "active").exists()
    runs = list(root.iterdir())
    assert len(runs) == 1
    return [json.loads(p.read_text()) for p in sorted(runs[0].glob("*.json"))]


@pytest.mark.parametrize("candidate_mode, recovery_mode, expected, phase, daemon", [
    ("ready", "ready", 0, "candidate_available", False),
    ("unready", "ready", 7, "recovered", False),
    ("crash", "ready", 7, "recovered", False),
    ("unready", "unready", 8, "recovery_failed", False),
    ("unready", "ready", 7, "recovered", True),
])
def test_controlled_candidate_and_single_recovery(rehearsal, candidate_mode, recovery_mode, expected, phase, daemon):
    r = rehearsal
    old = r.health()
    (r.project / "candidate.mode").write_text(candidate_mode)
    (r.project / "previous.mode").write_text(recovery_mode)
    rc, output = controlled(r, daemon)
    assert rc == expected, output
    evidence = records(r)
    assert evidence[-1]["phase"] == phase
    assert evidence[-1]["controller_exit_code"] == expected
    assert evidence[1]["previous"]["source_seal"] == old["runtime_generation"]["source_seal"]
    events = [e for e in r.events() if e["kind"] == "start"]
    assert [e["label"] for e in events] == (["previous", "candidate"] if expected == 0 else ["previous", "candidate", "previous"])
    assert all(e["label"] == e["dependency"] for e in events)
    assert all(e["kind"] != "overlap" for e in r.events())
    if expected == 7:
        for key in ("source_dir", "source_seal", "python_prefix", "dependency_fingerprint"):
            assert r.health()["runtime_generation"][key] == old["runtime_generation"][key]
    assert not r.forbidden_install.exists()


@pytest.mark.parametrize("refusal", ["invalid_previous", "wrong_serving", "incomplete_lease"])
def test_recovery_preconditions_refuse_before_stop(rehearsal, refusal):
    r = rehearsal
    if refusal == "invalid_previous":
        (r.generations["previous"][1] / "runtime/.req_hash").write_text("invalid")
    elif refusal == "wrong_serving":
        rc, output = r.command("candidate")
        assert rc == 0, output
    else:
        root = r.data / "runtime-transitions"
        root.mkdir(mode=0o700)
        (root / "active").mkdir(mode=0o700)
    pid = r.health()["process"]["pid"]
    before = r.events()
    rc, output = controlled(r)
    assert rc == 6, output
    assert process_alive(pid)
    assert r.health()["process"]["pid"] == pid
    assert r.events() == before
    if refusal != "incomplete_lease":
        assert records(r)[-1]["phase"] == "rejected"


@pytest.mark.parametrize("phase", ["validated", "candidate_available"])
def test_journal_failure_cannot_report_success_or_skip_before_stop_gate(rehearsal, phase):
    r = rehearsal
    old_pid = r.health()["process"]["pid"]
    before = r.events()
    wrapper = r.bin / "python3"
    script = wrapper.read_text()
    wrapper.write_text(script.replace("#!/bin/sh\n", '#!/bin/sh\ncase " $* " in\n'
        f'  *"prepared_transition.py advance "*" --phase {phase} "*) exit 99 ;;\nesac\n', 1))
    rc, output = controlled(r)
    assert rc == 9, output
    root = r.data / "runtime-transitions"
    assert (root / "active/owner.json").exists()
    if phase == "validated":
        assert process_alive(old_pid)
        assert r.health()["process"]["pid"] == old_pid
        assert r.events() == before
    else:
        assert [e["label"] for e in r.events() if e["kind"] == "start"] == ["previous", "candidate"]
        assert r.health()["runtime_generation"]["source_dir"] == str(r.generations["candidate"][0])
