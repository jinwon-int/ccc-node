# Jev skill advice in Telegram and Matrix

The bridge can attach one optional local-skill hint before an interactive
provider turn. It uses TypeSafe's `jev-1.13.0` at the fixed
`https://api.typesafe.ai/v1/systemone` endpoint. The existing locked `httpx`
runtime dependency (also required by python-telegram-bot) supplies HTTP transport.
The feature is **off unless explicitly configured for that channel and user**.

This is advice, not a router: it cannot dispatch A2A work, grant approval,
change tools/permissions, or force a skill. The agent must assess relevance and
read the named local SKILL.md. Explicit skill commands retain precedence.

## Configuration

For each running bridge's `BOT_DATA_DIR`, provision an owner-only regular file
`jev-skill-advice.json`:

```json
{"enabled": true, "allowed_user_ids": [123456789]}
```

Use the exact operator's numeric conversation identity. Telegram uses its user
ID. Matrix uses the **persisted** integer mapping for the configured owner in
`matrix-ids.json`; do not copy the Telegram ID, invent an integer, or enable all
family users. Group/shared rooms are excluded even for an allowed operator.
Telegram and Matrix have separate BOT_DATA_DIRs and need separate opt-ins.

Store the credential at the serving account's
`$HOME/.secrets/typesafe-api-key` (0600; parent 0700). Config must also be 0600,
owned by the service user with a non-writable-by-others parent. A missing key,
unsafe file, disabled flag or absent config leaves the original turn unchanged.
Never commit either file or place the key in command arguments, prompts or logs.
Provision on the actual account: a user-service home can differ from SSH root.

Config is read per eligible request; set `enabled` to false atomically to stop
new advice without restarting. Already-running inference is bounded to two
seconds. New bridge code needs an idle-safe restart using the node's normal
update mechanism. Preserve rollback artifacts through verified restart and
check the **serving source**, especially isolated Termux Matrix/Telegram copies.
Do not restart an active turn to enable advice.

## Scope, data and failure behavior

Candidates are the following installed, owned, non-writable-by-others skills in
`$HOME/.codex/skills` then `$HOME/.claude/skills` (first valid copy wins):
`a2a-task-poll`, `ccc-node-status`, `ccc-self-update`, `ccc-wiki-record`,
`gh-pr-flow`, `web-routing`, `ccc-agent-cron`, `research-hug-law`.
A missing skill is never proposed or installed. There are `no_skill` and `defer`
choices. Other skills remain available to the agent normally.

Only the bounded current request text (9–4000 characters) and these static
candidate descriptions are sent to TypeSafe. No session history, memory,
local skill contents, attachment contents read from disk, or tool output is
collected by this feature. This still sends eligible user text to an additional
external provider. Control/slash/explicit skill invocations, short context-only
approvals/continuations, external event envelopes, group conversations and
sensitive-log turns (including inbound documents), and recognizable
attachment/credential/code-block text are skipped. These textual
filters are conservative heuristics, **not** a comprehensive DLP classifier;
sensitive text should not be submitted on enabled conversations.

A request has one attempt, no ambient proxies, no redirects, a fixed model and
origin, a 32 KiB response cap and a two-second inference deadline. Timeouts,
invalid responses, API failures, unavailable dependencies or installations
leave the original message unchanged; cancellation propagates. Automatic
provider admission retries do not repeat inference. Original user logging is
unchanged; the hint exists only in the provider turn. It can consequently be
visible in provider session history.

The response must match the model, question, candidate set, numeric ranges and
usage schema. Only the local validated name/path is inserted, never external
free text. A top-choice weight below 0.65 or margin below 0.20 abstains. These
are conservative heuristics, **not calibrated accuracy or safety guarantees**.

The body-free `skill_advice` log records status, fixed model, validated choice,
elapsed milliseconds and validated token counts. It excludes user IDs, request
and response text, paths, exceptions and keys. Disabled/ineligible requests do
not emit advice logs. Check `status=recommended` or `abstain` versus
`unavailable` after a controlled synthetic private request, and verify normal
processing continues when the API is unavailable. A standalone probe confirms
connectivity/helper behavior; a restarted serving process plus an actual turn
is required to establish end-to-end adoption.

## Evidence and limits

The initial 120-case synthetic Korean/English pilot matched its intended skill
in 117 cases. Its median inference time was about 0.70 seconds. This is neither
a production accuracy guarantee nor evidence of faster full task completion.
Track request-to-first-action latency and wrong suggestions on actual work
before widening candidates or turning advice into automatic assignment.
