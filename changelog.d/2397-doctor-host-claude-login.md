- **doctor and `/node-status` flag a wiped or expiring host Claude login
  (#2397).** When a `/login` session fails to refresh, the CLI rewrites
  `~/.claude/.credentials.json` with empty tokens and `expiresAt=0`. Every
  bridge turn and `claude -p` cron then fails while all units stay active.
  The new doctor row `claude host credentials` reads the file through the
  owner-only 0600 loader and never renders a token value. A wiped login and
  an expired long-lived (`claude setup-token`) token are 수동필요, and a
  long-lived token within 30 days of expiry is 경고. A refresh login is
  정상. A missing file is 해당 없음, and a file that is unreadable, not 0600
  or a symlink is 경고 확인 불가. `/node-status` gains a `claude login` line
  that checks with `grep -q` only.
