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
