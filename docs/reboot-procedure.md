# Node reboot procedure — scheduled timers (#2827)

A planned reboot of a ccc-node host (kernel update, provider maintenance,
`reboot-required`) erases every **transient** systemd timer. `detached_jobs.py
schedule` creates its jobs as transient timers (`systemd-run --on-calendar`),
which live only under `/run/systemd/transient/` (system) or the user manager's
runtime directory (`--user`). After a reboot those jobs never fire, and the
owner's promised check or reminder is silently skipped.

This page fixes the steps that every reboot — local or toward a managed node —
must include. The commands themselves are documented in
[`skills/bridge-safe-detached-run/SKILL.md`](../skills/bridge-safe-detached-run/SKILL.md)
(Step 2d); who may reboot which host is in [`service-control.md`](service-control.md).

## Before the reboot

1. **Check what is waiting.** On the node, as **each harness account** that
   schedules jobs (root on most nodes; the dedicated service user, where
   the harness does not run as root):

   ```bash
   DJ=~/.claude/hooks/lib/detached_jobs.py
   SNAP=~/.claude/state/reboot-$(date +%Y%m%d)/timers.json
   mkdir -p "$(dirname "$SNAP")"
   python3 "$DJ" export-timers --out "$SNAP"; echo "export rc=$?"
   systemctl list-timers --all --no-pager          # system scope
   systemctl --user list-timers --all --no-pager   # user scope (non-root harness)
   ```

   - `export rc=0`: every waiting job can be restored from its saved argv.
   - `export rc=1`: some jobs are listed under `skipped` — `missed` (time already
     passed) or no launch definition. A job scheduled before
     `restore-timers` existed (#2862) has no `launch_argv`; write its unit name
     down and keep the snapshot — its live `systemctl cat` values are in it and
     it must be re-created by hand after the reboot.
   - The installed helper must have the commands:
     `grep -c 'def cmd_restore_timers' ~/.claude/hooks/lib/detached_jobs.py` → `1`.
     If `0`, the node has not self-updated past #2862; save `systemctl cat
     <unit>.timer <unit>.service` for every waiting timer instead.

2. **Decide whether to wait.** If a job fires within the next hour or the owner
   depends on it the same day (a measurement-session reminder, a result check),
   prefer rebooting after it fires. Otherwise continue; the next steps bring it
   back.

3. **Record the reboot** on the issue or log that asked for it, with the snapshot
   path and the list of waiting units, so the post-reboot check has a target.

## After the reboot

4. **Restore and compare.** Same account(s) as step 1:

   ```bash
   python3 "$DJ" restore-timers --from "$SNAP" --dry-run   # what would be launched; nothing changes
   python3 "$DJ" restore-timers --from "$SNAP"; echo "restore rc=$?"
   ```

   - Each job is reported as `restored`, `kept` (timer still there), `missed`,
     `unrestorable`, `failed` or `differs`. The last four make the exit code `1`.
   - `restore-timers` replays the argv `schedule` saved, so the job keeps the
     HOME/PATH and `--user` scope it was scheduled with — do not re-create it
     from the restoring shell's environment.
   - With `--from`, each re-created unit is compared with its pre-reboot values.
     `differs` means the re-created unit's time, command or `Environment=` is not
     what was waiting before the reboot; inspect it before trusting the job.

5. **Handle what was not restored.**
   - `missed`: the run time passed while the node was down. It is not re-armed.
     Run the job's command by hand if its result is still needed, and say so on
     its issue.
   - `unrestorable` (no saved argv): re-create it from the snapshot's live
     values with `systemd-run --unit <same> --on-calendar '<same>'`, carrying
     every `Environment=` value with `--setenv`. Compare the values, not the
     layout: `systemctl cat` writes one `Environment=` per line, a re-created
     unit may hold several on one line (`grep -oE '"[^"]*"' | sort` on both).
   - `failed`: read the `systemd-run` error the command printed; a non-root
     account that cannot start system units needs `--user` (see `loginctl
     enable-linger` so the user timer survives logout).

6. **Verify.** `systemctl list-timers --all` (and `--user`) shows every unit from
   step 1 that is not `missed`, at the same `NEXT` time. Add the result
   (`restore rc`, units restored/missed) to the record from step 3.

## Unplanned reboot (no snapshot)

A power cut or provider restart gives no chance to export. The next SessionStart
sweep reports such jobs as `lost`. Run `restore-timers` without `--from`; it
restores from the registry alone (no pre-reboot comparison). Jobs without
`launch_argv` stay `unrestorable` — re-create them from their issue comment or
the job's script directory.

## Persistent timers: `schedule --durable` (#2827 proposal A)

`detached_jobs.py schedule --durable` writes the job as a persistent pair —
`<unit>.service` (oneshot) + `<unit>.timer` (`Persistent=true`) under
`/etc/systemd/system` (or `~/.config/systemd/user` with `--user`) — enables the
timer, and has the wrapper disable the timer and delete both files after the
run. Such a job survives a reboot with no export/restore step; a run whose time
passed while the node was down fires once at the next boot. `export-timers`
lists it as `armed` with `durable: true`, and `restore-timers` never replays it
(if its timer has vanished it is reported, not re-armed).

- It is one-shot: `--at` must be an absolute future instant
  (`YYYY-MM-DD HH:MM[:SS] [zone]`). Recurring checks stay with agent-cron or a
  hand-written unit.
- Writing unit files is a node configuration change: it needs the same
  approval as a hand-written persistent unit. `--print` shows both files and
  the `systemctl` steps without writing anything.
- Jobs already armed with plain `schedule` are not converted; for them the
  export/restore steps above remain the reboot procedure.
