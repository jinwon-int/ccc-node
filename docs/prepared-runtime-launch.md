# Launching a prepared runtime

`start.sh --prepared-runtime <job>` selects a completed Termux preparation job
without creating a venv or running pip. This is the first launch integration
for #1527. Explicit one-shot recovery is available as an opt-in below.
Promotion policy, managed-service templates and production transition
validation remain separate work. The
unmanaged stop path now preserves the existing bounded application drain
(see [service control](service-control.md#bridge-restart-drain)).

## Prepare, retain, then select

Use a separate source checkout and a **new** preparation directory. Keep each
source checkout and its venv at their original paths: venv scripts and the
editable bridge package refer to those paths. Run the preparation command
from [Termux build preparation](termux-build-preparation.md) using a version
that records `source_seal`. Older unsealed receipts are refused; do not add a
seal by hand or copy an old receipt to another venv.

The seal covers Python, shell, TOML, text/JSON runtime inputs and
`crash-policy.env` under `bridge/`. It excludes tests, hidden configuration,
venvs, bytecode and generated egg metadata. Provider `.env` files remain
separate mutable inputs. This is a content fingerprint, not a signature or a
promise that an owner-writable checkout is immutable. Keep staged source and
environments unchanged while validating or running them.

Before selecting a candidate, record the **previous exact start command**,
source directory and preparation directory. Retain both generations through
the restart and subsequent health-observation window. No environment or old
launch record is removed by the prepared launch path.

```bash
# Read-only gate, using the candidate's own Python and source path:
"$candidate_job/runtime/bin/python" -I -B "$candidate_source/bridge/prepared_runtime.py" \
  --bridge-dir "$candidate_source/bridge" --prepared-dir "$candidate_job"

# Explicit unmanaged service transition, from outside its bridge process tree:
bash "$candidate_source/bridge/start.sh" --path "$project" \
  --prepared-runtime "$candidate_job" --restart -d
```

A restart validates the candidate **before stopping** the old bridge. It
checks the preparation receipt, unchanged source seal, selected interpreter,
editable package source, locked dependency fingerprint, native/SDK imports,
AES-GCM and `pip check`. A failed gate exits6 and leaves the old process alone.

The child repeats validation after acquiring the existing token lock, then
runs the selected venv without package installation. Foreground, detached
restart and daemon-supervisor spawn paths preserve the explicit selection.
Token cleanup now [retains the flock inode](service-control.md#token-lock-lifetime)
so a concurrent claimant cannot bypass the lock by recreating its file.
Existing duplicate-poller/process-tree guards remain in force; this change
does not introduce a second Telegram poller. A source change after the
pre-stop gate can still fail the post-stop gate: retention and recovery are
therefore required even after a successful preflight.

When this command runs through the self-updater, its external command budget
must include pre-stop validation, drain and readiness together. The default
180s watchdog can be shorter than their combined duration; configure and
measure the separate [restart command budget](self-update.md#budget-the-complete-external-command)
in the updater environment before a transition. The post-restart health wait
is a different limit.

## Receipts and recovery

After validation under the token lock, each launch appends a private receipt
under `<project>/.telegram_bot/runtime-history/`. It records source path,
content seal, Git identity when available, dependency fingerprint, selected
runtime and launcher PID. Archives without Git metadata report that absence;
they still have the content seal. Records are owner-only and never overwrite
previous generations. Their phase is `validated_before_launch`; **they do not
claim that the process became available**. Use `--status` and the existing
restart availability result to verify the serving process separately.

The running bridge also writes `runtime_generation` into its existing
`health.json` during process initialization. This startup snapshot contains
`source_dir`, `source_git`, `source_seal`, the actual `python_executable` and
`python_prefix`, and the venv's last bootstrap `dependency_fingerprint`.
Prepared `--restart` now pins the successful pre-stop validation JSON in its
own process and uses `prepared_serving.py` to verify the candidate. Success
requires matching source path/seal, dependency fingerprint, Python prefix and
interpreter, and Git head when the validation observed one. The health PID
must match the current bot PID, be alive rather than a zombie, differ from the
old bot, and report available service with healthy Telegram and agent states.
The process start and generation observation must be after the launch boundary;
the health update must be fresh, ordered after those observations, and not in
the future. Missing legacy generation metadata cannot confirm a prepared
restart. Non-prepared restart behavior is unchanged.

The read-only checker accepts the pinned validation report on stdin and emits
`ccc.prepared-serving.v1` with `status=available` only for a matching snapshot.
It reads bounded regular owner-controlled health data without following
symlinks. It does not run package/native probes, import candidate application
code, make network calls, install packages, or rewrite any state. On mismatch,
restart keeps observing until its existing availability limit, then exits4 and
leaves any candidate running for the documented explicit recovery procedure.
Generic `--status` may still show available for a different generation; that
alone is no longer prepared restart success.

These are observations of owner-controlled files, not cryptographic or loaded
code/package attestation. A matching snapshot does not prove lasting health,
exclude concurrent external lifecycle commands, or replace a monitored
production transition/rollback trial.

The snapshot is captured once for each reporter and retained across heartbeat
updates and stopped/degraded states. It describes files observed at startup;
it is neither a hash of loaded code nor a claim that later imports or installed
packages stayed unchanged. Missing Git metadata is reported as null; missing,
unreadable or malformed seal/fingerprint inputs produce categorical
`collection_errors` without blocking health reporting. The dependency value
is a bootstrap marker, not a fresh package integrity check. No credentials,
`.env` contents, provider calls or installation are involved.

If start or readiness fails after stop, inspect the existing restart exit
reason and log. A candidate process may remain alive after the availability
timeout. An operator can explicitly run the retained previous command with
`--restart`, allowing the ordinary stop/wait/token-lock path to stop that
candidate before starting the previous source/runtime. For a previously
prepared generation:

```bash
bash "$previous_source/bridge/start.sh" --path "$project" \
  --prepared-runtime "$previous_job" --restart -d
```

For a legacy generation, retain and use its original source/venv command.
Do not infer a safe legacy rollback merely from having a managed-asset backup;
that backup may not contain the old venv. If recovery also fails, retain both
environments and logs and report the failure. Without the recovery options
below, no automatic recovery is performed. Neither path cleans up generations.

### Opt-in one-shot recovery

When the currently healthy bridge is also a retained prepared generation,
provide its explicit source **bridge directory** and preparation job:

```bash
bash "$candidate_source/bridge/start.sh" --path "$project" \
  --prepared-runtime "$candidate_job" --restart -d \
  --recovery-source "$previous_source/bridge" \
  --recovery-runtime "$previous_job"
```

Both recovery options require prepared `--restart` and the default spawn
command. They do not enable managed-service transitions. The controller
validates both retained pairs and verifies that the current live, healthy
process matches the previous pair before stopping anything. On a candidate
start or readiness failure after a successful stop, it calls the previous
pair's normal `--restart` once. That command stops any surviving candidate
through the existing stop/token-lock path. Recovery succeeds only when fresh
serving evidence matches the **previous report pinned before stop**. Recovery
failure never triggers another automatic attempt.

| Exit | Meaning |
| --- | --- |
| `0` | Candidate generation became available. |
| `1` | Stop failed; candidate and recovery were not launched. |
| `2` | Invalid option combination (before transition). |
| `3` / `5` | Service-manager / caller-process-tree refusal. |
| `6` | Preparation, current-generation or lease precondition refused. |
| `7` | Candidate failed; the previous generation was verified restored. |
| `8` | Candidate failed and recovery was not verified successful. |
| `9` | Transition evidence could not be persisted; inspect retained state. |

Exit `7` is deliberately nonzero: a recovered service is still a failed
candidate update. These additional outcomes apply only when recovery is
selected; ordinary prepared restart retains its existing exit codes.

Each attempt writes private, exclusive phase JSON files under
`<project>/.telegram_bot/runtime-transitions/<attempt-id>/`. Intent identifies
both source/runtime paths; validation records both pinned reports; terminal
records distinguish the controller result from the underlying command result.
Files are `0600` and attempt directories `0700`. The `active/` directory is an
exclusive lease between cooperating recovery-option controllers. Completing
a recorded attempt archives the lease inside that attempt. Generation files,
launch receipts and transition evidence are retained without cleanup.

Interrupted or incomplete attempts leave the lease in place. A dead launcher
PID does **not** authorize reclaim: its children may still be stopping or
starting a bridge. There is no automatic resume or lease reclamation. Before
an operator archives a stale `active/` directory to an unused private name,
inspect the attempt and launcher/child processes, verify the actual serving
source/runtime, and finish or stop any outstanding lifecycle operation. Keep
the archived evidence with the attempt. Do not remove the lease simply to
retry. A journal write failure can leave a partial record or a terminal record
whose lease archival failed; only the command's final exit reports completion.

Direct legacy lifecycle commands do not participate in this lease. Coordinate
all external lifecycle callers separately. Shared `.env` settings, credentials
and service configuration are not restored; keep configuration compatible with
both retained generations. This is an observation of retained owner-controlled
artifacts, not an immutable-source or loaded-package attestation.

Budget external watchdogs for **both** candidate and possible recovery
validation, drains, launches and readiness waits. The self-updater's default
180-second external command budget may be insufficient; explicitly measure
and configure the linked restart command budget. The controller does not
silently extend an outer deadline or resume after it is killed.

### Offline recovery rehearsal

`bridge/tests/test_prepared_recovery.py` exercises the explicit recovery command
on Linux with two copied source trees and two real Python venvs. The launchers,
preparation/native/SDK checks, token locks, stop/restart path, generation capture
and serving verifier are real. Only the bot entrypoint and a small per-venv
dependency are fixtures. Existing test-environment packages are exposed read-only;
the fixture blocks installation and makes no Telegram or provider requests.

The scenarios cover candidate readiness timeout and process exit, an invalid
retained environment refusing recovery before stop, a valid environment whose
recovery process stays unavailable, and a subsequent successful explicit retry.
Assertions verify the restored source seal, interpreter, venv and dependency
fingerprint, the actual per-venv dependency value, retained preparation records,
private append-only launch receipts and absence of overlapping fixture pollers.

Run from `bridge/` in the normal bridge test environment:

```bash
python -m pytest tests/test_prepared_recovery.py -q
```

This proves the retained-pair command path under injected fixture failures.
The two venvs share the test host's installed SDK/native packages; this does not
exercise two independently installed SDK versions, production Telegram polling,
real workload drain, power loss, disk exhaustion or Termux. Those trials and
automatic promotion policy remain separate #1527 acceptance work.

The selector is supported for run/restart/status/stop. Install, uninstall,
upgrade and service-template operations reject it, because those templates
do not yet persist a selected generation. Managed systemd/launchd restarts
continue to use their service-manager boundary. Actual production transition,
workload-drain guarantees, rollback failure injection and downtime measurement
remain required before closing #1527.
