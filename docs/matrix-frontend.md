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
| commands | `/cancel /ack /approve /deny` | + `/new /model /effort /usage /skills /stop` |
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
  "not_before_ms": 1788825600000
}
```

Room policy is unchanged from the pilot: a direct room is exactly
`{owner, bot}`; a family room admits only the allowlisted family users and
answers only when addressed (`m.mentions` or a typed `@localpart`). Owner
and family devices are pinned; any change to the owner device *set* or a
pinned device *key* stops the service fail-closed (`owner-device-set-changed`,
`owner-device-key-changed`) until an operator re-pins — see
family-messenger #149 for the cross-signing-trust replacement.

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

The frontend is a second systemd service on the same node and project root.
Both services share the provider CLI, memory materializer and working-state
files; they do not share Telegram state. Cutover from the pilot bot:

1. Install the extra into the bridge venv; copy the pilot config to
   `CCC_MATRIX_CONFIG_PATH` (0600) and point `state_directory` at the pilot's
   state (same bot device, same crypto store — no new Matrix device).
2. `systemctl stop family-matrix` (pilot) — one bot device must not run twice.
3. Start `ccc-matrix-bridge` with `CCC_CHANNEL=matrix`; confirm health
   `ready` in the state store and a reply in the owner's direct room.
4. Family room: mention the bot from a family account; confirm the reply and
   that the disclosure notice was not re-posted.
5. Disable the pilot unit; keep its backups.

## Not yet

- Image/file input (Telegram folds images into the prompt; Matrix media is
  E2EE and needs the attachment path from family-messenger).
- Draft edits (`m.replace`) for streamed text — interim notices only.
- Approval buttons: approvals are `/approve <turn> <nonce>` replies in the
  room, exactly as the pilot.
