# 「한데」 frontend (`CCC_CHANNEL=hande`)

Status: #2380 PR-2 — the frontend and its transport; node rollout, doctor /
self-update / fleet-watch awareness are PR-3. Decision and gates:
family-messenger `docs/DECISION-2026-10-19-AGENT-CHAT-TO-HANDE.md`, prerequisites
tracked in [family-messenger#420](https://github.com/jinwon-int/family-messenger/issues/420).

## What it is

The family messenger's native E2EE bot (`native-mls-bot`, family-messenger
`archive/native-mls/bot`) decrypts room messages and hands each one to a
*responder* over a unix socket, speaking the FLEET-WORKER JSON-lines port
(family-messenger `docs/FLEET-WORKER.md`). With `CCC_CHANNEL=hande` **the bridge is
that responder**: `core/hande/transport.py` listens on the socket, and
`core/hande/bot.py` (`HandeBot`, a `MatrixBot` subclass) runs the same
`ProjectChatHandler` turn the Matrix frontend runs — commands, the redacted approval
snapshot and audit ledger, session continuity, continuation, waits, Danso recovery,
health. Nothing changes on the bot side: it connects to a socket path and speaks the
port it already speaks (`--responder-unix` / `NATIVE_MLS_BOT_RESPONDER_UNIX`).

```
native-mls-bot (steward)  ──unix socket, FLEET-WORKER frames──▶  ccc-node bridge (CCC_CHANNEL=hande)
                                                                   HandeTransport ──TurnRunner──▶ HandeBot(MatrixBot) ──▶ ProjectChatHandler
```

Compared with the Rust `native-responder` + `scripts/ccc_adapter.py` path (which
calls the ccc-node runtime directly): same provider, same approval policy, plus
everything above. Nodes without a bridge keep the Rust responder.

## Mapping

| Port (bot ↔ responder) | Bridge |
|---|---|
| `turn.sender` `@<device>:hande` | actor = `<device>` up to the first `-` (the bot's own rule) → user `@<actor>:hande`. A person's devices share one conversation, like a Matrix user's devices |
| `turn.room` (when the bot sends it, family-messenger#423) | room id; absent → the config's first room. Sessions are keyed `(user, room)` |
| `turn.session_id` (always `null` today) | ignored — the session manager is the resume authority, exactly as on Matrix |
| `turn.prompt` starting with `/` | the Matrix command set (`new model effort usage stop continue history resume waits cancelwait distill skills memory_promote restart task_*`); owner-only ones refuse non-owners |
| `TurnSink.approval` | `approval{approval_id, action, description=<redacted snapshot>, arguments={}}` → `approve`/`deny` → `approval-resolved`; `approval_timeout_s` (default 120) → denied. Audit ledger as on Matrix |
| `TurnSink.typing` / `status` | no-op (the port has no typing or editable bubble) |
| `TurnSink.interim` | dropped and counted unless `interim_frames: true` (then an `interim` frame the bot must know) |
| `TurnResult` | `result{status complete\|uncertain, text, session_id[, reason]}`; a cancelled, timed-out or failed turn is `uncertain` and never re-run automatically |
| `enqueue_notice` (restart receipt, waits status, push spool, async completion) | needs the `notice` frame (family-messenger#423). Until then **counted and dropped** — `health_signals().notices_dropped` — never claimed delivered. Alerts stay on the Telegram fallback (owner decision ②) |
| steward room tools (`room_create/join/invite/remove/close/members/mode/status`) | served (PR-4): `HandeBot` exposes the bot's tool table as an in-process Claude Agent SDK MCP server (`mcp__steward__*`, `core/hande/steward.py`); each call is a `tool` frame → the bot validates, enforces owner-only, executes → `tool-result`. Read-only `room_status`/`room_members` run unasked (as #378); every tool with a side effect first asks the owner through the hande `approval` frame **inside the bridge** (PR-5) — same redacted snapshot and audit ledger as a provider approval, independent of the node's `CCC_BRIDGE_BASH_POLICY` (all eight are pre-allowed at the SDK level so `approve-each` nodes never double-prompt and `auto-approve` nodes never skip it). Claude provider only; `steward_tools: false` turns it off, `steward_tools_approve_all: true` makes read-only calls ask too. One call at a time (`busy`), `tool_timeout_s` (default 90) → `steward_timeout`, cancel → `cancelled` |

Limits follow the port: 64 KiB per inbound line, 512 KiB per outbound line, turn ids
`[A-Za-z0-9_-]{1,128}`; a malformed or oversize frame closes the connection (the bot
restarts and reconnects). One client at a time — a second connection while the first
is alive is refused. A client that drops mid-turn gets that turn cancelled; the bot's
inbox is the duplicate guard (it re-offers the seq after marking it uncertain).

## Configuration

`CCC_CHANNEL=hande` requires `CCC_HANDE_CONFIG_PATH`, a private (0600) JSON file:

```json
{
  "account": "@bridgenode:hande",
  "owner": "@owner:hande",
  "rooms": ["agent"],
  "family_rooms": [],
  "family_users": [],
  "socket_path": "/run/ccc-hande/responder.sock",
  "socket_mode": "0660",
  "socket_group": "native-relay-bot",
  "approval_timeout_s": 120,
  "interim_frames": false,
  "notice_frames": false,
  "tool_timeout_s": 90,
  "steward_tools": true,
  "steward_tools_approve_all": false
}
```

- `account` is this node (`@<node>:hande`, same spelling as its Matrix account
  localpart); `owner` / `family_users` are **actors** (`@owner:hande`), not devices.
- `rooms` are relay room names; `family_rooms ⊆ rooms` answer only mentions, the
  rest are direct rooms (one owner conversation each).
- `socket_path` is where the bot connects; `CCC_HANDE_SOCKET_PATH` overrides it, and a
  systemd socket unit (`LISTEN_FDS`) overrides both. `socket_group` must be the bot's
  group so it can connect; `socket_mode` never grants *other*.
- `BOT_DATA_DIR` (e.g. `~/.ccc-hande`) holds `sessions.json`, `duration.jsonl`,
  `health.json`, `hande-ids.json`-style id maps and the approval audit ledger, separate
  from the Telegram and Matrix bridges.
- `CCC_BRIDGE_RESTART_UNIT` defaults to `ccc-hande-bridge.service`; `/restart` follows the
  same handoff rules as Matrix.

Private memory audiences are namespaced by channel (`ProjectChatHandler.memory_route`),
so `hande` starts with an empty private digest; shared memory is unchanged.

## Operations tooling awareness (PR-3)

`start.sh` treats `CCC_CHANNEL=hande` like `matrix`: lifecycle actions from a hande
provider shell (or `--channel hande`) are refused (exit 10), `--channel hande --status`
renders that frontend's health, and `--channel telegram` drops the inherited
`CCC_HANDE_*` settings. The Telegram watchdog and fleet-watch exclude hande processes
(`CCC_CHANNEL=hande`, `ccc-hande-bridge.service` cgroup) from the Telegram match.
Self-update's idle gate reads `~/.ccc-hande/health.json`
(`CCC_SELF_UPDATE_HANDE_HEALTH_FILE`) when `ccc-hande-bridge` is in
`~/.claude/self-update.services`. Doctor reads `.ccc-hande` session stores and distill
journals. Add `hande` to `~/.claude/frontends` on nodes that run it.

## Deployment (per node)

1. `bridge/service-systemd-hande.service.example` → `/etc/systemd/system/ccc-hande-bridge.service`;
   optionally `bridge/ccc-hande-responder.socket.example` for socket activation.
2. Write the config (0600), create the socket directory (`/run/ccc-hande`, group =
   the bot's group, 0750).
3. Point the bot at the socket: `NATIVE_MLS_BOT_RESPONDER_UNIX=/run/ccc-hande/responder.sock`
   in the bot's drop-in (replaces the `native-responder.socket` path). **Restarting the
   bot and the Rust responder units is an operator change** — approval per node.
4. `systemctl enable --now ccc-hande-bridge`, then check `health.json`'s transport
   block: `connected: true` once the bot reconnected.

Rollback: point the bot back at `native-responder.socket`, stop `ccc-hande-bridge`.

## Not yet / gaps

| Gap | Why | Tracking |
|---|---|---|
| Unsolicited output (notices, restart receipt, waits status, push spool) | the bot does not speak a `notice` frame | family-messenger#423 (`notice_frames: true` once it does) |
| Multi-room per node | the bot serves one agent room and sends no `room` | family-messenger#423 (bridge already keys by `(user, room)` when `room` is present) |
| Progress lines / live preview | no `interim`/edit frame | family-messenger#423 (`interim_frames`) |
| Attachments, voice | the bot drops `file-v1-*` messages before the responder | family-messenger#354 follow-up |
| Termux restart script (`termux-restart-frontends.sh`) | Matrix-only; Termux nodes are last in the rollout | #2380 follow-up |
| doctor bridge-status probe for the hande unit | doctor reads `.ccc-hande` session/journal paths (PR-3) but has no hande-specific health verdict yet | #2380 follow-up |
