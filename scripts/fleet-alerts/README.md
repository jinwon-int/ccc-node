# Fleet alert bot (`@fleet-alerts`) — relay host side (#2182)

Every node's Matrix frontend used to post its push spool (self-update,
agent-cron, fleet-watch, health alerts) into the owner's direct room **as the
agent**, between the agent's own progress bubbles. With the bridge's relay
mode (`CCC_PUSH_FLEET_RELAY_URL`, see `docs/matrix-frontend.md`) each node
sends those records here instead, and one dedicated bot posts them into a
single **🔔 플릿 알림** room. Agent rooms stay quiet.

```
node bridge (matrix unit, relay mode)
  └─ signed POST /v1/alerts ──▶ fleet_alerts_receiver.py  ──▶ owner-only SQLite queue
                                 (Tailscale bind, HMAC,          /var/lib/fleet-alerts/queue
                                  per-node secret = allowlist)         │
                                                              fleet_alerts_sender.py
                                                                (@fleet-alerts, E2EE, send-only)
                                                                        ▼
                                                              owner room "🔔 플릿 알림"
```

Telegram delivery is unchanged (stage 2).

**「한데」 lane (#2413 / family-messenger#460).** The owner is moving the fleet
alert room from Matrix to 「한데」. `fleet_alerts_sender_hande.py` is a second,
independent drain of the same queue: an `AFTER INSERT` trigger mirrors every
enqueued alert into `hande_alerts`, and the lane hands each row to a dedicated
relay bot (`native-mls-bot … steward <device> --responder-unix <sock>`) as a
FLEET-WORKER `notice` frame over a Unix socket. During the transition both
senders run (every alert in both rooms); afterwards the Matrix sender is
retired. See "「한데」 lane" below.

## Files

| File | Role |
|---|---|
| `fleet_alerts_receiver.py` | HTTP receiver: verifies `X-Fleet-Node` / `X-Fleet-Timestamp` / `X-Fleet-Signature`, enforces the per-node secret allowlist, replay skew and body cap, queues the formatted text. stdlib only. |
| `fleet_alerts_sender.py` | Send-only Matrix transport (ccc-node `MatrixTransport` with inbound admission disabled) draining the queue into the one configured room with a stable txn id. Needs the bridge venv's matrix extra. |
| `fleet_alerts_sender_hande.py` | 「한데」 lane: mirror table + trigger on the queue (`--install-only` / `--uninstall` / `--status`), Unix-socket FLEET-WORKER port for the relay bot (answers `turn` with an empty result, pushes paced `notice` frames). stdlib only. |
| `fleet_alerts_policy.py` | Tier policy (stage 2): info digest at 09:00 KST, action/critical re-notify once after 15 min; derived `digest`/`renotify` rows, `meta` table for the digest day. Pure queue operations, no network. |
| `fleet_alerts_receipt.py` | Operator read of one alert's receipts by alert id or txn (local, metadata only, exit 3 when unknown). |
| `fleet_alerts_outbox.py` | Shared owner-only queue (0700 dir, 0600 files, flock, dedup fingerprint window, bodies wiped on delivery; v2: `alert_id`, `tier`, `receipts` — migrated in place on first open). |
| `*.service.example` | systemd units for both processes. |
| `fleet_alerts_test.py` / `fleet-alerts.test.sh` | Hermetic tests (bridge client ↔ receiver round trip included). |

## Contract (`ccc.fleet-alert.v2`, v1 accepted)

`POST /v1/alerts`, JSON body built by `bridge/core/fleet_alert_relay.build_payload`;
signature `sha256=HMAC-SHA256(secret, "<unix-ts>." + body)`.

**v2 (#2413, field-additive).** The body carries `tier` — `info` (default) /
`action` (a person should look) / `critical` — and the receiver accepts both
`ccc.fleet-alert.v1` and `.v2` (an old node simply sends no tier → `info`).
Every queued alert gets a 6-char **alert id** (base36, derived from the txn,
unique in the queue) that the owner can quote back (`ok <id>` / `hold <id>`,
later stage); the queued text starts with `[<name>] #<id> …`. Each channel
records a **delivery receipt** in the queue: the Matrix sender's event id
(`matrix-event`, strong) and the 「한데」 lane's hand-off (`socket-write`, weak —
the relay bot has no notice ack). `notified` = at least one strong receipt;
an `action` alert without one must be treated as *not* notified (fail-closed
for L2 pre-notification, a2a-nexus#2317). Read receipts on the relay host:

```bash
python3 fleet_alerts_receipt.py --queue /var/lib/fleet-alerts/queue <alertId|txn>
# → {"alert_id":…,"tier":…,"node":…,"event":…,"receipts":[{"channel","kind","ref","at"}…],"notified":bool}
```

Producers set the tier in the spool record (`tier` key): agent-cron marks a
non-success run and the failure alarm `action`, a success notice `info`;
records without the key are `info`.

**Delivery policy per tier (stage 2, `fleet_alerts_policy.py`, run by the
Matrix sender before every round):**

| tier | leaves the queue | re-notify |
|---|---|---|
| `info` | held; one **digest** row per day at **09:00 KST** (`📋 플릿 알림 다이제스트 <date> — info N건`, one `• HH:MM [node] #id first-line` per alert, max 60 lines); held rows get a `digested` receipt pointing at the digest id | — |
| `action` | at once | once, 15 min after the Matrix receipt, unless a `reply` receipt (`ok/hold <id>`, responder stage) exists — a derived `⏰ 재통지 #id …` row |
| `critical` | at once | like `action`, `🚨` |

Derived rows carry internal tiers `digest` / `renotify` that a node payload
can never set (`normalize_tier` maps unknown values to `info`). The 「한데」
mirror copies every non-`info` insert, so both rooms show the same thing: the
digest and the re-notify rows, never a held info alert. The daily digest is
made from the relay host's clock (`Asia/Seoul`); a day with nothing held is
recorded so the check stays cheap. Policy errors are logged and never block
`action`/`critical` delivery.

| Response | Meaning | Node side |
|---|---|---|
| `202 {"accepted":true,"txn":…,"duplicate":bool,"alertId":…,"tier":…}` | queued (or already queued inside the dedup window — same alertId) | archive record |
| `401` missing headers / bad timestamp / stale (> `--skew`, default 300 s) / bad signature | final | archive record |
| `403` node not in `--secrets-dir` (or its secret file unsafe) | final | archive record |
| `400` bad JSON / schema / `node` ≠ header / empty or oversize `text` | final | archive record |
| `413` body > `--max-body` (default 64 KiB) | final | archive record |
| `503` queue unwritable | transient | keep record, retry |

`GET /healthz` → `200 {"ok":true}` (no auth, body-free). Logs never contain
alert text — only node, event, txn and refusal reason.

**Sender prefix.** Every queued alert starts with `[<name>] ` so the owner sees
which agent it came from in the first line. `<name>` comes from the optional
`--labels` JSON file (`{"<node>": "<agent display name>"}`, kept on the relay
host — fleet names stay out of the repo); unknown nodes show their node id.
The file is re-read when it changes (no restart).

## Provisioning (one-time, owner approval; never record secrets in docs)

1. **Secrets.** On the relay host: `install -d -m 0700 /etc/fleet-alerts/nodes`.
   For each node generate a random secret (`openssl rand -hex 32`) and store it
   **twice**: `/etc/fleet-alerts/nodes/<node>` (0600) on the relay host and
   `~/.config/ccc-node/fleet-alert-relay.secret` (0600) on the node. The file
   name on the relay host is the node name the bridge sends
   (`CCC_PUSH_FLEET_NODE`, default `CCC_NODE` or the short hostname). A node
   without a file is refused (403); removing the file revokes it; replacing
   both files rotates it — no restart needed on the relay host.
2. **Receiver.** `install -d -m 0700 /var/lib/fleet-alerts`; copy
   `fleet-alerts-receiver.service.example` to `/etc/systemd/system/`, set the
   Tailscale bind, `systemctl enable --now fleet-alerts-receiver`; check
   `curl -s http://<bind>/healthz` from a node.
3. **Bot account.** Create a non-admin account `fleet-alerts` on the family
   homeserver (admin room `users create-user fleet-alerts`; password file
   0600, not printed), display name **🔔 플릿 알림**. Log in from the relay
   host to obtain `access_token` / `device_id`.
4. **Config.** `/etc/fleet-alerts/matrix.json` (0600): the usual Matrix
   config (`docs/matrix-frontend.md`) with `state_directory` =
   `/var/lib/fleet-alerts/matrix`, the owner's pinned devices (same set as
   the other bots), **exactly one** room in `rooms`, no family section.
5. **Room.** Run the sender once with `--initialize`, create the encrypted
   private room **🔔 플릿 알림** (bot + owner only), put its id in `rooms`,
   `systemctl enable --now fleet-alerts-sender`.
6. **Canary, then rollout.** Enable relay mode on one node first
   (`CCC_PUSH_FLEET_RELAY_URL=http://<bind>/v1/alerts`,
   `CCC_PUSH_FLEET_RELAY_SECRET_FILE=…` in the matrix unit's drop-in, idle
   restart), confirm one alert arrives in the room as the bot and nothing in
   the agent room, then repeat per node.

Owner-personal notices (important mail, schedule brief) can stay in the
node's own agent room: set `CCC_PUSH_FLEET_RELAY_DIRECT_EVENTS` to their
record `event` values on that node (#2227, see `docs/matrix-frontend.md`).
Its default is `SelfUpdate` (#2242): self-update notices go to each node's
own room, not this room. An explicit list replaces the default, so include
`SelfUpdate` in it to keep that.

Rollback per node = unset `CCC_PUSH_FLEET_RELAY_URL` and restart the matrix
unit (records go back to the owner room as before). The queue and the
bot's encryption state are never deleted or auto-reinitialised.

## 「한데」 lane

Prerequisites live in family-messenger#460 and are installed separately
(each step is an operator decision): a relay bot device enrolled for actor
`fleet-alerts`, the 「한데」 fleet alert room (bot-to-bot allowed room, #425),
and the bot unit (`native-mls-bot <relay-url> steward <device> --identity-file
… --rooms-dir … --access-jwt-file … --responder-unix /run/fleet-alerts/responder.sock`)
running as the user named in `--socket-owner` **with
`NATIVE_MLS_BOT_ROOM_FRAMES=1`** — an alert-mode room accepts `notice` frames
only on the `--room-frames` wire; on the single-room wire the bot drops them
(`agent_outbound_dropped`). Device naming: the bot posts its actor as the
device id up to the first hyphen and the relay checks it against the policy
actor (`commit_actor_mismatch` otherwise), so use a hyphen-free actor such as
`fleet` / device `fleet-bot`. Then:

1. `python3 fleet_alerts_sender_hande.py --queue /var/lib/fleet-alerts/queue --install-only`
   — adds `hande_alerts` + trigger `hande_mirror` under the queue lock. The
   receiver and the Matrix sender are not modified; alerts enqueued before this
   step are not mirrored.
2. Copy `fleet-alerts-sender-hande.service.example` to `/etc/systemd/system/`,
   set `--room` to the relay room id, `systemctl enable --now fleet-alerts-sender-hande`.
   Keep `RuntimeDirectoryMode=0755`: the bot user must traverse
   `/run/fleet-alerts` (the socket itself stays 0600).
3. Canary: one alert must appear in **both** rooms; `--status` shows the mirror
   backlog (0 when drained).

Delivery semantics: a mirror row is deleted when its `notice` frame is written
to the bot's socket — the bot has no acknowledgement for notices, so a bot
crash between the write and its relay POST can lose that one 「한데」 copy
(Matrix is unaffected). Per-channel receipts are #2413's follow-up. Rows older
than 24h are dropped (a stopped lane is not an archive). The lane never posts
on its own behalf in reply to room traffic: every `turn` gets an empty
complete `result`, approve/deny/tool frames are ignored, no AI runtime.

Rollback: `systemctl stop fleet-alerts-sender-hande`, then `--uninstall`
(drops trigger + table). Pausing only: stop the unit — mirror rows wait up to
24h.

## Operations

- Pending backlog: `python3 -c 'import fleet_alerts_outbox as o; print(o.pending_count("/var/lib/fleet-alerts/queue"))'`
  from `scripts/fleet-alerts` (the receiver keeps accepting while the
  sender is down; the sender drains in order on return).
- `systemctl stop fleet-alerts-sender` pauses Matrix delivery without losing
  alerts (the 「한데」 lane keeps draining its own mirror and vice versa); `stop fleet-alerts-receiver` makes nodes keep their records (503 /
  unreachable are transient for the bridge).
- The code runs from `/opt/ccc-node`; a self-update changes it on disk but
  both processes pick it up only on their next restart.
