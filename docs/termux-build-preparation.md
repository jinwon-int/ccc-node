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

Run with Termux's base Python, with consistent Android build metadata. The
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
