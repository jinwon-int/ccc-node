from pathlib import Path

from telegram_bot.utils.session_resource_guard import (
    DEFAULT_SESSION_TREE_RSS_LIMIT_MB,
    MAX_SESSION_TREE_RSS_LIMIT_MB,
    MIN_SESSION_TREE_RSS_LIMIT_MB,
    default_session_tree_rss_limit_mb,
    process_tree_rss_mb,
    system_total_memory_mb,
)


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


# -- idle-session RSS watermark scaling (#1277) --------------------------------


def _write_meminfo(root: Path, total_kib: int) -> None:
    root.joinpath("meminfo").write_text(
        f"MemTotal:       {total_kib} kB\nMemFree:        {total_kib // 2} kB\n",
        encoding="utf-8",
    )


def test_system_total_memory_reads_meminfo(tmp_path: Path) -> None:
    _write_meminfo(tmp_path, 22 * 1024 * 1024)  # ~22 GiB, matches gwakga (#1277)

    assert system_total_memory_mb(proc_root=tmp_path) == 22 * 1024.0


def test_system_total_memory_fails_open_when_meminfo_missing(tmp_path: Path) -> None:
    assert system_total_memory_mb(proc_root=tmp_path) == 0.0


def test_system_total_memory_fails_open_on_malformed_line(tmp_path: Path) -> None:
    tmp_path.joinpath("meminfo").write_text("MemTotal:\n", encoding="utf-8")

    assert system_total_memory_mb(proc_root=tmp_path) == 0.0


def test_default_rss_limit_scales_to_a_quarter_of_total_memory(
    tmp_path: Path,
) -> None:
    _write_meminfo(tmp_path, 22 * 1024 * 1024)  # 22 GiB total, matches gwakga

    # 22528 MiB * 0.25 = 5632 MiB — well above the ~1.0-1.2 GiB idle baseline
    # that made the old fixed 1024 MiB default fire on almost every idle tick
    # (#1277: 48 evictions/day observed on a host with 20 GiB free).
    assert default_session_tree_rss_limit_mb(proc_root=tmp_path) == 5632


def test_default_rss_limit_floors_at_the_historical_default_on_small_hosts(
    tmp_path: Path,
) -> None:
    _write_meminfo(tmp_path, 2 * 1024 * 1024)  # 2 GiB total: a small phone node

    # 2048 MiB * 0.25 = 512 MiB, below the floor.
    assert (
        default_session_tree_rss_limit_mb(proc_root=tmp_path)
        == MIN_SESSION_TREE_RSS_LIMIT_MB
    )


def test_default_rss_limit_caps_on_very_large_hosts(tmp_path: Path) -> None:
    _write_meminfo(tmp_path, 256 * 1024 * 1024)  # 256 GiB total

    assert (
        default_session_tree_rss_limit_mb(proc_root=tmp_path)
        == MAX_SESSION_TREE_RSS_LIMIT_MB
    )


def test_default_rss_limit_falls_back_to_fixed_default_when_undetectable(
    tmp_path: Path,
) -> None:
    assert (
        default_session_tree_rss_limit_mb(proc_root=tmp_path)
        == DEFAULT_SESSION_TREE_RSS_LIMIT_MB
    )
