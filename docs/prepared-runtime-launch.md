# Launching a prepared runtime

`start.sh --prepared-runtime <job>` selects a completed Termux preparation job
without creating a venv or running pip. This is the first launch integration
for #1527. Promotion automation, workload draining, managed-service templates,
and automatic source/environment rollback remain separate work.

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
Existing duplicate-poller/process-tree guards remain in force; this change
does not introduce a second Telegram poller. A source change after the
pre-stop gate can still fail the post-stop gate: retention and recovery are
therefore required even after a successful preflight.

## Receipts and recovery

After validation under the token lock, each launch appends a private receipt
under `<project>/.telegram_bot/runtime-history/`. It records source path,
content seal, Git identity when available, dependency fingerprint, selected
runtime and launcher PID. Archives without Git metadata report that absence;
they still have the content seal. Records are owner-only and never overwrite
previous generations. Their phase is `validated_before_launch`; **they do not
claim that the process became available**. Use `--status` and the existing
restart availability result to verify the serving process separately.

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
