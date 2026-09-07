# Danso as the Telegram runtime

Set `CCC_AGENT_PROVIDER=danso` to route normal Telegram turns through the
sandboxed Danso CLI. This initial integration supports OpenAI Responses with
`gpt-6-astra` and `low`, `medium`, `high`, `xhigh`, or `max` reasoning effort.
The default is `medium`. It uses an explicit OpenAI API key; Codex subscription
OAuth credentials are not accepted or discovered.

## Prepare the runtime

Use Linux, Python 3.11+, and bubblewrap. Build a reviewed Danso checkout with
`cargo build --locked --release` (tested CLI baseline: jinwon-int/danso
`dac88d49a3ee99afed94ad6f806f24dc5f26f76e`). Install the executable at an
operator-controlled absolute path. ccc-node bundles the bounded subprocess
adapter derived from that revision's `integrations/ccc_node.py`; the Danso
Python repository does not need to be on `PYTHONPATH`.

Create an owner-only state directory **outside** the Telegram project workspace.
For a bridge running as `gongmyoung` with project `/home/gongmyoung`, an example
is `/var/lib/ccc-danso/gongmyoung`, owned by `gongmyoung`, mode `0700`.
Do not put state under `/home/gongmyoung` in this example. Paths must not contain
symlinks. The bridge creates private `home/` and `journals/` children; the isolated
HOME prevents importing another runtime's global instructions or credentials.

In the bridge's private project `.telegram_bot/.env`, configure:

```dotenv
CCC_AGENT_PROVIDER=danso
CCC_DANSO_CLI_PATH=/opt/danso/target/release/danso
CCC_DANSO_STATE_DIR=/var/lib/ccc-danso/gongmyoung
CCC_DANSO_MODEL=gpt-6-astra
CCC_DANSO_EFFORT=medium
CCC_BRIDGE_MEMORY_MODE=off
CCC_DANSO_TIMEOUT_SECONDS=300
CCC_DANSO_PROVIDER_TIMEOUT_SECONDS=60
CCC_DANSO_MAX_TURNS=32
CCC_DANSO_COMPACT_AT_BYTES=32768
```

Supply `OPENAI_API_KEY` through the existing private environment/configuration
channel. This is also the existing Whisper key setting. An explicitly configured
`DANSO_OPENAI_BASE_URL` changes only Danso's endpoint. Neither setting is inferred
from Codex authentication or the Whisper endpoint setting.
`CLAUDE_PROCESS_TIMEOUT` is the legacy name of the bridge-wide deadline; keep its
normal default, or set it at least 10 seconds above the Danso deadline while
preserving the bridge's other timeout invariants.

Readiness checks are local prerequisite checks, not proof of account access or
kernel sandbox support. Run the reviewed CLI's real sandbox tests and a separately
authorized provider canary before switching a live bot. Restarting a deployed
bridge is a separate operational step; source development does not switch a node.

## Telegram behavior

- Messages run through the normal queue, typing/status and final reply paths.
  The final answer is buffered; token-by-token and tool-progress streaming are
  not available. The finite subprocess deadline replaces the first-event
  admission timeout for this provider.
- `/model` shows the configured model. Arbitrary model changes are rejected.
  `/effort` selects a supported effort; `default` restores `CCC_DANSO_EFFORT`.
- The conversation UUID is durably saved before the subprocess can execute;
  failed persistence prevents launch. Failures retain that identity.
- `/new` starts a new journal. Ordinary turns and bridge restarts resume the
  current conversation's exact UUID. `/resume` reports that UUID; it cannot
  select another conversation's journal. Automatic time-based session resets are
  disabled for Danso so an unresolved journal cannot be abandoned silently.
- `/stop` terminates and reaps the owned process group. Journals are retained.
  Native unresolved tool operations remain blocked; the bridge never repairs,
  acknowledges, or automatically replays them.
- Completed runs record request and input/output token counters in the local
  usage meter. Cache input is included. Failed runs may have incurred unreported
  usage; counters are not a complete billing statement or account quota.
- CCC memory routing/bootstrap/distill, asynchronous completion injection,
  external-wait routing, transcript browsing and `/revert` are unsupported.
  Non-off CCC memory modes fail at startup instead of silently using shared
  memory. Native Danso journal compaction remains enabled independently.
- Danso always enforces its workspace bubblewrap sandbox; Codex approval and
  sandbox controls do not change it. No interactive tool approval UI exists.
  The first version is a bounded coding runtime, not an unrestricted node operator.

To roll back provider selection, restore the previous private configuration and
restart through the normal node procedure. Leave Danso journals intact. Provider
alignment clears the old provider's session/model/effort pointers on the next
turn; it never converts or deletes another runtime's history.

## Validation

`bridge/tests/test_danso_runtime.py` drives the real adapter through a synthetic
CLI, including Telegram composition, persisted resume, commands, counters,
failures, cancellation and environment isolation. It does not call OpenAI.
`bridge/tests/test_start_provider_cli.py` checks the shell startup gate.
The capability matrix distinguishes buffered output and absent memory features
from supported conversation behavior.
