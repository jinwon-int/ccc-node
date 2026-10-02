- **Fleet watch alerts on changes, and the alerts name the nodes (#2086).** A
  node lost DNS for ten hours. The daily fleet watch reported it eight hours
  late, under a title that gave counts only, on a task that had failed every
  day for a chronic issue on another node. Two opt-in options address this.
  `fleet-bridge-watch.sh --state-file PATH` keeps per-node, per-channel
  verdicts in an owner-only, atomic, corruption-tolerant state file and exits
  nonzero only on `NEW` (confirmed over 2 runs, 3 for `UNVERIFIED`), `STILL`
  (re-alert after 6h, then every 24h) or `RECOVERED`. Its report leads with
  those rows and drops `OK` rows. `--light` is the 15-minute cadence mode: no
  doctor, one transport retry, and a 600s probe deadline. Agent-cron fleet
  alert titles now list up to six validated node names
  (`DEGRADED=2 UNVERIFIED=1 (node-a, node-b)`) and count the change-mode rows.
  The alert body lists abnormal rows before `OK` rows, so the body cap cannot
  cut them off. Without the options, watch output and exit codes are
  unchanged. `docs/agent-cron.md` now warns that `--success-exit-codes 0,1`
  combined with `*-on-failure` notify silences findings.
