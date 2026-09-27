#!/usr/bin/env python3
"""Trigger-wording check for skill descriptions (single source for ccc-node).

Claude Code (and the other fleet runtimes) choose a skill from its
``description`` alone, and the per-turn skill listing truncates descriptions to
a budget, so a description must say WHEN to use the skill, ideally first.
fleet-skills#315/#316 made ``scripts/validate.py`` in jinwon-int/fleet-skills
ERROR on approved skills whose description lacks trigger wording, so an
autosave draft without it installs locally but later fails promotion.

``TRIGGER_RE`` below is a VERBATIM copy of ``TRIGGER_RE`` in fleet-skills
``scripts/validate.py`` (and ``has_trigger_wording`` of its helper of the same
name). Do not edit it here alone: change fleet-skills first, then copy the new
pattern into this file so the local autosave gate and the promotion gate never
diverge silently. ``description-trigger.test.sh`` pins the accepted/rejected
examples on both sides of the pattern.

CLI (used by autoinstall.sh ``gate_lint``): ``description_trigger.py check``
reads the description from stdin; exit 0 = trigger wording present,
1 = missing, 2 = usage error. ownership.py imports ``has_trigger_wording`` for
incremental SKILL.md patches that rewrite the description.
"""

from __future__ import annotations

import re
import sys

# Source: jinwon-int/fleet-skills scripts/validate.py TRIGGER_RE (fleet-skills#315).
# One regex, any match passes:
#   * a leading trigger clause ("When ...", "Before ...", "After ...");
#   * "Use/Invoke/Apply/Load [this|it [skill]] when|whenever|before|after|
#     for|if|during|while";
#   * "Trigger:", "Triggers on", "Triggered when", "When to use";
#   * Korean "... 할 때" / "... 때 사용" (any 때 except 때문/때때로/때로) and
#     "... 시 사용|적용|호출|실행" (optionally 시에).
TRIGGER_RE = re.compile(
    r"^(?:when|whenever|before|after)\b"
    r"|\b(?:use|invoke|apply|load)(?:\s+(?:this|it)(?:\s+skill)?)?\s+"
    r"(?:when|whenever|before|after|for|if|during|while)\b"
    r"|\btrigger(?:s|ed)?\s*(?::|on\b|when\b|if\b)"
    r"|\bwhen to use\b"
    r"|때(?![문때로])"
    r"|시(?:에)?\s*(?:사용|적용|호출|실행)",
    re.I,
)


def has_trigger_wording(description: str) -> bool:
    return TRIGGER_RE.search(description.strip().strip("\"'").strip()) is not None


def main(argv: list[str]) -> int:
    if argv[1:] != ["check"]:
        print("usage: description_trigger.py check < description", file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read()
    try:
        description = data.decode("utf-8")
    except UnicodeDecodeError:
        return 1
    return 0 if has_trigger_wording(description) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
