"""Private append-only Grok intent journal. Never auto-requeue an uncertain send."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator
import uuid

from .grok_protocol import (
    AcceptedPrompt, Baseline, BoundReply, HOST_VERSION, MAX_REPLY, ProtocolError,
    _text, identifier, prompt_digest, decode_wire,
)
from .grok_ssh import GrokSshTransport
from telegram_bot.utils.secure_fs import _validate_storage_directory

MAX_REVISIONS = 97  # initial binding + 32 attempted/accepted/complete operations
MAX_RECORD = 256 * 1024


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class GrokBinding:
    destination: str
    agent_id: str
    conversation_id: str
    working_directory: str

    def __post_init__(self) -> None:
        GrokSshTransport(self.destination, self.agent_id)  # validates only, no network
        identifier(self.conversation_id)
        _text(self.working_directory, 4096, "invalid_working_directory")
        if not Path(self.working_directory).is_absolute():
            raise ProtocolError("invalid_working_directory")

    @property
    def session_id(self) -> str:
        return "grok-" + hashlib.sha256(canonical(asdict(self))).hexdigest()


def _regular(fd: int) -> os.stat_result:
    s = os.fstat(fd)
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid()
            or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600):
        raise ProtocolError("unsafe_grok_state")
    return s


def _operation(value: Any, binding: GrokBinding) -> None:
    if value is None:
        return
    fields = {"stage", "nonce", "prompt", "digest", "baseline", "accepted", "reply"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ProtocolError("invalid_grok_operation")
    if value["digest"] != prompt_digest(binding.agent_id, value["nonce"], value["prompt"]):
        raise ProtocolError("grok_digest_mismatch")
    baseline = value["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {"last_id", "request_ids"}:
        raise ProtocolError("invalid_grok_baseline")
    if baseline["last_id"] is not None:
        identifier(baseline["last_id"])
    ids = baseline["request_ids"]
    if (not isinstance(ids, list) or len(ids) > 64
            or ids != sorted({identifier(v) for v in ids})):
        raise ProtocolError("invalid_grok_baseline")
    stage = value["stage"]
    if stage not in {"attempted", "accepted", "complete"}:
        raise ProtocolError("invalid_grok_stage")
    accepted = value["accepted"]
    if stage == "attempted":
        if accepted is not None or value["reply"] is not None:
            raise ProtocolError("invalid_grok_attempt")
        return
    expected = {"agent_id": binding.agent_id, "nonce": value["nonce"], "digest": value["digest"]}
    if (not isinstance(accepted, dict) or set(accepted) != {*expected, "echo_id"}
            or any(accepted[k] != v for k, v in expected.items())):
        raise ProtocolError("invalid_grok_acceptance")
    identifier(accepted["echo_id"])
    reply = value["reply"]
    if stage == "accepted":
        if reply is not None:
            raise ProtocolError("invalid_grok_reply")
        return
    _reply(reply, ids, accepted["echo_id"])


def _reply(reply: Any, prior_ids: list[str], echo_id: str) -> None:
    if not isinstance(reply, dict) or set(reply) != {"request_id", "entry_ids", "texts"}:
        raise ProtocolError("invalid_grok_reply")
    identifier(reply["request_id"])
    if reply["request_id"] in prior_ids:
        raise ProtocolError("reused_request_id")
    entries, texts = reply["entry_ids"], reply["texts"]
    if (not isinstance(entries, list) or not isinstance(texts, list)
            or not 1 <= len(entries) == len(texts) <= 63
            or len(set(identifier(v) for v in entries)) != len(entries)
            or echo_id in entries):
        raise ProtocolError("invalid_grok_reply")
    if sum(len(_text(t, MAX_REPLY, "invalid_reply_text").encode()) for t in texts) > MAX_REPLY:
        raise ProtocolError("invalid_grok_reply")


def _transition(old: Any, new: Any) -> None:
    if new is None:
        raise ProtocolError("grok_history_reset")
    if old is None or old["stage"] == "complete":
        if new["stage"] != "attempted":
            raise ProtocolError("invalid_grok_transition")
        return
    target = "accepted" if old["stage"] == "attempted" else "complete"
    if new["stage"] != target:
        raise ProtocolError("invalid_grok_transition")
    for key in ("nonce", "prompt", "digest", "baseline"):
        if old[key] != new[key]:
            raise ProtocolError("grok_intent_changed")
    if target == "complete" and old["accepted"] != new["accepted"]:
        raise ProtocolError("grok_acceptance_changed")


class GrokJournal:
    """Explicit create only; opening missing/partial state never initializes it."""
    def __init__(self, root: Path, binding: GrokBinding):
        self.root = Path(os.path.abspath(root))
        self.binding = binding

    def create(self) -> None:
        _validate_storage_directory(self.root.parent)
        os.mkdir(self.root, 0o700)  # existing, partial and unknown state is retained
        d = self._directory()
        try:
            lock = os.open("lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                           0o600, dir_fd=d)
            try:
                os.fchmod(lock, 0o600)
                os.fsync(lock)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                claim = GrokClaim(self, d, lock)
                claim.append(None, initial=True)
            finally:
                os.close(lock)
            os.fsync(d)
            parent = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            os.close(d)

    def _directory(self) -> int:
        _validate_storage_directory(self.root)
        d = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._validate_directory(d)
            return d
        except BaseException:
            os.close(d)
            raise

    def _validate_directory(self, d: int) -> None:
        s, named = os.fstat(d), self.root.lstat()
        if (not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid()
                or stat.S_IMODE(s.st_mode) != 0o700
                or (s.st_dev, s.st_ino) != (named.st_dev, named.st_ino)
                or stat.S_ISLNK(named.st_mode)):
            raise ProtocolError("unsafe_grok_directory")

    @contextmanager
    def claim(self) -> Iterator[GrokClaim]:
        d = self._directory()
        lock = None
        try:
            lock = os.open("lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
            _regular(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ProtocolError("grok_conversation_busy") from None
            claim = GrokClaim(self, d, lock)
            try:
                yield claim
            finally:
                claim.closed = True
        finally:
            if lock is not None:
                os.close(lock)
            os.close(d)


class GrokClaim:
    def __init__(self, journal: GrokJournal, directory: int, lock: int):
        self.journal, self.directory = journal, directory
        self.lock = lock
        self.closed = False

    def _check(self) -> None:
        if self.closed:
            raise ProtocolError("grok_claim_closed")
        self.journal._validate_directory(self.directory)
        opened = _regular(self.lock)
        named = os.stat("lock", dir_fd=self.directory, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ProtocolError("grok_lock_changed")

    def _read(self, name: str) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.directory)
        try:
            before = _regular(fd)
            if not 0 < before.st_size <= MAX_RECORD:
                raise ProtocolError("grok_record_limit")
            raw = b""
            while len(raw) <= MAX_RECORD:
                chunk = os.read(fd, min(65536, MAX_RECORD + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            after = _regular(fd)
            if (len(raw) != before.st_size or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns):
                raise ProtocolError("grok_state_changed")
            return raw
        finally:
            os.close(fd)

    def load(self, *, initial: bool = False) -> tuple[dict[str, Any] | None, int, str | None]:
        self._check()
        names = []
        with os.scandir(self.directory) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_REVISIONS + 1:
                    raise ProtocolError("grok_history_limit")
        if "lock" not in names:
            raise ProtocolError("grok_lock_missing")
        names.remove("lock")
        if names != [] and sorted(names) != [f"{n:04d}.json" for n in range(len(names))]:
            raise ProtocolError("grok_history_unknown_or_missing")
        if not names and not initial:
            raise ProtocolError("grok_history_missing")
        previous = None
        operation = None
        nonces: set[str] = set()
        for number, name in enumerate(sorted(names)):
            raw = self._read(name)
            value = decode_wire(raw)
            if (not isinstance(value, dict)
                    or set(value) != {"schema", "revision", "previous", "binding", "host_version", "operation"}
                    or type(value["schema"]) is not int or value["schema"] != 1
                    or type(value["revision"]) is not int or value["revision"] != number
                    or value["previous"] != previous or value["host_version"] != HOST_VERSION
                    or value["binding"] != asdict(self.journal.binding) or canonical(value) != raw):
                raise ProtocolError("grok_history_invalid")
            new = value["operation"]
            _operation(new, self.journal.binding)
            if number == 0:
                if new is not None:
                    raise ProtocolError("grok_initial_state_invalid")
            else:
                _transition(operation, new)
                if new["stage"] == "attempted":
                    if new["nonce"] in nonces:
                        raise ProtocolError("grok_nonce_reused")
                    nonces.add(new["nonce"])
            operation = new
            previous = hashlib.sha256(raw).hexdigest()
        return operation, len(names), previous

    def append(self, operation: Any, *, initial: bool = False) -> None:
        old, revision, previous = self.load(initial=initial)
        if revision >= MAX_REVISIONS:
            raise ProtocolError("grok_history_limit")
        if initial:
            if revision or operation is not None:
                raise ProtocolError("grok_already_initialized")
        else:
            # Normalize dataclass tuple fields before semantic validation.
            operation = decode_wire(canonical(operation))
            _operation(operation, self.journal.binding)
            _transition(old, operation)
        value = {"schema": 1, "revision": revision, "previous": previous,
                 "binding": asdict(self.journal.binding), "host_version": HOST_VERSION,
                 "operation": operation}
        raw = canonical(value)
        if len(raw) > MAX_RECORD:
            raise ProtocolError("grok_record_limit")
        temporary = "pending-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as out:
                out.write(raw)
                out.flush()
                os.fsync(fd)
            self._check()
            os.rename(temporary, f"{revision:04d}.json", src_dir_fd=self.directory,
                      dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            os.close(fd)  # incomplete pending files retained; next open denies

    def attempt(self, prompt: str, baseline: Baseline) -> dict[str, Any]:
        nonce = str(uuid.uuid4())
        value = {"stage": "attempted", "nonce": nonce, "prompt": prompt,
                 "digest": prompt_digest(self.journal.binding.agent_id, nonce, prompt),
                 "baseline": asdict(baseline), "accepted": None, "reply": None}
        self.append(value)
        return self.load()[0]  # type: ignore[return-value]

    def accept(self, value: dict[str, Any], accepted: AcceptedPrompt) -> dict[str, Any]:
        candidate = {**value, "stage": "accepted", "accepted": asdict(accepted)}
        self.append(candidate)
        return self.load()[0]  # type: ignore[return-value]

    def complete(self, value: dict[str, Any], reply: BoundReply) -> dict[str, Any]:
        candidate = {**value, "stage": "complete", "reply": asdict(reply)}
        self.append(candidate)
        return self.load()[0]  # type: ignore[return-value]
