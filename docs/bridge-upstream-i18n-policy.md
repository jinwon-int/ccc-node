# Bridge upstream and i18n policy

`bridge/` was vendored from a fork of `terranc/claude-telegram-bot-bridge`, but ccc-node now develops it independently. The upstream relationship is intentionally dropped; upstream changes are not automatically merged.

## Upstream security tracking

- Treat upstream releases/advisories as signals to review, not as automatic update instructions.
- For a security-relevant upstream change, open a ccc-node issue or PR that explains whether the patch is ported, already covered, not applicable, or deliberately rejected.
- Keep ccc-node safety boundaries authoritative: owner allowlists, token redaction, project path scoping, no raw Telegram/provider payloads in logs, and explicit approval for live sends/canaries.

## i18n / canonical docs

- `bridge/README.md` is the canonical bridge README.
- `bridge/README-zh.md` is a maintained translation/companion document.
- When changing operationally important bridge behavior, update the canonical README first and either update the translation in the same PR or add a visible note that the translation needs follow-up.
- Do not put secrets, node-local credential locations, or private fleet data in either README.
