"""Shell controller gates using its existing source-only seam, without services."""
import os
from pathlib import Path
import subprocess

import pytest


START = Path(__file__).resolve().parents[1] / "start.sh"


@pytest.mark.parametrize("options, spawn", [
    (["--recovery-source"], False),
    (["--recovery-runtime"], False),
    (["--restart", "--recovery-source", "old"], False),
    (["--restart", "--recovery-source", "old", "--recovery-runtime", "job"], False),
    (["--prepared-runtime", "job", "--recovery-source", "old", "--recovery-runtime", "job"], False),
    (["--prepared-runtime", "job", "--restart", "--recovery-source", "old", "--recovery-runtime", "old-job"], True),
])
def test_option_refusal_precedes_project_effects(tmp_path, options, spawn):
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    if spawn:
        env["CCC_BRIDGE_RESTART_SPAWN"] = "/never-executed"
    result = subprocess.run(["bash", str(START), "--path", str(tmp_path), *options],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not (tmp_path / ".telegram_bot").exists()


@pytest.mark.parametrize("failure, expected, phases", [
    ("stop", 1, ["validated", "stop", "stop_failed"]),
    ("journal", 9, ["validated"]),
])
def test_restart_does_not_launch_or_recover_after_prelaunch_failure(tmp_path, failure, expected, phases):
    # All process/lifecycle predicates are fixtures. The real do_restart decides
    # ordering; any accidental spawn or recovery becomes an explicit failure.
    script = r'''
CCC_START_SH_LIB_ONLY=1 . "$1" --path "$2" >/dev/null
TRANSITION_RUN=fixture
PREPARED_RUNTIME=fixture
RECOVERY_RUNTIME="$2/previous"
mkdir -p "$RECOVERY_RUNTIME/runtime/bin"
printf '#!/bin/sh\nprintf "{}\\n"\n' > "$RECOVERY_RUNTIME/runtime/bin/python"
chmod 700 "$RECOVERY_RUNTIME/runtime/bin/python"
restart_caller_bridge_ancestor() { :; }
bash() { case "$1" in */service-systemd.sh) return 1;; *) echo unexpected-command >> "$2/events"; exit 99;; esac; }
merge_env_files() { :; }
check_env() { :; }
validate_prepared_runtime() { printf '{}\n'; }
verify_previous_serving() { return 0; }
read_pid() { :; }
read_supervisor_pid() { :; }
transition_phase() { echo "$1" >> "$PROJECT_ROOT/events"; if [ "$FAILURE" = journal ]; then exit 9; fi; }
do_stop() { echo stop >> "$PROJECT_ROOT/events"; return 1; }
finish_prepared_restart_failure() { echo unexpected-recovery >> "$PROJECT_ROOT/events"; exit 99; }
do_restart
'''
    result = subprocess.run(["bash", "-c", script, "fixture", str(START), str(tmp_path)],
                            env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "FAILURE": failure},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stdout + result.stderr
    assert (tmp_path / "events").read_text().splitlines() == phases


def test_successful_recovery_command_cannot_replace_pinned_serving_verification(tmp_path):
    script = r'''
CCC_START_SH_LIB_ONLY=1 . "$1" --path "$2" >/dev/null
TRANSITION_RUN=fixture
RECOVERY_SOURCE="$2/old"
RECOVERY_RUNTIME="$2/job"
transition_phase() { echo "$1" >> "$PROJECT_ROOT/events"; }
bash() { echo recovery >> "$PROJECT_ROOT/events"; return 0; }
verify_previous_serving() { return 1; }
finish_prepared_restart_failure 4
'''
    result = subprocess.run(["bash", "-c", script, "fixture", str(START), str(tmp_path)],
                            env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 8, result.stdout + result.stderr
    assert (tmp_path / "events").read_text().splitlines() == ["candidate_failed", "recovery", "recovery_failed"]
