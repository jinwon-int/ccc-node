"""E2EE Matrix transport for the bridge (#1780, PR-2a).

Ported from the proven ``family-messenger`` pilot (``fleet_core`` /
``fleet_matrix_state`` / ``fleet_matrix``). Two modules:

* :mod:`telegram_bot.core.matrix.state` — transport-independent admission
  policy, private on-disk state (SQLite inbox/outbox, sync replay, operator
  audit) and configuration validation. No network.
* :mod:`telegram_bot.core.matrix.transport` — the ``/sync`` loop, device
  pinning, room gates, encrypted delivery and the turn loop. Turn execution is
  delegated to an injected :class:`~telegram_bot.core.matrix.transport.TurnRunner`
  instead of the pilot's subprocess worker.

``nio`` (matrix-nio) and ``aiohttp`` are imported lazily inside the functions
that need them, so importing this package never requires either.
"""
