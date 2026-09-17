# Fleet bridge watcher

Run `bash scripts/fleet-bridge-watch.sh` from the installed ccc-node checkout.
Set `CCC_FLEET_DOCTOR=1` to inspect installed harness drift as well. The command
only inspects state; it does not restart a bridge or run scheduled tasks.

The script streams `scripts/fleet_watch_metadata.py` with its probe. Keep these
two files together. A copied script without its helper reports `UNVERIFIED`.
Do not leave a scheduled task pointing at an old one-file emergency snapshot
after the reviewed fix is integrated into the normal checkout.

## Distinct sources of evidence

- **Availability:** confirmed unavailable/absent is `DOWN`; an alive but
  unmanaged service is `DEGRADED`. An incomplete or failed inspection is
  `UNVERIFIED`, not evidence of downtime.
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
  Validate the reference and expected CCC files; do not search for a checkout
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
`UNVERIFIED`). Only fixed category names and bounded counts enter the title;
node details remain in the redacted body.

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
