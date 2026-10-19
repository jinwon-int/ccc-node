#!/usr/bin/env python3
"""trajectory_render — one rendering grammar for every session trajectory (#2388).

Session captures arrive in three provider shapes (Claude project jsonl, Codex
rollout, Piri/pi session). Each consumer that shows a session to a model —
distill, skill-review, nunchi feeds — used to parse and render them on its own,
with its own tag words and its own (often silent) cuts. A tool output cut by a
renderer then reads like an incomplete run, and the reviewing model diagnoses a
failure the agent never had.

This module is the single place that turns a session into prompt text:

  input adapter (claude | codex | piri)  →  Event list  →  render(...)

Grammar (one block per event, in session order):

  [USER] …
  [ASSISTANT] …
  [THINKING] …                    only when the caller opts in
  [ACTION] tool(args…)            args cut at args_trunc, or omitted
  [OBSERVATION] …                 cut at obs_trunc, or body omitted
  [OBSERVATION ERROR] …           a tool result the runtime flagged as failed
  [ERROR] …                       a runtime/provider error event

Truncation convention: every cut the renderer makes leaves exactly one marker,
``…(+N chars — render truncation)``, where N is the number of characters
removed. Nothing is ever dropped without a marker. Prompts that show rendered
text include MARKER_NOTE so the model knows the marker is ours, not the agent's.

Budgets are never module defaults: every limit is a required keyword argument
(CLI: a required flag), the same discipline as harness2's ``BenchSpec.obs_trunc``.
``args_trunc`` is a positive int or ``None`` (arguments omitted, marked).
``obs_trunc`` accepts only ``None`` (observation bodies omitted entirely, the
marker reporting the full size) or an integer >= OBS_TRUNC_MIN. A short partial
observation is the range where a cut-off error message manufactures a defect,
so it is rejected at configuration time rather than rendered.

The projection helpers (``claude_rows``) let the provider normalizers
(scripts/codex-rollout-normalize.py, scripts/piri-session-normalize.py) reuse
the same adapters to emit the Claude transcript shape the skill-autosave
pipeline consumes; their output shape is unchanged.

CLI:
  trajectory_render.py render --format claude|codex|piri --obs-trunc omit|N
                              --args-trunc omit|N [--text-trunc N] [--thinking]
                              [--max-lines N] FILE
  trajectory_render.py clip --max-bytes N        (stdin → stdout, keeps the tail)
  trajectory_render.py prompt-note              (grammar + marker sentence)
  trajectory_render.py marker-note              (marker sentence only)
Exit codes: 0 = ok, 1 = unreadable input, 2 = usage / budget error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Iterable, Iterator, NamedTuple

TAG_USER = "[USER]"
TAG_ASSISTANT = "[ASSISTANT]"
TAG_THINKING = "[THINKING]"
TAG_ACTION = "[ACTION]"
TAG_OBSERVATION = "[OBSERVATION]"
TAG_OBSERVATION_ERROR = "[OBSERVATION ERROR]"
TAG_ERROR = "[ERROR]"
TAGS = (
    TAG_USER,
    TAG_ASSISTANT,
    TAG_THINKING,
    TAG_ACTION,
    TAG_OBSERVATION,
    TAG_OBSERVATION_ERROR,
    TAG_ERROR,
)

# harness2 core/spec.py: below this an observation cut starts fabricating defects.
OBS_TRUNC_MIN = 10_000

MARKER_RE = re.compile(r"…\(\+(\d+) chars — render truncation\)")
MARKER_NOTE = (
    "Markers of the form `…(+N chars — render truncation)` were inserted by the "
    "transcript renderer to fit this prompt's budget; they are not agent, tool, "
    "or runtime behavior. Never treat a marker as an incomplete run, a cut-off or "
    "failed command, or missing output."
)
PROMPT_NOTE = (
    "Transcript grammar: every block starts with one tag — [USER], [ASSISTANT], "
    "[ACTION] tool(args), [OBSERVATION], [OBSERVATION ERROR] (a tool result the "
    "runtime flagged as failed) or [ERROR] (a runtime error). " + MARKER_NOTE
)

# Codex function_call names whose arguments carry a runnable procedure. The
# skill-autosave projection (#1353) keeps exactly these as Bash tool_use rows.
CODEX_COMMAND_TOOLS = frozenset({"shell", "exec_command", "apply_patch"})


def marker(dropped: int) -> str:
    return f"…(+{dropped} chars — render truncation)"


def check_obs_trunc(obs_trunc: object) -> int | None:
    """Validate an observation budget: None (omit bodies) or int >= OBS_TRUNC_MIN."""
    if obs_trunc is None:
        return None
    if isinstance(obs_trunc, bool) or not isinstance(obs_trunc, int):
        raise ValueError(f"obs_trunc must be None or an int, got {obs_trunc!r}")
    if obs_trunc < OBS_TRUNC_MIN:
        raise ValueError(
            f"obs_trunc={obs_trunc} is below {OBS_TRUNC_MIN}: a short observation cut "
            "fabricates defects; omit observation bodies (None) or raise the budget"
        )
    return obs_trunc


def _check_positive(name: str, value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int, got {value!r}")
    return value


def truncate(text: str, limit: int) -> str:
    """Keep the first ``limit`` chars; mark what was removed."""
    if len(text) <= limit:
        return text
    return text[:limit] + marker(len(text) - limit)


# ---- events ----------------------------------------------------------------


class Event(NamedTuple):
    """One step of a trajectory.

    kind: "user" | "assistant" | "thinking" | "action" | "observation" | "error"
    parts: message text items (user/assistant keep item granularity so the
           Claude projection is byte-identical to the historical normalizers);
           a single item for every other kind.
    name/args: the tool call as the provider recorded it (action only).
    command: the runnable command a skill draft may reuse, when the provider
             adapter recognizes one (action only); drives the Bash projection.
    is_error: the provider flagged the observation as a failed tool result.
    """

    kind: str
    parts: tuple[str, ...] = ()
    name: str = ""
    args: object = None
    command: str | None = None
    is_error: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.parts)


def iter_records(lines: Iterable[str]) -> Iterator[dict]:
    """Yield parsed JSON-object lines; blank and malformed lines are skipped."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _content_text(content: object) -> str:
    """Flatten a tool-result content blob (str | list of text items) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "\n".join(texts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


# ---- claude adapter --------------------------------------------------------


def claude_events(records: Iterable[dict]) -> list[Event]:
    """Claude project jsonl (also the normalizers' projected shape) → events."""
    events: list[Event] = []
    for record in records:
        role = record.get("type")
        if role not in ("user", "assistant"):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            content = record.get("content")
        if isinstance(content, str):
            events.append(Event(str(role), (content,)))
            continue
        if not isinstance(content, list):
            continue
        texts: list[str] = []

        def flush() -> None:
            if texts:
                events.append(Event(str(role), tuple(texts)))
                texts.clear()

        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
            elif itype == "tool_use":
                flush()
                args = item.get("input")
                command = args.get("command") if isinstance(args, dict) else None
                events.append(
                    Event(
                        "action",
                        name=str(item.get("name") or "?"),
                        args=args,
                        command=command if isinstance(command, str) else None,
                    )
                )
            elif itype == "tool_result":
                flush()
                events.append(
                    Event(
                        "observation",
                        (_content_text(item.get("content")),),
                        is_error=item.get("is_error") is True,
                    )
                )
            elif itype in ("thinking", "redacted_thinking"):
                flush()
                thought = item.get("thinking")
                if isinstance(thought, str) and thought.strip():
                    events.append(Event("thinking", (thought,)))
        flush()
    return events


# ---- codex adapter ---------------------------------------------------------


def codex_command(name: str, arguments: object) -> str | None:
    """Extract the procedure payload from a codex function_call arguments blob."""
    if isinstance(arguments, str):
        try:
            args: object = json.loads(arguments)
        except ValueError:
            return None
    else:
        args = arguments
    if not isinstance(args, dict):
        return None
    if name in ("shell", "exec") and "command" in args:
        command = args["command"]
        if isinstance(command, list):
            parts = [str(part) for part in command]
            if len(parts) == 3 and parts[1] in ("lc", "-lc") and parts[0] in ("bash", "sh"):
                return parts[2]
            return " ".join(parts)
        if isinstance(command, str):
            return command
        return None
    if name == "exec_command":
        command = args.get("cmd", args.get("command"))
        if isinstance(command, list):
            return " ".join(str(part) for part in command)
        if isinstance(command, str):
            return command
        return None
    if name == "apply_patch":
        patch = args.get("input", args.get("patch"))
        return patch if isinstance(patch, str) else None
    return None


def _codex_output(output: object) -> tuple[str, bool]:
    """function_call_output.output → (text, is_error). Older rollouts wrap the
    output as a JSON string {"output":…, "metadata":{"exit_code":N}}."""
    if isinstance(output, str):
        try:
            wrapped = json.loads(output)
        except ValueError:
            return output, False
        if isinstance(wrapped, dict) and isinstance(wrapped.get("output"), str):
            meta = wrapped.get("metadata")
            code = meta.get("exit_code") if isinstance(meta, dict) else None
            return wrapped["output"], isinstance(code, int) and code != 0
        return output, False
    if isinstance(output, dict):
        text = _content_text(output.get("content", output.get("output")))
        return text, output.get("success") is False
    return _content_text(output), False


def codex_has_response_message(records: Iterable[dict]) -> bool:
    for record in records:
        payload = record.get("payload")
        if (
            record.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "message"
            and payload.get("role") in ("user", "assistant")
        ):
            return True
    return False


def _codex_response_event(payload: dict) -> Event | None:
    """One response_item payload → event (None = not part of the trajectory)."""
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return None  # developer/system: injected instruction noise
        content = payload.get("content")
        parts = tuple(
            item["text"]
            for item in (content if isinstance(content, list) else [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        return Event(str(role), parts)
    if ptype in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or "?")
        raw_args = payload.get("arguments", payload.get("input"))
        args: object = raw_args
        command = None
        if ptype == "function_call":
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except ValueError:
                    args = raw_args
            if name in CODEX_COMMAND_TOOLS:
                command = codex_command(name, raw_args)
        return Event("action", name=name, args=args, command=command)
    if ptype in ("function_call_output", "custom_tool_call_output"):
        text, is_error = _codex_output(payload.get("output"))
        return Event("observation", (text,), is_error=is_error)
    if ptype == "reasoning":
        summary = payload.get("summary")
        text = _content_text(summary) if isinstance(summary, list) else ""
        return Event("thinking", (text,)) if text.strip() else None
    return None


def _codex_event_msg(payload: dict, *, use_messages: bool) -> Event | None:
    ptype, message = payload.get("type"), payload.get("message")
    if not isinstance(message, str):
        return None
    if ptype in ("user_message", "agent_message") and use_messages:
        return Event("user" if ptype == "user_message" else "assistant", (message,))
    if ptype == "error":
        return Event("error", (message,))
    return None


def codex_events(records: list[dict]) -> list[Event]:
    """Codex rollout records → events.

    response_item messages (user/assistant) are the transcript; developer/system
    messages are injected instruction noise and dropped. event_msg user/agent
    messages duplicate them, so they are used only as a file-level fallback when
    a (headless) rollout carries no response_item message at all (#1353).
    """
    use_messages = not codex_has_response_message(records)
    events: list[Event] = []
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        event = None
        if record.get("type") == "response_item":
            event = _codex_response_event(payload)
        elif record.get("type") == "event_msg":
            event = _codex_event_msg(payload, use_messages=use_messages)
        if event is not None:
            events.append(event)
    return events


# ---- piri adapter ----------------------------------------------------------


def piri_bash_command(arguments: object) -> str | None:
    """Extract the runnable command from a piri bash toolCall arguments blob."""
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    if isinstance(command, str) and command.strip():
        return command
    if isinstance(command, list):
        parts = [str(part) for part in command]
        return " ".join(parts) if parts else None
    return None


def piri_events(records: Iterable[dict]) -> list[Event]:
    """Piri (pi, session version 3) records → events.

    Per assistant message the text items come first, then each toolCall — the
    order the skill-autosave projection has always emitted.
    """
    events: list[Event] = []
    for record in records:
        if record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        items = content if isinstance(content, list) else []
        if role in ("user", "assistant"):
            if role == "assistant":
                for item in items:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "thinking"
                        and isinstance(item.get("thinking"), str)
                        and item["thinking"].strip()
                    ):
                        events.append(Event("thinking", (item["thinking"],)))
            parts = tuple(
                item["text"]
                for item in items
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            )
            if parts:
                events.append(Event(str(role), parts))
            if role == "assistant":
                for item in items:
                    if isinstance(item, dict) and item.get("type") == "toolCall":
                        name = str(item.get("name") or "?")
                        args = item.get("arguments")
                        command = piri_bash_command(args) if name == "bash" else None
                        events.append(Event("action", name=name, args=args, command=command))
        elif role == "toolResult":
            events.append(
                Event(
                    "observation",
                    (_content_text(content),),
                    is_error=message.get("isError") is True,
                )
            )
    return events


ADAPTERS = {"claude": claude_events, "codex": codex_events, "piri": piri_events}


def load_events(fmt: str, lines: Iterable[str]) -> list[Event]:
    if fmt not in ADAPTERS:
        raise ValueError(f"unknown format {fmt!r}")
    return ADAPTERS[fmt](list(iter_records(lines)))


# ---- Claude-shape projection (skill-autosave normalizers) -------------------


def claude_rows(events: Iterable[Event]) -> list[dict]:
    """Project events into the Claude transcript rows scan.sh/extract.sh read:
    user/assistant text rows plus one Bash tool_use row per runnable command.
    Thinking, observations, errors and non-command tool calls are dropped."""
    rows: list[dict] = []
    for event in events:
        if event.kind in ("user", "assistant"):
            rows.append(
                {
                    "type": event.kind,
                    "message": {
                        "role": event.kind,
                        "content": [{"type": "text", "text": part} for part in event.parts],
                    },
                }
            )
        elif event.kind == "action" and event.command:
            rows.append(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": event.command}}
                        ],
                    },
                }
            )
    return rows


# ---- render ----------------------------------------------------------------


def _action_args(event: Event) -> str:
    if event.command:
        return event.command
    args = event.args
    if isinstance(args, str):
        return args
    if args is None:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


def _omit_or_cut(text: str, limit: int | None) -> str:
    """None = omit the body entirely (marked); otherwise cut at ``limit``."""
    if limit is None:
        return marker(len(text)) if text else ""
    return truncate(text, limit)


def render_event(
    event: Event,
    *,
    obs_trunc: int | None,
    args_trunc: int | None,
    text_trunc: int | None,
    thinking: bool,
) -> str | None:
    """Render one event to its grammar block (None = not rendered)."""

    def body(text: str) -> str:
        return truncate(text, text_trunc) if text_trunc is not None else text

    if event.kind == "user":
        return f"{TAG_USER} {body(event.text)}"
    if event.kind == "assistant":
        return f"{TAG_ASSISTANT} {body(event.text)}"
    if event.kind == "thinking":
        return f"{TAG_THINKING} {body(event.text)}" if thinking else None
    if event.kind == "action":
        return f"{TAG_ACTION} {event.name}({_omit_or_cut(_action_args(event), args_trunc)})"
    if event.kind == "observation":
        tag = TAG_OBSERVATION_ERROR if event.is_error else TAG_OBSERVATION
        shown = _omit_or_cut(event.text, obs_trunc)
        return f"{tag} {shown}" if shown else tag
    if event.kind == "error":
        return f"{TAG_ERROR} {body(event.text)}"
    return None


def render(
    events: Iterable[Event],
    *,
    obs_trunc: int | None,
    args_trunc: int | None,
    text_trunc: int | None,
    thinking: bool,
) -> list[str]:
    """Render events to grammar blocks. Every budget is caller-supplied."""
    obs = check_obs_trunc(obs_trunc)
    args_limit = _check_positive("args_trunc", args_trunc, optional=True)
    text_limit = _check_positive("text_trunc", text_trunc, optional=True)
    blocks: list[str] = []
    for event in events:
        block = render_event(
            event, obs_trunc=obs, args_trunc=args_limit, text_trunc=text_limit, thinking=thinking
        )
        if block is not None:
            blocks.append(block)
    return blocks


# ---- window clipping -------------------------------------------------------


def _split_leading_marker(text: str) -> tuple[int, str]:
    """Peel a leading marker line (from an earlier clip) so counts merge."""
    first, sep, rest = text.partition("\n")
    match = MARKER_RE.fullmatch(first)
    if match and sep:
        return int(match.group(1)), rest
    return 0, text


def _with_head_marker(dropped: int, kept: str) -> str:
    return f"{marker(dropped)}\n{kept}" if dropped else kept


def tail_lines(text: str, max_lines: int) -> str:
    """Keep the last ``max_lines`` lines; a head marker counts the dropped chars."""
    _check_positive("max_lines", max_lines, optional=False)
    prior, body = _split_leading_marker(text)
    lines = body.split("\n")
    if len(lines) <= max_lines:
        return _with_head_marker(prior, body)
    kept = "\n".join(lines[-max_lines:])
    dropped = len(body) - len(kept)
    return _with_head_marker(prior + dropped, kept)


def clip_tail_bytes(text: str, max_bytes: int) -> str:
    """Keep the most recent ``max_bytes`` UTF-8 bytes (whole characters only).

    The budget bounds the kept body; the head marker line is extra, exactly as
    the historical `…[truncated N bytes]…` header was.
    """
    _check_positive("max_bytes", max_bytes, optional=False)
    prior, body = _split_leading_marker(text)
    encoded = body.encode("utf-8")
    if len(encoded) <= max_bytes:
        return _with_head_marker(prior, body)
    kept = encoded[len(encoded) - max_bytes:].decode("utf-8", errors="ignore")
    dropped = len(body) - len(kept)
    return _with_head_marker(prior + dropped, kept)


# ---- CLI -------------------------------------------------------------------


def _obs_arg(value: str) -> int | None:
    if value == "omit":
        return None
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--obs-trunc must be 'omit' or an int, got {value!r}") from exc
    try:
        return check_obs_trunc(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _args_arg(value: str) -> int | None:
    return None if value == "omit" else _positive(value)


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive int, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a positive int, got {value!r}")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_render = sub.add_parser("render", help="render one session file to the shared grammar")
    p_render.add_argument("--format", required=True, choices=sorted(ADAPTERS))
    p_render.add_argument("--obs-trunc", required=True, type=_obs_arg,
                          help=f"'omit' or an int >= {OBS_TRUNC_MIN}")
    p_render.add_argument("--args-trunc", required=True, type=_args_arg,
                          help="'omit' or a positive int")
    p_render.add_argument("--text-trunc", type=_positive, default=None,
                          help="per-message text cap (unset: no per-message cut)")
    p_render.add_argument("--thinking", action="store_true", help="render [THINKING] blocks")
    p_render.add_argument("--max-lines", type=_positive, default=None,
                          help="keep only the last N rendered lines (marked)")
    p_render.add_argument("source")

    p_clip = sub.add_parser("clip", help="keep the tail of stdin within a byte budget (marked)")
    p_clip.add_argument("--max-bytes", required=True, type=_positive)

    sub.add_parser("prompt-note", help="print the prompt sentences explaining grammar + marker")
    sub.add_parser("marker-note", help="print only the render-truncation marker sentence")

    args = parser.parse_args(argv)

    if args.cmd == "prompt-note":
        print(PROMPT_NOTE)
        return 0
    if args.cmd == "marker-note":
        print(MARKER_NOTE)
        return 0
    if args.cmd == "clip":
        data = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        sys.stdout.write(clip_tail_bytes(data, args.max_bytes))
        return 0

    try:
        with open(args.source, encoding="utf-8", errors="replace") as fh:
            events = load_events(args.format, fh)
    except OSError as exc:
        print(f"unreadable source: {exc}", file=sys.stderr)
        return 1
    text = "\n".join(
        render(
            events,
            obs_trunc=args.obs_trunc,
            args_trunc=args.args_trunc,
            text_trunc=args.text_trunc,
            thinking=args.thinking,
        )
    )
    if args.max_lines is not None:
        text = tail_lines(text, args.max_lines)
    if text:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
