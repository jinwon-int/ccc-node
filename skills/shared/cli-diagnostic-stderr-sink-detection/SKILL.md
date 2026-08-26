---
name: cli-diagnostic-stderr-sink-detection
description: Diagnose probe/health-check logic that misreports status because stderr was redirected to /dev/null, hiding the authentication or error output it needed to read. Use when a credential/status probe reports failure despite a working login, when a check is silently always-green, or when a test stub and the real command disagree about output streams.
---

## When to Use

- A credential check returns "unauthorized" although login demonstrably works.
- A status probe silently never matches, or never fails, and `2>/dev/null` appears in it.
- A test stub passes while the real command fails (or vice versa).
- A CLI writes its status line to stderr, not stdout (common: `gh`, `git`, `docker`, many auth CLIs).

## Procedure

1. **Reproduce before changing anything.** Run the probe's exact command by hand
   and capture both streams separately — this is the diagnosis, not a formality:
   ```sh
   cmd >/tmp/out.txt 2>/tmp/err.txt; echo "exit=$?"
   ```
   Note which stream carries the status text and what the exit code is.

2. **Locate the sinks.** Search the probe/test code for `2>/dev/null`, `2>&1`,
   `&>/dev/null`, and `>/dev/null`. Each is a candidate discard point.

3. **Decide stream handling deliberately:**
   - Status info on stderr and you must parse it → merge with `2>&1`.
   - Status is conveyed by **exit code** → check `$?` instead of parsing text at
     all; that is more robust than either redirect.
   - Genuine noise only → keep the discard, but comment why.

4. **Harden the anchor string.** Before adopting an anchor like `^logged in`,
   confirm it cannot appear in an *error* message on the merged stream. Anchor to
   line start, be specific, and prefer exit code when available.

5. **Align test stubs to the real command.** If the real command writes to stderr,
   the stub must write to stderr and return the same exit code. A stub that uses a
   different stream makes the test prove nothing.

6. **Mind pipeline exit codes.** In a pipeline the exit status is the last
   command's. Use `set -o pipefail` (bash) or capture `${PIPESTATUS[0]}` when the
   probe's verdict depends on the first command succeeding.

7. **Test both poles.** Run against a known-good (authenticated) and a known-bad
   (revoked/logged-out) state. A probe validated only against success cannot
   distinguish "works" from "always green".

## Safety

- Merging streams can create false positives — an error message containing the
  anchor string will read as success. Pair a merged-stream match with an exit-code check.
- Never silence stderr in tests to quiet noise; fix the command or the assertion.
- Do not print or log captured stderr containing tokens/credentials; redact before recording evidence.

## Verification

- Probe correctly returns success in the authenticated state **and** failure in the
  revoked state (both actually exercised, not assumed).
- `command 2>&1` output and the stub's output match in stream, text, and exit code.
- The anchor string was tested against a real error message and did not match.
