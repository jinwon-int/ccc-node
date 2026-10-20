from pathlib import Path

from telegram_bot.utils.session_resource_guard import (
    DEFAULT_SESSION_TREE_RSS_LIMIT_MB,
    HOST_PRESSURE_MIN_AVAILABLE_MB,
    MAX_SESSION_TREE_RSS_LIMIT_MB,
    MIN_SESSION_TREE_RSS_LIMIT_MB,
    default_session_tree_rss_limit_mb,
    host_memory_pressure,
    host_pressure_available_threshold_mb,
    process_tree_rss_mb,
    system_available_memory_mb,
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


def test_process_tree_rss_reads_status_only_for_the_subtree(tmp_path: Path) -> None:
    """VmRSS is needed for descendants only; `status` must not be read for
    every process on the host on each idle tick."""
    _write_process(tmp_path, 10, 1, 1024)
    _write_process(tmp_path, 11, 10, 2048)
    for stranger in (90, 91, 92, 93):
        _write_process(tmp_path, stranger, 1, 8192)
    # An unreadable `status` outside the subtree must not affect the result.
    tmp_path.joinpath("94").mkdir()
    tmp_path.joinpath("94", "stat").write_text(
        "94 (other) S 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n", encoding="utf-8"
    )

    assert process_tree_rss_mb(10, proc_root=tmp_path) == 3.0


def test_process_tree_rss_skips_a_descendant_whose_status_vanished(
    tmp_path: Path,
) -> None:
    """A child that exits between the two passes contributes no RSS, but its
    own children are still walked."""
    _write_process(tmp_path, 10, 1, 1024)
    _write_process(tmp_path, 11, 10, 2048)
    _write_process(tmp_path, 12, 11, 1024)
    tmp_path.joinpath("11", "status").unlink()

    assert process_tree_rss_mb(10, proc_root=tmp_path) == 2.0


# -- MemAvailable / host pressure gate (#2298) ---------------------------------


def _write_full_meminfo(
    root: Path, *, total_kib: int | None, available_kib: int | None
) -> None:
    lines = []
    if total_kib is not None:
        lines.append(f"MemTotal:       {total_kib} kB")
    lines.append("MemFree:          123456 kB")
    if available_kib is not None:
        lines.append(f"MemAvailable:   {available_kib} kB")
    root.joinpath("meminfo").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_system_available_memory_reads_meminfo(tmp_path: Path) -> None:
    _write_full_meminfo(tmp_path, total_kib=4009 * 1024, available_kib=2400 * 1024)

    assert system_available_memory_mb(proc_root=tmp_path) == 2400.0


def test_system_available_memory_is_none_when_unavailable(tmp_path: Path) -> None:
    # No /proc/meminfo at all (macOS, restricted Termux).
    assert system_available_memory_mb(proc_root=tmp_path) is None
    # meminfo without the field (pre-3.14 kernel, filtered /proc).
    _write_full_meminfo(tmp_path, total_kib=4009 * 1024, available_kib=None)
    assert system_available_memory_mb(proc_root=tmp_path) is None
    # Malformed value.
    tmp_path.joinpath("meminfo").write_text(
        "MemAvailable: lots kB\n", encoding="utf-8"
    )
    assert system_available_memory_mb(proc_root=tmp_path) is None


def test_host_pressure_threshold_is_max_of_floor_and_fifteen_percent() -> None:
    # 4 GiB host: 15% of 3915 MiB = 587.25 MiB, above the 512 MiB floor.
    assert host_pressure_available_threshold_mb(3915.0) == 3915.0 * 0.15
    # 2 GiB host: 15% = 307 MiB, so the 512 MiB floor wins.
    assert host_pressure_available_threshold_mb(2048.0) == 512.0
    # Unknown total keeps the floor.
    assert host_pressure_available_threshold_mb(0.0) == HOST_PRESSURE_MIN_AVAILABLE_MB


def test_host_memory_pressure_small_host_idle_baseline_is_not_pressure(
    tmp_path: Path,
) -> None:
    # The #2298 field shape: 3.9 GiB total, ~2.4 GiB available.
    _write_full_meminfo(tmp_path, total_kib=3915 * 1024, available_kib=2400 * 1024)

    assert host_memory_pressure(proc_root=tmp_path) == (False, 2400.0)


def test_host_memory_pressure_low_available_is_pressure(tmp_path: Path) -> None:
    _write_full_meminfo(tmp_path, total_kib=3915 * 1024, available_kib=500 * 1024)

    assert host_memory_pressure(proc_root=tmp_path) == (True, 500.0)


def test_host_memory_pressure_uses_fraction_on_large_hosts(tmp_path: Path) -> None:
    # 22 GiB host: threshold is 15% = 3379 MiB, so 3000 MiB available is low.
    _write_full_meminfo(
        tmp_path, total_kib=22 * 1024 * 1024, available_kib=3000 * 1024
    )

    assert host_memory_pressure(proc_root=tmp_path) == (True, 3000.0)


def test_host_memory_pressure_unknown_without_mem_available(tmp_path: Path) -> None:
    assert host_memory_pressure(proc_root=tmp_path) == (None, None)
    _write_full_meminfo(tmp_path, total_kib=3915 * 1024, available_kib=None)
    assert host_memory_pressure(proc_root=tmp_path) == (None, None)


def test_host_memory_pressure_without_mem_total_keeps_the_floor(
    tmp_path: Path,
) -> None:
    _write_full_meminfo(tmp_path, total_kib=None, available_kib=600 * 1024)
    assert host_memory_pressure(proc_root=tmp_path) == (False, 600.0)
    _write_full_meminfo(tmp_path, total_kib=None, available_kib=400 * 1024)
    assert host_memory_pressure(proc_root=tmp_path) == (True, 400.0)
