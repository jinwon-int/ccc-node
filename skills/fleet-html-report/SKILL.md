---
name: fleet-html-report
description: Use when an owner-facing result is too wide or too long for a chat bubble — a multi-column fleet status matrix, a cost/usage dashboard, a per-node self-update table — and a throw-away single-file HTML page the owner can open on a phone or laptop would read better. Builds one self-contained HTML (inline CSS, no JavaScript, no external requests) from a small JSON spec, writes it under an artifacts/ directory, and names the path so the bridge attaches it to the Telegram/Matrix reply (#2109 C).
---

# fleet-html-report — disposable one-file HTML reports (#2109 C)

Karpathy (X, 2026-10-02): once intelligence and code are cheap, ask for
**large, custom, disposable artifacts** — an interactive page beats a wall of
text. This skill is the minimal, safe version: one HTML file, inline CSS,
no scripts, delivered as an attachment.

## When to use

- Tables wider than ~4 columns or longer than ~15 rows (fleet matrices,
  per-node inventories, cost/usage breakdowns).
- A report the owner will keep or forward (a PR census, an incident
  closeout) — Markdown in chat is still right for short answers.

Keep IDs/SHAs/commands also in the chat text when the owner must act on them;
never put secrets in a spec (values are rendered verbatim, escaped).

## How

1. Write the spec (UTF-8 JSON). Section kinds: `table` (`columns`, `rows`),
   `kv` (`items: [[key, value], …]`), `list` (`items`), `text` (`text`).
   Cells whose whole value is `ok|warn|fail|n/a|info|pending` render as a
   colour chip.

   ```json
   {"title": "플릿 self-update 매트릭스", "subtitle": "2026-10-02 15:00 KST · node-b",
    "sections": [
      {"kind": "table", "title": "노드별", "columns": ["node", "harness", "bridge", "worker"],
       "rows": [["node-a", "ok", "ok", "ok"], ["node-c", "ok", "warn", "fail"]]},
      {"kind": "kv", "title": "요약", "items": [["브로커 T1", "a930ce8b"], ["워커", "6/6 online"]]},
      {"kind": "list", "title": "다음 액션", "items": ["관측 창 종료 2026-10-03 18:00 KST"]}],
    "footer": "fleet-html-report · read-only snapshot"}
   ```

2. Build it **under an `artifacts/` directory** inside the bridge's
   `PROJECT_ROOT` (`$HOME` on a standard node). The bridge sends `html` only
   from such a path (`bridge/core/deliverables.py`, so edited web source like
   `public/index.html` is never auto-sent); the builder refuses other
   locations (exit 3).

   ```bash
   SKILL_DIR="${CLAUDE_SKILL_DIR:-$HOME/.claude/skills/fleet-html-report}"
   python3 "$SKILL_DIR/scripts/build_html_report.py" \
     --spec /tmp/report.json --out "$HOME/.claude/state/artifacts" --name fleet-matrix-20261002
   ```
   Output: `{"ok": true, "file": "/root/.claude/state/artifacts/fleet-matrix-20261002.html", "bytes": N}`.
   Bad specs exit 2 with `{"ok": false, "error"}`.

3. **Name the absolute file path in the answer.** That is the delivery
   mechanism (Telegram: document; Matrix: encrypted `m.file`). Phones open
   the file in the browser; it needs no network (CSP `default-src 'none'`).

## Notes

- Limits: 40 sections, 500 rows per section, 400 chars per cell (truncated
  with `…`). Dark mode via `prefers-color-scheme`; CJK font stack first.
- Hosting on a tunnel/domain is out of scope here (attachment only); if a
  link is ever wanted, that is a separate decision with its own gate.
- Tests: `scripts/build_html_report.test.sh` (hermetic).
