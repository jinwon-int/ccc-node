# `telegram_bot.contracts` — the provider-neutral runtime seam

This package holds the contract that bridge orchestration and provider adapters
both code against. It was carved out of `bridge/core/` in #1756 as a pure
relocation: no behavior, no signature, and no field in the contract changed.

## Why it is its own package

`core/` holds three different kinds of thing at once — the Telegram-facing
presentation mixins, the turn orchestration, and the provider adapters. While
the contract lived there too, "the seam" and "one of the things implementing
the seam" were the same import namespace, so nothing structural stopped an
adapter from reaching sideways into presentation, or the contract from growing
a dependency on bridge configuration. Moving the contract up into its own
package makes the direction of the dependency visible in the import path:
adapters and orchestration import `contracts`; `contracts` imports neither.

## The contract

`contracts/agent_runtime.py` defines the whole seam. Nothing in it is
provider-specific and nothing in it exposes a provider SDK object to a bridge
caller.

**Protocols** — what a provider must implement:

| Protocol | Role |
| --- | --- |
| `AgentRuntime` | Factory + model discovery: `start_or_resume`, `list_models` |
| `AgentSession` | One live session: `session_id`, `send_turn`, `interrupt` |
| `SessionBrowser` | *Optional* capability: list/read stored sessions |
| `AsyncCompletionRuntime` | *Optional* capability: out-of-turn completion delivery |

**Inputs** — `SessionRequest`, and the `ModelInfo` returned by discovery.

**Events** — the `AgentEvent` union a turn streams: `TextDeltaEvent`,
`MessageCompletedEvent`, `ReasoningDeltaEvent`, `ToolStartedEvent`,
`ToolCompletedEvent`, `ApprovalRequestEvent`, `ApprovalResolvedEvent`,
`DelegatedTaskLifecycleEvent`, `TaskProgressEvent`, `CompletionEvent`,
`ResultEvent`, `ErrorEvent`.

**Approvals** — `ApprovalDecision`, the `ApprovalHandler` alias, and the
`deny_approval` default.

Three invariants hold across every adapter and are the reason this is a
contract rather than a style guide:

1. **Fail closed.** Omitting `approval_handler` on `send_turn` denies every
   approval request. An adapter must preserve that default rather than
   delegating the omission to an SDK default.
2. **Immutable payloads.** `freeze_json` gives every event a recursively
   immutable snapshot of its arguments and results, so a consumer cannot
   mutate what another consumer will read, and a caller cannot mutate a
   payload out from under the adapter after construction.
3. **Body-free observability.** `DelegatedTaskLifecycleEvent`,
   `TaskProgressEvent`, `AsyncCompletionCapability`, and
   `approval_target_kind` carry counters, state labels, and shape hints only —
   never prompts, paths, arguments, provider responses, or env. They are
   designed to be safe to log and to render in a stall notice.

`contracts/codex_runtime.py` re-exports `CodexRuntime`, the reference
implementation, so a caller can name the adapter without importing from
`core`. It is not part of the package facade, because importing it pulls in
the Codex app-server stack that contract-only consumers do not need.

## Stability policy

The contract is a published surface. Two consumer classes depend on it: the
in-tree adapters, and `bridge/tests/runtime_conformance.py`, which is the
executable definition of conformant behavior.

- **`telegram_bot.core.agent_runtime` is permanently supported.** It is a
  re-export shim, not a copy: every public name is bound by `import`, so
  `core.agent_runtime.TextDeltaEvent is contracts.agent_runtime.TextDeltaEvent`.
  `isinstance` checks, dataclass identity, and `except` clauses behave
  identically through either path. Existing imports were deliberately left
  alone so a pure relocation would not be buried in call-site churn.
- **New code imports from `telegram_bot.contracts`.**
- **Additive changes are safe**: a new event type in the `AgentEvent` union, a
  new optional capability Protocol, a new field with a default. Consumers must
  tolerate an unrecognized event kind rather than crash on it.
- **Breaking changes need a conformance update in the same change.** Removing
  or renaming a public name, tightening validation, or changing a required
  field is only reviewable alongside the `runtime_conformance.py` update and
  every adapter it fails.
- **The contract stays dependency-light.** It imports from the standard library
  only. It must not import `telegram`, a provider SDK, or bridge
  configuration — `bridge/tests/test_agent_runtime_contract.py` asserts the
  `telegram`-free half of that mechanically, because an accidental import here
  would re-couple every adapter to the Telegram runtime.

## Why `bot_ports` stays Telegram-internal

`core/bot_ports.py` is also a file of nothing but `Protocol` declarations, so
it looks like it belongs here. It does not, and the distinction is the point of
this package.

`bot_ports` describes the collaborators `TelegramBot.__init__` injects into its
own mixins — `settings`, `session_manager`, `project_chat`, `clock` — plus
bound-method ports like `ReplySmartFn` that one mixin provides to another. Its
signatures are derived from concrete Telegram bot internals, and it imports
`telegram` directly (`Message`, `Update`) because those types appear in the
signatures it describes. It exists to stop the *same* mixin family from
re-declaring the same member with drifting shapes (#1484, #1509) — a cohesion
problem inside one class.

That makes it the opposite kind of Protocol from the ones here:

| | `contracts/agent_runtime.py` | `core/bot_ports.py` |
| --- | --- | --- |
| Describes | What a provider must implement | What `TelegramBot` injects into its own mixins |
| Implementors | Out-of-tree-capable provider adapters | One concrete class family, in-tree |
| Imports `telegram` | Never | Yes, by necessity |
| Changing it | Breaks adapters; needs a conformance update | Refactor internal to the bot |

Hoisting `bot_ports` into `contracts` would drag a hard `telegram` dependency
into the one package that must not have one, and would advertise the bot's
internal wiring as a stable external surface it was never meant to be. It
stays in `core` next to the mixins it serves.
