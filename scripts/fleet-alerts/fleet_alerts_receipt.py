"""Read one fleet alert's delivery receipts (#2413) — locally on the relay host, or over the signed HTTP endpoint (#2137).

Local (relay host, owner-only queue; full ``lookup`` view with per-channel receipts):

    python3 fleet_alerts_receipt.py --queue /var/lib/fleet-alerts/queue <alertId|txn>

Remote (any node that holds a relay secret; the receiver's ``receipt_view`` of
an alert **this node posted** — tier, ``notified``, the owner's ``ok``/``hold``
reply, re-notify/digest flags):

    python3 fleet_alerts_receipt.py --relay-url http://<relay>/v1/alerts \
        --node <node> --secret-file <owner-only file> <alertId|txn>

Prints one JSON object; never prints the alert body. Exit 3 when the id is
unknown (remote: or not this node's alert), 4 when the relay refused the read
for good (signature / unknown node / bad key), 5 when it is unreachable.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402

from telegram_bot.core import fleet_alert_relay as relay  # noqa: E402

EXIT_UNKNOWN = 3
EXIT_REFUSED = 4
EXIT_UNREACHABLE = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--queue", type=Path, help="owner-only queue directory (local read on the relay host)")
    where.add_argument("--relay-url", help="the node's relay url (…/v1/alerts); reads over the signed receipt endpoint")
    parser.add_argument("--node", help="node name the secret belongs to (remote; default CCC_NODE / hostname)")
    parser.add_argument("--secret-file", type=Path, help="owner-only (0600) relay secret file (remote)")
    parser.add_argument("--timeout", type=float, default=relay.DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("key", help="6-char alert id or fleet-… txn")
    args = parser.parse_args(argv)

    if args.queue is not None:
        info = outbox.lookup(args.queue, args.key.strip().lstrip("#"))
        print(outbox.render_lookup(info))
        return 0 if info is not None else EXIT_UNKNOWN

    if args.secret_file is None:
        parser.error("--relay-url needs --secret-file")
    try:
        secret = relay.read_secret(args.secret_file)
        client = relay.FleetAlertRelay(
            args.relay_url, secret, args.node or relay.default_node_name(), timeout=args.timeout
        )
        view = client.get_receipt(args.key)
    except ValueError as exc:  # unsafe secret file, bad url, bad key
        print(json.dumps({"error": "invalid-argument", "detail": str(exc)}), file=sys.stderr)
        return 2
    except relay.RelayRejected as exc:
        print(json.dumps({"error": "refused", "detail": str(exc)}))
        return EXIT_REFUSED
    except relay.RelayError as exc:
        print(json.dumps({"error": "unreachable", "detail": str(exc)}))
        return EXIT_UNREACHABLE
    print(json.dumps(view, ensure_ascii=False, sort_keys=True))
    return 0 if view is not None else EXIT_UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main())
