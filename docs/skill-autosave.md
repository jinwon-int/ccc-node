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

## Provider support (Claude / Codex)

The install/gate/ledger/rollback pipeline (`skill-review/autoinstall.sh`) is
provider-neutral: it screens a `SKILL.md` and installs the passing draft into a
skills directory. Only the **install target** and a **compatibility screen**
differ per provider. `skill-review/provider.sh` resolves both.

| Capability | Claude | Codex |
|---|---|---|
| Install target | `~/.claude/skills/<name>/` (`CLAUDE_SKILLS_DIR`) | `${CODEX_HOME:-~/.codex}/skills/<name>/` (`CODEX_SKILLS_DIR`) |
| Machine gates (secret / node-fact / dedup / lint) | ✅ identical | ✅ identical |
| Mode / daily cap / off-switch / ledger / rollback | ✅ identical | ✅ identical |
| Codex-compat screen (rejects `claude -p`, `~/.claude`, `CLAUDE_*`) | n/a | ✅ isolates Claude-only drafts as pending |
| Secure install dir (0700, no-symlink leaf, fail-closed) | existing dir untouched | ✅ created owner-only |
| Candidate **drafting/collection** (SessionEnd → draft) | ✅ (`skill-review.sh` + `extract.sh`) | ✅ engine + real `codex exec` backend + opt-in collector loop (`CCC_CODEX_SKILL_COLLECTOR`, default off); enabling on a node is canary-gated |

Select the provider explicitly with `CCC_SKILL_PROVIDER=claude|codex`. When
unset it auto-detects: a node with a Codex home but no `~/.claude` and no
`claude` binary resolves to `codex`; everything else stays `claude`
(back-compatible — existing Claude nodes are unchanged).

The Codex install pipeline (gates, cap, ledger, rollback, concurrency-safe
single-runner lock) is complete and covered by
`claude/hooks/skill-review/codex-autoinstall.test.sh`.

The Codex-native **collection engine** — `bridge/memory/skill_candidate.py`
(#667) — is landed too: a `SkillCandidateOutput` schema deliberately **separate**
from the memory-fact `DistillExtractionOutput` (it reuses only the neutral
`DistillProvenance`/`DistillTrigger`/snapshot transport), a backend `Protocol`,
and an idempotent owner-only `SkillCandidateSink` that stages pending-draft dirs
in the exact contract the installer above consumes. A staged draft installs into
`CODEX_HOME/skills` end-to-end via `CCC_SKILL_PROVIDER=codex` autoinstall
(covered by `bridge/tests/test_skill_candidate.py`). The real `codex exec`
backend (`CodexExecSkillCandidateBackend`) reuses the schema-neutral isolation
runner `run_codex_exec` (factored out of the memory distill backend, behavior
unchanged) with the skill schema/prompt/parser and a redacted stdin payload.

The **opt-in collector loop** is wired too (`CCC_CODEX_SKILL_COLLECTOR`, default
**off**). When enabled on a Codex node, `SkillCandidateCollectorWorker` reads the
distill journal's snapshots **read-only** (it never claims or mutates a distill
job, so memory distill is unaffected), drafts via the backend, and stages
pending drafts through the idempotent sink for the provider-aware installer.
Composition is three-guarded (Codex node **and** flag on **and** a distill
journal), so every other node's startup is unchanged — verified by the
composition suite. **Enabling the flag on a live node is canary-gated** — follow
[`codex-skill-collector-activation.md`](codex-skill-collector-activation.md)
(baseline → enable → verify clean startup → review first drafts → observe →
widen; rollback = disable + restart).

## Enable the daily sweep

```bash
# preview (dry-run is the default)
scripts/install-skill-autosave-cron.sh

# install (daily 20:45 UTC / 05:45 KST by default)
scripts/install-skill-autosave-cron.sh --apply

# remove
scripts/install-skill-autosave-cron.sh --remove --apply
```

`setup.sh` installs the sweep script to `~/.claude/hooks/ccc-skill-autosave.sh`
but — consistent with the other cron installers — never schedules it itself.
The installer converts the default `20:45 UTC` target into the host cron
daemon's local timezone when writing the crontab (`45 5` on KST hosts,
`45 20` on UTC hosts), and writes a managed `CRON_TZ` block pinned to that
detected system timezone. Cron implementations that support `CRON_TZ` honor
the pin; implementations that do not continue evaluating the already-local
schedule in the system timezone. An explicit `--schedule` or
`CCC_SKILL_AUTOSAVE_CRON` value is interpreted as a raw host-local cron
schedule.

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
5. **Codex-compat** (Codex provider only): a draft that hard-codes the Claude
   CLI (`claude -p`), the `~/.claude` tree, or `CLAUDE_*` env can't run on a
   Codex node, so it is isolated as pending (`codex-incompat <label>`) instead
   of installed. Prose that merely mentions "Claude Code" is untouched.

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
- **Fleet autonomy guard (#386)**: a single switch above every layer's own
  mode. `CCC_AUTONOMY=kill` (or `touch ~/.claude/state/autonomy.kill`) halts all
  autonomous installs; `CCC_AUTONOMY=dry-run` (or `autonomy.dry-run`) gates and
  reports what *would* install (`dry_run:true`, `would_install:[…]`) but writes
  nothing. Default `active` — existing nodes are unchanged. **The daily sweep
  honours the same switch**: under `kill` it exits before doing anything — no
  deterministic scan, no drafting LLM call, no pending-draft staging, no notify
  (`ccc-skill-autosave.sh status` shows the live `autonomy:` line). `dry-run`
  and `active` let the sweep run so drafts still stage for human review; the
  install layer self-guards, so nothing auto-installs under `dry-run`.
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
- **Concurrency-safe**: an atomic single-runner lock means the same checkpoint
  processed many times at once installs exactly once — no duplicate
  candidate/ledger/install rows.

## Migration & rollback (Claude ↔ Codex)

Skills are **not** mirrored across providers automatically — the install target
is chosen from the active provider, never both. To move an autosave-installed
skill between providers, roll it back on the source and let the target node
re-draft/install it, or copy the `SKILL.md` by hand (Codex reads the same
frontmatter/dir layout). Rollback is provider-scoped and marker-driven, so it
works identically on either surface and always refuses hand-authored skills:

```bash
# Codex node (CCC_SKILL_PROVIDER=codex): operates on ${CODEX_HOME}/skills
CCC_SKILL_PROVIDER=codex ~/.claude/hooks/skill-review/autoinstall.sh list
CCC_SKILL_PROVIDER=codex ~/.claude/hooks/skill-review/autoinstall.sh rollback <name>
CCC_SKILL_PROVIDER=codex ~/.claude/hooks/skill-review/autoinstall.sh rollback --all
```

A Claude-authored skill that hard-codes Claude-only couplings is rejected by the
Codex-compat gate on a Codex node (`codex-incompat`) rather than installed —
rework it to be provider-neutral before it can autosave there.

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
installs per UTC day), `CCC_SKILL_PROVIDER` (claude|codex, default auto-detect —
selects the install surface), `CODEX_SKILLS_DIR` (Codex install target override,
default `${CODEX_HOME:-~/.codex}/skills`).
