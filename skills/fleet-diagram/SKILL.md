---
name: fleet-diagram
description: Use when an owner-facing answer would be clearer as a picture than as a table or prose — a fleet status matrix, an incident/canary timeline, or a PR/issue dependency graph. Renders a small JSON spec to PNG (and SVG) with no external renderer, saves it under the node's project root, and names the path so the bridge attaches it to the Telegram/Matrix reply (#2109 B).
---

# fleet-diagram — pictures for owner-facing reports (#2109 B)

Karpathy (X, 2026-10-02): after controlled-language prose, the next lever for
*understanding* LLM output is a diagram. This skill makes one in-place: write
a spec, render it, name the file in the answer. The bridge sends every real
`png`/`svg`/`pdf`… path an answer mentions under `PROJECT_ROOT`
(`bridge/core/deliverables.py`), on Telegram and Matrix alike — a PNG goes
as a photo, an SVG as a document.

## When to use

- A **status matrix** (nodes × lanes/components, cells = ok/warn/fail/n/a).
- A **timeline** of an incident, canary or deploy (ordered events on lanes).
- A **dependency graph** (PRs, issues, deploy ticks) — must be acyclic.

Do not use it for data the owner needs to copy (IDs, SHAs, commands) — keep
those in text. Never put secrets or raw log bodies in labels; the labels are
rendered verbatim. Keep diagrams small (≤ ~40 cells / events / nodes); a
large graph reads worse than a list.

## How

1. Write the spec (UTF-8 JSON). Status keys with built-in colours: `ok`,
   `warn`, `fail`, `n/a`, `info`, `pending`; a `legend` may map keys to
   `green|amber|red|grey|blue|purple` or `#rrggbb`.

   ```json
   {"kind": "matrix", "title": "플릿 self-update 매트릭스",
    "rows": ["seoseo", "yukson"], "cols": ["harness", "bridge"],
    "cells": [["ok", "ok"], ["ok", "warn"]],
    "legend": {"ok": "green", "warn": "amber", "fail": "red", "n/a": "grey"}}
   ```
   ```json
   {"kind": "timeline", "title": "#2295 카나리",
    "events": [{"t": "13:25", "label": "nosuk 재시작", "lane": "nosuk", "status": "ok"},
               {"t": "13:40", "label": "r1 provider_timeout", "lane": "nosuk", "status": "fail"}]}
   ```
   ```json
   {"kind": "dag", "title": "PR 의존",
    "nodes": [{"id": "a", "label": "#2296", "status": "ok"}, {"id": "b", "label": "#2302"}],
    "edges": [["a", "b"]]}
   ```

2. Render into the owner-deliverable directory (under the bridge's
   `PROJECT_ROOT`, which is `$HOME` on a standard node):

   ```bash
   SKILL_DIR="${CLAUDE_SKILL_DIR:-$HOME/.claude/skills/fleet-diagram}"
   python3 "$SKILL_DIR/scripts/render_diagram.py" \
     --spec /tmp/spec.json --out "$HOME/.claude/state/artifacts" --name fleet-matrix-20261002
   ```
   Output is one JSON line: `{"ok": true, "files": [".../fleet-matrix-20261002.png", ".../....svg"], "png": true}`.
   `png: false` with `reason` means Pillow is not importable on this node —
   the SVG is still produced; name it instead (it arrives as a file, not a
   photo). `--format svg` skips the PNG attempt; `--format png` fails closed
   without Pillow (exit 4). Invalid specs exit 2 with `{"ok": false, "error"}`
   (cycles, shape mismatches, unknown node ids, oversized inputs).

3. **Name the file path in the answer** (absolute path, on its own line or
   inline). That is the delivery mechanism; a path the answer does not
   mention is not sent. Mention the PNG (photo) rather than the SVG when both
   exist. Delete nothing — the artifacts dir is the owner's.

## Notes

- Fonts: the PNG backend looks for Noto Sans/Serif CJK, Nanum, unifont, then
  DejaVu (`FLEET_DIAGRAM_FONT=/path/to/font` overrides); Korean labels render
  on every node that has a CJK font. The SVG carries a font-family fallback
  list and renders on the viewer's side.
- Size caps: images are subject to the bridge's `CCC_TELEGRAM_MAX_IMAGE_BYTES`;
  the renderer's outputs are tens of KB.
- Tests: `scripts/render_diagram.test.sh` (hermetic; PNG cases skip without
  Pillow).
