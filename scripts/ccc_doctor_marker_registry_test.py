#!/usr/bin/env python3
"""Guard: every cron installer's marker is known to ccc_doctor.

install-*-cron.sh scripts render `MARKER=`/`BLOCK_BEGIN=`/`BLOCK_END=` lines;
if a new installer lands without a CRON_MARKER_INSTALLERS / CRON_AUX_MARKERS
entry, doctor flags every correctly-installed node as an unknown unmanaged
marker (cost-ledger 12/12 nodes #1398; t2-starvation-observe on seoseo
2026-09-02 after #1421). This test fails at PR time instead.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ccc_doctor  # noqa: E402

ASSIGN = re.compile(r'^(MARKER|BLOCK_BEGIN|BLOCK_END)="(# [^"]+)"\s*$', re.M)


def main() -> int:
    installers = sorted(HERE.glob("install-*-cron.sh"))
    assert installers, "no install-*-cron.sh found next to the test"
    known_markers = {m for _, m, _, _ in ccc_doctor.CRON_MARKER_INSTALLERS}
    known_scripts = {s for _, _, s, _ in ccc_doctor.CRON_MARKER_INSTALLERS}
    aux = set(ccc_doctor.CRON_AUX_MARKERS)
    failures: list[str] = []
    checked = 0
    for script in installers:
        rel = f"scripts/{script.name}"
        found = dict(ASSIGN.findall(script.read_text(encoding="utf-8")))
        marker = found.get("MARKER")
        if marker is None:
            continue  # installer without a literal marker (e.g. nunchi) — not this guard's shape
        checked += 1
        if marker not in known_markers:
            failures.append(f"{rel}: MARKER {marker!r} missing from CRON_MARKER_INSTALLERS")
        if rel not in known_scripts:
            failures.append(f"{rel}: script path missing from CRON_MARKER_INSTALLERS")
        for key in ("BLOCK_BEGIN", "BLOCK_END"):
            blk = found.get(key)
            if blk is not None and blk not in aux:
                failures.append(f"{rel}: {key} {blk!r} missing from CRON_AUX_MARKERS")
    for line in failures:
        print(f"FAIL: {line}")
    print(f"checked={checked} installers; failures={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
