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


def _render_resume(document: Mapping[str, object], max_bytes: int) -> bytes | None:
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
    if len(payload) <= max_bytes:
        return payload
    suffix = "\n… [truncated by CCC resume budget]\n".encode()
    prefix = payload[: max(0, max_bytes - len(suffix))]
    return prefix.decode("utf-8", errors="ignore").encode() + suffix


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = json.loads(sys.stdin.read())
        if not isinstance(document, dict):
            raise ValueError("distill input must be an object")
        state_dir, _, _ = _state_paths()
        audience = _validate_audience(state_dir)
        max_facts = int(os.environ.get("CCC_LOCAL_FACTS_MAX", "1000"))
        max_resume = int(os.environ.get("CCC_RESUME_WRITE_MAX_BYTES", "4000"))
        if max_facts <= 0 or max_resume < 256:
            raise ValueError("local-memory bounds are invalid")
        transaction_module = _load_transaction_module()
        facts_added = 0

        def transform(current: Mapping[str, bytes | None]) -> Mapping[str, bytes | None]:
            nonlocal facts_added
            desired: dict[str, bytes | None] = {}
            if args.mode in {"both", "facts"}:
                facts, facts_added = _render_facts(
                    document,
                    current["memory-facts.jsonl"],
                    audience=audience,
                    max_facts=max_facts,
                )
                if facts is not None:
                    desired["memory-facts.jsonl"] = facts
            if args.mode in {"both", "resume"}:
                resume = _render_resume(document, max_resume)
                if resume is not None:
                    desired["resume.md"] = resume
            return desired

        result = transaction_module.LocalMemoryTransaction(state_dir).commit(
            transform,
            provider="claude",
            actor="distill",
            tool="local-memory-commit",
            session=str(document.get("session_id") or ""),
            diff=f"mode-{args.mode}",
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"local-memory commit failed: {error}", file=sys.stderr)
        return 1
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
