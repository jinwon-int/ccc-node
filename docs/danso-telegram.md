# Danso as the Telegram runtime

Set `CCC_AGENT_PROVIDER=danso` to route normal Telegram turns through the
Danso CLI. This initial integration supports OpenAI Responses with
`gpt-6-astra` and `low`, `medium`, `high`, `xhigh`, or `max` reasoning effort.
The default is `medium`. Select `CCC_DANSO_AUTH_MODE=api-key` (default) for
Platform Responses or `chatgpt` for an explicitly selected subscription auth
file. No credential discovery or fallback between billing/authentication modes
occurs. Danso owns the runtime in both modes.

## Prepare the runtime

Use Linux 5.3+ with procfs/pidfds, Bash and Python 3.11+. Bubblewrap is optional. Build a reviewed Danso checkout with
`cargo build --locked --release` (tested CLI baseline: jinwon-int/danso
`051536b02320f1d319b31c00fd653eecb47bc441` for read-only subscription credentials;
managed renewal additionally requires the reviewed native `auth-adopt` feature). Install the executable at an
operator-controlled absolute path. ccc-node bundles the bounded subprocess
adapter derived from Danso's `integrations/ccc_node.py`; the Danso
Python repository does not need to be on `PYTHONPATH`.

Choose a dedicated task workspace with `CCC_DANSO_WORKSPACE`, separate from
the bridge configuration and session storage. Never use the bridge project root
(or the whole login HOME) as the task workspace: its `.telegram_bot/.env` and
session pointers must not be exposed to coding tools. The bridge rejects these
overlaps. Telegram tasks operate in this explicit workspace.

Create an owner-only state directory **outside** the task workspace.
For a bridge running as `gongmyoung` with project `/home/gongmyoung`, an example
is `/var/lib/ccc-danso/gongmyoung`, owned by `gongmyoung`, mode `0700`.
Do not put state under `/home/gongmyoung` in this example. Paths must not contain
symlinks. The bridge creates private `home/` and `journals/` children; the private
HOME avoids automatic discovery of another runtime's global instructions. It is
not a security boundary in host mode; Bash can read current-user host files.

In the bridge's private project `.telegram_bot/.env`, configure:

```dotenv
CCC_AGENT_PROVIDER=danso
CCC_DANSO_CLI_PATH=/opt/danso/target/release/danso
CCC_DANSO_WORKSPACE=/home/gongmyoung/workspaces/danso
CCC_DANSO_STATE_DIR=/var/lib/ccc-danso/gongmyoung
CCC_DANSO_SANDBOX=host
CCC_DANSO_MODEL=gpt-6-astra
CCC_DANSO_EFFORT=medium
CCC_BRIDGE_MEMORY_MODE=off
CCC_MEMORY_DISTILL_PROVIDER=off
CCC_DANSO_TIMEOUT_SECONDS=300
CCC_DANSO_PROVIDER_TIMEOUT_SECONDS=60
CCC_DANSO_MAX_TURNS=32
CCC_DANSO_COMPACT_AT_BYTES=32768
```

Supply `OPENAI_API_KEY` through the existing private environment/configuration
channel. This is also the existing Whisper key setting. An explicitly configured
`DANSO_OPENAI_BASE_URL` changes only Danso's endpoint. Neither setting is inferred
from Codex authentication or the Whisper endpoint setting.
For subscription OAuth, configure these additional values instead:

```dotenv
CCC_DANSO_AUTH_MODE=chatgpt
DANSO_CHATGPT_AUTH_FILE=/private/isolated-login/danso-auth.json
# DANSO_CHATGPT_BASE_URL=https://chatgpt.com/backend-api/codex
```

Complete official Codex login in an isolated login home first. For managed
renewal, stop every Codex consumer of that isolated home and use the reviewed
native `danso auth-adopt --source /private/isolated-login/auth.json` command.
Select its resulting `danso-auth.json` path explicitly. The bridge never adopts,
reads token contents, repairs pending state, or refreshes credentials itself;
Danso owns these operations. A read-only Codex auth.json may also be selected,
but it requires explicit renewal/relogin when the access token expires.

Auth paths must be absolute and symlink-free, with a current-user-owned0600
single-link regular file up to64KiB in an owner-controlled0700 directory disjoint
from the workspace. Readiness checks metadata only; native validation at each
request remains authoritative. A managed store with an unresolved refresh marker
or reappeared Codex auth.json fails readiness. Follow native recovery instructions
and retain private artifacts; do not remove markers to force another exchange.

Subscription subprocesses receive only PATH/HOME and the selected auth-file/base
settings. OPENAI_API_KEY may still be configured independently for voice
transcription, but neither it nor the Platform endpoint is passed to the
subscription subprocess. Subscription access does not supply a Whisper API key.
The endpoint is the fixed Codex service or an explicitly selected literal-loopback
HTTP fixture. Never run fake fixtures with production tokens. Shell fallback
merging preserves process-selected auth mode/file/base settings.

API-key journals stay in `journals/`; subscription journals use
`chatgpt-journals/`. Switching authentication mode does not migrate or replay
history. Inspect previous work and start a new conversation with `/new` when
changing modes; an old UUID missing in the selected journal root fails explicitly.
Changing ccc-node runtime from Piri to Danso uses the normal provider alignment
path, preserving the previous runtime's history.

`CLAUDE_PROCESS_TIMEOUT` is the legacy name of the bridge-wide deadline; keep its
normal default, or set it at least 10 seconds above the Danso deadline while
preserving the bridge's other timeout invariants.

Readiness checks are local prerequisite checks, not proof of account access or
kernel sandbox support. Run the reviewed CLI's tests for the chosen backend and an authorized
provider canary before switching a live bot. Restarting a deployed
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
  acknowledges, or automatically replays them. If startup failed before creating
  a journal, the saved ID is deliberately retained; the next attempt explains
  that the journal is unavailable. Check the prior work, then use `/new`.
- Completed runs record request and input/output token counters in the local
  usage meter. Cache input is included. Failed runs may have incurred unreported
  usage; counters are not a complete billing statement or account quota.
- CCC memory routing/bootstrap/distill, asynchronous completion injection,
  external-wait routing, transcript browsing and `/revert` are unsupported.
  Explicit cross-provider distill overrides also fail at startup.
  Non-off CCC memory modes fail at startup instead of silently using shared
  memory. Native Danso journal compaction remains enabled independently.
- `CCC_DANSO_SANDBOX=host` is the default: tools run with current-user host
  filesystem/network permissions and native descendant supervision. Workspace
  path checks and cleared environments do not prevent Bash from accessing host
  credentials or services. `bubblewrap` explicitly selects the original isolation
  boundary and requires the helper/user namespaces; failure never falls back.
  The backend is passed explicitly to the CLI. Codex approval/sandbox controls
  do not change it; no interactive tool approval UI exists.

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
