- **fix(skill-autosave): a refusing curator now leaves its reason in the log (#2351).**
  `curator.py` reports a refusal as `{"ok": false, "code": "<reason>"}` on
  stdout and exits 2 with nothing on stderr; the sweep logged only
  `curator failed (non-fatal)` and threw that line away. On one node the
  curator failed every night from 2026-10-12 (archive directory mode 755 →
  `unsafe_state_directory`) and the log never said why. The failure line now
  carries the exit code and the first 500 bytes of the curator's stdout. The
  sweep stays non-fatal on a curator failure.
