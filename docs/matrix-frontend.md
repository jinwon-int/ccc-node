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
| progress | "작업을 시작했습니다" only | typing + interim/status notices |
| commands | `/cancel /ack /approve /deny` | + `/new /model /effort /usage /skills /stop` (`/ack` gate removed: interrupted turns end with a notice, like Telegram) |
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
  "identities": {"@owner:matrix.example.invalid": {"master": "43_BASE64_CHARACTERS"}}
}
```

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
`family_devices`): any change to the device *set* or a pinned *key* stops the
service (`owner-device-set-changed`, `owner-device-key-changed`) until an
operator re-pins. Migration: read the master key from `keys/query`
(`master_keys[user].keys`), set `identities`, set `devices` to `{}`, and
update the saved policy (`meta.policy`: `devices`, `identities`) in the
state store before restarting — the policy comparison is fail-closed.

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

## Not yet

- Image/file input (Telegram folds images into the prompt; Matrix media is
  E2EE and needs the attachment path from family-messenger).
- Draft edits (`m.replace`) for streamed text — interim notices only.
- Approval buttons: approvals are `/approve <turn> <nonce>` replies in the
  room, exactly as the pilot.
