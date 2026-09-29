- **Wrapper-only keys from the `.env` files now reach the ccc-piri / ccc-codex
  wrappers (#1771).** `CCC_PIRI_REAL_CLI_PATH`, `CCC_PIRI_MEMORY_*`,
  `CCC_CODEX_REAL_CLI_PATH` and `CCC_CODEX_MEMORY_MATERIALIZER_PATH` written to
  the project `.telegram_bot/.env` (or to `bridge/.env` for the Matrix unit,
  which never runs `start.sh`) were loaded into the settings but never handed
  to the wrapper child, so the provider degraded with
  `piri command unavailable` / `Piri runtime failed to start`. The bridge now
  injects exactly this allowlist from its merged settings into the readiness
  probes, the Piri/Codex runtimes and the Piri distill/skill-candidate lanes;
  a value already in the process environment wins and nothing else (no
  secrets) is exported.
