"""Keep a frontend's channel selection out of its provider children (#2177).

The Matrix frontend runs with ``CCC_CHANNEL=matrix`` and
``CCC_MATRIX_CONFIG_PATH`` in its process environment, and every provider
child (Claude / Codex / Piri / crush) used to inherit them. An operator who
then ran a Telegram lifecycle command from that agent's tool shell had
``start.sh`` and ``Config.load`` resolve the *Matrix* frontend: on a Termux
node (2026-10-08) a Telegram restart stopped the Matrix bridge and relaunched a
second Matrix frontend.

Only the keys that *select* a frontend are withheld. ``BOT_DATA_DIR`` and
``LOGS_DIR`` stay: the memory hooks (``claude/hooks/lib/distill-journal.sh``,
``claude/hooks/nunchi/*``) and ``ccc_doctor`` read ``BOT_DATA_DIR`` to find
this frontend's journals, so removing it would file Matrix memory under the
Telegram data directory. ``start.sh`` re-derives both from ``--path``, so an
inherited value cannot point a lifecycle command at the wrong state either.

Two child-spawn shapes exist:

* runtimes that own the whole child environment (Piri, crush) drop the keys
  with :func:`without_channel_selection`;
* runtimes whose transport merges ``os.environ`` underneath an overlay (the
  Claude Agent SDK, ``CodexRuntime``) cannot delete a key, so they overlay an
  empty value from :func:`channel_selection_blank_overlay`. ``start.sh``
  (``${CCC_CHANNEL:-telegram}``) and ``Config.load`` treat an empty value as
  unset.

Known limit: ``Config.load`` inside such a child resolves ``channel=telegram``
with the frontend's ``BOT_DATA_DIR`` (and so its session store). ``start.sh``
re-derives the data directory and is unaffected; launching
``python -m telegram_bot`` directly from an agent shell is not a supported
lifecycle path. Before this change the same call started a second Matrix
frontend instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

# Keys that decide WHICH frontend a lifecycle command or ``Config.load``
# targets. The same set ``start.sh --channel telegram`` drops (#2196), plus the
# Matrix-only selection keys. Never add a data-location key (BOT_DATA_DIR,
# LOGS_DIR) here — see the module docstring.
CHANNEL_SELECTION_KEYS: Final = (
    "CCC_CHANNEL",
    "CCC_MATRIX_CONFIG_PATH",
    "CCC_MATRIX_INITIALIZE",
    "SESSION_STORE_PATH",
    "CCC_BOT_ENV_FILE",
)


def without_channel_selection(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy ``environment`` without the frontend-selection keys."""

    return {
        name: value
        for name, value in environment.items()
        if name not in CHANNEL_SELECTION_KEYS
    }


def channel_selection_blank_overlay(base: Mapping[str, str]) -> dict[str, str]:
    """Empty values for the selection keys ``base`` carries.

    For transports that apply an overlay on top of the inherited process
    environment and so cannot remove a key.
    """

    return {name: "" for name in CHANNEL_SELECTION_KEYS if name in base}


def drop_blank_channel_selection(values: Mapping[str, str]) -> dict[str, str]:
    """Treat an empty selection key as unset (the blank overlay's reader side)."""

    return {
        name: value
        for name, value in values.items()
        if not (name in CHANNEL_SELECTION_KEYS and not str(value).strip())
    }


__all__ = [
    "CHANNEL_SELECTION_KEYS",
    "channel_selection_blank_overlay",
    "drop_blank_channel_selection",
    "without_channel_selection",
]
