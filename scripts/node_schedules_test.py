"""Hermetic tests for scripts/ccc-node-schedules.py (#2914 stage 3).

Every source is a fixture or a stubbed command, so the assertions are about
what the collector keeps — and above all what it never keeps (command lines,
environment values, prompts) — not about this host's real schedules.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ccc_node_schedules", HERE / "ccc-node-schedules.py")
assert _spec is not None and _spec.loader is not None
ns = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ns
_spec.loader.exec_module(ns)

SECRET = "hunter2-SECRET-VALUE"


def test_crontab_keeps_schedule_script_and_comment_only():
    text = "\n".join([
        "# a comment line",
        f"API_TOKEN={SECRET}",
        "",
        f"3 6 * * 1 PATH=/usr/bin TOKEN=\"{SECRET}\" /opt/x/scripts/run-eval.sh --key={SECRET} >> /var/log/e.log 2>&1",
        f"*/5 * * * * cd /srv && bash -lc 'curl -H \"Authorization: {SECRET}\" https://x' # nunchi: owner check repo-a#12",
        "@reboot /usr/local/bin/startup-thing",
        f"47 * * * * /root/.wiki-agent/bin/wiki-agent semantic-refresh --token {SECRET}",
        "bad line",
    ])
    items = ns.crontab_lines(text)
    raw = json.dumps(items)
    assert SECRET not in raw and "Authorization" not in raw and "curl" not in raw
    assert [(i["schedule"], i["name"]) for i in items] == [
        ("3 6 * * 1", "run-eval.sh"), ("*/5 * * * *", "command"), ("@reboot", "startup-thing"), ("47 * * * *", "wiki-agent")]
    assert items[1]["summary"] == "nunchi: owner check repo-a#12"
    assert items[1]["issues"] == [{"repo": "repo-a", "number": 12}]
    assert all(i["source"] == "cron" and i["persistent"] is True for i in items)


def test_script_name_never_takes_assignments_or_flags():
    assert ns._script_name(['"TOKEN=abc"', "--node=CCC_NODE=x", "/opt/a/b.py"]) == "b.py"
    assert ns._script_name(["X=1", "-e", "FOO=bar"]) == ""
    assert ns._script_name(["$HOME/bin/tool"]) == "tool"
    assert ns._script_name(["$S/$NAME"]) == ""
    assert ns._script_name([">>", "/tmp/out.log"]) == ""


def test_detached_jobs_last_row_wins_and_acked_drop(tmp_path):
    rows = [
        {"unit": "check-a", "scheduled_at": "2026-11-30 11:40:00 Asia/Seoul", "summary": "repo-b#7 결과 확인",
         "log": f"/x/{SECRET}"},
        {"unit": "check-b", "scheduled_at": "2026-12-01 09:00", "summary": "x"},
        {"unit": "check-b", "acked": True},
        {"unit": "run-now", "started_at": "2026-11-28", "summary": "not scheduled"},
    ]
    (tmp_path / "detached-jobs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    items = ns.detached_jobs(tmp_path)
    assert [i["name"] for i in items] == ["check-a"]
    assert items[0]["next_at"] == "2026-11-30 11:40:00 Asia/Seoul" and items[0]["persistent"] is False
    assert items[0]["issues"] == [{"repo": "repo-b", "number": 7}]
    assert SECRET not in json.dumps(items)
    assert ns.detached_jobs(tmp_path / "missing") == []


def test_agent_cron_never_keeps_the_prompt(tmp_path):
    (tmp_path / "agent-cron").mkdir()
    (tmp_path / "agent-cron" / "tasks.json").write_text(json.dumps({"version": 1, "tasks": [
        {"id": "weekly-x", "name": "weekly #55 check", "schedule": "0 9 * * 1", "enabled": False,
         "prompt": SECRET, "notify": "owner"}]}))
    items = ns.agent_cron(tmp_path)
    assert SECRET not in json.dumps(items)
    assert items == [{"source": "agent-cron", "name": "weekly-x", "next_at": "", "schedule": "0 9 * * 1",
                      "summary": "weekly #55 check", "persistent": True, "enabled": False,
                      "issues": [{"repo": "", "number": 55}]}]


def test_systemd_timers_mark_transient_and_read_descriptions(tmp_path, monkeypatch):
    (tmp_path / "check-z.timer").write_text("")
    listing = json.dumps([{"unit": "check-z.timer", "next": 1796000000000000},
                          {"unit": "nightly.timer", "next": 0}])
    show = "Id=check-z.timer\nDescription=repo-c#9 one-shot check\n\nId=nightly.timer\nDescription=nightly job\n"

    def fake_run(argv, timeout=20):
        if "--user" in argv:
            return None
        return listing if "list-timers" in argv else show

    monkeypatch.setattr(ns, "_run", fake_run)
    monkeypatch.setattr(ns.shutil, "which", lambda name: "/bin/" + name)
    items = ns.systemd_timers([tmp_path, tmp_path / "absent"])
    assert [(i["name"], i["persistent"], bool(i["next_at"])) for i in items] == [
        ("check-z.timer", False, True), ("nightly.timer", True, False)]
    assert items[0]["issues"] == [{"repo": "repo-c", "number": 9}]


def test_text_is_one_short_line_and_refs_are_strict():
    long = "줄1\n줄2 " + "가" * 300
    assert "\n" not in ns.clean_text(long) and len(ns.clean_text(long)) == ns.TEXT_MAX
    assert ns.issue_refs("see repo-a#12, #5 and a/b#3, url.com/x#9, C#") == [
        {"repo": "repo-a", "number": 12}, {"repo": "", "number": 5}]


def test_collect_survives_a_broken_source(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "crontab", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ns, "systemd_timers", lambda dirs: [])
    doc = ns.collect("node-x", tmp_path, [], now=datetime(2026, 11, 28, tzinfo=timezone.utc))
    assert doc["schema"] == ns.SCHEMA and doc["node"] == "node-x" and doc["errors"] == ["crontab: RuntimeError"]


def test_runs_from_stdin_like_the_hub_pipes_it(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    src = (HERE / "ccc-node-schedules.py").read_text()
    out = subprocess.run([sys.executable, "-", "--node", "n1", "--state-dir", str(tmp_path)], input=src,
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc["schema"] == ns.SCHEMA and doc["node"] == "n1"
