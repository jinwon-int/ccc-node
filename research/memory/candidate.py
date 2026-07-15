"""Editable ranking candidate for the shared-all memory autoresearch track."""

from __future__ import annotations

import re
from typing import Any


_STOP_WORDS = {
    "about",
    "and",
    "current",
    "does",
    "from",
    "have",
    "is",
    "only",
    "operator",
    "the",
    "use",
    "uses",
    "was",
    "what",
    "which",
    "with",
    "관련",
    "뭐야",
    "어디야",
    "현재",
}


def _tokens(value: str) -> set[str]:
    normalized: set[str] = set()
    for token in re.findall(r"[0-9A-Za-z_가-힣]+", value):
        lowered = token.lower()
        if lowered.isascii() and len(lowered) > 4 and lowered.endswith("s"):
            lowered = lowered[:-1]
        if len(lowered) >= 2 and lowered not in _STOP_WORDS:
            normalized.add(lowered)
    return normalized


def rank(
    query: str,
    context: dict[str, Any],
    documents: list[dict[str, Any]],
    limit: int,
) -> list[str]:
    """Return ranked document ids without crossing owner or secret boundaries."""

    query_tokens = _tokens(query)
    historical_requested = bool(
        query_tokens & {"historical", "history", "before", "과거", "이전"}
    )
    volatile_requested = bool(
        query_tokens
        & {"pending", "rollout", "status", "temporary", "volatile", "진행", "상태", "임시"}
    )
    owner_id = context.get("owner_id")
    current_scope = context.get("conversation_scope")
    current_group = context.get("group_id")
    scored: list[tuple[float, str]] = []

    for document in documents:
        if document.get("owner_id") != owner_id:
            continue
        if document.get("sensitivity") == "secret":
            continue
        if document.get("valid_until") and not historical_requested:
            continue
        if document.get("durability") == "volatile" and not volatile_requested:
            continue

        text_tokens = _tokens(
            " ".join(
                str(document.get(key, ""))
                for key in ("text", "label", "group_id", "conversation_scope")
            )
        )
        overlap = len(query_tokens & text_tokens)
        if overlap == 0:
            continue

        score = overlap * 4.0
        phrase = query.strip().lower()
        if phrase and phrase in str(document.get("text", "")).lower():
            score += 3.0

        scope = document.get("conversation_scope")
        if scope == "global":
            score += 1.0
        elif scope == current_scope:
            score += 2.0
            if scope == "group" and document.get("group_id") == current_group:
                score += 2.0

        durability = document.get("durability", "durable")
        if durability == "durable":
            score += 1.0
        elif durability == "volatile":
            score -= 2.0

        if document.get("valid_until"):
            score -= 1.5

        scored.append((score, str(document["id"])))

    scored.sort(key=lambda item: (-item[0], item[1]))
    if not scored or limit <= 0:
        return []
    relevance_floor = max(4.0, scored[0][0] - 2.0)
    return [
        document_id
        for score, document_id in scored
        if score >= relevance_floor
    ][:limit]
