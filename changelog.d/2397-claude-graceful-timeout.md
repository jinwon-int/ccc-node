- **Timed-out `claude -p` children get SIGTERM before SIGKILL (#2397).** A
  claude CLI killed with SIGKILL inside its ~0.5 s OAuth refresh window loses
  the rotated refresh token, and the next call wipes the host login
  (reproduced). SIGTERM is safe because the CLI finishes saving first. nunchi
  judge-batch, nunchi synth and skill-promotion `_run` used
  `subprocess.run(timeout=)`, which SIGKILLs on timeout. They now use
  `run_with_graceful_timeout`: SIGTERM first, then SIGKILL only after a 10 s
  grace. The bridge distill stop grace goes from 0.25 s to 10 s.
  auto-distill is not changed here, because its `extract_json` is on the
  evaluation-receipt surface and needs a reissued receipt.
