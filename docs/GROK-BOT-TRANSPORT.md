# Existing Grok Bot transport qualification (#1632)

This is a protocol and transport component, **not an enabled CCC provider**.
It addresses the existing persistent Grok Bot host, not the xAI model API.
No Telegram poller, live provider selection, gateway settings or vendor files
are changed by this component.

The qualified host version is `5c534e9`, with `orderedReplicasV1` and
`sendAcceptanceV1`. A different version fails validation until its contracts
are qualified. The public API reference is the MIT-licensed
[`grokbot-sdk`](https://github.com/Adam91holt/grokbot-sdk/tree/c14347fa82d167b9a5984ec1baff56b2f074485a)
at that pinned revision. Actual authenticated host observations verify the
acceptance digest and transcript/request association; no upstream application
code or SDK dependency is included.

## Transport and credentials

`GrokSshTransport` sends our stdlib-only helper through SSH stdin to
`python3 -I -`. Request content never enters the remote command or argv.
The configured destination has a restricted account/hostname grammar. SSH
requires existing host-key trust and noninteractive authentication, clears
forwardings, and uses the operator's existing SSH configuration.

The helper reads `~/sand-data/gateway.json` **on the original host**. Its
directory must be owned by the current user and exactly 0700. The vendor file
may be 0644 within that private directory, but must be a single-link regular
file, owned by the user, not group/world writable, and at most 16 KiB. File
and directory symlinks and file changes during the bounded read are rejected.
The helper neither changes permissions nor removes any file. Token contents
are never returned or placed in process arguments, logs or local configuration.

Only HTTP loopback `127.0.0.1:1340` is qualified. No caller URL, headers,
attachments, remote shell operation, redirect or HTTP proxy is accepted.

| Operation | Method/path | Reconstructed input |
| --- | --- | --- |
| health | GET `/health` | none |
| status | POST `/api/getHostStatus` | empty object |
| tail | POST `/api/getAgentTranscriptTail` | configured Bot ID, limit 64 |
| acceptance | POST `/api/promptAcceptanceStatus` | account `host`, Bot ID, nonce |
| send | POST `/api/sendPrompt` | Bot ID, nonce, bounded plain text |

The HTTP timeout is 10 seconds; the local SSH exchange is bounded to
20 seconds, with a separate maximum two-second process cleanup budget.
The transport owns a dedicated local process group, terminates that group
on failure/cancellation, and closes local pipes even if a helper escapes it.
That does not terminate an escaped helper or interrupt the remote Bot.
stdout is capped at 1 MiB and stderr at 4 KiB. JSON rejects
duplicate keys, excessive structure, non-finite and oversized numeric values.
Errors are categorical and contain no gateway error body. Each call makes at
most one HTTP request. **It never retries a send.** An SSH failure can happen
after the host accepted a prompt and must be treated as an unknown outcome.
Terminating SSH stops local transport; it does not stop the Bot.

## Reply attribution

Before any send, the future runtime must durably store the exact nonce,
prompt, canonical digest and pre-send transcript baseline. HTTP acceptance
does not mean completion. The acceptance record must match account, Bot,
nonce and digest; its echo must match the prompt and carry a fresh request ID.
Only the contiguous range after the saved baseline, beginning with that echo
and containing only the same request's visible text messages, is accepted.
An idle observation must refer to the configured Bot.

Missing/truncated baselines, an unanchored full or paginated tail, concurrent
foreign input, reused request IDs, unknown records, approval/tool/attachment
records and oversized output deny completion. This deliberately does not use
the latest assistant message as a substitute. Read-only observations are not
an exclusive lease on the Bot: another app can interfere and freeze the turn.

Rejecting tool records **after execution does not prevent tool side effects**.
This parser is qualified for synthetic text probes; it is not an approval
policy or a claim that arbitrary production prompts are safe to run.

## Required before provider activation

The journal/runtime and restricted frontend now implement the local boundaries
below; see [GROK-BOT-RUNTIME.md](GROK-BOT-RUNTIME.md) and
[GROK-BOT-PROVIDER.md](GROK-BOT-PROVIDER.md). Their hermetic qualification does
not replace the final real-DM/single-poller acceptance.

- A private, bounded, process-serialized journal that writes before sending,
  preserves immutable nonce/prompt/baseline, commits bound results before
  release, and reconciles crash/lost-response outcomes without fresh-nonce
  replay. Missing/corrupt state must not reset a persistent Bot conversation.
- One explicit owner/audience mapping to one existing Bot. Arbitrary local
  session IDs and `/new` cannot pretend the same Bot is a fresh conversation.
- Truthful unsupported/degraded options, model selection, resume/reset,
  readiness and runtime conformance across all provider integration points.
- Qualified approval/tool handling. The observed interrupt API is Bot/session
  scoped, not a request-ID CAS; do not race an ownership read with a global
  interrupt or claim that local cancellation stopped remote work.
- An isolated, allowlisted Telegram integration proof. Do not start a second
  poller on an existing production token or switch Seoseo's live provider to
  demonstrate this component.

Tests use generated data. Live synthetic probes verify nonce/echo/request/reply
binding; an accepted same-nonce retry produced no extra transcript entry. Those
observations do not yet qualify a persistent runtime, Telegram delivery,
calendar/drive tools, or crash recovery. Exact proof versions and body-free
receipts are retained in operator-private artifacts; no credentials or human
conversation bodies belong in this repository.
