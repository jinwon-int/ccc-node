- **auto-distill runs claude behind a graceful-stop wrapper (#2397).**
  auto-distill's per-call timeout SIGKILLs its direct child. A claude CLI that
  gets SIGKILL inside its OAuth refresh window wipes the host login, but
  SIGTERM is safe. Resolved and `--model-cmd` claude commands now run as
  `model_command.py --claude-graceful-exec <argv>`. The wrapper relays stdio
  and sets `PR_SET_PDEATHSIG=SIGTERM` on claude (Linux and Android/Termux).
  Its own death therefore reaches claude as SIGTERM, and the caller's pipes
  close at once. `auto-distill.py` and its evaluation receipt are unchanged.
