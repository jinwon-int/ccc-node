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
`aad826a4b011e815452ac99f9ab59d64a403134a`, including native `auth-adopt`, managed
renewal and completed SSE output-item handling). Install the executable at an
operator-controlled absolute path. ccc-node bundles the bounded subprocess
adapter derived from Danso's `integrations/ccc_node.py`; the Danso
Python repository does not need to be on `PYTHONPATH`.

The CLI must also support `--progress-jsonl` (tested progress revision
`ffc06d7b7191609314e04866190c4f22205298b5`; the original `dac88d49` baseline
does not). Upgrade the CLI before enabling this bridge version; there is no
silent fallback to an older buffered protocol.

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
# Optional host-only HOME for development tools such as a user Rust install.
# The provider/context HOME remains the private state/home directory.
# CCC_DANSO_TOOL_HOME=/home/gongmyoung
CCC_DANSO_MODEL=gpt-6-astra
CCC_DANSO_EFFORT=medium
CCC_BRIDGE_MEMORY_MODE=off
CCC_MEMORY_DISTILL_PROVIDER=off
CCC_DANSO_TIMEOUT_SECONDS=3600
CCC_DANSO_LONG_TASK_ENABLED=true
CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS=21600
CLAUDE_PROCESS_TIMEOUT=21660
CCC_DANSO_PROVIDER_TIMEOUT_SECONDS=180
CCC_DANSO_MAX_TURNS=32
CCC_DANSO_COMPACT_AT_BYTES=131072
ENABLE_STREAMING=true
ENABLE_STREAMING_TOOL_CALLS=true
```

The provider request default is 180 seconds. Existing environments that pin
`CCC_DANSO_PROVIDER_TIMEOUT_SECONDS=60` should update that explicit setting to
`180` if they want the new default; other explicit values remain valid
overrides in the supported 1..300 second range. Native HTTP failures may also
carry an optional validated `DANSO_TRANSPORT` record with only the phase,
elapsed milliseconds, and request byte count; malformed records are ignored.

`CCC_DANSO_COMPACT_AT_BYTES` defaults to 131072 bytes (128 KiB). Native Danso
measures the serialized request, including system context, JSON escaping and
tool schemas, so the default leaves room for the normal 32 KiB CCC memory
snapshot and its request envelope. Existing installations with an explicit
`32768` setting should migrate it to `131072`; explicit lower values remain
valid overrides but can fail closed when the fixed context cannot fit.

`CCC_DANSO_TOOL_HOME` is optional and host-only. When set, it must be an
absolute path and the native CLI must expose `--tool-home`; readiness fails
before a provider request for an older binary or a bubblewrap configuration.
The flag changes `HOME` and `PATH` only for native development-tool children
(including `<tool-home>/.cargo/bin`). The provider process and context
discovery continue to use the bridge-created private `state/home`, preserving
audience isolation. Leave this unset for ordinary host runs.

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

Auth paths must be absolute and symlink-free, with a current-user-owned 0600
single-link regular file up to 64 KiB in an owner-controlled 0700 directory disjoint
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

Long-task mode is enabled by default. Set `CCC_DANSO_LONG_TASK_ENABLED=false`
to select ordinary mode with its one-hour default deadline. Configure
the native limits (`CCC_DANSO_TASK_STAGE_REQUESTS`,
`CCC_DANSO_TASK_MAX_REQUESTS`, `CCC_DANSO_TASK_MAX_TOKENS`, and
`CCC_DANSO_TASK_REPEAT_LIMIT`) only with a native binary that exposes the full
long-task CLI. The bridge rejects older binaries before a provider request. The
native cumulative active-runtime limit, excluding operator pauses, defaults to and is at most
21600 seconds; for the maximum, set
`CCC_DANSO_LONG_TASK_TIMEOUT_SECONDS=21600` and
`CLAUDE_PROCESS_TIMEOUT=21660` (also the bridge-wide default). Existing explicit
settings remain authoritative within their selected mode.
`CCC_DANSO_TIMEOUT_SECONDS` applies only when
`CCC_DANSO_LONG_TASK_ENABLED=false`: an existing ordinary timeout such as 300
seconds does not opt out of the new long-task default. To retain that ordinary
deadline on upgrade, explicitly set the mode to false. A pinned outer deadline of 21600 must
be raised to at least 21610 when using the six-hour task limit. Older binaries
must be upgraded or explicitly use ordinary mode; capability checks still fail
closed. This default change does not replay, repair, or migrate unresolved
session journals. Provider requests keep the 180-second default.
The stage request setting is a target: native safe-boundary compaction may
consume additional bounded requests before it records the next checkpoint.
The repeat setting limits repeated identical tool batches. The defaults remain
1024 requests and 10,000,000 reported tokens; the native build may expose
bounded upper limits of 2048 requests and 25,000,000 tokens.
The native journal owns cumulative budgets, stages, repetition decisions, and
resume eligibility. The bridge forwards these limits and consumes only bounded,
body-free `DANSO_TASK` checkpoint records for the status heartbeat.

Readiness checks are local prerequisite checks, not proof of account access or
kernel sandbox support. Run the reviewed CLI's tests for the chosen backend and an authorized
provider canary before switching a live bot. Restarting a deployed
bridge is a separate operational step; source development does not switch a node.

## Telegram behavior

- Messages run through the normal queue, typing/status and final reply paths.
  `CCC_DANSO_PROGRESS_ENABLED=true` (default) selects `--progress-jsonl` when
  the installed binary advertises it, for ordinary and long-task turns.
  Completed assistant explanations accompanying tool calls are delivered as
  separate interim bubbles, including when the draft streaming master switch
  is off. The final answer is delivered once, after process cleanup and usage
  validation. Older CLIs without the flag retain final-only output; setting
  `CCC_DANSO_PROGRESS_ENABLED=false` explicitly selects that behavior.
- Interim text is limited to 4096 characters per assistant message and credential
  patterns are redacted before truncation. User messages, reasoning blocks, tool
  arguments and tool-result bodies never become interim bubbles. Body-free tool
  notifications still follow the existing display settings. JSONL framing keeps
  its 2 MiB line and 32 MiB run limits; malformed output fails closed.
- Native Rust progress guidance asks for concise updates in the user's language
  alongside useful tool calls at the start and meaningful milestones. This is
  completed-message delivery, not provider token streaming or a promise of a
  fixed reporting interval. A slow provider request or tool is covered by the
  existing status heartbeat; reporting adds no model request or replay.
- Long-task checkpoint counters remain on stderr as `DANSO_TASK`; pause, resume,
  cancellation and failure handling stay independent of assistant commentary.
  The finite subprocess deadline replaces the first-event admission timeout.
- Active requests keep their status message even when a long tool or a native
  task stage produces no events. After `CCC_HEARTBEAT_STALL_SECONDS` (default
  300), it shows `Waiting for progress`, total elapsed time, the age of the last
  report, and the last known work label. This indicates missing progress
  reports, not proof that the worker stopped. ETA is hidden until new events
  arrive; the normal ETA is a historical estimate, not a completion percentage.
  The status continues updating at the configured heartbeat interval (15s by
  default). Setting the silence threshold to 0 disables the waiting indicator.
  Completion, failure, cancellation, and startup reconciliation still own status
  cleanup. This shared heartbeat behavior also applies to other providers.
- `/model` shows the configured model. Arbitrary model changes are rejected.
  `/effort` selects a supported effort; `default` restores `CCC_DANSO_EFFORT`.
- The conversation UUID is durably saved before the subprocess can execute;
  failed persistence prevents launch. Failures retain that identity.
- `/new` starts a new journal. Ordinary turns and bridge restarts resume the
  current conversation's exact UUID. `/resume` reports that UUID; it cannot
  select another conversation's journal. Automatic time-based session resets are
  disabled for Danso so an unresolved journal cannot be abandoned silently.
- With long-task mode enabled, `/task_resume` is an explicit resume
  command. It accepts no prompt or session id and uses the current authorized
  conversation journal; it does not duplicate a user prompt. A normal message
  never auto-resumes a pending native task.
- `/task_pause` asks the active native parent to pause at its next settled
  checkpoint after the bridge has observed the native-ready record. It never
  signals the process group or launches a replacement process. `/stop` remains
  the hard cancellation path.
- `/stop` terminates and reaps the owned process group. Journals are retained.
  It is a hard cancellation and never automatically replays the task. Native
  unresolved tool operations remain blocked; the bridge never repairs or
  acknowledges them. If startup failed before creating
  a journal, the saved ID is deliberately retained; the next attempt explains
  that the journal is unavailable. Check the prior work, then use `/new`.
- Completed runs record request and input/output token counters in the local
  usage meter. Cache input is included. Failed runs may have incurred unreported
  usage; counters are not a complete billing statement or account quota.
- Automatic memory extraction/write-back, asynchronous completion injection,
  external-wait routing, transcript browsing and `/revert` are unsupported.
  Explicit cross-provider distill overrides also fail at startup.
  CCC memory reading is opt-in with audience-scoped routes (below); curated
  mode fails at startup. Native journal compaction remains independent.
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
The capability matrix distinguishes buffered output and limited memory features
from supported conversation behavior.

## CCC memory (read connection)

With a native Danso build supporting `--system-context-file`, select
`CCC_BRIDGE_MEMORY_MODE=audience-scoped` and keep
`CCC_MEMORY_DISTILL_PROVIDER=off`. Use `CCC_TELEGRAM_SESSION_SCOPE=shared-groups`
(or another non-shared-all scope). The configured
`CCC_CODEX_MEMORY_MATERIALIZER_PATH` must point to this revision's CCC Python
materializer; the historical setting name does not require a Codex CLI, API key,
or a second model runtime. Existing CCC loader/hook installation is required.

Before every native invocation the bridge refreshes a private bounded CCC
snapshot under `<audience-root>/<opaque-scope>/danso/bootstrap/AGENTS.md`.
It supplies only the path through `--system-context-file`; memory bodies do not
enter argv, the user prompt, or bridge diagnostics. Native run-local context
survives compaction. The next invocation refreshes again, including resume.
A failed materialization aborts before dispatch; there is no stale-snapshot fallback.
The loader receives a small explicit environment, without provider credentials.
Global pending promises/detached jobs and background cache refresh are disabled;
stale Nunchi snapshots are omitted instead of regenerated. Stop/cancellation
terminates the owned materializer and its loaders before returning.
Private DMs retain the existing CCC private/legacy read policy; groups read
only the shared route. Native HOME and OAuth credentials remain separate from
materializer homes. Host execution still has the runtime user's permissions;
audience routing is not a filesystem sandbox.

Memory-enabled journals use separate `journals-audience/<opaque-scope>` or
`chatgpt-journals-audience/<opaque-scope>` roots. Old unscoped journals and
journals from another audience are never silently resumed. On first enabling
memory, start a new Telegram conversation with `/new` after recording any
unfinished work; retain the previous journal for recovery. Turning memory off
also needs a new conversation rather than moving journals between namespaces.

With extraction off, only CCC memory reading is active. Search tools are not added. A model
may quote supplied memory in its response; the snapshot is not a secret vault.


## Automatic extraction and local storage

Requires native Danso supporting `--no-tools` (Danso PR #37) as well as the
system-context flag. Keep audience-scoped memory and configure, for example:

```dotenv
CCC_MEMORY_DISTILL_PROVIDER=danso
CCC_MEMORY_DISTILL_MODEL=gpt-5.6-luna
CCC_MEMORY_DISTILL_CHECKPOINT_TURNS=4
CCC_MEMORY_DISTILL_CHECKPOINT_AGE_SECONDS=1800
CCC_USAGE_BUDGET_TOKENS_DANSO=1000000
```

Completed-turn checkpoints, `/distill`, `/new`, provider changes and shutdown
use a separate `danso-distill-journal` durable queue; prior Codex/Piri jobs stay untouched. Age is checked at turn completion. A finite
usage-meter budget is required; zero budget leaves extraction off. Reservations
conservatively charge serialized input/schema/output bounds, not subscription
quota measurements. Interactive turns are not blocked by this autonomous budget.

Snapshots read only the exact audience's UUID journal under the native flock,
reject missing/partial/unresolved journals, and retain a bounded recent text
window (8192 bytes for extraction, within the native 16 MiB journal limit).
JSON escaping is included in the native context limit; further reduction keeps
recent messages and explicitly marks the input truncated. Native header UUIDs
are independent of filenames; the reader checks the expected workspace too.
Extraction runs the native CLI with one request, no tools, `max` reasoning effort,
an empty temporary HOME/workspace, explicit selected authentication and a private
reference file. It does not launch Codex or adopt/copy authentication. Strict
schema, decision-reason and provenance checks precede any storage. Only generated
scratch files are removed after owned subprocess cleanup.

The existing local sink writes audience-scoped `memory-facts.jsonl` and
`resume.md`, rebuilds the local index and deduplicates retries. A later materializer
reads these facts; the node must have its normal CCC memory search helpers
installed. Existing memory namespaces and journals do not move. New Danso jobs
do not generate Wiki candidates. Skill candidates ARE collected (#1662): the
danso journal drives the same `SkillCandidateCollectorWorker` via
`DansoSkillCandidateBackend` (default on, `CCC_DANSO_SKILL_COLLECTOR=false`
opts out), staging pending drafts that the danso install lane installs into
`<CCC_DANSO_STATE_DIR>/home/.pi/agent/skills` (#1659). This does not
backfill old conversations or provide a new automatic Honcho ingestion path.

## Provider failure diagnostics

HTTP status and ChatGPT SSE failures may additionally emit:

```text
DANSO_PROVIDER={"version":1,"reason":"http_status","http_status":429}
```

Legacy records have exactly `version`, `reason`, and `http_status`. Current
native records additionally carry `output_tokens_max`: null for ordinary
failures, or a positive uint32 for `reason=max_tokens` with null HTTP status.
The adapter accepts both shapes, validates the cap, and displays it for output
limit failures. Unknown keys and inconsistent reason/cap combinations remain
invalid. Reasons are the
closed native enum `http_status`, `invalid_json`, `response_too_large`,
`stream_ended`, `invalid_stream`, `unsupported_stream_event`, `response_failed`,
`response_incomplete`, `response_error`, and `max_tokens`. `http_status` is a non-2xx,
three-digit status accepted by reqwest (100..999), and is null for every other
reason. No response body, remote error code/message, URL, or credential is
copied into this record. `invalid_stream` includes malformed or inconsistent
SSE frames and terminal responses; `stream_ended` means no completed response
was present at a valid stream boundary or at `[DONE]`.

The adapter accepts exactly one valid record only with category `provider` and
exit code 3. Missing, malformed, duplicate, or inconsistent optional records
are ignored. Any reserved failure record on exit 0 is an adapter error.
Older binaries remain supported without the extra detail. Usage counts and
terminal failure behavior are unchanged; there is no automatic replay or new
retry. These fields describe the observed failure, not its underlying cause;
previous failures without this metadata cannot be diagnosed retroactively.
Auth-store errors and other response-processing failures may still carry only
the existing category.

### Optional Z.AI limit details

Updated native Danso may emit a separate `DANSO_HTTP` v1 record with exactly
`version`, `provider` (`zai`), `http_status`, `provider_code`, and
`retry_after_seconds`. The adapter requires a matching valid HTTP
`DANSO_PROVIDER` record, a documented allowlisted integer code or null, and a
0..86400 integer delay or null. Invalid/duplicate extensions are ignored without
losing the primary HTTP diagnosis. Display uses `zai_code` and
`retry_after_seconds`; provider prose is never relayed. Any such failure marker
on successful exit remains a protocol error. Native retry/replay rules do not
change. Upgrade the bridge before the native binary; previous binaries remain
compatible but cannot supply the new detail.


## Restart and failure recovery confirmation (#1667)

When long-task mode is enabled, startup inspects this conversation's saved
Danso journal without provider credentials or a model request. A failed or
unfinished task gets a bounded, credential-redacted summary and three buttons:

- **이어서 진행 (Continue):** at a resumable ready/paused checkpoint, authorize
  one native no-prompt resume. Otherwise preserve the old journal and start a
  separate session with bounded historical excerpts and an instruction to
  inspect actual results first, then do only remaining authorized work. The
  excerpts are reference data, not proof of success or fresh instructions.
  An incomplete goal must be clarified with the user rather than guessed.
- **새 작업 시작 (New task):** reset only this conversation's session binding;
  keep the original journal and wait for a new user message. No model call.
- **상태만 확인 (Inspect):** reread the native state and show the summary.
  No model call, journal mutation, replay, or acknowledgement of uncertain work.

The same offer appears after a failed Danso turn on every interactive input path —
a normal message, a numbered `opt:` choice, and `/task_resume` (#1690) — including
a provider timeout or explicit pause. Always after the failure text, never instead
of it: the response the user answered to is delivered first. Utility commands that
happen to call the model (`/skills`, `/command`) deliberately do not offer: they
are one-shot lookups where a fresh retry is user-driven and a queued recovery
offer would only add noise; this was reviewed when wiring the two paths above and
the offer set stays closed until a path is observed to strand a failed long task.
`/task_recover` requests a fresh offer on demand.
Completed tasks and sessions without a long-task ledger do not trigger startup
offers. An unreadable, malformed, or actively locked journal produces no advice;
use `/task_recover` again after the active writer finishes. The current native
`--task-status` contract is required; optional `DANSO_RECOVERY` from Danso #90
also improves the fallback error message. Older binaries without that optional
record keep the existing generic error and never authorize execution from text.

Startup notification is capped at 100 stored rows, 10 sends, and 10 seconds;
remaining or temporarily unreadable sessions can use `/task_recover`. Delivery
is deduplicated by the unchanged journal fingerprint, with a retry after a
failed send. A crash after delivery but before the marker commits can duplicate
a notification, but cannot dispatch work. Shared-group/shared-all sentinel keys
have no unambiguous owner recipient and are skipped at startup; an authorized
user can request an offer from the actual chat with `/task_recover`.

Choices bind the owner, chat, provider, memory audience, saved session, and a
fingerprint obtained from matching locked reads around native validation.
Queue-time revalidation and atomic one-shot claims reject stale/duplicate clicks.
A new message, `/new`, or provider/audience switch invalidates old dispatches;
identity persistence and native launch recheck the conversation generation.
Only explicit Continue can dispatch work. Automatic checkpoint resume is not
enabled by this feature, and the provider's existing bounded HTTP retry policy
is unchanged. An uncertain tool effect is never silently marked settled.

The summary labels the last agent note as unverified and counts durable tool
completion records; it does not invent a completed/remaining checklist. Raw
transcripts and provider response bodies are not copied into session state.
Only the offer token, route/identity binding and content fingerprint are saved
through the existing private, atomic session store. This is a source change;
production rollout and restart are separate from merging it.

### Provider interruption recovery (native Danso #99)

With long-task mode enabled, an existing journal is assessed through the
provider-free `--task-status` path before a normal turn is dispatched. Unfinished
or invalid state returns recovery guidance without starting a model/tool worker;
a new user message never becomes an implicit continuation of the saved objective.
Completed/failed tasks and fresh sessions retain their existing behavior. Native
runtime locking and full journal validation remain authoritative at dispatch.

The adapter accepts both legacy status and the additive native recovery
assessment. A durable provider-only cancellation can be explicitly resumed using
`/task_resume` or the existing Continue choice; `/stop` never initiates a resume.
The assessment preserves cumulative limits and exposes unknown token usage. The
recovery summary explains that a repeated model request may add cost. Pending
legacy/SIGKILL journals and uncertain tool effects remain blocked. Native and
adapter changes must both be installed before relying on the new assessment;
this source change does not migrate journals or deploy a node.

The bridge's session filename UUID and native journal-header UUID are independent.
Status binding reads the selected owner-only journal under the native shared lock,
checks its header UUID and workspace, and requires the same bytes and inode after
native inspection. It never searches another filename/audience to make an ID fit.
The parser rejects state/pending contradictions even when resume is false, so a
claimed terminal state cannot hide a pending request and authorize a new turn.
