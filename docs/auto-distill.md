# Auto-distill (TM-2380)

Auto-distill is the 30-minute session-to-Wiki-candidate canary. It is separate
from the Claude SessionEnd/SessionStart hook distiller:

| | Auto-distill | Hook distill |
|---|---|---|
| Managed source | `scripts/auto-distill/` | `claude/hooks/distill*` |
| Installed path | `~/.hermes/auto-distill/` | `~/.claude/hooks/` |
| Trigger | operator-managed cron | Claude lifecycle hooks |
| Input | Piri and/or Claude interactive session logs | current Claude transcript |
| Output | unverified `AUTO.md` candidates | local/Honcho/Wiki-candidate memory sinks |

The managed source was brought into ccc-node after a Piri node silently chose
Claude for at least 96 consecutive runs because the hand-deployed script only
looked for `$HOME/piri/piri-ccc.sh` (#1257). Raw fleet regression transcripts
are not tracked; the nine retained fixtures remain node-local.

## Model-engine resolution

`auto-distill.py` accepts `--model-cmd` for an explicit controlled experiment.
Without it, `model_command.py` applies this contract:

1. Read `CCC_AUTO_DISTILL_PROVIDER`, then a supported
   `CCC_AGENT_PROVIDER`, from the cron environment.
2. When cron did not inherit them, read only the allowlisted provider/CLI
   values from the effective system or user `ccc-telegram-bridge.service`.
3. Resolve Piri in this order:
   `CCC_PIRI_REAL_CLI_PATH` (process, then systemd),
   `CCC_PIRI_CLI_PATH` (process, then systemd),
   `/opt/piri/piri-ccc.sh`, `$HOME/piri/piri-ccc.sh`, then `piri` on `PATH`.
4. Resolve Claude from `CLAUDE_CLI_PATH` and then `claude` on `PATH`.

An explicitly identified Piri node **fails closed** when no Piri command is
runnable. It never falls through to Claude. Auto mode may select Claude only
when no runnable Piri lane exists, and records `reason=no-runnable-piri` in
both console and the body-free `engine_selected` audit event. Selection never
records command arguments or prompt/session bodies.

Every child model invocation receives `CLAUDE_DISTILL_INFLIGHT=1` and
`CCC_AUTO_DISTILL_INFLIGHT=1`. This preserves the lifecycle recursion guard
even if a Piri wrapper or future backend eventually enters a Claude session.

## Install and drift check

The dedicated installer defaults to preview and does not touch cron or
services:

```bash
bash scripts/install-auto-distill.sh --preview
bash scripts/install-auto-distill.sh --check
bash scripts/install-auto-distill.sh --apply
```

`--apply` validates owner-controlled, non-symlink targets, stages all three
Python files in the destination filesystem, backs up changed existing files
under `~/.hermes/backups/auto-distill/<UTC-stamp>/`, and replaces only changed
files. A failed replacement rolls back files already moved. Reapplying an
identical source is a no-op and creates no backup.

For a fixture or an explicitly authorized remote home, pass
`--target-home /absolute/path` or `CCC_AUTO_DISTILL_TARGET_HOME`. The target
must exist and belong to the invoking user.

## Verification and rollout boundary

Repository verification is hermetic and does not call a model:

```bash
bash scripts/auto-distill.test.sh
```

Before an approved canary rollout, verify per node:

1. installer preview and backup destination;
2. source drift check after apply;
3. one bounded dry-run showing the intended `engine=` and `source=`;
4. `engine_selected` or `engine_unavailable` in the local audit log;
5. no cron, bridge service, Wiki PR, or pending-session mutation outside the
   separately approved rollout scope.

Repository merge does not authorize fleet installation, cron changes, model
spend, service restart, or Wiki candidate publication.
