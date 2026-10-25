# Preparing a Termux runtime with existing build tools

An empty pip cache can make pip compile maturin itself before building bridge
packages. On Gongyung, that recursive maturin 1.15.0 build failed in the LLVM
linker with `Pointer tag ... was truncated`. This is a build-tool failure,
separate from the Python API 24 versus device SDK 33/36 wheel-tag mismatch
fixed in #1532. This workflow avoids that recursive backend build; it does
not repair LLVM or claim that its upstream crash is fixed.

`bridge/termux_prepare.py` automates the preparation used successfully on the
phone. It creates a new builder and runtime, builds the five locked native
packages with the existing Termux maturin, then runs ordinary hash-locked
bootstrap and readiness checks. It never promotes the runtime or starts a
service. Use a separate, trusted source checkout: the runtime's editable
first-party package remains linked to that checkout, so keep it in place.

## Android linker mitigation and validation boundary

The initial 2026-09-06 clean pip/build-cache run used maturin 1.14.1 and API24.
Four native wheels built, but pydantic-core's build-script link hit the same
LLVM pointer-tag crash. The driver failed after740seconds without creating a
runtime. Preparing maturin alone did not resolve the native linker failure.

The driver now sets job-local `RUSTFLAGS=-C link-arg=-Wl,--threads=1` and
`LDFLAGS=-Wl,--threads=1`. Cargo's `--jobs` setting does not constrain LLD's
internal threads. This serial-link mitigation follows the similar Android
AArch64 failure reported in [LLVM#62165](https://github.com/llvm/llvm-project/issues/62165)
and [Termux#15867](https://github.com/termux/termux-packages/issues/15867).
The isolated pydantic-core rebuild passed in405seconds with the same wheel
hash as the earlier successful artifact. That single-package result does not
prove the underlying LLVM21.1.8 defect has been repaired or establish a fleet
reliability guarantee. Full preparation/lifecycle evidence is tracked in
[PR#1543](https://github.com/jinwon-int/ccc-node/pull/1543).

The flags apply only to this job's compiler children, overriding ambient Rust
and linker flags. The receipt records `linker_threads: 1` and the LLD binary's
version/hash. The system compiler and serving environment are unchanged.

## Prerequisites and invocation

Run with Termux's base Python (`$PREFIX/bin/python3`), with consistent Android
build metadata. A venv interpreter is refused before any workspace is claimed
(`must_run_with_base_interpreter`): an inherited `PATH` whose first `python3`
was an old runtime venv otherwise failed the backend probe with a bare
`No module named 'maturin'`. The
reviewed backend profile requires **maturin 1.14.1**, both its system Python
module and `$PREFIX/bin/maturin`. Rust, the Android Rust standard library,
clang and patchelf must already be installed through the node's package
management procedure. The helper records their versions and binary hashes;
it does not install or update system packages. Missing tools or an unsupported
backend version fail with private phase logs. Do not respond by using
`pip install maturin` in an empty environment: that can recreate the failure.

Create an owner-private parent and choose a **nonexistent** child path:

```bash
umask 077
mkdir -p "$HOME/.ccc-node/preparations"
chmod 700 "$HOME/.ccc-node/preparations"
python -B bridge/termux_prepare.py \
  --work-dir "$HOME/.ccc-node/preparations/api24-first" \
  --timeout-seconds 3600 --jobs 1 --verify-reinstall
```

An existing child is refused, including a previous failed run. Symlinked
ancestors and a non-private immediate parent are refused. Failed artifacts
are retained for diagnosis, without deleting or replacing an operator's
existing environment. The tool has no resume, cleanup or serving-venv mode.

## What is checked

- Python's build API is derived with the same contract as bootstrap; a
  conflicting `ANDROID_API_LEVEL` override fails before creating the job.
- Setuptools, packaging, cffi and pycparser pins and SHA-256 hashes are copied
  from `.github/requirements/bridge-ci.txt`. No second hash lock is maintained.
- Cryptography, jiter, pydantic-core, rpds-py **and pyromark** are built from the
  runtime lock using `--require-hashes --no-build-isolation
  --check-build-dependencies`. Pip verifies each source package's declared
  build requirements against the prepared builder. A later backend minimum
  version bump therefore fails instead of silently using an unsuitable tool.
- Pip's private wheel cache retains the original source-hash metadata. The
  new runtime uses the original lock through ordinary bootstrap, with normal
  wheel compatibility and dependency checks enabled. Locally built wheel
  hashes are evidence, not replacements for the source hashes in the lock.
- Native and SDK imports, AES-GCM, and `pip check` must pass. An additional
  import check covers all five native packages. `--verify-reinstall` performs
  a real `pip install --force-reinstall --require-hashes`, reconciles native
  linkage again, and repeats readiness. Without that flag, reinstall is
  explicitly `not_run`.

The builder uses `--system-site-packages` to access the distro backend; the
runtime does not. This is reproducible preparation with recorded toolchain
provenance, not a hermetic compiler distribution. Pip configuration and
Python/Rust/Cargo environment overrides are removed from child processes;
Rust and linker flags are replaced with the fixed serial-link profile.
Operator network/proxy settings and the default Cargo configuration/registry
remain applicable.

## Reusing native wheels from a prior job on the same node

The native build is about 90% of a full preparation (2702 of 3015 seconds on one
Termux node), and three consecutive builds of an unchanged lock on that node
produced bit-identical wheels. When nothing that determines the build output
changed, point the new job at a previous **ready** job on the same node:

```bash
python -B bridge/termux_prepare.py \
  --work-dir "$HOME/.ccc-node/preparations/<gen>/job" --verify-reinstall \
  --reuse-wheels-from "$HOME/.ccc-node/preparations/<prior-gen>/job"
```

The new job still gets its own source seal, runtime venv, receipt, fresh
install, readiness probes and (with `--verify-reinstall`) a real forced
reinstall. Only the builder venv, build-tool install and `native-wheels` stage
are replaced by a `native-wheels-reuse` stage, which:

- refuses, before claiming the workspace, a prior job that is not an
  owner-private non-symlinked directory, or whose owner-private receipt is not
  `ready` with `fresh_install: pass`, names another `work_dir`, or does not
  list exactly the five native wheels;
- after measuring this run's own toolchain, refuses unless `android_api`,
  `linker_threads`, both `lock_subset_sha256` values (below) and the complete
  `toolchain` record (versions and binary hashes) are identical — any change
  means a full build (rerun without the flag; reason `reuse_<field>_mismatch`).
  Toolchain hashes keep the reuse on one node: another node's builds differ (a
  second Termux node's pydantic-core wheel hash differs from the first's);
- copies the prior job's pip wheel cache (`pip-cache/wheels`, regular files
  only, bounded) into this job's private cache and the wheelhouse evidence
  copy, then requires every native wheel to match the prior receipt's digest;
- after fresh install and reinstall, requires the cache to still hold exactly
  those wheels (`reused_wheels_not_used` if pip built a native package again).

### Which lock change refuses reuse (#2524)

The lock input is matched on the **subsets the native build actually reads**,
not on the whole lock files. `lock_subset()` (`bridge/termux_lock_subset.py`)
copies, for each package in `NATIVE` (`bridge/requirements.lock.txt`) and
`BUILD_TOOLS` (`.github/requirements/bridge-ci.txt`), its exact
`name==version` pin plus every `--hash=sha256:` entry into
`job/native.lock.txt` and `job/build-tools.lock.txt`; those two files are the
only lock input of the `build-tools` and `native-wheels` stages. The receipt
records `lock_subset_sha256: {"build-tools": ..., "native": ...}` (SHA-256 of
those files) and reuse matches on it. Every other locked package is pure and
is installed fresh by the fresh-install bootstrap and the forced reinstall —
both `pip install --require-hashes -r requirements.lock.txt` against the
**full** lock — so a pure-Python bump (#2433 certifi) keeps the subsets
byte-identical and the wheels reusable, while any native or build-tool pin or
hash change is refused. `lock_sha256` (whole-file digests) stays in the
receipt as evidence only. Matching on it refused reuse for every unrelated bump; a
slow node then hit the full-build timeout every day, and because a failed
build never becomes serving, each tick repeated the same refusal (5 days).

A ready receipt written before #2524 has no `lock_subset_sha256`; the prior
digests are then computed from that job's own `native.lock.txt` /
`build-tools.lock.txt`, which must be regular, non-symlink, owner-private,
bounded (4 MiB) files inside the already-validated prior job. Anything else
fails closed with `reuse_lock_subset_unavailable` (as does a malformed
recorded value).

`termux-prepare-generation.sh` applies the same rule as a prefilter before any
worktree exists: it pipes the **target** commit's `bridge/termux_lock_subset.py`
(stdlib only) into `python3 -I -B - --compare-git <repo> <serving> <target>`,
which reads both commits' lock files with `git show` (no temp files, no
network). Only `equal` passes `--reuse-wheels-from`; `different`, a target
without the helper, an unparseable lock or any other `undecided:<why>` means
no reuse. The decision is logged as `lock_subsets=<result>` next to `reuse=`
in the dry-run and `building` lines. The prefilter never replaces the tool's
own check. If the tool refuses the reuse anyway with `reuse_*_mismatch` or
`reuse_lock_subset_unavailable` (e.g. an old serving job without usable
subset files), the refused generation dir is kept for diagnosis and the script
retries **once** as a full build (no reuse) in a fresh generation dir, with the
remaining timeout budget (no retry when under 60 s remain). Any other failure,
or a failed retry, ends the tick as before (exit 3, pointer unchanged).

pip's cached wheels keep their original sdist hash in `origin.json`, so the
ordinary bootstrap still enforces `--require-hashes` against the lock. The
receipt records `native_wheels: {"mode": "reused", "from": ..., "receipt_sha256": ...}`
(`{"mode": "built"}` otherwise) and a `cache_scope` naming the reuse. Measured
on that node (2026-10-09, reusing the 55f6b7bc job): **262 seconds** instead of
3015, wheels identical. Reuse is a same-node shortcut, not cross-node wheel
distribution or a signature; keep the prior job until the new one has served.

## Optional frontend extras (`--extra matrix`)

A node that also serves the Matrix frontend (`CCC_CHANNEL=matrix`) needs
`bridge/requirements-matrix.txt` (`matrix-nio[e2e]`, which builds against the
system `libolm`) in the same runtime. That extra is deliberately outside the
hash lock (see the file's header), so it used to be installed by hand after
preparation. `--extra matrix` (repeatable flag; `matrix` is the only extra
today) does it inside the job, after the fresh-install and reinstall
scenarios:

1. `pip freeze --exclude-editable` of the finished core runtime is written to
   `extra-matrix-constraints.txt` (0600) and passed to `pip install -r
   requirements-matrix.txt -c <that file>`, so the extra can **add**
   distributions but never move a core pin;
2. a second freeze must still contain every core line unchanged
   (`extra_matrix_changed_core` otherwise);
3. an import probe requires `nio`, `olm`, `aiohttp` and
   `nio.crypto.ENCRYPTION_ENABLED` (an e2e-less `matrix-nio` fails), then
   `pip check`;
4. the core readiness probes run once more (`extras-readiness-*`).

Before step 1's install, python-olm is built against the **system** libolm.
Its sdist otherwise builds a bundled C++ libolm 3.2.16 that current Termux
toolchains reject (CMake 4 refuses its `cmake_minimum_required`; clang 21
rejects `include/olm/list.hh`), while Termux's `libolm` package carries the
fix. The driver fetches the pinned `python-olm-3.2.16.tar.gz` from its PyPI
file URL and checks its SHA-256 (not `pip download`: preparing an sdist's
metadata would run the unpatched bundled build), requires `olm_build.py` to match its
recorded SHA-256, applies three fixed edits (system include dir, system lib
dir, no bundled build — no crypto source changes), builds the wheel and
installs it. It needs `$PREFIX/lib/libolm.so` and `$PREFIX/include/olm/olm.h`
(`pkg install libolm`; else `extra_matrix_system_libolm_missing`); any
upstream drift fails as `extra_matrix_olm_build_script_unexpected`. The
receipt's `extras.matrix.olm` records the sdist, original/patched build
script, system libolm and wheel hashes.

The receipt's `extras.matrix` records `status`, the requirements file's
SHA-256 and the exact distributions it added. These versions come from the
index at preparation time; they are recorded evidence, not a lock. Extra
wheels built in the job's private pip cache are carried forward by
`--reuse-wheels-from` like any other cached wheel. The serving gate
(`prepared_runtime.py`) is unchanged: it never inspected extras.

## Per-node preparation timeout (operator file)

A change to a native or build-tool pin/hash (since #2524 — before it, *any*
`bridge/requirements.lock.txt` change) means no wheel reuse (`reuse=none`) and
a full native build. A slow Termux node needs ~50+ minutes
for `native-wheels` alone, more than the `termux-prepare-generation.sh` default
of 3300 seconds: one slow node stayed on `main-fb17fc4-20261021` from 2026-10-22 to
2026-10-26 because #2433 (certifi) changed the lock and every daily tick ended
`native-wheels_timeout` at 3300 s. #2524 since keeps reuse for such
pure-package bumps (see above); a real native change still needs the full
build, so the node's operator records a longer budget in one per-node file
(#2487):

```bash
mkdir -p ~/.config/ccc-node
printf 'CCC_TERMUX_PREPARE_TIMEOUT=6000\n' > ~/.config/ccc-node/termux-prepare.env
chmod 600 ~/.config/ccc-node/termux-prepare.env
```

- **Path:** `${XDG_CONFIG_HOME:-~/.config}/ccc-node/termux-prepare.env` (a
  relative `XDG_CONFIG_HOME` is ignored); `CCC_TERMUX_PREPARE_ENV_FILE`
  overrides the path.
- **Format:** `KEY=VALUE` lines, parsed and **never sourced**; blank lines and
  `#` comments are skipped. Only `CCC_TERMUX_PREPARE_TIMEOUT` is read; other
  keys are ignored with a warning.
- **Value:** an integer 1..**6900** seconds. 6900 is self-update's 7200 s
  prepare-budget maximum minus the 300 s margin that covers the script around
  the build (worktree, receipt gate, repoint); `termux_prepare.py`'s own limit
  is two hours.
- **Fail-safe:** a symlink, a file not owned by the running user, any group or
  world permission bit, or any invalid `CCC_TERMUX_PREPARE_TIMEOUT` assignment
  makes the whole file be ignored with a warning; both scripts then use their
  defaults. An update is never aborted because of this file.
- **Who reads it:** `scripts/termux-prepare-generation.sh` passes the value as
  `--timeout-seconds`, and `ccc-self-update.sh` raises its prepare-cmd budget to
  `value + 300` when that exceeds its 3600 s default (it never lowers it), so the
  self-update watchdog does not kill a build that is still inside its own
  timeout. The shared parser is `ccc_termux_prepare_read_env_file` in
  `scripts/lib/harness-paths.sh`.
- **Precedence:** explicit environment > file > default, per script: an
  exported `CCC_TERMUX_PREPARE_TIMEOUT` (e.g. a manual rerun) wins in
  `termux-prepare-generation.sh`, and an exported
  `CCC_SELF_UPDATE_PREPARE_COMMAND_TIMEOUT_SECONDS` wins in self-update (it is
  then not raised from the file). Under self-update prefer the file, which both
  read.
- **Where it shows:** the effective value and its source (`env`, `file` or
  `default`) appear in `termux-prepare-generation.sh --dry-run`, `--status` and
  its `building …` line, in `ccc-self-update.sh status` (`prepare command:
  configured (timeout 5700s, source=file, termux_prepare_timeout=5400s)`), in
  the `prepare begin timeout=… source=…` log line and, on failure, in the
  audit record's `prepare_budget_source`.

## Time, disk and receipts

One shared command deadline (maximum two hours) covers tool checks, builds,
installation and verification. Timed-out commands and their process groups
are killed and reaped; SIGINT/SIGTERM also cancel the active group and retain
a failure receipt. SIGKILL or power loss cannot provide a completed receipt. `--jobs` allows one or two Cargo jobs. A 2 GiB free-space
preflight is a minimum check, **not a disk or memory quota**. Allow more space
for failures and repeated runs. The job confines pip cache, Cargo target and
temporary build files; Cargo's default shared registry remains outside it.
Thus an empty job means a **clean pip/build cache**, not an empty system or
Cargo source registry. Do not infer fleet downtime from an isolated build.

Stdout and `receipt.json` contain phase status, timing, source/tool/wheel
hashes and actual lifecycle results. Logs and receipts are owner-only (0600)
inside an owner-only directory (0700). Child output remains in private logs;
inspect and redact it before sharing. A failed install is recorded as failed,
not as a readiness success. The driver reuses the observer's four canonical
probe commands directly, so it owns their process groups during cancellation.
Runtime identity is saved separately in private phase logs; the preparation
receipt reports the lifecycle operations actually performed by this driver.

Keep the source checkout and all successful/failed job artifacts until a
separate staging/promotion and recovery plan (#1527) is complete. Copying only
the venv to another location is not a supported deployment procedure. The
helper intentionally leaves rollback, service restart and promotion as
`not_run`.
