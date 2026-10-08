"""Send-only encrypted Matrix sender for the fleet alert bot (#2182). No AI runtime, no inbound commands.

Drains the owner-only queue filled by ``fleet_alerts_receiver.py`` into one
private E2EE room (the owner's "🔔 플릿 알림" room) as the dedicated bot
account. Same shape as the card-alert sender that has run on the relay host
since 2026-09-18: the ccc-node Matrix transport layer (device pinning, room
gate, Megolm) with ``admit_event`` disabled, and a stable per-alert
transaction id so a timeout or restart never produces a duplicate event.

Config: the usual private 0600 Matrix config JSON (docs/matrix-frontend.md)
with exactly one room in ``rooms`` and no family section. First run with
``--initialize`` to create the device keys; then run as a service.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_alerts_outbox as outbox  # noqa: E402

DEFAULT_MSGTYPE = "m.text"
IDLE_SLEEP_S = 1.0


async def deliver_one(queue: Path, transport: Any, room: str, *, msgtype: str = DEFAULT_MSGTYPE) -> bool:
    """Deliver the oldest pending alert; returns False when the queue is empty.

    ``transport`` only needs ``matrix_lock``, ``pin_devices``, ``room_gate``
    and ``encrypted_send`` (duck-typed so the loop is unit-testable without
    matrix-nio).
    """
    item = await asyncio.to_thread(outbox.pending, queue)
    if item is None:
        return False
    txn, body = item
    await asyncio.to_thread(outbox.attempted, queue, txn)
    async with transport.matrix_lock:
        await transport.pin_devices()
        if not await transport.room_gate(room):
            raise RuntimeError("private recipient gate failed")
        # Stable transaction id survives timeout/restart and prevents duplicate sends.
        event_id = await transport.encrypted_send(room, body, txn, msgtype=msgtype)
    await asyncio.to_thread(outbox.delivered, queue, txn, event_id)
    logging.info("fleet alert delivered txn=%s", txn)
    return True


def build_transport(config: dict, queue: Path, *, msgtype: str = DEFAULT_MSGTYPE) -> Any:
    """Construct the send-only transport (imports matrix-nio lazily)."""
    from telegram_bot.core.matrix.transport import MatrixTransport

    class FleetAlertsTransport(MatrixTransport):
        def admit_event(self, room: str, event: Any) -> None:
            # Sync is needed for room/device state, never to execute chat requests.
            return None

        async def dispatch(self) -> None:
            room = self.c["rooms"][0]
            while True:
                if not await deliver_one(queue, self, room, msgtype=msgtype):
                    await asyncio.sleep(IDLE_SLEEP_S)

        async def run(self) -> None:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.retry(self.receive, leg="receive"))
                group.create_task(self.retry(self.dispatch, leg="send"))

    return FleetAlertsTransport(config, None)


def validate_config(config: dict) -> None:
    if len(config.get("rooms") or []) != 1 or config.get("family"):
        raise ValueError("exactly one private alert room required, no family section")


async def main_async(args: argparse.Namespace) -> None:
    from telegram_bot.core.matrix.state import load_config
    from telegram_bot.core.matrix.transport import serve, stop_reason

    config = load_config(args.config)
    validate_config(config)
    outbox.pending_count(args.queue)  # fail fast on an unsafe queue directory
    transport = build_transport(config, args.queue, msgtype=args.msgtype)
    try:
        await serve(transport, initialize=args.initialize)
    except BaseException as exc:
        # Never put alert bodies, tokens or raw network exceptions into service logs.
        logging.error("fleet alert sender stopped reason=%s", stop_reason(exc))
        raise SystemExit(1) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, type=Path, help="private 0600 Matrix config JSON")
    parser.add_argument("--queue", required=True, type=Path, help="owner-only queue directory shared with the receiver")
    parser.add_argument("--msgtype", default=DEFAULT_MSGTYPE, choices=("m.text", "m.notice"))
    parser.add_argument("--initialize", action="store_true", help="create device keys and exit")
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
