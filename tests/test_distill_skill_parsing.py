"""Packaged /distill parsing scripts: deterministic behavior contract.

The `status` and `stats [days]` parsing used to live as inline shell blocks in
skills/distill/SKILL.md, where it could drift silently and could not be
exercised without firing the distiller. It is now packaged as skill-local
scripts:

    skills/distill/scripts/distill-status.sh
    skills/distill/scripts/distill-stats.sh

built line-for-line from the repaired inline blocks (#1630). The scripts are
read-only: they never create, write, rename, or delete anything under the
distill state directory (``CCC_STATE_DIR``, default ``$HOME/.claude/state``).

These tests pin the preserved behavior on synthetic fixtures only — the real
distiller runtime (claude/hooks/distill*) and any production state are never
touched:

- custom vs default stats window (`stats`, `stats 30`, `stats days=30`, `30`,
  and malformed windows falling back to the 7-day default);
- deterministic trigger row order regardless of log order — the #1630 repair
  that replaced unspecified awk array traversal with a fixed order list;
- interleaved parent/background events: `spawned bg pid=N` bridges the worker
  pid to the most recent *scan-order* `start` trigger, so drift lines that log
  only the bg pid resolve to the right trigger;
- malformed and missing logs fail soft (no crash, no invented rows, no state
  mutation);
- invocation from an installed skill path containing spaces;

"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "distill"
STATUS_SCRIPT = SKILL_DIR / "scripts" / "distill-status.sh"
STATS_SCRIPT = SKILL_DIR / "scripts" / "distill-stats.sh"
SKILL_MD = SKILL_DIR / "SKILL.md"

RUN_KWARGS = {"capture_output": True, "text": True, "timeout": 10}

# Expected log and output contracts are fixed independently of implementation.
BS = chr(92)
TRIGGER_ORDER = ["manual", "precompact", "sessionend", "unknown"]
START_ANCHOR = "start trigger="
DONE_ANCHOR = "done trigger="
SPAWN_ANCHOR = "spawned bg pid="
FAILED_ANCHOR = "extract failed"
SKIP_ANCHOR = "skip reason="
ELAPSED_KEY = "elapsed_s="
QUEUE_FILE, SEEN_FILE = "wiki-candidates.md", "wiki-candidates.seen"
LOG_FILE, LAST_JSON = "distill.log", "distill-last.json"
HEADER_FMT = "[distill stats — last %s days]" + BS + "n"
HONCHO_FMT = "Honcho push: %s ok / %s queued" + BS + "n"
WIKI_FMT = "Wiki queue:   %s candidates added / %s dedup-skipped" + BS + "n"
QUEUE_FMT = "wiki-candidates total=%d pending=%d stale=%d hot=%d" + BS + "n"
SEEN_FMT = "seen-hot=%d threshold=%s" + BS + "n"
ROW_FMT = "%-10s %4d runs (%3d done / %3d failed / %3d dryrun / %3d skipped) avg=%s" + BS + "n"
JQ_KEYS = ["trigger", "session_id", "distilled_at", "honcho", "wiki_candidates"]
JQ_CHUNKS = ["trigger=", " session=", " at=", " honcho=", " wiki="]


def _strip_literal_newline(fmt: str) -> str:
    # Script text carries the two characters backslash+n where awk will print
    # a real newline; strip that tail before building expectations.
    return fmt[: -2] if fmt.endswith(BS + "n") else fmt


def _fmt_to_row_regex(fmt: str) -> re.Pattern[str]:
    """Regex over the text after 'name:' for one trigger-table row."""
    body = _strip_literal_newline(fmt.split("%-10s", 1)[1])
    pattern = re.escape(body)
    pattern = pattern.replace(BS + " ", " ")  # re.escape turns ' ' into '\ '
    for token in ("%4d", "%3d"):
        # %Nd padding can add spaces before the digits.
        pattern = pattern.replace(token, BS + "s*(" + BS + "d+)")
    pattern = pattern.replace("%s", "(" + BS + "S+)")
    pattern = pattern.replace(" ", BS + "s+")
    return re.compile(pattern)


ROW_RE = _fmt_to_row_regex(ROW_FMT)


def fmt_fill(fmt: str, *values: object) -> str:
    """Render a printf-style format with %d/%s placeholders left to right."""
    out, values, i = "", list(values), 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt) and fmt[i + 1] in "ds":
            out += str(values.pop(0))
            i += 2
        else:
            out += fmt[i]
            i += 1
    return out


def zero_footer(fmt: str) -> str:
    return fmt_fill(_strip_literal_newline(fmt), 0, 0)


def parse_rows(stdout: str) -> dict[str, dict[str, object]]:
    """Parse the trigger table into {trigger: {total, done, failed, ...}}."""
    rows: dict[str, dict[str, object]] = {}
    for line in stdout.splitlines():
        name, sep, rest = line.partition(":")
        if not sep or name not in TRIGGER_ORDER:
            continue
        match = ROW_RE.match(rest)
        assert match is not None, f"unparseable row {line!r} (fmt {ROW_FMT!r})"
        (total, done, failed, dryrun, skipped, avg) = match.groups()
        rows[name] = {
            "total": int(total),
            "done": int(done),
            "failed": int(failed),
            "dryrun": int(dryrun),
            "skipped": int(skipped),
            "avg": avg,
        }
    return rows


def run_script(script: Path, *args: str, state_dir: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if state_dir is not None:
        env["CCC_STATE_DIR"] = str(state_dir)
    else:
        env.pop("CCC_STATE_DIR", None)
    return subprocess.run(["bash", str(script), *args], env=env, **RUN_KWARGS)


def ts(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_state(
    tmp_path: Path,
    log_lines: list[str] | None = None,
    last_json: str | None = None,
    queue: str | None = None,
    seen: str | None = None,
) -> Path:
    """Build a synthetic distill state dir; nothing here is live state."""
    state = tmp_path / "state"
    state.mkdir()
    if log_lines is not None:
        (state / LOG_FILE).write_text("\n".join(log_lines) + "\n")
    if last_json is not None:
        (state / LAST_JSON).write_text(last_json)
    if queue is not None:
        (state / QUEUE_FILE).write_text(queue)
    if seen is not None:
        (state / SEEN_FILE).write_text(seen)
    return state


def state_file_names(state: Path) -> list[str]:
    return sorted(p.name for p in state.iterdir())


# --------------------------------------------------------------------- window


def test_stats_default_window_is_seven_days(tmp_path):
    state = make_state(
        tmp_path,
        log_lines=[
            f"{ts(1)} {START_ANCHOR}manual pid=100",
            f"{ts(1)} {DONE_ANCHOR}manual pid=100 {ELAPSED_KEY}12",
            f"{ts(20)} {START_ANCHOR}precompact pid=200",
            f"{ts(20)} {DONE_ANCHOR}precompact pid=200 {ELAPSED_KEY}30",
        ],
    )
    proc = run_script(STATS_SCRIPT, state_dir=state)
    assert proc.returncode == 0, proc.stderr
    header = proc.stdout.splitlines()[0]
    left, right = HEADER_FMT.split("%s")
    right = _strip_literal_newline(right)
    assert header.startswith(left)
    assert header.endswith(right)
    assert int(header[len(left) : len(header) - len(right)]) == 7
    rows = parse_rows(proc.stdout)
    assert set(rows) == {"manual"}
    assert rows["manual"]["done"] == 1
    assert rows["manual"]["avg"] == "12s"


def test_stats_custom_window_widens_the_scan(tmp_path):
    state = make_state(
        tmp_path,
        log_lines=[
            f"{ts(1)} {START_ANCHOR}manual pid=100",
            f"{ts(1)} {DONE_ANCHOR}manual pid=100 {ELAPSED_KEY}12",
            f"{ts(20)} {START_ANCHOR}precompact pid=200",
            f"{ts(20)} {DONE_ANCHOR}precompact pid=200 {ELAPSED_KEY}30",
        ],
    )
    proc = run_script(STATS_SCRIPT, "stats", "30", state_dir=state)
    assert proc.returncode == 0, proc.stderr
    left, right = HEADER_FMT.split("%s")
    right = _strip_literal_newline(right)
    header = proc.stdout.splitlines()[0]
    assert header.startswith(left) and header.endswith(right)
    assert int(header[len(left) : len(header) - len(right)]) == 30
    rows = parse_rows(proc.stdout)
    assert set(rows) == {"manual", "precompact"}
    assert rows["precompact"]["done"] == 1
    assert rows["precompact"]["avg"] == "30s"


def test_stats_days_eq_and_bare_forms_match_stats_days(tmp_path):
    state = make_state(
        tmp_path,
        log_lines=[
            f"{ts(1)} {START_ANCHOR}sessionend pid=100",
            f"{ts(1)} {DONE_ANCHOR}sessionend pid=100 {ELAPSED_KEY}4",
        ],
    )
    via_stats = run_script(STATS_SCRIPT, "stats", "30", state_dir=state).stdout
    assert run_script(STATS_SCRIPT, "stats", "days=30", state_dir=state).stdout == via_stats
    assert run_script(STATS_SCRIPT, "30", state_dir=state).stdout == via_stats
    rows = parse_rows(via_stats)
    assert rows["sessionend"]["done"] == 1
    assert rows["sessionend"]["avg"] == "4s"


def test_stats_bare_stats_word_keeps_default(tmp_path):
    state = make_state(tmp_path, log_lines=[f"{ts(1)} {DONE_ANCHOR}manual pid=1 {ELAPSED_KEY}1"])
    default = run_script(STATS_SCRIPT, state_dir=state).stdout
    bare = run_script(STATS_SCRIPT, "stats", state_dir=state).stdout
    assert bare == default
    assert parse_rows(bare) == parse_rows(default)


def test_stats_malformed_window_falls_back_to_seven(tmp_path):
    state = make_state(tmp_path, log_lines=[f"{ts(1)} {DONE_ANCHOR}manual pid=1 {ELAPSED_KEY}1"])
    default = run_script(STATS_SCRIPT, state_dir=state).stdout
    sentinel = tmp_path / "must-not-execute"
    for bad in (
        ("stats", "soon"), ("stats", "-3"), ("stats", "1e9"), ("stats", "7 days"),
        ("stats", f"$(touch {sentinel})"), ("stats", f"; touch {sentinel}"),
    ):
        proc = run_script(STATS_SCRIPT, *bad, state_dir=state)
        assert proc.returncode == 0, (bad, proc.stderr)
        assert proc.stdout == default, bad
        assert not sentinel.exists()


# -------------------------------------------------------------- trigger order


def test_trigger_rows_render_in_fixed_order_regardless_of_log_order(tmp_path):
    # Log written in hostile order; the table must still print triggers in the
    # scripts' fixed order list — never awk's unspecified array traversal order
    # (#1630).
    state = make_state(
        tmp_path,
        log_lines=[
            f"{ts(1)} {FAILED_ANCHOR} ec=1 pid=777 {ELAPSED_KEY}3",  # never bridged -> unknown
            f"{ts(2)} {START_ANCHOR}sessionend pid=150",
            f"{ts(1)} {START_ANCHOR}manual pid=100",
            f"{ts(2)} {DONE_ANCHOR}sessionend pid=150 {ELAPSED_KEY}5",
            f"{ts(1)} {START_ANCHOR}precompact pid=200",
            f"{ts(1)} {DONE_ANCHOR}manual pid=100 {ELAPSED_KEY}7",
            f"{ts(1)} {SKIP_ANCHOR}cwd-out-of-scope trigger=precompact pid=200",
        ],
    )
    proc = run_script(STATS_SCRIPT, state_dir=state)
    assert proc.returncode == 0, proc.stderr
    printed = [name for name in TRIGGER_ORDER if f"{name}:" in proc.stdout]
    assert printed == TRIGGER_ORDER
    rows = parse_rows(proc.stdout)
    assert rows["manual"]["done"] == 1 and rows["manual"]["avg"] == "7s"
    assert rows["precompact"]["skipped"] == 1
    assert rows["sessionend"]["done"] == 1 and rows["sessionend"]["avg"] == "5s"
    assert rows["unknown"]["failed"] == 1 and rows["unknown"]["avg"] == "3s"


# ----------------------------------------------- parent/background interleaving


def test_bg_pid_bridges_to_scan_order_most_recent_start(tmp_path):
    # spawned bg pid=N bridges N to the most recent `start` seen *at that point
    # in the scan*. The old `for (p in pid_trigger)` form picked an arbitrary
    # parent instead (#1630): a bg worker spawned before a later `start` keeps
    # the earlier trigger.
    state = make_state(
        tmp_path,
        log_lines=[
            f"{ts(1)} {START_ANCHOR}manual pid=100",
            f"{ts(1)} {SPAWN_ANCHOR}300 mode=setsid",  # 300 -> manual
            f"{ts(1)} {START_ANCHOR}precompact pid=200",
            f"{ts(1)} {SPAWN_ANCHOR}400 mode=setsid",  # 400 -> precompact
            f"{ts(1)} {FAILED_ANCHOR} ec=1 pid=300 {ELAPSED_KEY}4",
            f"{ts(1)} {FAILED_ANCHOR} ec=1 pid=400 {ELAPSED_KEY}40",
            f"{ts(1)} {FAILED_ANCHOR} ec=1 pid=999 {ELAPSED_KEY}7",
        ],
    )
    proc = run_script(STATS_SCRIPT, state_dir=state)
    assert proc.returncode == 0, proc.stderr
    rows = parse_rows(proc.stdout)
    assert rows["manual"]["failed"] == 1 and rows["manual"]["avg"] == "4s"
    assert rows["precompact"]["failed"] == 1 and rows["precompact"]["avg"] == "40s"
    assert rows["unknown"]["failed"] == 1 and rows["unknown"]["avg"] == "7s"


def test_inline_trigger_wins_over_pid_bridge(tmp_path):
    state = make_state(
        tmp_path,
        log_lines=[
            f"{ts(1)} {START_ANCHOR}precompact pid=200",
            f"{ts(1)} {SPAWN_ANCHOR}400 mode=setsid",  # 400 -> precompact
            f"{ts(1)} {DONE_ANCHOR}manual pid=400 {ELAPSED_KEY}7",
        ],
    )
    proc = run_script(STATS_SCRIPT, state_dir=state)
    assert proc.returncode == 0, proc.stderr
    rows = parse_rows(proc.stdout)
    assert rows["manual"]["done"] == 1 and rows["manual"]["avg"] == "7s"
    assert rows["precompact"]["done"] == 0


# ------------------------------------------------------- malformed/missing logs


def test_stats_with_missing_state_dir_fails_soft_and_creates_nothing(tmp_path):
    absent = tmp_path / "does-not-exist"
    proc = run_script(STATS_SCRIPT, state_dir=absent)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(HEADER_FMT.split("%s")[0])
    assert parse_rows(proc.stdout) == {}
    assert not absent.exists(), "stats must not create the state dir"


def test_stats_with_garbage_log_reports_zero_and_exits_zero(tmp_path):
    state = make_state(
        tmp_path,
        log_lines=["\x00\x01garbage", "random text without timestamps", "2020 not-a-real-entry"],
    )
    proc = run_script(STATS_SCRIPT, state_dir=state)
    assert proc.returncode == 0, proc.stderr
    assert parse_rows(proc.stdout) == {}
    assert zero_footer(HONCHO_FMT) in proc.stdout
    assert zero_footer(WIKI_FMT) in proc.stdout


def test_status_with_missing_state_dir_reports_nothing_and_creates_nothing(tmp_path):
    absent = tmp_path / "does-not-exist"
    proc = run_script(STATUS_SCRIPT, state_dir=absent)
    # Faithful to the inline block it replaced: every input open fails
    # silently, nothing is invented on stdout, and no directory appears.
    assert proc.stdout == ""
    assert not absent.exists(), "status must not create the state dir"


def test_status_with_malformed_last_json_still_reports_queue(tmp_path):
    now = ts(0)
    state = make_state(
        tmp_path,
        log_lines=[f"{now} {DONE_ANCHOR}manual pid=1 {ELAPSED_KEY}2"],
        last_json="{not json at all",
        queue="## [CAND-1] topic\n- status: pending\n",
        seen="a b 9 d\n",
    )
    proc = run_script(STATUS_SCRIPT, state_dir=state)
    assert proc.returncode == 0, proc.stderr
    # jq fails silently on the malformed json: no one-liner is invented.
    assert not any(chunk in proc.stdout for chunk in JQ_CHUNKS[1:3])
    assert DONE_ANCHOR in proc.stdout  # tail -5 still served
    assert fmt_fill(_strip_literal_newline(QUEUE_FMT), 1, 1, 0, 0) in proc.stdout
    assert fmt_fill(_strip_literal_newline(SEEN_FMT), 1, "3") in proc.stdout


def test_status_reports_last_run_one_liner(tmp_path):
    state = make_state(
        tmp_path,
        last_json=json.dumps(
            {
                JQ_KEYS[0]: "manual",
                JQ_KEYS[1]: "s-1",
                JQ_KEYS[2]: ts(0),
                JQ_KEYS[3]: [1, 2],
                JQ_KEYS[4]: [],
            }
        ),
        log_lines=[f"{ts(0)} {DONE_ANCHOR}manual pid=1 {ELAPSED_KEY}2"],
        seen="a b 0 d\n",
    )
    proc = run_script(STATUS_SCRIPT, state_dir=state)
    assert proc.returncode == 0, proc.stderr
    assert JQ_CHUNKS[0] + "manual" in proc.stdout
    assert JQ_CHUNKS[1] + "s-1" in proc.stdout
    assert JQ_CHUNKS[3] + "2" in proc.stdout
    assert JQ_CHUNKS[4] + "0" in proc.stdout


# ------------------------------------------------------ installed path handling


def test_scripts_run_from_installed_path_with_spaces(tmp_path):
    now = ts(1)
    log_lines = [
        f"{now} {START_ANCHOR}manual pid=100",
        f"{now} {DONE_ANCHOR}manual pid=100 {ELAPSED_KEY}6",
    ]
    state = make_state(tmp_path, log_lines=log_lines)
    installed = tmp_path / "Distill Skill (prod)" / "scripts"
    installed.mkdir(parents=True)
    for script in (STATUS_SCRIPT, STATS_SCRIPT):
        shutil.copy2(script, installed / script.name)
        from_repo = run_script(script, state_dir=state)
        from_installed = subprocess.run(
            ["bash", str(installed / script.name)],
            env={**os.environ, "CCC_STATE_DIR": str(state)},
            **RUN_KWARGS,
        )
        assert from_installed.returncode == from_repo.returncode
        assert from_installed.stdout == from_repo.stdout
        assert from_installed.stderr == from_repo.stderr


# ------------------------------------------------------------ read-only contract


def _snapshot(state: Path) -> dict[str, tuple]:
    snap: dict[str, tuple] = {}
    for path in sorted(state.rglob("*")):
        if path.is_file():
            st = path.stat()
            snap[str(path.relative_to(state))] = (
                st.st_size,
                st.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return snap


def test_scripts_never_mutate_distill_state(tmp_path):
    state = make_state(
        tmp_path,
        log_lines=[
            f"{ts(1)} {START_ANCHOR}manual pid=100",
            f"{ts(1)} {SPAWN_ANCHOR}300 mode=setsid",
            f"{ts(1)} {DONE_ANCHOR}manual pid=300 {ELAPSED_KEY}9",
        ],
        last_json=json.dumps(
            {
                JQ_KEYS[0]: "manual",
                JQ_KEYS[1]: "s",
                JQ_KEYS[2]: "t",
                JQ_KEYS[3]: [],
                JQ_KEYS[4]: [],
            }
        ),
        queue="## [CAND-1] topic\n- status: pending\n",
        seen="a b 3 d\n",
    )
    before = _snapshot(state)
    for args in ((), ("stats",), ("stats", "30"), ("stats", "days=30")):
        assert run_script(STATS_SCRIPT, *args, state_dir=state).returncode == 0
    assert run_script(STATUS_SCRIPT, state_dir=state).returncode == 0
    assert _snapshot(state) == before
    assert state_file_names(state) == sorted([LOG_FILE, LAST_JSON, QUEUE_FILE, SEEN_FILE])
