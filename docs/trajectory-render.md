# Trajectory rendering grammar (#2388)

Every hook that shows a recorded session to a model renders it through one
module, `claude/hooks/lib/trajectory_render.py`. Before #2388, distill and
skill-review each had their own jq renderer, and nunchi's `session-tail.py` cut
messages at 800 characters without any mark. A tool output or message cut by a
renderer then looked like an unfinished run, and the reviewing model reported
failures that the agent did not have.

## Pipeline

```
input adapter  →  Event list  →  render(...)  →  window clip
claude | codex | piri
```

| Adapter | Input | Used by |
|---|---|---|
| `claude_events` | `~/.claude/projects/**.jsonl` and the normalized trees | distill, skill-review |
| `codex_events` | `$CODEX_HOME/sessions/**/rollout-*.jsonl` | `scripts/codex-rollout-normalize.py` |
| `piri_events` | Piri/pi session v3 jsonl | `scripts/piri-session-normalize.py` |

The two normalizers are thin wrappers. They use the shared adapters and
`claude_rows()` to project sessions into the Claude transcript shape that
`scan.sh` and `extract.sh` read. This projection is byte-identical to the
earlier output, with one exception. In a headless rollout, the `event_msg`
fallback rows now stay in session order. Before #2388 they were appended
after all command rows.

## Grammar

One block per event, in session order:

```
[USER] …
[ASSISTANT] …
[THINKING] …              only when the caller passes thinking=True / --thinking
[ACTION] tool(args…)      the provider's own tool name; args cut or omitted
[OBSERVATION] …           tool result
[OBSERVATION ERROR] …     tool result that the runtime marked as failed
[ERROR] …                 runtime/provider error event
```

## Truncation marker

Each cut that the renderer makes leaves one marker, where N is the number of
characters removed:

```
…(+N chars — render truncation)
```

- A per-item cut appends the marker to the kept text.
- A window cut (`--max-lines`, `clip --max-bytes`) puts the marker on its own
  line before the kept tail. A later clip merges its count into the existing
  marker and does not add a second one.
- An omitted body becomes the marker alone. An omitted observation is still
  tagged `[OBSERVATION ERROR]` when it failed.

Each prompt that shows rendered text includes the module's note:
`prompt-note` gives the grammar and the marker sentence, and `marker-note`
gives the marker sentence only. The note tells the model that the marker was
added by the renderer and is not agent behavior.

## Budgets

The caller sets every limit. The module has no default budgets:

- `obs_trunc` is `None` (omit observation bodies) or an int **≥ 10,000**. Any
  other value raises `ValueError`, and the CLI exits 2. A short partial
  observation can cut off an error message and create a defect that the run
  did not have, so this guard rejects the configuration (as harness2's
  `BenchSpec.obs_trunc` does).
- `args_trunc` is `None` (omit, marked) or a positive int.
- `text_trunc` is `None` (no per-message cut) or a positive int.

Current callers:

| Caller | obs | args | window |
|---|---|---|---|
| distill `extract.sh` | omit | omit (distill never showed commands; its redaction has no generic `key=value` rule) | `MAX_TURNS` lines, `MAX_BYTES` bytes |
| skill-review `extract.sh` | omit | 400 (the command is the procedure; redaction scrubs `key=value` secrets) | `MAX_TURNS` lines, `MAX_BYTES` bytes |
| nunchi `session-tail.py` | not shown | not shown | 800 chars/message, 60 messages, 40,000 chars — all marked |

A 60 KB window cannot hold one ≥10,000-character observation and the
conversation, so both extract hooks omit observation bodies. They do not
render a short slice.

`session-tail.py` keeps its own security-hardened reader and its
`USER:`/`AGENT:` labels. These are part of the nunchi extraction contract and
are outside the scope of #2388. Only its cuts use the shared marker. Its 4 MiB
read bound is a reader limit and is not a render cut.

## Tests

`claude/hooks/lib/trajectory_render.test.sh` renders the same trajectory, recorded
once by each of Claude, Codex and Piri (`tests/fixtures/trajectory_render/`).
It compares each rendering with its golden file and checks that all three use
the same tag sequence and the same markers. Regenerate the golden files after
an intended grammar change:

```bash
CCC_UPDATE_GOLDEN=1 bash claude/hooks/lib/trajectory_render.test.sh
```
