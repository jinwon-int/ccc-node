# Android / Termux install notes (high Android versions)

The ccc-node harness and the Telegram bridge run fine under Termux. The hard part
on **recent Android (Android 16 / certain Samsung firmware, e.g. Galaxy S23 Ultra
class)** is the **Claude Code CLI itself**, not this repo. This page captures the
blocker and the working path so the next Android node does not have to re-discover
it from scratch.

> Field-verified on `daegyo` = Samsung **SM-S918N** (S23 Ultra), Android 16,
> kernel `5.15.189-android13-8`, aarch64, `getconf PAGE_SIZE = 4096`.
> Contrast: an older-Android Termux node (`gongyung`) runs glibc-native fine.

## TL;DR

- **glibc-native Claude Code does not run** on these devices (the native binary
  fails to link). This is a device/firmware-level blocker, *not fixable from
  userspace*.
- Use the **JS-pinned, non-proot path**: `@anthropic-ai/claude-code@2.1.112`
  (the last pure-JS release) on Termux's bionic Node, with the auto-updater hard
  locked so it can never pull a native build.
- The installer + playbook live in **[`jinwon-int/android-fleet-ops`](https://github.com/jinwon-int/android-fleet-ops)**
  (`scripts/install-claude-js-pinned.sh`, `playbooks/claude-code-native-runtime.md`).
  This repo (ccc-node) only needs the bridge-side tweaks noted below.
- Avoid proot for the runtime: it works but is ~9x slower.

## Bridge dependency builds: Rust toolchain is a hard prerequisite (#968)

Hash-locked installs (`bridge/requirements.lock.txt`) may contain packages
with **no Android wheel** — e.g. `cryptography` 50, which builds via maturin
and therefore needs **Rust**. A missing toolchain killed the `daegyo` bridge
on 2026-08-06 (restart -> lock reconcile -> maturin failure -> 4h15m outage);
`gongyung` survived only because Rust was already present.

- `setup.sh` now installs `rust` + `rust-std-aarch64-linux-android` via `pkg`
  on Termux (or prints the exact install line when it cannot).
- `dependency_bootstrap.py` warns upfront when an Android/Termux host lacks
  cargo, and its install-failure message names the toolchain as the likely
  cause. `CCC_DEPS_UNLOCKED=1` does **not** bypass a missing toolchain.
- Manual fix: `pkg install rust rust-std-aarch64-linux-android`.

## Root cause — why glibc-native fails

Symptom when launching the native node/claude binary:

```
Could not find a PHDR: broken executable?
# or
CANNOT LINK EXECUTABLE ... library "libstdc++.so.6" not found
```

What is actually happening:

1. `termux-exec` rewrites every `execve` to
   `/system/bin/linker64 <interp> <binary>` to get around SELinux W^X on the
   Termux data dir (this is intended Termux behavior).
2. On S23U-class firmware the patched `PT_INTERP` is ignored, so the **bionic**
   `linker64` ends up loading a **glibc** binary. glibc's own `ld.so` never gets
   to take over, and the link aborts.
3. Even `ld.so libc.so.6 --version` fails identically — so it is **not** specific
   to node or claude; it is a glibc-loader vs. bionic-linker incompatibility on
   this OS/firmware. Same-version, byte-identical (md5) binaries that work on an
   older Android node still fail here.

Why we can't just rebuild it:

- Claude Code switched from JS to a **closed, glibc-only native binary at
  `v2.1.113`** (see [anthropics/claude-code#50270](https://github.com/anthropics/claude-code/issues/50270)).
  Closed binary → no bionic recompile possible. (Contrast: `opencode-termux-native`
  works only because it is recompiled from source for bionic.)
- It is **not** a 16 KB page-size problem — the device reports a 4 KB page size.

Independent confirmation on identical hardware:
[gtbuchanan/claude-code-termux#20](https://github.com/gtbuchanan/claude-code-termux/issues/20)
(Samsung S918B/S23U, same kernel) — maintainer verdict: *"not fixable from
userspace"*, firmware-patch-level dependent (a Fold5 on the same kernel works,
S23U does not).

## The working path — JS-pinned 2.1.112, non-proot

- Install `@anthropic-ai/claude-code@2.1.112` and run it directly on Termux's
  bionic Node (no proot, no glibc runtime swap).
- **Pin hard** — reject any version other than `2.1.112` and triple-lock the
  auto-updater so it can never silently upgrade into a native build:
  - `DISABLE_AUTOUPDATER=1`
  - the equivalent updater-off key in `settings.json` `env`
  - `chmod -R a-w` on the installed package directory
  - a lock marker file
- Use `jinwon-int/android-fleet-ops` `scripts/install-claude-js-pinned.sh`. It is
  **dry-run by default**; a live apply requires the explicit approval envs
  documented in that repo (e.g. `ANDROID_FLEET_CLAUDE_JS_APPROVED=...` plus
  `--execute --confirm-node <node>`).

## ccc-node bridge specifics on Termux

These are already handled in this repo — just be aware of them:

- **HOME-path rewrite** (`setup.sh`): the harness templates assume
  `/root/.claude`; on Termux (`HOME=/data/data/com.termux/files/home`) `setup.sh`
  rewrites the installed hook/command paths to the node's real `$CLAUDE_DIR`.
  See also [Non-root path overrides](../README.md#non-root-path-overrides). (PR #154)
- **Auth-status timeout**: the JS-pinned `claude auth status --json` takes ~5s on
  these devices, which tripped the old hardcoded 5s health probe and produced a
  false `Claude: degraded`. The bridge now reads `CLAUDE_AUTH_STATUS_TIMEOUT`
  (default `15`). Raise it if you still see false degraded states. (PR #165)
- adb/Wireless-debugging persistence on Android 14+ (sleep/reboot drops the
  tcp:5555 listener) is a separate concern tracked in
  [android-fleet-ops#34](https://github.com/jinwon-int/android-fleet-ops/issues/34).

## References

- `jinwon-int/android-fleet-ops`:
  [#41](https://github.com/jinwon-int/android-fleet-ops/issues/41) root-cause
  tracker (kept OPEN as an upstream-dependency tracker),
  [#42](https://github.com/jinwon-int/android-fleet-ops/issues/42) implementation
  tracker, [#43](https://github.com/jinwon-int/android-fleet-ops/pull/43) JS-pinned
  installer, [#40](https://github.com/jinwon-int/android-fleet-ops/pull/40) glibc
  runtime self-test guard (aborts the node-swap on incompatible devices).
- Upstream: [anthropics/claude-code#50270](https://github.com/anthropics/claude-code/issues/50270)
  (JS→native transition — when/if resolved, this blocker disappears).
- Same-device evidence: [gtbuchanan/claude-code-termux#20](https://github.com/gtbuchanan/claude-code-termux/issues/20),
  [AveryRPeterson/android-termux-claude#2](https://github.com/AveryRPeterson/android-termux-claude/issues/2).

## Isolated native validation after dependency changes (#1525)

Ubuntu/Python 3.14 CI is useful compatibility coverage, but is not Android
linker evidence. On a Termux test device, record Android ABI, Python version,
source SHA, dependency-lock hash, patchelf version and timestamp before testing.
Use an owner-only disposable directory and a separate venv; never use the
serving bridge venv or start another Telegram poller for this check.

1. Install the hash-pinned runtime lock into the isolated venv using the same
   bootstrap path and platform environment as the node. Preserve installation
   failure diagnostics without raw environment variables or task bodies.
2. Import `cryptography.exceptions`, `cryptography.hazmat.bindings._rust`,
   `cryptography.hazmat.primitives.ciphers.aead.AESGCM` and `claude_agent_sdk`.
   Perform an AES-GCM round trip with generated throwaway bytes. No model or
   Telegram request is needed.
3. If the libpython linkage defect recurs, call
   `termux_native.ensure_termux_cryptography(python, venv, env, stdout)` from the
   checked-out source. Use the actual isolated Python and venv paths; the
   helper takes its own venv lock. Require a successful return and repeat the
   imports/round trip in a fresh Python process.
4. Reinstall the same pinned cryptography artifact into the isolated venv,
   repeat bootstrap/repair and import checks, and verify that two concurrent
   bootstraps serialize. Keep original extension hashes and owner-only repair
   backups until evidence review finishes.
5. Inject an invalid repair candidate only in the disposable fixture; verify
   a nonzero result, preserved original bytes and an explicit recovery path.
   Missing patchelf and a non-linker import error must fail closed.

Report each step as pass/fail/not-run with the platform and versions; do not
turn Linux mocks or a single import into a claim of Android rollout safety.
#1522 supplied one-time real-device evidence. #1525 remains open until a device
runner collects this evidence automatically and a separately scoped update
recovery check passes. Environment staging and full dependency rollback are
tracked in #1527; native binary repair alone does not provide that rollback.

### Machine-readable baseline receipt (#1525, #1527)

Run the observer with the **environment being checked**, using an absolute
script path so isolated Python mode does not require a package path override:

```bash
/path/to/target-venv/bin/python -I -B /path/to/ccc-node/bridge/runtime_readiness.py \
  --bridge-dir /path/to/ccc-node/bridge --timeout-seconds 60
```

The standard-library observer runs native imports, SDK import, a throwaway
AES-GCM round trip and `pip check`. It performs no pip install, native repair,
model/provider request, Telegram polling or service action. Probe subprocesses
ignore user-site/PYTHONPATH, disable bytecode writes and discard stdout/stderr.
A shared deadline covers child-process waits; a timed-out probe process group
is killed and reaped. Source file reads are size-bounded. Local filesystem and
interpreter metadata calls are not a hard real-time or hostile-code sandbox.
Use trusted source and an operator-controlled environment on a responsive
local filesystem. POSIX group cleanup is required; unsupported platforms
report `not_run`, not success.

One JSON object on stdout uses `ccc.runtime-readiness.v1`. Exit 0 means the
four checks passed, 1 means unready (including timeout/not-run), and 2 means
identity collection failed. The receipt contains UTC time, elapsed/per-check
milliseconds, interpreter/platform and selected installed package versions,
source lock/requirements/pyproject hashes, observed checkout HEAD and tracked
change flag, and the observer file's own hash. Git failure is represented by
null HEAD/change information; an environment hint is not proof of Android.
`python_android_api_level` is Python's Android compatibility/build API, not
the device OS SDK level (read the latter separately from Android properties).
The tracked-change field excludes untracked files. An observer copied outside
the checkout is identifiable by its own hash; the checkout HEAD does not claim
that the observer is installed there.

The lock hash describes desired inputs, not proof that all installed versions
match that lock. `pip check` validates installed dependency constraints. Exit 0
is a readiness observation, not a release gate: it does not authenticate a
provider, prove serving readiness or freeze the environment against concurrent
writers. Future staged activation must hold its own environment/switch lock
and bind the tested environment to the activated one.

`fresh_install`, `reinstall`, `rollback` and `service_restart` remain explicitly
`not_run` in every receipt. Do not mark those #1525/#1527 acceptance criteria
complete from this baseline alone. For performance baselines collect at least
eight receipts per platform under comparable conditions; summarize native,
SDK and pip-check timings separately. Import time is neither installation time
nor observed bridge downtime. If persisting receipts, use an owner-only file:

```bash
(umask 077; /path/to/target-venv/bin/python -I -B \
  /path/to/ccc-node/bridge/runtime_readiness.py > readiness.json)
```

Keep credentials, environment dumps and raw task bodies out of receipts.


### Python build API and Android wheel tags (#1532)

Termux intentionally patches `platform.android_ver().api_level` to its package
build API. In Python 3.14.6-1 this is 24 even on OS SDK 33 or 36. The upstream
[Termux patch](https://github.com/termux/termux-packages/blob/fe7ac2107098332e4ee457e71d823064c8831040/packages/python/hardcode-android-api-level.diff)
explains why a device's OS SDK is not the build target for its Python packages.
The previous bootstrap used `getprop ro.build.version.sdk` as
`ANDROID_API_LEVEL`; maturin then produced Android 33/36 wheels while pip
accepted Android 24 and below. Native import success does not make these tags
consistent.

Bootstrap now derives the build target from its selected venv interpreter's
`sys.getandroidapilevel()` and checks `sysconfig.get_platform()` plus Python's
packaging API. Explicit `ANDROID_API_LEVEL` must match this build target;
unknown/inconsistent interpreter metadata or an incompatible override fails
before pip runs, including on a requirements-cache hit. The child environment
receives this value for locked and unlocked installs; the parent's environment
is unchanged. Termux reconciliation also runs isolated `pip check` after
native/SDK checks, including on cache hits, before reporting success.

For an existing mismatched environment, first preserve it and prepare a separate
venv and source checkout. Remove the old OS-SDK override from that isolated
invocation and run the bootstrap there with its venv Python. Rebuild artifacts
from their original source using the normal hash lock; do not rename wheels,
rewrite installed WHEEL metadata, override pip's supported tags, or disable its
checks. Verify native/SDK/AES and pip checks after installation and forced
reinstallation. If compilation or lock verification fails, retain the log and
failed environment; it is not a successful migration. Only promote an environment
in a separately verified service transition. This source fix does not rewrite
existing wheels or automatically migrate a serving venv.
