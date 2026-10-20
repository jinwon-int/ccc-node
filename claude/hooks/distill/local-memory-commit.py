#!/usr/bin/env python3
"""Render and transactionally commit Claude local distill memory (#386)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Mapping, Sequence


def _load_transaction_module() -> ModuleType:
    configured = os.environ.get("CCC_LOCAL_MEMORY_TRANSACTION_PY")
    installed = Path(__file__).resolve().parents[1] / "ccc_local_memory_transaction.py"
    repository = Path(__file__).resolve().parents[3] / "bridge/memory/local_memory_transaction.py"
    candidates = [Path(configured)] if configured else []
    candidates.extend((installed, repository))
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise RuntimeError("local-memory transaction module is unavailable")
    spec = importlib.util.spec_from_file_location("ccc_local_memory_transaction", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("local-memory transaction module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("both", "facts", "resume"), default="both")
    return parser


def _absolute(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def _state_paths() -> tuple[Path, Path, Path]:
    state_dir = _absolute(
        os.environ.get("CCC_STATE_DIR", str(Path.home() / ".claude" / "state"))
    )
    facts_path = _absolute(
        os.environ.get("CCC_MEMORY_FACTS_FILE", str(state_dir / "memory-facts.jsonl"))
    )
    resume_path = _absolute(
        os.environ.get("CCC_RESUME_FILE", str(state_dir / "resume.md"))
    )
    if facts_path != state_dir / "memory-facts.jsonl":
        raise ValueError("CCC_MEMORY_FACTS_FILE must select the state facts target")
    if resume_path != state_dir / "resume.md":
        raise ValueError("CCC_RESUME_FILE must select the state resume target")
    return state_dir, facts_path, resume_path


def _enabled(value: str) -> bool:
    return value.lower() not in {"0", "false", "off", "no"}


_RESUME_SKIP_VALUES = {"skip", "facts-only", "0", "off", "false", "no"}


def _resume_policy() -> str:
    """``write`` or ``skip`` (#2319).

    distill.sh resolves ownership once per session and hands the verdict down
    as ``CLAUDE_DISTILL_RESUME_POLICY`` (``write`` | ``skip:<reason>``); callers
    that know they are automated (headless.sh) export
    ``CCC_DISTILL_RESUME_POLICY`` directly. Either spelling of skip means: this
    session may add facts but must not replace the owner's resume.md.
    """
    for key in ("CLAUDE_DISTILL_RESUME_POLICY", "CCC_DISTILL_RESUME_POLICY"):
        value = os.environ.get(key, "").strip()
        if value:
            head = value.split(":", 1)[0].lower()
            return "skip" if head in _RESUME_SKIP_VALUES else "write"
    return "write"


def _validate_audience(state_dir: Path) -> str:
    scoped = _enabled(os.environ.get("CCC_MEMORY_AUDIENCE_SCOPED", "0"))
    audience = os.environ.get("CCC_MEMORY_AUDIENCE", "legacy")
    if not scoped:
        return "private"
    if audience not in {"private", "shared"}:
        raise ValueError("invalid audience metadata")
    root_value = os.environ.get("CCC_MEMORY_AUDIENCE_ROOT", "")
    scope = os.environ.get("CCC_MEMORY_SCOPE", "")
    if not root_value:
        raise ValueError("invalid audience paths")
    root = _absolute(root_value)
    if audience == "shared":
        valid_scope = scope == "shared"
    else:
        suffix = scope.removeprefix("private-")
        valid_scope = (
            scope.startswith("private-")
            and len(suffix) == 32
            and not set(suffix) - set("0123456789abcdef")
        )
    if not valid_scope or state_dir != root / scope / "state":
        raise ValueError("invalid audience paths")
    return audience


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[0-9a-z가-힣]+", text.lower()))


def _render_facts(
    document: Mapping[str, object],
    existing: bytes | None,
    *,
    audience: str,
    max_facts: int,
) -> tuple[bytes | None, int]:
    items = document.get("honcho")
    if not isinstance(items, list) or not items:
        return None, 0
    existing_lines = []
    seen: set[str] = set()
    for raw_line in (existing or b"").decode("utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        existing_lines.append(raw_line)
        try:
            value = json.loads(raw_line)
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                normalized = _normalize(value["text"])
                if normalized:
                    seen.add(normalized)
        except (TypeError, ValueError):
            continue
    session = str(document.get("session_id") or "")
    trigger = str(document.get("trigger") or "")
    observed_at = str(document.get("distilled_at") or "") or datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    added: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        normalized = _normalize(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        subject = str(item.get("subject") or "").strip()
        fact = {
            "id": "distill-"
            + hashlib.sha256((normalized + session).encode()).hexdigest()[:12],
            "kind": str(item.get("kind") or "observation"),
            "text": text,
            "review": "auto-local",
            "privacy": audience,
            "confidence": 0.7,
            "observed_at": observed_at,
            "entities": [subject] if subject else [],
            "tags": ["distilled"] + ([trigger] if trigger else []),
            "source": {"type": "distill", "session": session, "trigger": trigger},
        }
        because = str(item.get("because") or "").strip()
        if because:
            fact["because"] = because
        if _enabled(os.environ.get("CCC_MEMORY_AUDIENCE_SCOPED", "0")):
            fact["audience"] = audience
        added.append(json.dumps(fact, ensure_ascii=False))
    if not added:
        return None, 0
    lines = (existing_lines + added)[-max_facts:]
    return ("\n".join(lines) + "\n").encode(), len(added)


def _clean(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, (list, tuple)):
        return ", ".join(item for entry in value if (item := _clean(entry)))
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = re.sub(
        r"(?:ghp|gho|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}",
        "[REDACTED:gh-token]",
        text,
    )
    text = re.sub(r"sk-[A-Za-z0-9_-]{20,}", "[REDACTED:api-key]", text)
    text = re.sub(r"AKIA[A-Z0-9]{16}", "[REDACTED:aws-key]", text)
    text = re.sub(r"Bearer [A-Za-z0-9._-]{20,}", "Bearer [REDACTED]", text)
    return text.replace(
        "-----BEGIN PRIVATE KEY-----", "[REDACTED:pem-begin]"
    )[:1000]


# Same first-line header the bridge sink writes and reads back for ordering
# (bridge/memory/distill_local_sink.py); load-memory.sh drops it at injection.
_RESUME_HEADER_PREFIX = "<!-- ccc-node:distill "
_RESUME_HEADER_SUFFIX = " -->"
_CAPTURED_AT_KEY = "captured_at="


# Mirrors CodexLocalMemorySink._parse_capture_time and
# CodexLocalMemorySink._current_resume_captured_at in
# bridge/memory/distill_local_sink.py. The hook cannot import the sink at
# runtime: setup.sh installs only the transaction module next to the hooks and
# the sink module needs the bridge package (pydantic). Keep the two in sync;
# bridge/tests/test_distill_local_worker.py asserts they agree on fixtures.
def _parse_capture_time(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _current_resume_captured_at(payload: bytes | None) -> datetime | None:
    """The ``captured_at`` of the current resume.md header, if parseable."""
    if not payload:
        return None
    first_line = payload.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    if not (
        first_line.startswith(_RESUME_HEADER_PREFIX)
        and first_line.endswith(_RESUME_HEADER_SUFFIX)
    ):
        return None
    fields = first_line[len(_RESUME_HEADER_PREFIX) : -len(_RESUME_HEADER_SUFFIX)]
    for token in fields.split(" "):
        if token.startswith(_CAPTURED_AT_KEY):
            return _parse_capture_time(token[len(_CAPTURED_AT_KEY) :])
    return None


def _resume_mtime(path: Path) -> datetime | None:
    try:
        stamp = os.stat(path, follow_symlinks=False).st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc)


def _keep_current_resume(
    current: bytes | None, new_capture: datetime | None, resume_path: Path
) -> bool:
    """True when this job must not replace the current resume.md (#2320).

    Same ordering as the bridge sink's ``write`` (resume_never_regresses), so
    a pending-drain replay of an old CLI backlog cannot overwrite a resume
    captured later (by the bridge or by a newer hook job):

    - current header has ``captured_at`` strictly later than this job's
      capture -> keep. Equal times write (the later commit wins, as in the
      sink, whose comparison is also strict).
    - current resume is non-empty without a readable ``captured_at`` (a
      pre-#2310 hook resume or a pre-#2297 audience header) -> keep when its
      file mtime is later than this job's capture (#2309's mtime proxy).
    - this job has no capture time (run outside the pending journal) ->
      write, as before #2320. Here the hook deliberately differs from the
      sink, which keeps a stamped resume against an unstamped capture: the
      hook path has no other ordering input and a direct run is the newest
      session by construction.
    """
    if new_capture is None or not current:
        return False
    current_capture = _current_resume_captured_at(current)
    if current_capture is not None:
        return current_capture > new_capture
    current_mtime = _resume_mtime(resume_path)
    return current_mtime is not None and current_mtime > new_capture


def _capture_header(value: str | None) -> bytes:
    """Resume header carrying the job's capture time, or b"" without one.

    #2310 L1: a hook resume used to be header-less, so the bridge sink ordered
    it by file mtime — when extraction finished, not when the CLI session was
    captured. ``CLAUDE_DISTILL_CAPTURED_AT`` is set by the pending journal from
    the job's enqueue time (the transcript hash pin), a host-clock time that a
    replay keeps unchanged; it is rendered as the bridge's fixed-width UTC.
    Unset or unparseable -> no header (mtime ordering, as before).
    """
    utc = _parse_capture_time(value)
    if utc is None:
        return b""
    stamp = f"{utc.year:04d}" + utc.strftime("-%m-%dT%H:%M:%S.%fZ")
    return (
        f"{_RESUME_HEADER_PREFIX}captured_at={stamp} provider=claude"
        f"{_RESUME_HEADER_SUFFIX}\n"
    ).encode()


def _render_resume(
    document: Mapping[str, object],
    max_bytes: int,
    *,
    header: bytes = b"",
) -> bytes | None:
    resume = document.get("resume")
    if not isinstance(resume, dict):
        return None
    fields = (
        ("last_activity", "마지막 작업"),
        ("pending_action", "다음 액션"),
        ("awaiting_user", "사용자 대기"),
        ("open_question", "열린 질문"),
        ("next_step", "다음 한 수"),
    )
    lines = [
        f"- {label}: {cleaned}"
        for key, label in fields
        if (cleaned := _clean(resume.get(key)))
    ]
    if evidence := _clean(resume.get("evidence")):
        lines.append(f"- 근거: {evidence}")
    if not lines:
        return None
    payload = ("\n".join(lines) + "\n").encode()
    # The header is never truncated; only the body is budgeted (as in the sink).
    max_bytes -= len(header)
    if len(payload) <= max_bytes:
        return header + payload
    suffix = "\n… [truncated by CCC resume budget]\n".encode()
    prefix = payload[: max(0, max_bytes - len(suffix))]
    return header + prefix.decode("utf-8", errors="ignore").encode() + suffix


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = json.loads(sys.stdin.read())
        if not isinstance(document, dict):
            raise ValueError("distill input must be an object")
        state_dir, _, resume_path = _state_paths()
        audience = _validate_audience(state_dir)
        max_facts = int(os.environ.get("CCC_LOCAL_FACTS_MAX", "1000"))
        max_resume = int(os.environ.get("CCC_RESUME_WRITE_MAX_BYTES", "4000"))
        if max_facts <= 0 or max_resume < 256:
            raise ValueError("local-memory bounds are invalid")
        transaction_module = _load_transaction_module()
        captured_at = os.environ.get("CLAUDE_DISTILL_CAPTURED_AT")
        header = _capture_header(captured_at)
        new_capture = _parse_capture_time(captured_at)
        facts_added = 0
        resume_kept = False
        mode = args.mode
        if _resume_policy() == "skip" and mode in {"both", "resume"}:
            if mode == "resume":
                print("local-memory: resume skipped reason=resume-policy")
                return 0
            mode = "facts"

        def transform(current: Mapping[str, bytes | None]) -> Mapping[str, bytes | None]:
            nonlocal facts_added, resume_kept
            desired: dict[str, bytes | None] = {}
            if mode in {"both", "facts"}:
                facts, facts_added = _render_facts(
                    document,
                    current["memory-facts.jsonl"],
                    audience=audience,
                    max_facts=max_facts,
                )
                if facts is not None:
                    desired["memory-facts.jsonl"] = facts
            if mode in {"both", "resume"}:
                resume = _render_resume(document, max_resume, header=header)
                # Decided under the transaction lock against the CURRENT
                # resume (#2320); facts above are merged either way.
                resume_kept = resume is not None and _keep_current_resume(
                    current["resume.md"], new_capture, resume_path
                )
                if resume is not None and not resume_kept:
                    desired["resume.md"] = resume
            return desired

        result = transaction_module.LocalMemoryTransaction(state_dir).commit(
            transform,
            provider="claude",
            actor="distill",
            tool="local-memory-commit",
            session=str(document.get("session_id") or ""),
            diff=f"mode-{mode}",
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"local-memory commit failed: {error}", file=sys.stderr)
        return 1
    if resume_kept:
        print("local-memory: resume kept reason=newer-capture")
    if facts_added:
        print(f"local-facts: appended {facts_added} fact(s) to {state_dir / 'memory-facts.jsonl'}")
    if result.action_id:
        print(
            f"local-memory: committed action={result.action_id} "
            f"targets={','.join(result.changed_targets)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
