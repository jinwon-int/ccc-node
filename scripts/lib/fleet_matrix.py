#!/usr/bin/env python3
"""Shared fleet-matrix evidence parser + classifier (#451).

The doctor and security-audit fleet rollups consume the same
``===== <node> =====`` evidence-block format and previously carried a
byte-identical block parser plus a copy-pasted classifier skeleton that had
already drifted (only security handled ``critical``/secret mentions, only doctor
extracted a version). One format with three maintenance points meant a fix to
the block rule in one place silently mis-classified the others.

This module is the single home of the block parser and the classification core.
Per-domain differences (keyword tables, extra node fields, mutation flags, the
JSON ``kind``) are injected via a :class:`Domain` config, so the shell scripts
shrink to thin wrappers. Output is intentionally byte-for-byte identical to the
pre-refactor scripts (pinned by ccc-fleet-matrix.test.sh golden fixtures).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

#: Canonical evidence-block delimiter. Defined ONCE here (was duplicated across
#: the doctor and security parsers, and re-implemented a third time in awk).
_BLOCK_RE = re.compile(r"^=====\s+([^=\s]+)\s+=====$")

#: Substrings that mark a node as unreachable / probe-failed (shared).
_UNREACHABLE = [
    "permission denied",
    "connection refused",
    "timed out",
    "no route to host",
    "ssh:",
]

#: Fixed summary key order (shared).
_SUMMARY_KEYS = ["정상", "경고", "교정가능", "수동필요", "위험"]

_SECRET_WORDS = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|bearer)", re.I
)


def parse_evidence_blocks(text: str) -> Dict[str, str]:
    """Split evidence text into ``{node: body}`` using the canonical delimiter."""
    blocks: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in text.splitlines():
        m = _BLOCK_RE.match(line.strip())
        if m:
            if current is not None:
                blocks[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        blocks[current] = "\n".join(buf).strip()
    return blocks


@dataclass
class Domain:
    """Per-matrix classification/output knobs injected into the shared core."""

    kind: str
    #: Extra substrings (beyond 위험/danger) that make a JSON body 위험.
    json_danger_extra: List[str]
    #: Extra substrings checked against ``body.lower()`` in the text-danger branch.
    text_danger_low_extra: List[str]
    #: Extra substrings checked against the raw ``body`` in the text-danger branch.
    text_danger_body_extra: List[str]
    #: The lowercased "…ok" phrase recognised as 정상 in the text branch.
    ok_phrase_low: str
    #: (status, reason) table keyed by an internal outcome id.
    reasons: Dict[str, Tuple[str, str]]
    #: Ordered mutation-flag dict (all False; read-only guarantee).
    mutations: Dict[str, bool]
    #: Optional field inserted right after ``node`` (doctor: version).
    after_node: Optional[Tuple[str, Callable[[str], object]]] = None
    #: Optional field appended after ``lineCount`` (security: secretWordOnlyMention).
    tail_field: Optional[Tuple[str, Callable[[str], object]]] = None


def classify(body: str, domain: Domain) -> Tuple[str, str]:
    low = body.lower()
    r = domain.reasons
    if not body:
        return r["missing"]
    if any(s in low for s in _UNREACHABLE):
        return r["unreachable"]
    try:
        obj = json.loads(body)
        serial = json.dumps(obj, ensure_ascii=False).lower()
        danger = ["위험", "danger", *domain.json_danger_extra]
        if (isinstance(obj, dict) and obj.get("ok") is False) or any(
            s in serial for s in danger
        ):
            return r["json_danger"]
        if "수동필요" in serial or "manual" in serial:
            return r["json_manual"]
        if "교정가능" in serial or "fixable" in serial:
            return r["json_fixable"]
        if "경고" in serial or "warning" in serial:
            return r["json_warning"]
        return r["json_ok"]
    except Exception:
        pass
    if re.search(r"\bFAIL=([1-9][0-9]*)\b", body) or any(
        s in low for s in domain.text_danger_low_extra
    ) or any(s in body for s in domain.text_danger_body_extra):
        return r["text_fail"]
    if (
        re.search(r"\bPASS=\d+\s+FAIL=0\b", body)
        or domain.ok_phrase_low in low
        or "정상" in body
    ):
        return r["text_ok"]
    if "warning" in low or "경고" in body:
        return r["text_warning"]
    return r["unclassified"]


def extract_version(body: str) -> Optional[str]:
    if not body:
        return None
    try:
        obj = json.loads(body)
        for key in ("harnessVersion", "harness_version", "version"):
            if isinstance(obj, dict) and obj.get(key):
                return str(obj[key])
    except Exception:
        pass
    m = re.search(r"harness version:\s*`?([^`\n]+)`?", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"\bversion[:=]\s*`?([^`\s]+)`?", body, re.IGNORECASE)
    return m.group(1).strip() if m else None


def build_matrix(path: Path, known: List[str], domain: Domain) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = parse_evidence_blocks(text)
    nodes: List[dict] = []
    seen = set()
    for name in known + [n for n in blocks if n not in known]:
        if name in seen:
            continue
        seen.add(name)
        body = blocks.get(name, "")
        status, reason = classify(body, domain)
        rec: Dict[str, object] = {"node": name}
        if domain.after_node is not None:
            rec[domain.after_node[0]] = domain.after_node[1](body)
        rec["status"] = status
        rec["reason"] = reason
        rec["evidencePresent"] = bool(body)
        rec["lineCount"] = len(body.splitlines()) if body else 0
        if domain.tail_field is not None:
            rec[domain.tail_field[0]] = domain.tail_field[1](body)
        nodes.append(rec)
    summary = {k: sum(1 for n in nodes if n["status"] == k) for k in _SUMMARY_KEYS}
    return {
        "kind": domain.kind,
        "mode": "read-only",
        "source": str(path),
        "nodes": nodes,
        "summary": summary,
        "mutations": domain.mutations,
    }


DOMAINS: Dict[str, Domain] = {
    "doctor": Domain(
        kind="ccc-doctor-fleet-matrix",
        json_danger_extra=[],
        text_danger_low_extra=[],
        text_danger_body_extra=[],
        ok_phrase_low="doctor ok",
        reasons={
            "missing": ("수동필요", "missing_evidence"),
            "unreachable": ("수동필요", "node_unreachable_or_probe_failed"),
            "json_danger": ("위험", "doctor_reported_failure"),
            "json_manual": ("수동필요", "manual_action_required"),
            "json_fixable": ("교정가능", "fixable_drift"),
            "json_warning": ("경고", "warnings_present"),
            "json_ok": ("정상", "doctor_ok_json"),
            "text_fail": ("위험", "test_failures_present"),
            "text_ok": ("정상", "doctor_ok_text"),
            "text_warning": ("경고", "warnings_present"),
            "unclassified": ("수동필요", "unclassified_output"),
        },
        mutations={
            "ssh": False,
            "serviceRestart": False,
            "providerSend": False,
            "secretRead": False,
        },
        after_node=("version", extract_version),
    ),
    "security": Domain(
        kind="ccc-security-audit-fleet-matrix",
        json_danger_extra=["critical"],
        text_danger_low_extra=["critical"],
        text_danger_body_extra=["위험"],
        ok_phrase_low="security audit ok",
        reasons={
            "missing": ("수동필요", "missing_evidence"),
            "unreachable": ("수동필요", "node_unreachable_or_probe_failed"),
            "json_danger": ("위험", "security_audit_reported_failure"),
            "json_manual": ("수동필요", "manual_action_required"),
            "json_fixable": ("교정가능", "fixable_security_drift"),
            "json_warning": ("경고", "security_warnings_present"),
            "json_ok": ("정상", "security_audit_ok_json"),
            "text_fail": ("위험", "security_failures_present"),
            "text_ok": ("정상", "security_audit_ok_text"),
            "text_warning": ("경고", "security_warnings_present"),
            "unclassified": ("수동필요", "unclassified_output"),
        },
        mutations={
            "ssh": False,
            "permissionChange": False,
            "serviceRestart": False,
            "providerSend": False,
            "secretRead": False,
        },
        tail_field=("secretWordOnlyMention", lambda body: bool(_SECRET_WORDS.search(body))),
    ),
}


def main(argv: List[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--domain", required=True, choices=sorted(DOMAINS))
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--node-list", default="")
    args = parser.parse_args(argv)

    known = [x.strip() for x in args.node_list.split(",") if x.strip()]
    matrix = build_matrix(Path(args.evidence), known, DOMAINS[args.domain])
    print(json.dumps(matrix, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
