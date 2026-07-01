# Termux / VPS parity

ccc-node supports both Linux VPS nodes and Android/Termux companions. The portable surface should keep behavior aligned while respecting platform differences.

## Parity checks

| Area | VPS/Linux | Termux/Android |
|---|---|---|
| Temp files | `/tmp` may exist | prefer `${TMPDIR:-$HOME/tmp}` |
| Service manager | systemd unit may exist | verify tmux/supervisor/processes instead |
| Python | distro Python | Termux Python, often newer |
| Bridge | `ccc-telegram-bridge.service` or `bridge/start.sh` | `bridge/start.sh --path "$HOME" -d` / supervisor |
| Native A2A worker | systemd poller | `scripts/a2a-termux-native-worker.sh` / glibc runner path |

## Rules

- Do not hardcode `/tmp` in tests or rollout scripts unless the test is explicitly checking a command-safety string.
- Do not assume `systemctl` exists on Termux.
- Keep credentials in node-local 0600 files and report only presence/source class.
- Treat bridge restart, provider canary, and A2A worker restart as separate scoped approvals.

## CI smoke

GitHub Actions should at least run selected tests with `TMPDIR=$HOME/tmp` to catch accidental writable-`/tmp` assumptions without requiring a full Android emulator.
