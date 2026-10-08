- **Fleet alert bot, relay host side (#2182, part 2).** `scripts/fleet-alerts/`
  adds the receiver for the bridge's relay mode (`/v1/alerts`: HMAC
  verification, per-node secret file = allowlist, replay skew, body cap,
  owner-only SQLite queue with a dedup window so a node's retry after a lost
  2xx never doubles a message) and the send-only `@fleet-alerts` Matrix
  sender (ccc-node E2EE transport, inbound admission disabled, stable txn
  ids) that drains the queue into the owner's "🔔 플릿 알림" room, with
  systemd unit examples, a provisioning/rollback runbook and hermetic tests
  including a bridge-client ↔ receiver round trip.
