# Doctor diagnostics

`ccc-doctor.sh` is the read-only harness drift report and conservative repair entry point.

## What it checks

- `settings.json` JSON validity and install mode (`standalone`, `plugin`, or ambiguous).
- `outputStyle` and `statusLine` wiring.
- SessionStart/PostCompact memory bootstrap hooks and portable enforcement hook wiring.
- Installed hook scripts and output-style files under the target Claude directory.
- Telegram bridge status output when `bridge/start.sh` is present.
- Harness version anchor via `scripts/ccc-version.sh`.

The report is Markdown and classifies rows as:

| Class | Meaning |
|---|---|
| `정상` | Matches the expected harness state. |
| `경고` | Non-fatal drift or optional component needs attention. |
| `교정가능` | Doctor can repair it in an explicitly approved scoped apply. |
| `수동필요` | Manual/operator action required; doctor refuses automatic repair. |

## Commands

```bash
scripts/ccc-doctor.sh
scripts/ccc-doctor.sh --fix                  # dry-run only
scripts/ccc-doctor.sh --fix --apply          # settings-only repair after backup
scripts/ccc-doctor.sh --fix --apply --scope=files
scripts/ccc-doctor.sh --rollback             # dry-run only
scripts/ccc-doctor.sh --rollback --apply
```

## Repair boundary

- `--fix` and `--rollback` are dry-run by default.
- `--fix --apply` defaults to `--scope=settings` and writes only deterministic `settings.json` repairs from canonical repo templates after creating a backup tar.
- `--fix --apply --scope=files` reinstalls only allowlisted hook/output-style files from `claude/` after a scoped backup.
- File repair refuses symlinks, path traversal, missing repo sources, plugin/standalone double-firing risk, and unsupported targets.
- `--rollback --apply` restores only `settings.json` from the latest doctor backup after creating a pre-rollback backup.
- It never touches remote nodes, secrets, broker/Gateway restarts, bridge restarts, migrations, provider sends, or DB/ACK/replay state.

## Fleet matrix

`ccc-doctor-fleet-matrix.sh` summarizes already-collected doctor output; it does not SSH or mutate nodes.

```bash
bash scripts/ccc-doctor-fleet-matrix.sh --evidence doctor.txt --node-list dungae,nosuk,soonwook --json
```

Input blocks look like:

```text
===== nosuk =====
# ccc doctor
- harness version: `v0.4.0-2-gabc1234`
...
```

The JSON output includes per-node `version`, `status`, `reason`, evidence presence, and summary counts.
