# Skill autosave (Hermes-style auto-skillification)

ccc-node learns frequently-repeated procedures as skill drafts automatically,
with a strict human approval gate. Three layers cooperate:

| Layer | Trigger | What it does |
|---|---|---|
| `claude/hooks/skill-review.sh` | SessionEnd hook (interactive `claude` sessions) | LLM reviews the session transcript and stages `SKILL.md` drafts under `~/.claude/state/pending-skills/`. Never installs anything. |
| `scripts/ccc-skill-autosave.sh` | daily cron (this doc) | Covers what hooks cannot: Telegram-bridge / SDK sessions never fire SessionEnd, so the sweep pushes their recent transcripts through the same skill-review pipeline, refreshes the deterministic candidate report (`skill-suggest/scan.sh`), and queues an owner Telegram notification when drafts await approval. |
| `/skill-suggest` skill | operator (terminal or Telegram) | Reviews pending drafts + ranked candidates and installs approved skills into `~/.claude/skills/`. |

## Enable the daily sweep

```bash
# preview (dry-run is the default)
scripts/install-skill-autosave-cron.sh

# install (daily 20:45 UTC by default; override with --schedule)
scripts/install-skill-autosave-cron.sh --apply

# remove
scripts/install-skill-autosave-cron.sh --remove --apply
```

`setup.sh` installs the sweep script to `~/.claude/hooks/ccc-skill-autosave.sh`
but — consistent with the other cron installers — never schedules it itself.

## Telegram notification

The sweep writes a short, redaction-safe summary file into the bridge push
spool (`~/.claude/state/telegram-spool/`); the bridge `PushNotifier` delivers
it to the owner chat. The sweep never touches the bot token. Delivery requires
the bridge opt-in in the bridge `.env`:

```dotenv
CCC_PUSH_ENABLED=true
# CCC_PUSH_CHAT_ID=<owner chat id>   # optional when ALLOWED_USER_IDS has one entry
```

A notification fires only when the pending-draft count changed since the last
notification, so a quiet node stays quiet.

## Review / approve from Telegram

Ask the bot to run `/skill-suggest` (or "스킬 후보 검토해줘"). It lists pending
drafts and ranked candidates; approval copies the draft into
`~/.claude/skills/<name>/SKILL.md`. Nothing is ever installed without approval.

## Operations

```bash
~/.claude/hooks/ccc-skill-autosave.sh status   # pending count, ledger, log tail
touch ~/.claude/state/skill-autosave.disabled  # off-switch (sweep)
touch ~/.claude/state/skill-review.disabled    # off-switch (drafting pipeline)
```

Tuning (env): `CCC_SKILL_AUTOSAVE_MAX_SESSIONS` (default 3 transcripts/run —
each drafting run is an LLM call), `CCC_SKILL_AUTOSAVE_WINDOW_DAYS` (2),
`CCC_SKILL_AUTOSAVE_REGROWTH_BYTES` (16384 — a long-lived bridge transcript is
re-reviewed only after growing this much), `CCC_SKILL_AUTOSAVE_NOTIFY` (1),
`CCC_SKILL_AUTOSAVE_SETTLE_SECONDS` (90).
