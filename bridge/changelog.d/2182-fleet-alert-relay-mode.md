- **Matrix spool notifier: fleet alert relay mode (#2182).** With
  `CCC_PUSH_FLEET_RELAY_URL` set, push-spool records (self-update, agent-cron,
  fleet-watch, health alerts) are forwarded to the central fleet alert relay
  as an HMAC-signed POST (`X-Fleet-Node` / `X-Fleet-Timestamp` /
  `X-Fleet-Signature`, secret from the owner-only
  `CCC_PUSH_FLEET_RELAY_SECRET_FILE`) instead of being posted into the owner's
  direct room *as the agent* — the room that also carries the agent's progress
  bubbles. Archive/dedup/rate-limit/fan-out semantics are unchanged; a
  transient relay failure keeps the record for the next cycle, a 4xx refusal
  archives it, and a misconfigured relay (missing or group-readable secret)
  keeps records and logs instead of falling back to the agent room. The owner
  room is not required in relay mode. Telegram delivery is untouched.
