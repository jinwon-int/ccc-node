# Matrix frontend (family messenger) — `CCC_CHANNEL=matrix`

Status: #1780 in progress. PR-1 (#1781) landed the transport-neutral seams;
PR-2 adds the frontend (`bridge/core/matrix/`); PR-3 is the seoseo cutover.

The Matrix frontend runs the **same** `ProjectChatHandler` as the Telegram
bridge — persistent per-room sessions, draft/interim progress, `/new /model
/effort /usage /skills /stop`, memory materializer, working state — behind the
E2EE Matrix transport ported from `jinwon-int/family-messenger`
(`scripts/fleet_matrix.py`, `fleet_core.py`, `fleet_matrix_state.py`). What
changed versus the family-messenger pilot bot ("fambot"):

| | fambot (family-messenger `fleet_worker`) | `CCC_CHANNEL=matrix` |
|---|---|---|
| brain | one Codex process per turn, JSON port | `ProjectChatHandler` in-process (as Telegram) |
| session | `session_id` per room, cold start each turn | warm `AgentSession` per room |
| progress | "작업을 시작했습니다" only | typing + interim/status notices + the Telegram session-start banner (`◐ CCC session started (<reason>)…`) whenever a turn opens a fresh provider stream |
| commands | `/cancel /ack /approve /deny` | + `/new /model /effort /usage /skills /stop` (`/ack` gate removed: interrupted turns end with a notice, like Telegram) + `/task_pause /task_resume /task_recover` (#1895, Danso long-task mode only) + `/history /resume` (#1895 PR-B) |
| output | plain `m.text` | plain `body` + Matrix HTML `formatted_body` |
| E2EE / trust / room gate | fleet_matrix | same code, ported (fail-closed reasons unchanged) |

## Install

```sh
cd bridge && pip install -r requirements-matrix.txt   # matrix-nio[e2e] (needs libolm)
```

Telegram-only nodes never import `nio`; the transport imports it lazily.

## Configuration

Environment (same `.env` as the Telegram bridge may be reused; the Matrix
service overrides two keys in its unit):

| key | value |
|---|---|
| `CCC_CHANNEL` | `matrix` |
| `CCC_MATRIX_CONFIG_PATH` | private 0600 JSON (below) |
| `CCC_AGENT_PROVIDER`, `CCC_CODEX_*`, memory keys | as the Telegram bridge on the node |

`CCC_MATRIX_CONFIG_PATH` JSON — identical to family-messenger's
`/etc/family-matrix/config.json` minus the worker keys (`worker_argv`,
`worker_argv_family`, `remote_worker` are ignored on upgrade):

```json
{
  "homeserver": "https://matrix.example.invalid",
  "account": "@agent:matrix.example.invalid",
  "device_id": "AGENT_DEVICE",
  "access_token": "PRIVATE_TOKEN",
  "pickle_key": "GENERATE_A_PRIVATE_RANDOM_KEY_AT_LEAST_24_CHARACTERS",
  "state_directory": "/var/lib/family-matrix-pilot",
  "owner": "@owner:matrix.example.invalid",
  "rooms": ["!PRIVATE_ROOM:matrix.example.invalid", "!FAMILY_ROOM:matrix.example.invalid"],
  "devices": {"OWNER_DEVICE": {"ed25519": "43_BASE64_CHARACTERS", "curve25519": "43_BASE64_CHARACTERS"}},
  "family_rooms": ["!FAMILY_ROOM:matrix.example.invalid"],
  "family_users": ["@dad:matrix.example.invalid"],
  "family_devices": {"@dad:matrix.example.invalid": {"DAD_DEVICE": {"ed25519": "…", "curve25519": "…"}}},
  "not_before_ms": 1788825600000,
  "mention_aliases": ["seoseo"],
  "wake_words": ["서서", "서서야"],
  "turn_timeout_minutes": 360,
  "identities": {"@owner:matrix.example.invalid": {"master": "43_BASE64_CHARACTERS"}}
}
```

The frontend also consumes the channel-neutral push spool
(`CCC_PUSH_SPOOL`, default `~/.claude/state/telegram-spool`) when
`CCC_PUSH_ENABLED` is set for the **matrix service** — self-update,
fleet-alert and agent-cron records are delivered to the owner's direct
room (fallback: family room) with the same dedup/rate/archive semantics
as the Telegram notifier. On a node running both frontends, set the
override on each unit (`Environment=CCC_PUSH_ENABLED=true` on the matrix
unit, `=false` on the telegram unit) so exactly one process consumes the
spool; real environment beats the shared `.env`.

`turn_timeout_minutes` (optional, default 360 — 6 h —, allowed 5–360) caps one
running turn; a timed-out turn still resolves uncertain exactly as
before — only the ceiling moves. Set it to 360 (6 h) for genuinely long
work (owner request 2026-09-18).

Room policy is unchanged from the pilot: a direct room is exactly
`{owner, bot}`; a family room admits only the allowlisted family users and
answers only when addressed (`m.mentions`, a typed `@localpart`, or a typed
`@<alias>` from `mention_aliases` — Matrix ids cannot be renamed, so a bot
that is *displayed* as "seoseo" is reachable as `@seoseo` this way).

`wake_words` (optional, up to 8, `[0-9A-Za-z가-힣]{1,64}`) widens the same
gate to **bare** whole-token nicknames without a leading `@` — for Korean
display names that have no typed handle (the family calls the bot "서서" or
"서서야"). Matching is whole-token only, so a wake word never fires inside a
longer word ("서서" does not match "서서히"); a token glued to another word
without a space ("서서야뭐해") does not match either. Aliases only widen the
mention gate — sender and room admission are unchanged.

**Replies carry their parent (#1943).** When someone uses the client's reply
feature, the agent receives the replied-to message as a quoted
`[Reply context: …]` excerpt (≤2000 chars) above the reply text. The parent
comes from a bounded in-memory cache of recent trusted texts (the bot's own
answers included) or is fetched and decrypted on demand; only encrypted
parents from the bot or a trusted device of an allowed sender are quoted.
A missing, plaintext, untrusted or undecryptable parent simply leaves the
reply as-is. `/command` and bare-number replies are never rewritten. A reply
in a family room still needs a mention, exactly like any other message.

### Device trust: cross-signing identity (preferred) or pinned devices

`"identities": {"@owner:hs": {"master": "<ed25519 master key>"}}` pins the
user's **cross-signing master key** instead of a device list
(family-messenger #149). On every `keys/query` the transport checks that the
master key is unchanged, that the self-signing key is signed by it, and
trusts exactly the devices the self-signing key has signed (nio
`verify_json` over canonical JSON). Logging in, logging out, deleting a
device or verifying a new one in the app therefore never stops the service;
unsigned devices are blacklisted for key sharing and a message sent from
one is ignored with a one-time notice ("기기 검증을 마친 뒤 다시 보내
주세요"). Only a changed master key (account reset) stops the service
(`owner-identity-changed`; `cross-signing-missing` / `cross-signing-invalid`
for a broken chain). With an identity, `devices` may be `{}`.

Users without an identity keep the pinned-device rule (`devices`,
`family_devices`):

- **Owner** — any change to the owner's device *set* (a new device or a
  signed-out pinned one) or a pinned *key* stops the service
  (`owner-device-set-changed`, `owner-device-key-changed`) until an
  operator re-pins. An owner message from a device outside the (unchanged)
  pinned set is anomalous key material on the operator's own channel and
  also stops the service (`unverified-owner-event`).
- **Family member** (#1958) — an *extra* unpinned device (a new phone) and a
  pinned device that is *gone* (signed out or deleted) are both contained:
  a message — text or media — from such a device is never processed, the
  room gets one notice per device ("등록되지 않은 기기에서 보낸 메시지는
  처리하지 않습니다 … 운영자에게 기기 등록을 요청해 주세요"; deduplicated
  through meta `untrusted_senders`), and the sync batch still commits, so
  the owner's room and every other room keep working and nothing is
  replayed on restart. A missing pinned device leaves the trusted set (family
  room sends stop expecting it) and is listed in meta `family_pins_missing`
  (logged once per change); it is trusted again if it reappears with its
  pinned keys. In-app verification cannot fix pin mode; the operator re-pins
  (or moves the user to `identities`). A pinned family device id that
  presents *different* keys still stops the service
  (`pinned-device-key-changed`): clients never re-key a device id (a new
  login is a new device), so that is key injection by the homeserver — the
  same one that serves the owner's device list — and needs an operator.

**Re-pinning** (`devices`, `family_devices`, `identities`). The saved policy
(`meta.policy`) is fail-closed against config edits (`saved-policy-changed`),
so never edit it in the database; use the audited `repin` command:

1. Read the new device keys (`keys/query` from the bot token:
   `device_keys[user][device].keys`) or, for cross-signing, the master key
   (`master_keys[user].keys`; then set `devices` to `{}` for the owner).
2. `systemctl stop ccc-matrix-bridge` — the state store is single-process
   and locked while the service runs (the command exits 3 otherwise).
3. Edit only the pins in the 0600 config (`CCC_MATRIX_CONFIG_PATH`).
4. `<venv>/bin/python -m telegram_bot.core.matrix.repin --config <config>
   --reason "<why>"` (same venv and `WorkingDirectory` as the unit's
   `ExecStart`). It validates the config, refuses anything but a pin
   change (owner, rooms, family membership and `not_before_ms` reroute saved
   jobs and stay `saved-policy-changed`), refuses a no-op, then writes the new
   `meta.policy` and an `operator_audit` row (`action=repin`, actor, reason,
   before/after device ids with short key fingerprints — no key material or
   secrets) in one transaction and prints that record. Exit 2 = refused.
5. `systemctl start ccc-matrix-bridge`; the replayed batch (if any) is
   checked against the new pins.

Known gap: an `unverified-owner-event` stop with an *unchanged* owner device
set is not something a re-pin can clear (there is nothing to re-pin); the
failed batch stays in `meta.pending_sync` and needs a deliberate operator
decision to discard it after the device has been investigated.

## Identity mapping

`ProjectChatHandler` keys conversations by `(user_id: int, chat_id: int)`.
`core/matrix_ids.py` maps `@user:server` / `!room:server` to stable positive
ints (persisted at `<bot_data_dir>/matrix-ids.json`, 0600) above the
Telegram id range. A direct room reports the sender's int as `chat_id`, so
`session_scope.is_group_conversation` stays false (private memory/session
scope); a family room gets its own int (shared audience). Private memory
audiences are namespaced by `memory_route="matrix"`, so a Matrix user can
never collide with a Telegram user's private scope.

## Running alongside the Telegram bridge (seoseo)

The frontend is a second systemd service on the same node and project root
(`bridge/service-systemd-matrix.service.example`). The Matrix unit launches
the package directly — `<venv>/bin/python -m telegram_bot --path /root` —
with its own `BOT_DATA_DIR` (logs and the session store follow it),
`CCC_CHANNEL=matrix` and `CCC_MATRIX_CONFIG_PATH`. `start.sh` still owns the
Telegram unit (pid file, health file, token lock under `.telegram_bot`). Its
process oracle (`find_project_bot_pids`) matches `--path` **and**
`CCC_CHANNEL`, so a healthy Matrix frontend is not "already running" for
Telegram start/`--stop`/`reap_competing_pollers` (jingun 2026-09-18 crash
loop). It still reads the project `.env`
(`<project>/.telegram_bot/.env`) for provider and memory keys, so both
frontends run the same model, materializer and working-state files; they do
not share Telegram state or sessions. `MatrixBot.run()` is the blocking entry
`__main__` expects (access control + session store init, SIGTERM/SIGINT →
orderly stop).

Cutover from the pilot bot:

1. `<venv>/bin/pip install -r bridge/requirements-matrix.txt`; point
   `CCC_MATRIX_CONFIG_PATH` at the pilot config (0600) with the pilot's
   `state_directory` (same bot device, same crypto store — no new Matrix
   device). Worker keys in that file are ignored.
2. `systemctl stop family-matrix` (pilot) — one bot device must not run twice.
3. `systemctl enable --now ccc-matrix-bridge`; confirm health `ready` in the
   state store (`meta.health`) and a reply in the owner's direct room.
4. Family room: mention the bot from a family account; confirm the reply and
   that the disclosure notice was not re-posted.
5. `systemctl disable family-matrix`; keep its backups.

Rollback: stop `ccc-matrix-bridge`, start `family-matrix` — the pilot's
config, state and crypto store are untouched by the frontend.

## Provisioning a bot account for another node (e.g. `@jingun` on jingun)

Every node gets its own Matrix account, device and private room with the
owner; nodes never share a token or crypto store. Family rooms admit exactly
one bot (the room gate mutes a family room that contains a second bot), so a
second node starts with a direct room only.

1. **Account** — on the homeserver admin room: `!admin users create-user
   <localpart>`; capture the generated password into a 0600 file, never into
   a transcript. Set a display name (`PUT /profile/<id>/displayname`).
2. **Login on the node** — `POST /_matrix/client/v3/login` (password login)
   from the node itself → `device_id` + `access_token`; write them with a fresh
   random `pickle_key` (≥24 chars) into `CCC_MATRIX_CONFIG_PATH` (0600).
   `state_directory` is a new private directory (e.g. `/var/lib/ccc-matrix`).
3. **Room** — the bot creates an encrypted private room and invites the owner
   (`createRoom` with `preset: private_chat`, `m.room.encryption`
   `m.megolm.v1.aes-sha2`, `invite: [owner]`); the owner accepts in the app.
   Put the room id in `rooms` (not in `family_rooms`).
4. **Pins** — `devices` = the owner's current device keys (same set the other
   node pins; `keys/query` from the bot token), `not_before_ms` = now.
5. **Extra + unit** — `pip install -r bridge/requirements-matrix.txt`
   (needs `libolm3`/`libolm-dev`), install
   `bridge/service-systemd-matrix.service.example`. If the node's Telegram
   unit sets the provider through `Environment=` lines (Piri on jingun),
   mirror them into a `ccc-matrix-bridge.service.d/provider.conf` drop-in
   (recipe in the example file) — otherwise the Matrix frontend runs the
   default provider.
6. **Initialize once** (after the owner accepted the invite — a direct room must be exactly {owner, bot}) — `CCC_MATRIX_INITIALIZE=1 BOT_DATA_DIR=… CCC_CHANNEL=matrix
   CCC_MATRIX_CONFIG_PATH=… <venv>/bin/python -m telegram_bot --path <root>`:
   creates the crypto store, uploads keys, pins devices, gates the room, exits.
   A normal start refuses an empty store (`explicit-new-device-initialization-required`).
7. `systemctl enable --now ccc-matrix-bridge`; expect the startup banner in
   the direct room.

## Photos and files (#1795)

Encrypted `m.image` / `m.file` / `m.video` / `m.audio` events are admitted
under the text rules — decrypted, from an allowed sender's pinned/verified
device, inside the 24 h window, not an edit — and reach the agent with the
same prompt contract as Telegram (a local path in the prompt):

- **Admission** — direct rooms accept a photo with or without a caption; a
  family room needs an explicit address in the caption (`@handle`, alias,
  wake word) or an `m.mentions` pill, so a caption-less photo there is
  ignored. Per Matrix v1.10 the `body` is a caption only when `filename` is
  present and differs from it. Plaintext media (a bare `url`, no
  `EncryptedFile`) is refused. An unverified device is ignored (with the
  existing cross-signing notice, or the pin-mode "unregistered device"
  notice for a family member) — media never adds a `SafetyStop` path. A
  media event the transport sees but does not admit is logged body-free as
  `matrix media ignored reason=… kind=…` and kept in meta `media_ignored`;
  an event nio cannot parse at all (e.g. a malformed `file`, which nio turns
  into a `BadEvent`) is dropped before admission, as before.
- **Queue** — the job keeps the caption as its body (`(attachment)` when there
  is none) plus the `EncryptedFile` in the `jobs.attachment` column (added by
  an idempotent migration); the caption stays the job body, so a long caption
  is not bounded by the attachment JSON cap. The column — it holds the
  decryption key — is cleared as soon as the turn has a result or is left
  uncertain, including a turn left `running` by a crash (cleared when the
  store reopens). A caption
  that looks like `/stop` is still an attachment turn, never a control.
- **Staging** — the runner downloads the ciphertext from the authenticated
  `/_matrix/client/v1/media/download/{server}/{id}` (no redirects), verifies
  the SHA-256, AES-256-CTR decrypts it (`cryptography`), and writes it to
  `<BOT_DATA_DIR>/matrix-media/document_<hex>.<ext>` (0700 dir, 0600
  `O_EXCL` file — the Telegram document helpers). Limits reuse the Telegram
  settings: images use `CCC_TELEGRAM_MAX_IMAGE_BYTES` /
  `CCC_TELEGRAM_MAX_IMAGE_PIXELS` when `CCC_BRIDGE_IMAGE_CONTEXT_GUARD` is on,
  everything else `CCC_MAX_DOCUMENT_SIZE_MB`; the declared size is checked
  before downloading. The file is deleted when the turn ends (also on
  failure); a file left by a killed process is swept (older than 1 h) on the
  next attachment.
- **Prompt** — images use `build_image_prompt(..., channel="Matrix")`, other
  media `build_document_prompt(..., channel="Matrix")`; the Telegram wording is
  unchanged. A download/integrity/size failure answers the room once and does
  not run the agent. The Grok frontend stays text-only.

## Not yet

- Voice transcription for `m.audio` (handled as a file today).
- Draft edits (`m.replace`) for streamed text — interim notices only.
- Approval buttons: approvals are `/approve <turn> <nonce>` replies in the
  room, exactly as the pilot.

## Danso long tasks and recovery (#1895 PR-A)

With `CCC_AGENT_PROVIDER=danso` and `CCC_DANSO_LONG_TASK_ENABLED=true` the
Matrix frontend reuses the Telegram recovery discipline
(`bot_danso_recovery.DansoRecoveryMixin`: one-shot claim, binding guard,
evidence-first continue) through a few channel ports:

- `/task_pause` asks the exact active native task to pause at a settled
  boundary; `/task_resume` re-issues the explicit no-prompt resume for the
  stored journal; `/task_recover` posts the recovery summary on demand.
- Offers are always the numbered **text** menu (`1` continue / `2` new /
  `3` status) delivered through the outbox; Matrix has no inline keyboards.
  Only the owner sees offers, and only the owner's typed `1`/`2`/`3` answers
  one — inside the served turn, with that room's typing/status/interim
  callbacks. Other senders' digits are ordinary messages.
- A failed Danso turn is followed by the offer, after the failure text.
- The restart scan never dispatches by itself. With
  `CCC_TELEGRAM_DANSO_RECOVERY_AUTO_RESUME=true` an eligible journal
  (`ready`/`paused`, `resume_allowed`, not yet auto-resumed at this
  fingerprint) is handed to the transport as a **self-job** (`$self-…` event
  in the owner's scope, idempotent per fingerprint); it then runs as a normal
  turn — claim, room sink, finish — and inside that turn the shared automatic
  path posts the notice, claims the offer once and issues the explicit resume,
  with the same stale-lock retry as Telegram (#1888). Off, or ineligible, the
  scan just offers the menu; answer `1`.

## External waits — CI promises from a Matrix room are watched (#1934)

`gh-ci-wait` registrations made from a Matrix conversation are polled, not
just recorded. Previously the CLI answered `ok` and wrote the record while
nothing ever read it (`ExternalWaitMonitor` was Telegram-only), so the CI
rollup and the promised continuation silently never arrived. The Matrix
frontend now runs the shared `ExternalWaitMonitor` in its serve loop:

- **Registry** — this frontend's own home, `BOT_DATA_DIR/external-wait/` —
  exactly the directory the agent-side CLI resolves through
  `CCC_EXTERNAL_WAIT_HOME`; Telegram and Matrix registries stay separate.
- **Notifications** ride the room notice path, so the CI rollup lands in the
  conversation that registered the wait.
- **Continuation** is a durable **self-job** (`$self-…` turn in the waiting
  room, idempotent per `wait_id`, same mechanism as Danso auto-resume
  above): the resumed turn resolves the ordinary session, streams through
  the room sink and finishes like any answer. A restart drains the pending
  self-job instead of losing the wake.
- A wait whose session has moved on (`/new`, provider switch) is notified
  only — the stale promise is never injected into a new session (#740).

Flags: `CCC_EXTERNAL_WAIT_ENABLED` (default on), `CCC_EXTERNAL_WAIT_RESUME`
(default on), `CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP` (default 10 continuations
per day; beyond the cap the rollup is still delivered, only the
auto-continuation is skipped).

## Grok (`CCC_AGENT_PROVIDER=grok`) — owner direct room, opt-in family rooms

With the Grok provider, `CCC_CHANNEL=matrix` selects `core/grok_matrix_bot.py`
(`GrokMatrixBot`) instead of `MatrixBot`: the same restricted contract as the
Grok Telegram frontend ([GROK-BOT-PROVIDER.md](GROK-BOT-PROVIDER.md)) — text
only, one turn at a time, no `/new`/model/effort/approvals/history/files, the
persisted Grok journal as the only session authority — served through the
unchanged E2EE transport, room gate and event admission.

- **Direct room by default.** The Grok journal binds exactly one owner
  conversation. A config with `family_rooms` or `family_users` is refused
  before the transport opens (`grok_matrix_direct_room_only`) unless
  `CCC_GROK_MATRIX_FAMILY_ROOMS=1`; a message from any other sender, room
  kind or unlisted room gets a static denial and never reaches the journal or
  the Bot.
- **Family rooms (opt-in, `CCC_GROK_MATRIX_FAMILY_ROOMS=1`).** The listed
  `family_rooms` are served to the owner and the allowlisted `family_users`
  through the unchanged family gate: pinned `family_devices`/`identities`, and
  the bot must be addressed (`m.mentions`, a typed `@grok`, or a typed
  `@<alias>` from `mention_aliases`). What does not change: **every admitted
  family prompt enters the owner's one Grok conversation** (the family shares
  the owner's context and the exchange is visible in the owner's Grok app),
  one turn at a time across all rooms (`BUSY` otherwise), text only, and the
  reply is committed to the journal before the outbox posts it to the room it
  came from. The startup banner is still posted to the owner's direct rooms
  only. A family room admits exactly one bot (room gate), so the Grok bot must
  be that room's only bot.
- **Startup gates after the device authenticated:** single local Grok
  frontend (the same abstract socket as the Telegram frontend — Telegram and
  Matrix cannot serve the same Bot at once), persisted journal
  (`start_or_resume`), qualified host version, idle Bot. A failed gate closes
  the transport and exits; nothing is reset.
- `/status` describes the attachment; `/stop` and `/cancel <turn>` are the
  transport's controls and only cancel local waiting (the Bot's remote tools
  are not stopped). `CCC_MATRIX_INITIALIZE=1` provisions the bot device and
  exits without opening the journal, as for `MatrixBot`.
- The route still needs the Telegram identity keys (`TELEGRAM_BOT_TOKEN`,
  `CCC_GROK_TELEGRAM_BOT_ID`, `CCC_GROK_OWNER_ID`): they are the journal's
  immutable binding label, not a Telegram connection. No `getMe`/webhook
  check runs; the Matrix login and pinned owner devices are the identity gate.
- No streaming, typing/status bubbles, health.json tick or spool notifier:
  the reply is delivered by the outbox once the journal has committed it.

## health.json for the Matrix frontend

The Matrix unit writes `<BOT_DATA_DIR>/health.json` and `bot.pid` like the
Telegram bridge: bound to its own data dir at startup, `service`/`agent` marked
on start and stop, and a 10 s tick that records transport liveness (the
`telegram` block means the Matrix sync transport in this file) plus the in-flight
workload (`workload.turn_occupancy`, `active_requests`, `waiting_for_turn`).
Before this, the shared handler only recorded *turn* events, so an idle frontend
left the file frozen at its last turn and fleet freshness checks could not tell
idle from dead (observed 2026-09-21 on two nodes, stale since 09-19).

## /history and /resume (#1895 PR-B)

Same provider rules as Telegram, plain text: `/history` shows the last five
transcript messages for Claude (`get_recent_messages`) and Codex/Crush
(`read_runtime_session`); Piri and Danso resume by exact id and expose no
bounded history. `/resume` lists Codex/Crush runtime threads or Claude
sessions and stores a `resume_list`; the next **digit** reply switches to that
entry (provider mismatch and out-of-range are refused), any other reply clears
the list and is served normally. Claude browsing stays locked while private
memory is `audience-scoped` (`/new` instead); Danso reports the current
auto-resuming session; Piri accepts `/resume <session-id>`.

## Automatic memory writeback

Matrix and Telegram share `MemoryDistillMixin`: `/new`, provider changes,
automatic session expiry, opted-in completed-turn checkpoints and bounded
shutdown all enqueue the departing/current session in the channel's durable
`distill-journal`. Matrix also accepts `/distill` for an explicit queued save.
Matrix queues the departing session before `/model` changes its provider,
`/resume` selects another session, or `/skills` starts a new one. Successful
`/skills` responses participate in the same opted-in checkpoint policy.
The same snapshot, budget-gated extraction, audience-local sink and local Wiki
candidate workers run while the Matrix transport serves. Closing the transport
cancels its background workers and queues only bounded shutdown receipts; it
does not wait for an AI extraction. Private Matrix audiences retain the
`matrix` namespace; family rooms use the existing shared policy.

The current policy still applies: checkpoint thresholds default to zero; set
`CCC_MEMORY_DISTILL_CHECKPOINT_TURNS`, `_BYTES` or `_AGE_SECONDS` to opt in.
Extraction requires the configured provider budget. Local audience writeback
requires `CCC_BRIDGE_MEMORY_MODE=audience-scoped`; this change neither enables
it nor merges private memory stores. In unscoped Codex deployments, the existing
nunchi Codex feed independently collects the common Codex transcript tree.
`off` for `CCC_MEMORY_DISTILL_PROVIDER` or the global `distill.disabled` marker
prevents new bridge jobs. Wiki candidates remain local pending-review records.
