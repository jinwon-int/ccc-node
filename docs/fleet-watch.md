# Fleet bridge watcher

Run `bash scripts/fleet-bridge-watch.sh` from the installed ccc-node checkout.
Set `CCC_FLEET_DOCTOR=1` to inspect installed harness drift as well. The command
only inspects state; it does not restart a bridge or run scheduled tasks.
During the Telegram-to-Matrix transition, it checks both channels by default.
Set `CCC_FLEET_MATRIX=0` only for a Telegram-only diagnostic run.

The script streams `scripts/fleet_watch_metadata.py` with its Telegram probe
and runs `scripts/fleet-matrix-watch.sh`, which streams
`scripts/fleet_matrix_probe.py`. Keep these files together. A copied script
without its helper reports `UNVERIFIED`.
Do not leave a scheduled task pointing at an old one-file emergency snapshot
after the reviewed fix is integrated into the normal checkout.

## Distinct sources of evidence

- **Availability:** confirmed unavailable/absent is `DOWN`; an alive but
  unmanaged service, or one whose own health snapshot is degraded, is
  `DEGRADED reason=<source>` (`pid-bookkeeping-lost`, `health-stale`,
  `health:telegram-degraded`, …). A degraded node is re-probed once, without
  the doctor, after one shared wait (`CCC_FLEET_DEGRADED_RECHECK_DELAY`,
  default 75s — longer than the bridge's 60s Telegram watchdog). If it has
  healed it is judged on its first answer as `OK (…, recovered-from:<reason>)`;
  otherwise the recheck's answer stands. A recheck that cannot complete keeps
  the first answer with `recheck=unverified`. `CCC_FLEET_DEGRADED_RECHECK=0`
  pages on the first answer. An incomplete or failed inspection is
  `UNVERIFIED`, not evidence of downtime. A successful transport exit and final
  completion marker are required before accepting a result; a truncated doctor
  response cannot become `OK`.
- **Matrix availability:** identify the Matrix process by its exact
  `CCC_CHANNEL=matrix` environment or systemd cgroup. Check its private
  `BOT_DATA_DIR/health.json` only when it is fresh and names that PID. An older
  Matrix frontend may leave this file at `starting`; then check the recent
  `meta.health` sync state in the read-only SQLite store named by its config.
  Missing, stale or ambiguous evidence is `UNVERIFIED`. Matrix rows carry
  `channel=matrix`; Telegram rows retain their existing format.
  The health snapshot keeps `service.state=degraded` from one failed agent
  turn until the next turn succeeds, which on a quiet room can be days
  (ccc-node#2098). When that degraded state is explained only by an agent
  failure older than `--stale-error-secs` (default 3600) with no later
  success, and the sync store is `ready`, the row is `OK reason=degraded-stale-error`
  instead of `DEGRADED`; a failure inside the window, a different service
  reason, or a transport retry still reports `DEGRADED`.
- **Runtime source:** read from the worker, or its parent supervisor. Prepared
  launches must bind worker UID, parent PID, project path and interpreter to
  that supervisor. Another project's supervisor cannot supply the source.
- **Separate prepared checkout:** `.ccc-node/checkouts/<commit-prefix>` is
  accepted only when tracked source is clean, the prefix matches HEAD, HEAD
  belongs to the locally recorded `origin/main` history, and the actual prepared
  launcher's read-only validator verifies the private receipt, current source
  seal, editable package, dependency fingerprint and native probes. This is
  local provenance, not a fresh network fetch or an immutability guarantee.
  The existing sibling `preparations/<name>/{source,job}` layout stays supported.
- **Installed harness:** for a prepared launch, use the owner-private
  `.claude/self-update.repo` recorded by setup and maintained by the operator.
  Validate the reference, expected CCC files, imported script tree and their
  ancestor ownership/write permissions (root or the serving owner; Android
  platform ancestors are treated separately); do not search for a checkout
  that happens to pass. Missing, unsafe or unusable references alert as
  `UNVERIFIED`. The reference selects the installation baseline, not the running
  runtime, and cannot authorize a noncanonical runtime.
- **Service manager:** Gongmyoung's process UID does not identify its manager.
  Read the worker cgroup, then check that system or user manager. A system unit
  with `User=gongmyoung` does not need a user unit or user-bus cron variables.
  Numeric systemd User values are compared as UIDs. Unknown domains alert.
- **Git access:** check readability, directory write access and existing
  reflog/FETCH_HEAD write access as the updater account. Readable immutable
  root-owned objects alone are not drift. Git inspection errors never mean
  clean. This is a read-only permission screen, not a guarantee that every
  future Git operation will succeed.

The scheduled command's title counts every watcher category (`DOWN`,
`UNREACHABLE`, `DRIFT`, `BOOTPATH`, `DUALDOMAIN`, `NONCANONICAL`, `DEGRADED`,
`UNVERIFIED`) across both channels, then names the affected nodes:
`DEGRADED=2 UNVERIFIED=1 (node-a, node-b)`. A name is listed only when it is a
short hostname-shaped word; at most six are shown, then `+N more`. Paths and
the rest of each row remain in the redacted body. The title uses all captured
rows even when the body is shortened for delivery, and the body lists the
counted rows first and plain `OK` rows last, so an abnormal row is not cut off
by the ~900-character body limit (#2086).

## Change-based alerting and the 15-minute cadence (#2086)

On 2026-10-01 a node lost external DNS for about ten hours. The daily watch did
alert, but eight hours late, under a title with counts only, and on a task that
had already failed every day for days because another node was chronically
`DEGRADED`. Two options address this; both are opt-in, and without them the
script's output and exit codes are unchanged.

- **`--light`** (or `CCC_FLEET_LIGHT=1`) is the cadence mode for frequent runs.
  It runs only the availability, canonical-root and boot-path probes over ssh,
  one node at a time (one ssh session in flight). It never runs the doctor
  (`CCC_FLEET_DOCTOR` is ignored, with a note on stderr), retries a transport
  failure once after 5s instead of twice after 10s, and stops probing new nodes
  600s after the start (`CCC_FLEET_DEADLINE`; the rest report
  `UNVERIFIED <node> inspection=deadline-600s`). The Matrix check keeps its own
  caps (at most 22s per node). Each of these defaults can be overridden by its
  environment variable.
- **`--state-file PATH`** (or `CCC_FLEET_WATCH_STATE`) folds each run's
  verdicts into a per-node, per-channel state file
  (`scripts/fleet_watch_state.py`) and exits nonzero only when one of these
  events happens:
  - `NEW <VERDICT> <node> channel=<ch> … since=<first abnormal run>`: a
    pair that was OK or unseen has been abnormal for
    `CCC_FLEET_WATCH_CONFIRM` consecutive runs (default 2). For `UNVERIFIED`,
    a failed inspection, the threshold is `CCC_FLEET_WATCH_CONFIRM_UNVERIFIED`
    (default 3). An alerted pair whose verdict changes to a different
    abnormal verdict is `NEW` again once that verdict is confirmed.
  - `STILL <VERDICT> <node> … for=<age> alerts=<n>`: an alerted pair is
    still abnormal when its re-alert is due. `CCC_FLEET_WATCH_REALERT`
    (default `6h,24h`) means 6h after the first alert, then every 24h. `off`
    disables re-alerts.
  - `RECOVERED <node> channel=<ch> was=<VERDICT> … for=<age>`: an alerted pair
    answered OK for `CCC_FLEET_WATCH_RECOVER_AFTER` consecutive runs
    (default 2). `CCC_FLEET_WATCH_PAGE_RECOVERY=0` still prints this row but
    does not page for it.

  The following rows never page: `PENDING` (abnormal, not yet confirmed),
  `KNOWN` (already alerted, with `next-alert=`) and `RECOVERING`. `OK` rows are
  dropped. A final `SUMMARY` row gives the totals. The report is ordered NEW,
  STILL, RECOVERED, PENDING, KNOWN, RECOVERING. A chronic `KNOWN` pair
  therefore never hides a `NEW` one, and the alert title counts only the
  paging rows: `NEW-DEGRADED=1 NEW-UNVERIFIED=1 (node-a)`.

  In this mode the 75s degraded recheck defaults to off
  (`CCC_FLEET_DEGRADED_RECHECK=1` turns it back on), because the consecutive-run
  confirmation already absorbs a one-minute health blip. The recheck is not
  what makes a run slow: it is one shared wait plus one probe per degraded
  node, which is about 2-3 minutes in total for 12 nodes.

  The state file is created `0600` in a `0700` directory if the directory is
  missing. It is replaced atomically under an `flock` on `PATH.lock` and read
  with symlink, owner and mode checks. An unreadable, group- or world-accessible,
  symlinked, non-JSON or wrong-schema file is reported on stderr and replaced by
  empty state. A malformed entry is dropped on its own. The cost is at most one
  repeated alert, never a crash or a missed alert. Pairs not seen for 7 days are
  pruned, and a pair missing from one run (for example, a node removed from
  `CCC_FLEET_NODES`) is left as it is rather than counted as recovered. If the
  state cannot be locked or written, or `python3` is missing, the run prints
  `UNVERIFIED watcher state-file=unusable` followed by the raw verdicts,
  abnormal rows first, and exits 1. It never goes quiet.

State file schema (`ccc.fleet-watch-state.v1`), one entry per
`<node>/<channel>` (`channel` is `telegram` for bridge rows, `matrix` for
Matrix rows):

```json
{"schema": "ccc.fleet-watch-state.v1", "updatedAt": "2026-10-01T00:30:00Z",
 "entries": {"node-a/telegram": {
   "verdict": "DEGRADED", "detail": "runtime=/opt/ccc-node reason=health-stale",
   "streak": 3, "since": "2026-10-01T00:00:00Z", "lastSeen": "2026-10-01T00:30:00Z",
   "abnormalSince": "2026-10-01T00:00:00Z", "alerted": "DEGRADED",
   "alertedAt": "2026-10-01T00:15:00Z", "lastAlertAt": "2026-10-01T00:15:00Z",
   "alertCount": 1}}}
```

### Recipe: 15-minute change watch on the watcher node

Use one state file per task, and do not share it between tasks. Use
`telegram-owner-on-failure` and do **not** set `--success-exit-codes 0,1`: in
change mode exit 1 means "a person needs to look". Every nonzero exit already
pages, so turn the consecutive-failure alarm off for this task. The example is
for the watcher node that already runs `adapter-fleet-watch`, with its checkout
at `/root/ccc-node`. Match the path that task uses (`agent-cron.sh list --json`).
This is an example only. Adding it is an operator decision.

```sh
/root/ccc-node/scripts/agent-cron.sh add fleet-watch-15m \
  --name "fleet bridge watch (15 min, change alerts)" \
  --prompt "Fleet bridge health every 15 minutes; pages only on NEW/STILL/RECOVERED (#2086)" \
  --schedule '*/15 * * * *' --timezone Asia/Seoul \
  --notify telegram-owner-on-failure --catch-up-policy skip \
  --failure-alert-after 0 --timeout-sec 840 \
  --argv bash --argv /root/ccc-node/scripts/fleet-bridge-watch.sh \
  --argv --light --argv --state-file \
  --argv /root/.claude/state/fleet-watch/state.json
/root/ccc-node/scripts/agent-cron.sh run fleet-watch-15m --dry-run --json
```

`--timeout-sec 840` sits above the light-mode worst case: the Matrix pass is at
most about 264s, and the deadline stops new probes at 600s, after which at most
one in-flight probe (≤ 65s) remains. It also stays below the 15-minute
interval. A measured all-OK run of 12 nodes × 2 channels took about 53s. The
first two runs after installation report every already-abnormal pair as
`PENDING`, then `NEW` once. After that, chronic pairs stay `KNOWN` until their
re-alert is due. Keep `fleet-doctor-sweep` daily for harness drift. Once the
15-minute task has proven itself, the daily `adapter-fleet-watch` duplicates it
and can be disabled (`agent-cron.sh disable adapter-fleet-watch`).

## Updating an existing schedule

Verify the installed checkout contains the reviewed script and helper before
changing a task. Inspect and save the target task's previous definition. Use
`agent-cron.sh edit <id> --argv ...` to change only its command; preserve its
schedule, timezone, notifications, history and other tasks. Preview with
`agent-cron.sh run <id> --dry-run --json`, then run the read-only watcher directly
for verification. Do not replace the whole task store with an old backup or
invoke the scheduler merely to test a detector change.

The watcher is not a reboot test. Its unit-file comparison and the installed
node doctor's boot-path implementation have their own coverage limits;
prepared-runtime acceptance proves neither a Termux:Boot selector nor a future
restart. Inspect the actual boot/recovery entrypoint separately when changing
that entrypoint or migrating a runtime.
