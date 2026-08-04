# PiriRuntime contract

`PiriRuntime` is the ccc-node adapter for Piri's headless JSONL RPC mode. Set
`CCC_AGENT_PROVIDER=piri` and, when needed, `CCC_PIRI_CLI_PATH` to select it in
the Telegram bridge.

## Execution policy

Piri is intentionally unrestricted for this personal harness:

- each ccc-node session owns one persistent `piri --mode rpc` process;
- the process starts with `--approve`, so project-local Piri resources load
  without a trust prompt;
- built-in and extension tools remain enabled; no tool allowlist or denylist is
  passed;
- commands run with the ccc-node process user's OS permissions and without an
  adapter sandbox;
- Piri built-in commands never emit ccc-node `ApprovalRequestEvent` values;
- yes/no dialogs from optional Piri extensions are confirmed automatically,
  while selection and text-entry dialogs are cancelled because there is no
  unambiguous unattended answer.

`SessionRequest.approval_policy` may be omitted or set to `never`.
`sandbox_policy` may be omitted or identify `dangerFullAccess` /
`danger-full-access`. Restrictive policies and approval reviewers are rejected
at startup rather than silently ignored.

This policy removes interactive friction, not operating-system boundaries. A
Piri tool can read, change, execute, or delete anything available to the Unix
user that runs ccc-node.

## Session contract

- A new request receives the non-empty `sessionId` returned by Piri.
- A resume request launches Piri with `--session-id <exact-id>` and verifies
  that `get_state` returns that same identifier.
- The working directory is the subprocess `cwd`, so Piri's project-scoped
  session lookup and resource loading share the ccc-node conversation root.
- Turns on one live session are serialized. Different session objects can run
  concurrently.
- `interrupt()` is a no-op while idle and sends RPC `abort` while a turn is
  active.
- Persisted Piri session ids resume automatically after a bridge restart.
- `/resume <piri-session-id>` selects an exact known id. Piri RPC 0.83 does
  not expose a bounded stored-session browser, so `/resume` cannot list or
  preview arbitrary Piri sessions.

## Telegram surface

- `/model` uses Piri's live `get_available_models` catalog.
- `/effort` maps to Piri `--thinking` levels advertised for the selected
  model.
- `/history` and `/revert` report that Piri transcript browsing is unavailable
  instead of reading Claude or Codex storage by mistake.
- `/usage` includes local request-attempt metering. Piri RPC 0.83 does not
  expose normalized tokens, account quota, or reset windows.
- Startup readiness requires both a working Piri CLI and at least one model
  returned by RPC discovery for the configured Piri auth store.
- Audience-scoped ccc memory is rejected for Piri until configuration,
  credentials, and session storage can be isolated per audience. `off` and
  `curated` memory modes remain available.

## Event contract

Piri RPC events map to ccc-node events as follows:

| Piri RPC event | ccc-node event |
| --- | --- |
| `message_update/text_delta` | `TextDeltaEvent` |
| `message_update/thinking_delta` | `ReasoningDeltaEvent` |
| assistant `message_end` after visible text | `MessageCompletedEvent` |
| `tool_execution_start` | `ToolStartedEvent` |
| `tool_execution_end` | `ToolCompletedEvent` |
| successful `agent_settled` | `ResultEvent`, then terminal `CompletionEvent` |
| aborted/failed settled run | terminal `ErrorEvent` |

`agent_settled`, rather than the lower-level `agent_end`, is authoritative. It
ensures automatic retries, compaction retries, and queued continuations have
all finished before ccc-node closes the turn.
