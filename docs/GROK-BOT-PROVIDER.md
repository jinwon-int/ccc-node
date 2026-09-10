# Existing Grok Bot: restricted Telegram provider (#1632)

`CCC_AGENT_PROVIDER=grok` selects `GrokRuntime` through `build_context` and a
dedicated `GrokTelegramBot` through `create_app`. It does **not** instantiate the
generic ProjectChat/SessionManager frontend, prompt decorators, memory workers,
spool delivery, continuation/cron runners or general command handlers. Those
components assume independent sessions, reset, model and execution-policy
controls that the existing Bot protocol cannot implement. Direct construction
of the generic session/project frontend with Grok is rejected. The generic
readiness probe also rejects Grok instead of falling through to Claude.

This is a deliberately limited first slice, using the existing normalized
AgentRuntime events and the qualified SSH transport and journal. No xAI model
API, new SDK/runtime dependency or CCC installation on the Grok host is involved.
The generated [capability matrix](provider-capability-matrix.md) records every
unsupported/degraded axis. Runnable synthetic tests are not an operational
Telegram acceptance or evidence that a provider was enabled on a fleet node.

## Explicit configuration and attachment

Use a **dedicated** local project/config and a dedicated Telegram bot. Credentials
remain in their original private configuration files, never command arguments,
Git, screenshots or reports. In that configuration select:

| Setting | Required value |
| --- | --- |
| `CCC_AGENT_PROVIDER` | `grok` |
| `CCC_GROK_SSH_DESTINATION` | Explicit SSH account and trusted host |
| `CCC_GROK_BOT_ID` | Existing Grok Bot UUID |
| `CCC_GROK_OWNER_ID` | One positive Telegram human user ID |
| `CCC_GROK_TELEGRAM_BOT_ID` | Dedicated Telegram bot numeric ID |
| `ALLOWED_USER_IDS` | Exactly the singleton owner ID list |
| `CCC_REQUIRE_ALLOWLIST` | `true` |
| `CCC_GROK_JOURNAL_PATH` | One explicit absolute private journal directory |

The usual `TELEGRAM_BOT_TOKEN` stays in that project's private environment file.
Its numeric prefix must match the selected Telegram bot; authenticated `getMe`
must also match before startup opens the journal or accesses Grok. An existing
Telegram webhook causes startup denial before polling (no webhook takeover).
`project_root` is an immutable **local label**, never an applied remote cwd.

Initialize once, explicitly acknowledging attachment to the Bot's **existing**
context, from the installed bridge Python environment:

```sh
python -m telegram_bot.core.grok_manage --path /absolute/dedicated/project attach-existing --acknowledge-existing-context
python -m telegram_bot.core.grok_manage --path /absolute/dedicated/project inspect
python -m telegram_bot --path /absolute/dedicated/project
```

Management creates only the local immutable binding. It reports
`remote_identity_verified: false`; this declaration is not remote authentication.
The private parent must already exist and satisfy the journal's file invariants.
Existing, partial, unknown or corrupt state is retained and denied. Startup never
creates it, and neither management nor `/new` resets the remote Bot. The binding
includes owner, Telegram bot, remote Bot, SSH destination and local project label;
moving among owners/bots is not an implicit migration. Earlier manually labelled
transport/runtime proof journals are not reinterpreted as Telegram journals.

## Admission and supported interaction

After authenticated Telegram identity, startup verifies the existing local
journal, qualified Grok host version and idle Bot. Missing/corrupt/unknown state,
wrong host contract, busy/approval-only state or SSH failure aborts startup before
polling. One Linux abstract socket excludes another local frontend for the same
configured SSH destination/Bot, including a different journal or Telegram token.
Aliases to the same host and other computers/apps are not a distributed lock.

Only an original, non-forwarded, non-topic **private message** from the configured
human owner, in that owner's numeric DM, through the configured Telegram bot is
admitted. Edited messages, callbacks, groups, channels and other actors do not
reach the journal or remote Bot. Files and unsupported commands receive a static
denial only in the admitted owner DM. They never become prompts.

- Plain text: original bytes, no memory/context decoration, model override or
  automatic new nonce after uncertain submission. Limit 32 KiB UTF-8.
- `/start`, `/status`: describe this fixed attachment, not a new remote probe or
  proof that the remote Bot is currently idle/healthy.
- `/stop`: cancels local waiting/output, retains uncertainty, does **not** claim
  to stop the Bot's remote tools. A later explicit text input reopens the retained
  journal and reconciles it; it does not reset it.
- `/new`, `/resume`, model/effort/sandbox/approval controls, history, skills,
  arbitrary commands, voice and attachments: unsupported.

One turn is admitted at a time. A concurrent input is rejected, not queued or
silently replayed. Only a complete, durably committed ResultEvent followed by
Completion is delivered. Text is inert, with no Markdown interpretation or link
preview, in chunks of at most 2,000 Unicode code points. A stop/shutdown generation
prevents later chunks from starting; a Telegram HTTP request already in flight
may already have been delivered and cannot be recalled.

The frontend owns the explicit PTB async startup/drain lifecycle and its event
loop (including Python 3.14). SIGINT/SIGTERM immediately retire the route and
cancel its active callback **before** updater/application shutdown waits. Closing
a frontend is terminal: each external initialization wait rechecks its original
generation, so late getMe/status/health replies cannot reopen state or a poller.
The shell provider label names Grok, but generic shell health/status automation
is not a qualified replacement for this dedicated Python entrypoint.

## Honest delivery and execution limits

The runtime journal is **not** a Telegram exactly-once inbox/outbox. The bounded
process-local update-ID set suppresses duplicate callbacks in the running
process, but Telegram polling acknowledgement is not atomic with journal commit.
A crash may lose a polled input before submission. A crash/lost reply can also
repeat some already-delivered reply chunks. Re-entering the same latest completed
text returns its cached result without executing again; an intentional identical
new operation is consequently suppressed. A delayed older message after a newer
completed operation is not deduplicated by this runtime seam. No hidden prompt
decoration or fresh nonce is used to pretend these limits are solved.

There are 32 turns/97 revisions per journal and 1,024 admitted update IDs per
frontend process; these are finite qualification limits, not an unlimited service.
Exhaustion fails closed without eviction, pruning or reset. Restart clears only
the process update-ID set, not journal limits. Removing a journal to evade limits
or replay uncertain work is not supported. General CCC health observers and
autonomous delivery are not installed; `/status` is a static route description.

Remote tools are governed by the existing Bot host's policy. Rejecting a tool or
approval transcript **after execution does not prevent its side effects**. This
provider offers no CCC sandbox/approval enforcement, remote cwd/model/effort
control, separate `/new` context or host-global cancellation. Its current journal
is private plaintext and has no witness for a valid whole-prefix rollback. See
[runtime](GROK-BOT-RUNTIME.md) and [transport](GROK-BOT-TRANSPORT.md).

## Single-poller rollout and rollback acceptance

1. Verify the reviewed build and Python/SSH/Linux requirements in an isolated
   checkout. Run hermetic composition, actual framework polling, owner/group/
   reset/file denial, restart, uncertainty, cancellation and journal tests.
2. Identify the dedicated bot and its original token host/file location without
   copying the token. Confirm no webhook and inventory **all** possible pollers,
   including other machines and destination aliases; the local socket is not
   sufficient evidence. Do not reuse Seoseo's serving Telegram credentials.
3. Preserve existing config, journal and service definitions privately. Explicitly
   attach the one journal, verify it with `inspect`, then run one isolated bridge.
   No unrelated provider/service is restarted. Keep the prior bridge stopped if
   it shares this exact token; never run both to test.
4. Use one allowlisted real owner DM with generated text. Observe attributable
   durable completion, real Telegram receipt, same-input cached reopen, unsupported
   command/group denial and local-stop uncertainty. Record body-free evidence and
   public build IDs only. This operational acceptance is still required; the
   synthetic API test does not satisfy it or close #1632 alone.
5. To roll back, stop and verify termination of the new poller first. Preserve its
   journal and reconcile any attempted/accepted work against the original Bot.
   Restart a previous token owner only after verifying single-poller state.
   Never restore an older journal prefix or silently replay an uncertain request.

Transport diagnostics are categorical; the restricted frontend suppresses raw
Telegram/httpx/httpcore diagnostic bodies and exception URLs even under debug
logging. No prompt/response logging, gateway token relocation, or automatic
background agent execution is installed by this frontend.
