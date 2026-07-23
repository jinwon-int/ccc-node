# Doctor diagnostics

`ccc-doctor.sh` is the read-only harness drift report and conservative repair entry point.

## What it checks

- `settings.json` JSON validity and install mode (`standalone`, `plugin`, or ambiguous).
- `outputStyle` and `statusLine` wiring.
- SessionStart/PostCompact memory bootstrap hooks and portable enforcement hook wiring.
- Installed hook scripts and output-style files under the target Claude directory.
- Telegram bridge status output when `bridge/start.sh` is present.
- Harness version anchor via `scripts/ccc-version.sh`.
- Selected agent provider. For `CCC_AGENT_PROVIDER=codex`, deterministic CLI, app-server surface, and login readiness without a model turn or Telegram access.

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
scripts/ccc-doctor.sh --json                 # one JSON object
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

## Provider readiness

`CCC_AGENT_PROVIDER` accepts `claude` (the unchanged default) or `codex`. Set
`CCC_CODEX_CLI_PATH` to the Codex executable path or command name when the
default `codex` lookup is not suitable.

For Codex, doctor resolves an executable and runs bounded, non-mutating probes:

- `--version` must return recognizable Codex version output.
- `app-server --help` must expose the app-server surface used by the bridge.
- `login status` must report an authenticated CLI session.

The probes never invoke a model turn or Telegram. Their raw stdout/stderr,
command payloads, executable/credential paths, auth JSON, tokens, and account
identity are never included in diagnostics. Missing/non-executable binaries,
timeouts, unauthenticated or malformed output, and probe exceptions produce
stable redacted `수동필요` rows, `readiness: failed`, and a nonzero exit.

On a Codex node, doctor also diagnoses the **repo-shipped managed Codex skills**
(#647) via the read-only `ccc_codex_skills.py plan` contract, body-free:

- Missing or outdated managed skills are `교정가능` — reinstall with `setup.sh`.
- A drifted managed skill or a user skill that name-collides with a managed one
  is `교정가능` (drift restore / rename), never a silent overwrite.
- An unsafe `CODEX_HOME`/skill layout (not owner-only `0700`, or a symlink) is a
  `수동필요` blocker with a fix action.

These findings are **independent of readiness**: dormant Claude-only harness
assets (outputStyle/statusLine/hook/overlay drift) on a Codex node stay
`교정가능`/`정상` and never block the Codex readiness verdict, and unprovisioned
managed skills are correctable rather than a readiness failure.

Human output adds `provider` and `readiness` headers. `--json` carries the same
diagnostic information with additive `provider`, `readiness`, `counts`, and
`rows` fields.

Before a Codex rollout, install/authenticate the CLI, set the provider variables,
and require a ready doctor result. Codex approval requests are owner-only and
turn-scoped: Allow or Deny each request; there is no **Allow All**. Stop the old
bridge before starting the new one because two services must never poll the same
Telegram bot token concurrently. Roll back by stopping Codex, restoring
`CCC_AGENT_PROVIDER=claude`, and starting Claude as the sole poller.

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
