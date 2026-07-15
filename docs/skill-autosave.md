# Skill autosave (Hermes-style auto-skillification)

ccc-node learns frequently-repeated procedures as skill drafts automatically.
By default a strict human approval gate applies; the opt-in **auto mode**
(#355) replaces it with machine gates + after-the-fact notification and
rollback, Hermes-style. Three layers cooperate:

| Layer | Trigger | What it does |
|---|---|---|
| `claude/hooks/skill-review.sh` | SessionEnd hook (interactive `claude` sessions) | LLM reviews the session transcript and stages `SKILL.md` drafts under `~/.claude/state/pending-skills/`. In auto mode it then hands fresh drafts to `skill-review/autoinstall.sh`. |
| `scripts/ccc-skill-autosave.sh` | daily cron (this doc) | Covers what hooks cannot: Telegram-bridge / SDK sessions never fire SessionEnd, so the sweep pushes their recent transcripts through the same skill-review pipeline, refreshes the deterministic candidate report (`skill-suggest/scan.sh`), and queues an owner Telegram notification — an approval reminder in approve mode, or the autoinstall install/block notice in auto mode. |
| `/skill-suggest` skill | operator (terminal or Telegram) | approve mode: reviews pending drafts + ranked candidates and installs approved skills into `~/.claude/skills/`. auto mode: post-hoc review — list, audit and roll back auto-installed skills. |

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
`~/.claude/skills/<name>/SKILL.md`. In the default approve mode nothing is
ever installed without approval.

## Auto mode — unattended install with post-hoc review (#355)

Opt in per node (default stays `approve`; existing nodes are unchanged):

```bash
export CCC_SKILL_AUTOSAVE_MODE=auto              # env (wins), or
printf 'auto' > ~/.claude/state/skill-autosave.mode   # durable state file
```

Drafting is unchanged. What changes is the gate: instead of a human,
`claude/hooks/skill-review/autoinstall.sh` (installed to
`~/.claude/hooks/skill-review/autoinstall.sh`) runs deterministic machine
gates over each pending draft — the Hermes trust model of a narrow write
surface + enforced authoring standards + after-the-fact visibility:

1. **Secret scan** (hard-fail): the redaction scanner's pattern family — GitHub
   /API/AWS tokens, private keys, bearer tokens, literal credential
   assignments, leaked `[REDACTED]` markers, long token-like strings.
2. **Node-specific facts**: `/home/<user>`-style absolute paths, `/root/`
   paths, non-loopback IPs, `user@host`/emails (git@github.com allowed).
3. **Dedup** against installed skills: existing directory is never
   overwritten; normalized-name and description-similarity matches are blocked.
4. **Structure lint** (Hermes HARDLINE-style): frontmatter with kebab-case
   `name` (≤64), routing-friendly `description` (20–1024 chars), non-trivial
   body with headings.

Passing drafts are installed to `~/.claude/skills/<name>/` immediately and
recorded in the `installed-by=autosave` ledger
(`~/.claude/state/skill-autosave-install.jsonl`) plus an in-dir
`.autosave-meta.json` marker. Failing drafts are **never dropped**: they stay
in the pending queue with an `autosave-block.json` reason and keep the normal
human review path. The Telegram push becomes a post-hoc notice ("스킬 자동
설치 N건 …"), not an approval request.

Safety rails:

- **Daily cap**: at most `CCC_SKILL_AUTOSAVE_DAILY_CAP` (default 3) installs
  per UTC day; over-cap drafts stay pending and retry later.
- **Off-switch**: `touch ~/.claude/state/skill-autosave.disabled` also stops
  auto installs.
- **Rollback, always**: every install is reversible, individually or in bulk —
  archives to `~/.claude/state/skill-autosave-rollback/`, never deletes, and
  refuses skills without the autosave marker (hand-authored skills are safe):

  ```bash
  ~/.claude/hooks/skill-review/autoinstall.sh list
  ~/.claude/hooks/skill-review/autoinstall.sh rollback <name>
  ~/.claude/hooks/skill-review/autoinstall.sh rollback --all
  ~/.claude/hooks/skill-review/autoinstall.sh status
  ```

- **Node-local only**: auto mode never touches the ccc-node template repo —
  promoting a skill into `claude/skills/` remains PR-first.

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
`CCC_SKILL_AUTOSAVE_SETTLE_SECONDS` (90), `CCC_SKILL_AUTOSAVE_MODE`
(approve|auto, default approve), `CCC_SKILL_AUTOSAVE_DAILY_CAP` (3 — auto-mode
installs per UTC day).
