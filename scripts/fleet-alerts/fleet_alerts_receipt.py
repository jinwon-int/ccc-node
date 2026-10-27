"""Operator read of one fleet alert's delivery receipts (#2413). Local, metadata only.

    python3 fleet_alerts_receipt.py --queue /var/lib/fleet-alerts/queue <alertId|txn>

Prints one JSON object: alert_id, tier, node, event, created, per-channel
receipts (``matrix-event`` and ``hande-ack`` are strong; ``socket-write`` is
the hande lane's weak hand-off) and ``notified`` (any strong receipt). Never
prints the alert body. Exit 3 when the id is unknown.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", required=True, type=Path, help="owner-only queue directory")
    parser.add_argument("key", help="6-char alert id or fleet-… txn")
    args = parser.parse_args(argv)
    info = outbox.lookup(args.queue, args.key.strip().lstrip("#"))
    print(outbox.render_lookup(info))
    return 0 if info is not None else 3


if __name__ == "__main__":
    raise SystemExit(main())
