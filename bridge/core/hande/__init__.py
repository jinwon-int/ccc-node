"""「한데」 frontend (``CCC_CHANNEL=hande``, #2380 PR-2).

The family messenger's native E2EE bot (``native-mls-bot``, family-messenger
``archive/native-mls/bot``) decrypts room messages and hands each one to a
*responder* over a unix socket, speaking the FLEET-WORKER JSON-lines port
(family-messenger ``docs/FLEET-WORKER.md``). Until now that responder was the
Rust ``native-responder`` plus ``scripts/ccc_adapter.py``, which calls the
ccc-node runtime directly and so bypasses everything the bridge adds on top
of a turn: commands, the redacted approval snapshot and audit ledger,
continuation, waits, session continuity, health.

Here the **bridge is the responder**. :class:`~.transport.HandeTransport`
listens on the socket and implements the same transport surface the Matrix
frontend uses; :class:`~.bot.HandeBot` is ``MatrixBot`` with the Matrix
config loader and transport swapped out. Nothing changes on the bot side: it
connects to a socket path and speaks the port it already speaks.

Mapping (one line each; the long form is ``docs/hande-frontend.md``):

* ``turn.sender`` ``@<device>:hande`` → actor (``<device>`` up to the first
  ``-``) → ``@<actor>:hande`` is the user; a person's devices share one
  conversation, as a Matrix user's devices do.
* ``turn.room`` (when the bot sends it) → room id; absent → the config's
  first room. Sessions are keyed ``(user, room)``.
* ``turn.session_id`` is ignored: the session manager is the resume authority.
* ``TurnSink.approval`` → ``approval`` frame, answered by ``approve``/``deny``,
  denied on timeout. ``typing``/``status`` are no-ops; ``interim`` is dropped
  unless the config opts into the ``interim`` frame.
* Unsolicited output (``enqueue_notice``: CI-wait results, background-task
  completions, restart receipt) is a ``notice`` frame once the config opts
  into ``notice_frames``; off, it is counted and dropped, never silently
  claimed delivered.
* Self-jobs (``enqueue_self_job``, #2561) — the CI-wait resume, a
  yield-and-continue bundle, a Danso auto-resume — run between bot turns and
  speak through ``notice`` frames, as Matrix runs its ``$self-`` jobs.
"""

from __future__ import annotations

__all__ = ["HandeBot", "HandeConfigError", "HandeTransport", "load_config"]


def __getattr__(name: str):  # lazy: keep ``import telegram_bot.core.hande`` light
    if name == "HandeBot":
        from .bot import HandeBot

        return HandeBot
    if name == "HandeTransport":
        from .transport import HandeTransport

        return HandeTransport
    if name in ("HandeConfigError", "load_config"):
        from . import state

        return getattr(state, name)
    raise AttributeError(name)
