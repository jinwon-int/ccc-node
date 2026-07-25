from pathlib import Path

from telegram_bot.utils.session_resource_guard import process_tree_rss_mb


def _write_process(root: Path, pid: int, parent: int, rss_kib: int) -> None:
    process = root / str(pid)
    process.mkdir()
    # Fields after the command begin at stat field 3. The helper reads PPID
    # from index 1 of that suffix, matching Linux /proc/<pid>/stat.
    process.joinpath("stat").write_text(
        f"{pid} (worker name) S {parent} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    process.joinpath("status").write_text(
        f"Name:\tworker\nVmRSS:\t{rss_kib} kB\n",
        encoding="utf-8",
    )


def test_process_tree_rss_sums_only_root_and_descendants(tmp_path: Path) -> None:
    _write_process(tmp_path, 10, 1, 1024)
    _write_process(tmp_path, 11, 10, 2048)
    _write_process(tmp_path, 12, 11, 512)
    _write_process(tmp_path, 99, 1, 8192)

    assert process_tree_rss_mb(10, proc_root=tmp_path) == 3.5


def test_process_tree_rss_fails_open_when_root_is_unreadable(tmp_path: Path) -> None:
    _write_process(tmp_path, 11, 10, 2048)

    assert process_tree_rss_mb(10, proc_root=tmp_path) == 0.0
