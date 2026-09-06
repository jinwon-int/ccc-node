# Launching a prepared runtime

`start.sh --prepared-runtime <job>` selects a completed Termux preparation job
without creating a venv or running pip. This is the first launch integration
for #1527. Promotion automation, managed-service templates, automatic source/environment
rollback and production transition validation remain separate work. The
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
environments and logs and report the failure. No automatic rollback or
cleanup is performed by this integration.

The selector is supported for run/restart/status/stop. Install, uninstall,
upgrade and service-template operations reject it, because those templates
do not yet persist a selected generation. Managed systemd/launchd restarts
continue to use their service-manager boundary. Actual production transition,
workload-drain guarantees, rollback failure injection and downtime measurement
remain required before closing #1527.
