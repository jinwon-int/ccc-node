"""ccc-node push notifier — owner-only outbound delivery of Claude Code lifecycle
notifications, decoupled from the hook via a filesystem spool.

Design / approval boundary (baked in, see ccc-node RISK-PROFILES + Fresh-Approval):
- DISABLED by default (``config.push_enabled``). Nothing is ever sent unless an operator
  explicitly opts in. Merging/restarting the bridge with this module present is a no-op.
- OWNER-ONLY: messages go solely to the resolved owner chat id — the explicit
  ``CCC_PUSH_CHAT_ID``, or the sole ``ALLOWED_USER_IDS`` entry when unambiguous. Never to
  an arbitrary chat. If the target is ambiguous, the notifier stays silent.
- TOKEN ISOLATION: the Claude Code hook (notify.sh) never touches the bot token. It only
  writes short, pre-redacted summary files into the spool; this module (inside the bridge,
  which already holds the token) performs delivery.
- RATE-LIMITED + DEDUPED, and fully best-effort: any delivery failure is logged and never
  crashes the bot. Spool files are retried next cycle until sent, then archived.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from telegram.ext import Application

from telegram_bot.utils.config import config

logger = logging.getLogger(__name__)

_DEDUP_WINDOW_SECONDS = 300
_SENT_RETENTION_SECONDS = 7 * 24 * 60 * 60


class PushNotifier:
    """Polls a spool directory and delivers queued notifications to the owner chat."""

    def __init__(self) -> None:
        self.enabled: bool = bool(getattr(config, "push_enabled", False))
        self.spool_dir: Path = Path(getattr(config, "push_spool_dir"))
        self.interval: float = float(getattr(config, "push_poll_interval", 3.0))
        self.max_per_minute: int = int(getattr(config, "push_max_per_minute", 10))
        self._recent: Dict[str, float] = {}
        self._sent_times: List[float] = []

    def _resolve_target(self) -> Optional[int]:
        """Owner-only target: explicit chat id, else the single allowed user id."""
        cid = getattr(config, "push_chat_id", None)
        if cid:
            return int(cid)
        allowed = getattr(config, "allowed_user_ids", None) or []
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
        logger.info("Push notifier active → chat %s, spool %s", target, self.spool_dir)

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

            now = time.time()
            key = data.get("dedup") or text
            if key in self._recent and now - self._recent[key] < _DEDUP_WINDOW_SECONDS:
                self._archive(p, sent_dir)
                continue

            self._sent_times = [t for t in self._sent_times if now - t < 60]
            if len(self._sent_times) >= self.max_per_minute:
                logger.warning("Push rate limit reached (%d/min); deferring", self.max_per_minute)
                return

            try:
                await application.bot.send_message(chat_id=target, text=self._format(data))
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
