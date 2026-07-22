# Provider capability matrix

<!-- Generated from bridge/core/provider_capabilities.py — do not edit by hand.
     Regenerate with:
     python -m telegram_bot.core.provider_capabilities > docs/provider-capability-matrix.md -->

Single source of truth for what each agent provider supports through the
bridge (#387). The Python module `bridge/core/provider_capabilities.py` is
authoritative; this file is rendered from it and
`bridge/tests/test_provider_capabilities.py` fails CI when they drift apart.

## States

| State | Meaning |
|---|---|
| `supported` | Implemented, exercised by tests, and (where noted) verified live. |
| `degraded` | Partially implemented or missing live evidence; the reason names the gap. |
| `unsupported` | Not implemented for this provider yet. |
| `unknown` | Cannot be judged until the named dependency lands. |

Non-`supported` states carry a machine-readable reason and, where one
exists, the tracking issue dependency.

## Conformance gate

The behavioral runtime axes are executable: `bridge/tests/test_runtime_conformance.py`
runs the shared `AgentRuntime` conformance suite against every adapter
(currently `CodexRuntime` over a scripted fake app-server, plus the
normative in-memory reference runtime) with no live provider calls, and a
negative harness proves that contract-violating runtimes fail the suite.
New provider adapters (#354 successors) must pass this suite and add a
column here before landing.

## Runtime behavior

| Capability | claude | codex |
|---|---|---|
| `runtime_adapter` — Provider-neutral runtime adapter: Sessions and turns run behind the AgentRuntime seam and pass the runtime conformance suite. | `supported` — ClaudeRuntime adapts the Claude Agent SDK to AgentRuntime, passes the runtime conformance suite, and is the only Claude path since the #584 slice C-2 cutover removed the legacy direct SDK path and its kill-switch flag. | `supported` — CodexRuntime adapts the app-server protocol to AgentRuntime and passes the runtime conformance suite. |
| `session_resume` — Session resume: Re-attach to an existing provider session by its stable id. | `supported` — SDK sessions resume by persisted session_id (CCC_RESUME_PERSISTED_SESSIONS) while the transcript exists. | `supported` — thread/resume re-attaches by thread id and rejects a mismatched returned thread. |
| `text_streaming` — Streaming answer text: User-visible answer text arrives as incremental deltas within a turn. | `supported` — SDK stream deltas drive Telegram draft updates when CCC_TELEGRAM_STREAMING is enabled. | `supported` — item/agentMessage/delta notifications normalize to TextDeltaEvent. |
| `reasoning_stream` — Normalized reasoning stream: Provider reasoning is normalized as private ReasoningDeltaEvent and never delivered to the user. | `supported` — On the default adapter path, SDK ThinkingBlock content normalizes to ReasoningDeltaEvent (#599) and the consume loop keeps it private. | `supported` — item/reasoning textDelta and summaryTextDelta normalize to ReasoningDeltaEvent and stay private. |
| `message_boundaries` — Intra-turn message boundaries: Distinct assistant messages inside one turn are delimited so interim answers can deliver before tool work continues. | `supported` — Each SDK AssistantMessage frame is a message boundary in the reader path. | `supported` — item/completed for agentMessage items normalizes to MessageCompletedEvent. |
| `tool_event_stream` — Tool lifecycle events: Tool execution start and completion surface as normalized paired events. | `supported` — SDK tool_use/tool_result blocks drive tool status in the reader path. | `supported` — item/started and item/completed normalize to ToolStartedEvent/ToolCompletedEvent pairs. |
| `interactive_approvals` — Interactive approvals: Privileged actions pause for an explicit allow/deny decision; an omitted or failing handler is fail-closed deny. | `supported` — SDK permission callbacks gate tool use through Telegram inline approval. | `supported` — Approval server requests normalize to ApprovalRequestEvent; a missing or failing handler denies. |
| `turn_interrupt` — Turn interrupt: In-flight work can be cancelled; interrupting an idle session is a safe no-op. | `supported` — /stop cancels the active task and interrupts the SDK client. | `supported` — turn/interrupt targets the exact active turn id; interrupted turns end with error code 'interrupted'. |
| `turn_serialization` — Per-session turn serialization: Concurrent sends on one conversation execute strictly one turn at a time. | `supported` — The conversation FIFO serializes requests until a terminal result releases the lock. | `supported` — A per-thread turn lock serializes send_turn calls on the same thread. |
| `session_browsing` — Stored-session browsing: Stored sessions can be listed and read back in bounded, normalized form. | `supported` — /resume lists and previews SDK transcripts from the projects directory. | `supported` — SessionBrowser is implemented over thread/list and thread/read with bounded output. |
| `model_discovery` — Model discovery: Selectable models are enumerated from the provider at runtime. | `degraded` — The Claude /model list is a static curated set in the bridge, not enumerated from the provider. | `supported` — model/list responses normalize to ModelInfo including reasoning-effort metadata. |
| `usage_metering` — Usage metering: Account/session usage normalizes into provider-tagged UsageSnapshot values. | `supported` — Rate-limit events and result usage parse via parse_claude_rate_limit_event and parse_claude_result. | `supported` — Account rate limits, account usage, and thread token usage parse and merge into UsageSnapshot. |
| `terminal_stall_release` — Terminal-event stall release: A vanished completion event releases the conversation after a bounded grace with exactly-once buffered answer delivery. | `supported` — The Claude reader applies CCC_TERMINAL_STALL_SECONDS with exactly-once buffered delivery (#411). | `supported` — The provider-neutral consumer shares the same stall guard and closes abandoned iterators (#411). |

## Memory parity

| Capability | claude | codex |
|---|---|---|
| `memory_session_resume` — Memory: session resume: Conversation context resumes from the provider thread/session id. | `supported` — Persisted SDK session ids resume with full provider-side context. | `supported` — Thread ids persist per conversation and resume through thread/resume; live cold resume verified 2026-07-15. |
| `memory_read_bootstrap` — Memory: read bootstrap: The MEMORY/USER/local/Wiki/Honcho/resume startup snapshot is recognized at session start. | `supported` — SessionStart injects the bounded local snapshot via claude/hooks/load-memory.sh. | `supported` — The AGENTS.override.md materializer runs before thread start/resume; promoted after the 2026-07-15 live gate (#419). |
| `memory_postcompact_reinject` — Memory: post-compaction reinjection: Instruction/memory meaning survives compaction and cold resume. | `supported` — PostCompact re-injects the bounded snapshot through claude/hooks/load-memory.sh. | `degraded` — Cold resume re-reads the refreshed global snapshot, but a real provider compaction event has not been forced in a live gate. |
| `memory_writeback_distill` — Memory: write-back distill: Durable facts are extracted from provider threads for the memory sinks. | `supported` — PreCompact/SessionEnd distill transcripts into resume/local facts and sink candidates via claude/hooks/distill.sh. | `degraded` — Session-reset triggers, extraction, and the local sink are scheduled; explicit/checkpoint triggers and Honcho/Wiki routing remain pending. (depends on #465) |
| `memory_sink_local` — Memory: local sink: resume/local structured facts are written replay-safe with provenance. | `supported` — Distill writes resume.md and local structured facts (claude/hooks/distill/resume-write.sh, local-facts.sh). | `supported` — Supported session-reset triggers bind an opaque audience route, and an independently leased worker writes replay-safe local facts/resume. |
| `memory_sink_honcho` — Memory: Honcho sink: Redacted conclusions push to Honcho through a durable retry queue. | `supported` — Redacted payloads push via claude/hooks/distill/honcho-push.sh with the queue-drain retry path. | `unsupported` — No Codex-side Honcho routing exists yet. (depends on #465) |
| `memory_sink_wiki_candidate` — Memory: Wiki candidate sink: Only human-gated Wiki candidates are generated; nothing auto-merges. | `supported` — Distill emits human-gated Wiki candidates via claude/hooks/distill/wiki-queue.sh. | `unsupported` — No Codex-side Wiki candidate generation exists yet. (depends on #465) |
| `memory_roundtrip` — Memory: read/write round-trip: A durable fact written in session A is recalled by an isolated later session B. | `supported` — SessionEnd write-back feeds the next SessionStart snapshot; both hook directions carry executable tests (memory-hooks.test.sh, distill/*.test.sh). | `unknown` — Requires the #465 write-back path plus an approved live A→distill→B recall proof. (depends on #465) |
