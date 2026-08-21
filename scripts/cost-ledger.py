#!/usr/bin/env python3
"""Daily per-model token/cost ledger (#1205 stage 1+2).

Aggregates one KST day (default: yesterday) of transcript usage per model:

  - claude : ~/.claude/projects/**/*.jsonl (main sessions + subagents),
             deduped by (message.id, requestId) — the CLI writes one JSONL
             line per content block with the same usage object
  - codex  : ~/.codex/sessions/**/rollout-*.jsonl — `token_count` events,
             using `last_token_usage` (true per-turn delta; `total_token_usage`
             is the session-cumulative snapshot). Model from turn_context.
  - piri   : $PIRI_CODING_AGENT_SESSION_DIR, ~/.piri/agent/sessions/**,
             ~/.telegram_bot/memory-audiences/*/piri/sessions — per-message
             usage deltas with inline model, deduped by (file, message id)

  - turns  : unique assistant API responses
  - input_tokens / output_tokens
  - cache_read_input_tokens / cache_creation_input_tokens
  - thinking_tokens (subset of output, informational)
  - est_cost_usd   : computed only for models with a filled pricing entry
                     (see PRICING below), else null — codex/piri models price
                     null until their official pages are read, never guessed

Output:
  - appends/replaces one JSON line for the date in the ledger file
    (default ~/.claude/state/cost-ledger.jsonl; idempotent per (date, node):
    an existing line for the same date+node is replaced, not duplicated)
  - prints a small markdown daily summary to stdout

Design notes (survey 2026-08-20, stage-2 investigation 2026-08-21):
  - Ground truth = transcript usage fields. The bridge's
    ~/.telegram_bot/usage-cost-ledger.jsonl rows are session-CUMULATIVE
    ResultMessage snapshots (SDK model_usage semantics, zeroed on
    ConversationResetMessage) — naive summing over-counts, so we do not use
    it here (fix tracked separately in stage 2 / D-3).
  - ~/.telegram_bot/usage-meter.json day keys are already KST; useful as a
    cross-check but has no per-model / cache split.
  - Transcript timestamps are UTC ISO-8601 with 'Z'; we window on KST.

Usage:
  cost-ledger.py [--date YYYY-MM-DD(KST)] [--out LEDGER.jsonl]
                 [--projects DIR] [--codex-sessions DIR] [--piri-sessions DIR]
                 [--providers claude,codex,piri] [--node NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
DEFAULT_LEDGER = Path.home() / ".claude" / "state" / "cost-ledger.jsonl"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Pricing table — USD per 1M tokens (MTok).
#
# Transcribed 2026-08-21 from the official page,
# https://platform.claude.com/docs/en/about-claude/pricing (§ Model pricing).
# Do not edit these from memory: the page carries no "last updated" stamp, so
# the only way to know a rate is current is to re-read it. A model absent here
# prices as null rather than guessing — a wrong number is worse than no number,
# because a null is visibly missing while a wrong number is quietly believed.
#
# Cache writes are split by TTL because they are priced differently (5m = 1.25x
# base input, 1h = 2x) and the fleet uses both: measured on nosuk over 3,759
# responses, 1h was 56.7% of cache-write tokens. Pricing every write at the 5m
# rate — which the flat cache_creation_input_tokens field invites — understated
# cache-write cost by about a quarter.
#
# Keys match by exact model id first, then by longest prefix.
# ---------------------------------------------------------------------------
PRICING: dict[str, dict[str, float | None]] = {
    # Claude Sonnet 5 is $2/$10 permanently. It launched at that rate as
    # "introductory pricing through 2026-08-31", and the pricing page now
    # states the scheduled 2026-09-01 rise to $3/$15 "will not occur". Cached
    # summaries written before that reversal still show the step; encoding one
    # here would have overcharged this fleet by 50% from September.
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20,
                        "cache_write_5m": 2.50, "cache_write_1h": 4.0},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0,
                       "cache_write_5m": 12.50, "cache_write_1h": 20.0},
    "claude-mythos-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0,
                        "cache_write_5m": 12.50, "cache_write_1h": 20.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50,
                      "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50,
                        "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.50,
                        "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.50,
                        "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50,
                        "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30,
                          "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.30,
                          "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10,
                         "cache_write_5m": 1.25, "cache_write_1h": 2.0},
    # Deliberately absent, each priced null rather than assumed:
    #   claude-mythos-preview — no published price anywhere (Glasswing).
    #   non-Anthropic ids (e.g. the piri nodes' kimi-coding/*) — not ours to
    #     price from this page at all.
    # --- stage 2 providers (read 2026-08-21, each from its official page) ---
    # OpenAI Standard tier, short context. The page also publishes a ~2x
    # long-context class ($10/$1/$12.50/$45), but the boundary at which a
    # request becomes "long context" was not stated in the fetched text, and
    # codex's token_count record does not say which class it was billed at —
    # so rows are priced at the short-context rate, a documented possible
    # UNDERSTATEMENT for long-context turns (same least-wrong pattern as the
    # untyped-cache-write 5m assumption below). One cache-write rate exists
    # ($6.25) — codex has no TTL split, so both buckets carry it.
    #   https://developers.openai.com/api/docs/pricing (Standard, 2026-08-21)
    "gpt-5.6-sol": {"input": 5.0, "output": 30.0, "cache_read": 0.50,
                    "cache_write_5m": 6.25, "cache_write_1h": 6.25},
    # Kimi K3: input $3.00, cache-hit read $0.30, output $15.00, 1M context.
    # The official table lists no cache-write rate at all — Kimi's automatic
    # context caching bills cache-hit reads only, so writes are encoded 0.0
    # (unbilled), not guessed.
    #   https://platform.kimi.ai/docs/pricing/chat-k3 (2026-08-21)
    "k3": {"input": 3.0, "output": 15.0, "cache_read": 0.30,
           "cache_write_5m": 0.0, "cache_write_1h": 0.0},
    # GLM-5.3: input $1.4, cached input $0.26, output $4.4. "Cached Input
    # Storage" is marked Limited-time Free — encoded 0.0 with the promo note;
    # re-read the page when the promotion ends.
    #   https://docs.z.ai/guides/overview/pricing (2026-08-21)
    "glm-5.3": {"input": 1.4, "output": 4.4, "cache_read": 0.26,
                "cache_write_5m": 0.0, "cache_write_1h": 0.0},
}

# Usage-level modifiers the pricing page defines but this stage does not price.
# Each one silently changes the true cost, so a bucket carrying any of them is
# reported with est_cost_usd = null and the reason named, instead of being
# priced at standard rates and quietly understated. Measured on nosuk: every
# one of 3,759 responses was global / standard / standard, so in practice this
# costs no coverage today — it just refuses to be wrong if that changes.
#   inference_geo "us"      -> 1.1x on every category
#   speed "fast"            -> flat $10/$50 (Opus 5 / 4.8), not a multiplier
#   service_tier != standard-> batch is 50% off; priority differs
_STANDARD_GEO = {"global", "not_available", "", None}
_STANDARD_SPEED = {"standard", "", None}
_STANDARD_TIER = {"standard", "", None}


def _pricing_for(model: str) -> dict[str, float | None] | None:
    # Collector namespaces (stage 2): "codex:gpt-5.6-sol" prices as
    # "gpt-5.6-sol" — the prefix is provenance, not a different model.
    bare = model.split(":", 1)[1] if model.split(":", 1)[0] in {"codex", "piri"} else model
    if bare in PRICING:
        return PRICING[bare]
    best = None
    for key, val in PRICING.items():
        if bare.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, val)
    return best[1] if best else None


def _est_cost(model: str, tok: dict) -> tuple[float | None, str | None]:
    """Return (usd, reason_when_null). Never guesses a missing rate."""
    if tok.get("modifiers"):
        return None, "unpriced-modifier:" + ",".join(sorted(tok["modifiers"]))
    p = _pricing_for(model)
    if not p:
        return None, "no-published-price"
    # An untyped cache write predates the cache_creation TTL breakdown. The
    # API's default TTL is 5m, so that is the least-wrong assumption, but it IS
    # an assumption — the count is carried in the record so a reader can see
    # how much of the figure rests on it.
    parts = [
        (tok["input_tokens"], p.get("input")),
        (tok["output_tokens"], p.get("output")),
        (tok["cache_read_input_tokens"], p.get("cache_read")),
        (tok["cache_write_5m_tokens"] + tok["cache_write_untyped_tokens"],
         p.get("cache_write_5m")),
        (tok["cache_write_1h_tokens"], p.get("cache_write_1h")),
    ]
    if any(rate is None for _n, rate in parts):
        return None, "incomplete-price-entry"
    return round(sum(n * rate / 1_000_000 for n, rate in parts), 6), None


def _resolve_node() -> str:
    """Fleet identity, in the order the other installers use.

    node.txt exists on only 5 of 12 nodes, so it cannot be the sole source;
    hostname is the last resort because it disagrees with the fleet name on at
    least one node (yukson reports vps5).
    """
    env = os.environ.get("CCC_NODE", "").strip()
    if env:
        return env
    state = Path(os.environ.get("CCC_STATE_DIR") or (Path.home() / ".claude" / "state"))
    try:
        first = (state / "node.txt").read_text(encoding="utf-8").splitlines()[0].strip()
        if first:
            return first
    except (OSError, IndexError):
        pass
    return os.uname().nodename.split(".")[0] or "ccc-node"


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _new_slot() -> dict:
    return {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cache_write_untyped_tokens": 0,
        "thinking_tokens": 0,
        "modifiers": set(),
    }


def _iter_jsonl_files(roots, start_epoch):
    """Yield jsonl paths under each existing root, mtime-gated.

    A missing root is a fail-open skip (stage-1 convention: a node without a
    given provider's transcripts must not fail the whole ledger run).
    """
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in sorted(root.glob("**/*.jsonl")):
            try:
                if path.stat().st_mtime < start_epoch:
                    continue
            except OSError:
                continue
            yield path


# ---------------------------------------------------------------------------
# Codex collector (#1205 stage 2 / D-1) — rollout token_count events.
#
# ~/.codex/sessions/**/rollout-*.jsonl carries per-turn usage directly:
#   {"type":"event_msg","payload":{"type":"token_count","info":{
#      "total_token_usage":{...cumulative...},
#      "last_token_usage":{...this turn...}}}}
# `last_token_usage` is a true per-turn delta, so no snapshot subtraction is
# needed (contrast with the bridge's cumulative ResultMessage rows — D-3).
# The model rides in `turn_context` records ahead of their turn's events.
# cache_write_input_tokens has no 5m/1h TTL split, so it lands in the untyped
# bucket and prices null under the absolute-rate rule.
# ---------------------------------------------------------------------------

def _accumulate_codex(slot: dict, usage: dict) -> None:
    slot["turns"] += 1
    slot["input_tokens"] += int(usage.get("input_tokens") or 0)
    slot["output_tokens"] += int(usage.get("output_tokens") or 0)
    slot["cache_read_input_tokens"] += int(usage.get("cached_input_tokens") or 0)
    created = int(usage.get("cache_write_input_tokens") or 0)
    slot["cache_creation_input_tokens"] += created
    slot["cache_write_untyped_tokens"] += created
    slot["thinking_tokens"] += int(usage.get("reasoning_output_tokens") or 0)


def aggregate_codex(sessions_dir: Path, start_utc: datetime, end_utc: datetime):
    per_model: dict[str, dict] = {}
    files_scanned = 0
    lines_bad = 0
    sessions: set[str] = set()
    start_epoch = start_utc.timestamp()
    for path in _iter_jsonl_files([sessions_dir], start_epoch):
        files_scanned += 1
        sessions.add(path.stem)
        current_model = "unknown"
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"token_count"' not in line and '"turn_context"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    lines_bad += 1
                    continue
                payload = obj.get("payload") or {}
                ptype = payload.get("type")
                if obj.get("type") == "turn_context" or ptype == "turn_context":
                    model = payload.get("model")
                    if model:
                        current_model = str(model)
                    continue
                if ptype != "token_count":
                    continue
                ts = _parse_ts(obj.get("timestamp") or "")
                if ts is None or not (start_utc <= ts < end_utc):
                    continue
                info = payload.get("info") or {}
                usage = info.get("last_token_usage") or {}
                if not usage:
                    continue
                slot = per_model.setdefault(f"codex:{current_model}", _new_slot())
                _accumulate_codex(slot, usage)
    return per_model, {"files_scanned": files_scanned, "sessions": len(sessions), "lines_unparsable": lines_bad}


# ---------------------------------------------------------------------------
# Piri collector (#1205 stage 2 / D-2) — per-message usage deltas.
#
# Piri session jsonl (scoped nodes: <audience>/piri/sessions/; unscoped:
# ~/.piri/agent/sessions/**) records usage per assistant message — already a
# delta, with the model beside it:
#   {"type":"message","id":"...","timestamp":"...",
#    "message":{"role":"assistant","model":"k3",
#               "usage":{"input":n,"output":n,"cacheRead":n,"cacheWrite":n}}}
# Dedup on (session file, message id): session files are append-only but the
# same message id must never be counted twice if a line is replayed.
# cacheWrite carries no TTL split -> untyped bucket -> prices null.
# ---------------------------------------------------------------------------

def _accumulate_piri(slot: dict, usage: dict) -> None:
    slot["turns"] += 1
    slot["input_tokens"] += int(usage.get("input") or 0)
    slot["output_tokens"] += int(usage.get("output") or 0)
    slot["cache_read_input_tokens"] += int(usage.get("cacheRead") or 0)
    created = int(usage.get("cacheWrite") or 0)
    slot["cache_creation_input_tokens"] += created
    slot["cache_write_untyped_tokens"] += created


def _default_piri_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.environ.get("PIRI_CODING_AGENT_SESSION_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    dirs.append(Path.home() / ".piri" / "agent" / "sessions")
    dirs.extend(sorted(Path.home().glob(".telegram_bot/memory-audiences/*/piri/sessions")))
    return dirs


def aggregate_piri(session_dirs, start_utc: datetime, end_utc: datetime):
    per_model: dict[str, dict] = {}
    files_scanned = 0
    lines_bad = 0
    sessions: set[str] = set()
    seen: set[tuple] = set()
    start_epoch = start_utc.timestamp()
    for path in _iter_jsonl_files(session_dirs, start_epoch):
        files_scanned += 1
        sessions.add(path.stem)
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    lines_bad += 1
                    continue
                if obj.get("type") != "message":
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                ts = _parse_ts(obj.get("timestamp") or "")
                if ts is None or not (start_utc <= ts < end_utc):
                    continue
                key = (path.stem, obj.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                model = msg.get("model") or "unknown"
                slot = per_model.setdefault(f"piri:{model}", _new_slot())
                _accumulate_piri(slot, usage)
    return per_model, {"files_scanned": files_scanned, "sessions": len(sessions), "lines_unparsable": lines_bad}


def _accumulate(slot: dict, usage: dict) -> None:
    """Fold one API response's usage into a per-model slot."""
    slot["turns"] += 1
    slot["input_tokens"] += int(usage.get("input_tokens") or 0)
    slot["output_tokens"] += int(usage.get("output_tokens") or 0)
    slot["cache_read_input_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    slot["cache_creation_input_tokens"] += created
    # cache_creation carries the TTL split; the flat field does not, and the
    # two are priced differently, so an older record without the breakdown is
    # counted separately rather than folded into either bucket.
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        slot["cache_write_5m_tokens"] += int(cc.get("ephemeral_5m_input_tokens") or 0)
        slot["cache_write_1h_tokens"] += int(cc.get("ephemeral_1h_input_tokens") or 0)
    else:
        slot["cache_write_untyped_tokens"] += created
    for field, standard in (
        ("inference_geo", _STANDARD_GEO),
        ("speed", _STANDARD_SPEED),
        ("service_tier", _STANDARD_TIER),
    ):
        value = usage.get(field)
        if str(value or "") not in standard:
            slot["modifiers"].add(f"{field}={value}")
    details = usage.get("output_tokens_details") or {}
    slot["thinking_tokens"] += int(details.get("thinking_tokens") or 0)


def aggregate(projects_dir: Path, start_utc: datetime, end_utc: datetime):
    """Scan all transcript jsonl files; return (per_model, meta)."""
    per_model: dict[str, dict] = {}
    seen: set[tuple] = set()
    sessions: set[str] = set()
    files_scanned = 0
    lines_bad = 0

    # mtime filter: a file last written before the window start cannot
    # contain records inside the window.
    start_epoch = start_utc.timestamp()
    for path in sorted(projects_dir.glob("**/*.jsonl")):
        try:
            if path.stat().st_mtime < start_epoch:
                continue
        except OSError:
            continue
        files_scanned += 1
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"assistant"' not in line or '"usage"' not in line:
                    continue  # cheap pre-filter before json parse
                try:
                    obj = json.loads(line)
                except Exception:
                    lines_bad += 1
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                ts = _parse_ts(obj.get("timestamp") or "")
                if ts is None or not (start_utc <= ts < end_utc):
                    continue
                key = (msg.get("id"), obj.get("requestId"))
                if key in seen:
                    continue  # same API response split across content blocks
                seen.add(key)
                sid = obj.get("sessionId")
                if sid:
                    sessions.add(sid)
                model = msg.get("model") or "unknown"
                slot = per_model.setdefault(model, _new_slot())
                slot["provider"] = "claude"
                _accumulate(slot, usage)

    meta = {
        "files_scanned": files_scanned,
        "sessions": len(sessions),
        "lines_unparsable": lines_bad,
    }
    return per_model, meta


def build_record(date_kst: str, node: str, per_model: dict, meta: dict) -> dict:
    models_out = {}
    totals = {
        "turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cache_write_untyped_tokens": 0,
    }
    total_cost = 0.0
    cost_complete = True
    for model in sorted(per_model):
        tok = per_model[model]
        cost, reason = _est_cost(model, tok)
        if cost is None:
            cost_complete = False
        else:
            total_cost += cost
        row = {k: v for k, v in tok.items() if k != "modifiers"}
        row["est_cost_usd"] = cost
        if reason:
            row["est_cost_null_reason"] = reason
        if tok["modifiers"]:
            row["usage_modifiers"] = sorted(tok["modifiers"])
        models_out[model] = row
        for k in totals:
            totals[k] += tok[k]
    record = {
        "schema": SCHEMA_VERSION,
        "date": date_kst,          # KST calendar day
        "tz": "Asia/Seoul",
        "node": node,
        "source": "claude-projects-transcripts",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": models_out,
        "totals": {
            **totals,
            # null until PRICING is filled for every model seen that day
            "est_cost_usd": round(total_cost, 6) if cost_complete and models_out else None,
        },
        "meta": meta,
    }
    return record


def upsert_ledger(ledger_path: Path, record: dict) -> str:
    """Idempotent per (date, node): replace an existing line, else append."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    replaced = False
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
            except Exception:
                lines.append(line)
                continue
            if old.get("date") == record["date"] and old.get("node") == record["node"]:
                if not replaced:
                    lines.append(json.dumps(record, ensure_ascii=False))
                    replaced = True
                # drop duplicates for same date+node
            else:
                lines.append(line)
    if not replaced:
        lines.append(json.dumps(record, ensure_ascii=False))
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(ledger_path.parent), prefix=".cost-ledger.", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp_name, ledger_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return "replaced" if replaced else "appended"


def markdown_summary(record: dict) -> str:
    t = record["totals"]
    out = [
        f"## Cost ledger — {record['node']} — {record['date']} (KST)",
        "",
        "| model | turns | input | output | cache_read | cw_5m | cw_1h | est_cost_usd |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model, tok in record["models"].items():
        cost = tok["est_cost_usd"]
        out.append(
            f"| {model} | {tok['turns']} | {tok['input_tokens']:,} "
            f"| {tok['output_tokens']:,} | {tok['cache_read_input_tokens']:,} "
            f"| {tok['cache_write_5m_tokens'] + tok['cache_write_untyped_tokens']:,} "
            f"| {tok['cache_write_1h_tokens']:,} "
            f"| {cost if cost is not None else tok.get('est_cost_null_reason', 'null')} |"
        )
    out.append(
        f"| **total** | {t['turns']} | {t['input_tokens']:,} | {t['output_tokens']:,} "
        f"| {t['cache_read_input_tokens']:,} "
        f"| {t['cache_write_5m_tokens'] + t['cache_write_untyped_tokens']:,} "
        f"| {t['cache_write_1h_tokens']:,} "
        f"| {t['est_cost_usd'] if t['est_cost_usd'] is not None else 'null'} |"
    )
    m = record["meta"]
    src = m.get("sources") or {}
    sessions = sum(s["sessions"] for s in src.values())
    scanned = sum(s["files_scanned"] for s in src.values())
    bad = sum(s["lines_unparsable"] for s in src.values())
    src_desc = ", ".join(
        "{}({}s/{}f)".format(k, v["sessions"], v["files_scanned"]) for k, v in sorted(src.items())
    ) or "none"
    out += [
        "",
        f"- sources: {src_desc}",
        f"- sessions: {sessions}, files scanned: {scanned}, "
        f"unparsable lines: {bad}",
        "- prices: platform.claude.com/docs/en/about-claude/pricing (2026-08-21).",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="KST date YYYY-MM-DD (default: yesterday KST)")
    ap.add_argument("--out", type=Path, default=DEFAULT_LEDGER,
                    help=f"ledger jsonl path (default {DEFAULT_LEDGER})")
    ap.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS)
    ap.add_argument("--codex-sessions", type=Path, default=DEFAULT_CODEX_SESSIONS,
                    help=f"codex rollouts root (default {DEFAULT_CODEX_SESSIONS})")
    ap.add_argument("--piri-sessions", type=Path, action="append", default=None,
                    help="piri session dir (repeatable; default: "
                         "$PIRI_CODING_AGENT_SESSION_DIR, ~/.piri/agent/sessions, "
                         "~/.telegram_bot/memory-audiences/*/piri/sessions)")
    ap.add_argument("--providers", default="claude,codex,piri",
                    help="comma list of collectors to run (default all three; each "
                         "fail-open skips when its transcript root is absent)")
    ap.add_argument("--node", default=None,
                    help="fleet identity (default: $CCC_NODE, then "
                         "$CCC_STATE_DIR/node.txt, then hostname -s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print summary + record, do not write the ledger")
    args = ap.parse_args()
    node = args.node or _resolve_node()
    providers = {p.strip() for p in args.providers.split(",") if p.strip()}

    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=KST)
    else:
        day = (datetime.now(KST) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    start_kst = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end_kst = start_kst + timedelta(days=1)
    start_utc = start_kst.astimezone(timezone.utc)
    end_utc = end_kst.astimezone(timezone.utc)
    date_kst = start_kst.strftime("%Y-%m-%d")

    if not args.projects.is_dir() and "claude" in providers and len(providers) == 1:
        # Two of twelve fleet nodes legitimately have no Claude Code
        # transcripts (daegyo is Codex-primary; gongmyoung has no harness
        # tree), so this is a normal state, not a fault. Erroring here would
        # hand those nodes a failing cron every single day — the exact
        # unattended-noise class the installer marker exists to avoid — and
        # writing a zero row would assert "no usage" where the truth is "not
        # measured here". Skip, say so, exit 0.
        print(json.dumps({"ok": True, "skipped": "no-transcripts",
                          "projects": str(args.projects), "node": node}))
        return 0

    per_model: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    if "claude" in providers and args.projects.is_dir():
        per_model, meta = aggregate(args.projects, start_utc, end_utc)
        sources["claude-projects-transcripts"] = meta
    if "codex" in providers:
        codex_models, codex_meta = aggregate_codex(args.codex_sessions, start_utc, end_utc)
        for model, slot in codex_models.items():
            slot["provider"] = "codex"
        per_model.update(codex_models)
        if codex_meta["files_scanned"]:
            sources["codex-rollouts"] = codex_meta
    if "piri" in providers:
        piri_dirs = args.piri_sessions if args.piri_sessions else _default_piri_dirs()
        piri_models, piri_meta = aggregate_piri(piri_dirs, start_utc, end_utc)
        for model, slot in piri_models.items():
            slot["provider"] = "piri"
        per_model.update(piri_models)
        if piri_meta["files_scanned"]:
            sources["piri-sessions"] = piri_meta

    if not sources:
        # No collector found any transcript root — same "not measured here"
        # contract as the stage-1 no-transcripts skip (a zero row would assert
        # "no usage" where the truth is "no input").
        print(json.dumps({"ok": True, "skipped": "no-transcripts",
                          "providers": sorted(providers), "node": node}))
        return 0

    meta = {"sources": sources}
    record = build_record(date_kst, node, per_model, meta)
    active = [k for k, v in sources.items() if v["files_scanned"] > 0]
    if len(active) > 1:
        record["source"] = "multi-provider-transcripts"

    if args.dry_run:
        print(markdown_summary(record))
        print()
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    action = upsert_ledger(args.out, record)
    print(markdown_summary(record))
    print(f"\n({action} in {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
