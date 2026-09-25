"""ccc-node push notifier — owner-only outbound delivery of Claude Code lifecycle
notifications, decoupled from the hook via a filesystem spool.

Design / approval boundary (baked in, see ccc-node Fresh-Approval policy):
- DISABLED by default (``config.push_enabled``). Nothing is ever sent unless an operator
  explicitly opts in. Merging/restarting the bridge with this module present is a no-op.
- OWNER-DEFAULT: messages go to the resolved owner chat id — the explicit
  ``CCC_PUSH_CHAT_ID``, or the sole ``ALLOWED_USER_IDS`` entry when unambiguous. If the
  target is ambiguous, the notifier stays silent. A spool record MAY name an explicit
  ``chatId`` (agent-cron group/channel delivery, #665), but it is delivered ONLY when that
  id is on the ``CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS`` allowlist — re-validated here on read
  (defense in depth); a non-allowlisted chat id is dropped, never sent. Never an arbitrary chat.
- TOKEN ISOLATION: the Claude Code hook (notify.sh) never touches the bot token. It only
  writes short, pre-redacted summary files into the spool; this module (inside the bridge,
  which already holds the token) performs delivery.
- RATE-LIMITED + DEDUPED, and fully best-effort: any delivery failure is logged and never
  crashes the bot. Spool files are retried next cycle until sent, then archived.
- FAN-OUT (opt-in, ``CCC_PUSH_MIRROR_DIRS``): exactly one process consumes a spool dir.
  To deliver on a second frontend too, the consumer first copies every pending record into
  each mirror spool dir (``fan_out_pending``) and only then delivers; the other frontend
  drains that mirror dir (``CCC_PUSH_CONSUME_SPOOL``). Each dir still has a single
  consumer, so nothing is sent twice per channel.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

from telegram.ext import Application

from telegram_bot.utils.config import config

logger = logging.getLogger(__name__)

_DEDUP_WINDOW_SECONDS = 300
_SENT_RETENTION_SECONDS = 7 * 24 * 60 * 60


def mirror_dirs_from(settings, *own_dirs: Path) -> List[Path]:
    """Parse ``push_mirror_dirs`` (``os.pathsep``/comma separated) into mirror spool dirs.

    A mirror equal to one of ``own_dirs`` (the consumed spool, or the dir this
    process writes into) is dropped: copying a record back into a spool that
    feeds this consumer would re-queue it forever.
    """
    raw = getattr(settings, "push_mirror_dirs", None)
    if not raw:
        return []
    if isinstance(raw, (str, os.PathLike)):
        parts = re.split(r"[,%s]" % re.escape(os.pathsep), str(raw))
    else:
        parts = [str(x) for x in raw]
    own = set()
    for o in own_dirs:
        try:
            own.add(Path(o).expanduser().resolve())
        except OSError:
            own.add(Path(o))
    dirs: List[Path] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        d = Path(text).expanduser()
        try:
            same = d.resolve() in own
        except OSError:
            same = False
        if same:
            logger.warning("Push mirror dir %s feeds this consumer; ignoring", d)
            continue
        if d not in dirs:
            dirs.append(d)
    return dirs


def fan_out(record: Path, raw: str, mirror_dirs: List[Path]) -> bool:
    """Copy one spool record into every mirror spool dir, idempotently.

    Skips a mirror that already holds the record (pending or in its ``sent/``
    archive), so a record retried by the consumer is never mirrored twice.
    The copy is written to a non-``.json`` temp name and renamed into place,
    so the mirror's consumer never reads a half-written file. Returns False on
    the first failure; the caller keeps the record and retries next cycle.
    """
    for d in mirror_dirs:
        dest = d / record.name
        if dest.exists() or (d / "sent" / record.name).exists():
            continue
        tmp = d / f".{record.name}.{os.getpid()}.tmp"
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp.write_text(raw, encoding="utf-8")
            os.replace(tmp, dest)
        except OSError as e:
            logger.warning("Push fan-out to %s failed (will retry next cycle): %s", d, e)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
    return True


_FANOUT_TMP_STALE_SECONDS = 60 * 60


def _sweep_stale_tmp(mirror_dirs: List[Path]) -> None:
    """Remove fan-out temp files a crash left behind (never read as records)."""
    cutoff = time.time() - _FANOUT_TMP_STALE_SECONDS
    for d in mirror_dirs:
        try:
            for t in d.glob(".*.tmp"):
                try:
                    if t.stat().st_mtime < cutoff:
                        t.unlink()
                except OSError:
                    pass
        except OSError:
            pass


def fan_out_pending(spool_dir: Path, mirror_dirs: List[Path]) -> set:
    """Mirror every pending record before any delivery; return the names safe to deliver.

    A separate pass, so the mirror's channel is neither throttled by this
    consumer's rate limit nor stuck behind a record this consumer cannot send.
    Stops at the first copy failure, preserving order: that record and all later
    ones are withheld from delivery until a later cycle mirrors them, so no
    channel gets a notice the other cannot. Malformed / empty records are not
    mirrored — every consumer archives those without delivering them.
    """
    ready: set = set()
    _sweep_stale_tmp(mirror_dirs)
    for p in sorted(spool_dir.glob("*.json")):
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not (data.get("text") or "").strip():
            continue
        if not fan_out(p, raw, mirror_dirs):
            break
        ready.add(p.name)
    return ready


class PushNotifier:
    """Polls a spool directory and delivers queued notifications to the owner chat."""

    def __init__(self, settings=None) -> None:
        self._config = config if settings is None else settings
        self.enabled: bool = bool(getattr(self._config, "push_enabled", False))
        # Default mirrors utils.config.push_spool_dir so the notifier is robust to a
        # config object that omits the key (e.g. SimpleNamespace stubs in tests).
        # write_spool_dir is where this process's own writers queue records;
        # spool_dir is the dir this notifier consumes. They differ only on the
        # receiving side of a fan-out (CCC_PUSH_CONSUME_SPOOL = a mirror dir).
        self.write_spool_dir: Path = Path(
            getattr(self._config, "push_spool_dir", None)
            or (Path.home() / ".claude" / "state" / "telegram-spool")
        )
        consume = getattr(self._config, "push_consume_spool_dir", None)
        self.spool_dir: Path = Path(consume).expanduser() if consume else self.write_spool_dir
        self.interval: float = float(getattr(self._config, "push_poll_interval", 3.0))
        self.max_per_minute: int = int(getattr(self._config, "push_max_per_minute", 10))
        self.mirror_dirs: List[Path] = mirror_dirs_from(
            self._config, self.spool_dir, self.write_spool_dir
        )
        self._notify_allowed_chats: set = self._load_notify_allowlist()
        self._recent: Dict[str, float] = {}
        self._sent_times: List[float] = []

    def _load_notify_allowlist(self) -> set:
        """Allowlisted group/channel chat ids for record-targeted delivery (#665).

        Fail-closed: empty allowlist ⇒ no record may target a non-owner chat.
        """
        configured = getattr(self._config, "push_notify_allowed_chats", None)
        if configured:
            return {str(c).strip() for c in configured if str(c).strip()}
        raw = os.environ.get("CCC_AGENT_CRON_NOTIFY_ALLOWED_CHATS", "") or ""
        return {part for part in re.split(r"[,\s]+", raw.strip()) if part}

    def _target_for_record(
        self, data: dict, owner_target: int
    ) -> Optional[Union[int, str]]:
        """Owner target by default; an allowlisted record ``chatId`` overrides it.

        Returns ``None`` (drop, never send) when the record names a chat id that
        is not on the allowlist — the spool file is not trusted blindly.
        """
        chat_id = data.get("chatId")
        if not chat_id:
            return owner_target
        chat_id = str(chat_id).strip()
        if chat_id not in self._notify_allowed_chats:
            logger.warning(
                "Push record targets a non-allowlisted chat id; dropping (event=%s)",
                data.get("event", "notify"),
            )
            return None
        return int(chat_id) if re.fullmatch(r"-?[0-9]+", chat_id) else chat_id

    def _resolve_target(self) -> Optional[int]:
        """Owner-only target: explicit chat id, else the single allowed user id."""
        cid = getattr(self._config, "push_chat_id", None)
        if cid:
            return int(cid)
        allowed = getattr(self._config, "allowed_user_ids", None) or []
        if len(allowed) == 1:
            return int(allowed[0])
        return None

    async def run(self, application: Application, stop_event: asyncio.Event) -> None:
        if not self.enabled:
            logger.info("Push notifier disabled (config.push_enabled is false)")
            return
        target = self._resolve_target()
        if not target:
            logger.warning(
                "Push notifier enabled but target chat id is ambiguous "
                "(set CCC_PUSH_CHAT_ID, or exactly one ALLOWED_USER_IDS). Not sending."
            )
            return

        sent_dir = self.spool_dir / "sent"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            sent_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Push notifier cannot create spool dir %s: %s", self.spool_dir, e)
            return
        self._prune_sent(sent_dir)
        logger.info(
            "Push notifier active → chat %s, spool %s, fan-out %s",
            target,
            self.spool_dir,
            [str(d) for d in self.mirror_dirs] or "none",
        )

        while not stop_event.is_set():
            try:
                await self._drain(application, target, sent_dir)
            except Exception as e:  # never let the loop die
                logger.warning("Push notifier drain error (continuing): %s", e)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def _drain(self, application: Application, target: int, sent_dir: Path) -> None:
        ready = fan_out_pending(self.spool_dir, self.mirror_dirs) if self.mirror_dirs else None
        for p in sorted(self.spool_dir.glob("*.json")):
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._archive(p, sent_dir)  # malformed → don't retry forever
                continue

            text = (data.get("text") or "").strip()
            if not text:
                self._archive(p, sent_dir)
                continue

            # Not yet mirrored (copy failed, or it arrived after the fan-out
            # pass): keep it for the next cycle, preserving order.
            if ready is not None and p.name not in ready:
                return

            record_target = self._target_for_record(data, target)
            if record_target is None:
                # Record named a non-allowlisted chat id — never send it.
                self._archive(p, sent_dir)
                continue

            now = time.time()
            key = data.get("dedup") or text
            if key in self._recent and now - self._recent[key] < _DEDUP_WINDOW_SECONDS:
                self._archive(p, sent_dir)
                continue

            self._sent_times = [t for t in self._sent_times if now - t < 60]
            # Prune the dedup map with the same discipline: entries were only
            # ever compared against the window, never removed, so a long-lived
            # bridge accumulated one entry per distinct notification forever.
            self._recent = {
                k: t
                for k, t in self._recent.items()
                if now - t < _DEDUP_WINDOW_SECONDS
            }
            if len(self._sent_times) >= self.max_per_minute:
                logger.warning("Push rate limit reached (%d/min); deferring", self.max_per_minute)
                return

            try:
                await application.bot.send_message(
                    chat_id=record_target, text=self._format(data)
                )
            except Exception as e:
                logger.warning("Push send failed (will retry next cycle): %s", e)
                return  # keep file; stop this cycle to preserve order
            self._recent[key] = now
            self._sent_times.append(now)
            self._archive(p, sent_dir)

    @staticmethod
    def _archive(p: Path, sent_dir: Path) -> None:
        try:
            p.rename(sent_dir / p.name)
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass

    @staticmethod
    def _prune_sent(sent_dir: Path) -> None:
        cutoff = time.time() - _SENT_RETENTION_SECONDS
        try:
            for p in sent_dir.glob("*.json"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    @staticmethod
    def _format(data: dict) -> str:
        ev = data.get("event", "notify")
        node = data.get("node", "")
        ts = data.get("ts", "")
        text = data.get("text", "")
        head = f"🔔 ccc-node [{ev}]" + (f" · {node}" if node else "")
        body = f"{head}\n{text}"
        return body + (f"\n{ts}" if ts else "")
