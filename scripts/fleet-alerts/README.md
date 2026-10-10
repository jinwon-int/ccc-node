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

## Files

| File | Role |
|---|---|
| `fleet_alerts_receiver.py` | HTTP receiver: verifies `X-Fleet-Node` / `X-Fleet-Timestamp` / `X-Fleet-Signature`, enforces the per-node secret allowlist, replay skew and body cap, queues the formatted text. stdlib only. |
| `fleet_alerts_sender.py` | Send-only Matrix transport (ccc-node `MatrixTransport` with inbound admission disabled) draining the queue into the one configured room with a stable txn id. Needs the bridge venv's matrix extra. |
| `fleet_alerts_outbox.py` | Shared owner-only queue (0700 dir, 0600 files, flock, dedup fingerprint window, bodies wiped on delivery). |
| `*.service.example` | systemd units for both processes. |
| `fleet_alerts_test.py` / `fleet-alerts.test.sh` | Hermetic tests (bridge client ↔ receiver round trip included). |

## Contract (`ccc.fleet-alert.v1`)

`POST /v1/alerts`, JSON body built by `bridge/core/fleet_alert_relay.build_payload`;
signature `sha256=HMAC-SHA256(secret, "<unix-ts>." + body)`.

| Response | Meaning | Node side |
|---|---|---|
| `202 {"accepted":true,"txn":…,"duplicate":bool}` | queued (or already queued inside the dedup window) | archive record |
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

Rollback per node = unset `CCC_PUSH_FLEET_RELAY_URL` and restart the matrix
unit (records go back to the owner room as before). The queue and the
bot's encryption state are never deleted or auto-reinitialised.

## Operations

- Pending backlog: `python3 -c 'import fleet_alerts_outbox as o; print(o.pending_count("/var/lib/fleet-alerts/queue"))'`
  from `scripts/fleet-alerts` (the receiver keeps accepting while the
  sender is down; the sender drains in order on return).
- `systemctl stop fleet-alerts-sender` pauses Matrix delivery without losing
  alerts; `stop fleet-alerts-receiver` makes nodes keep their records (503 /
  unreachable are transient for the bridge).
- The code runs from `/opt/ccc-node`; a self-update changes it on disk but
  both processes pick it up only on their next restart.
