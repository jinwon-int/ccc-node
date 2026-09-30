- **Slash commands run their inline scripts via `bash` (#2078, security audit
  2026-09-30).** `/security-audit`, `/doctor`, `/agent-cron` and
  `/node-status` executed repo scripts directly from `!` inline commands,
  which fails on Termux without termux-exec (`/usr/bin/env: bad
  interpreter`). They now invoke `bash <script>`; `allowed-tools` gains the
  `bash <path>` form (direct-path entries kept), and `/node-status` gets a
  narrow status-only entry for the bridge probe. Shebangs are unchanged.
