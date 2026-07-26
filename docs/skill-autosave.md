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
| Candidate **drafting/collection** (SessionEnd → draft) | ✅ (`skill-review.sh` + `extract.sh`) | ✅ engine + real `codex exec` backend + Codex-only default-ON collector (`CCC_CODEX_SKILL_COLLECTOR=false` opts out) |

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

The collector loop is default **on for Codex nodes**
(`CCC_CODEX_SKILL_COLLECTOR=false` opts out).
`SkillCandidateCollectorWorker` reads the distill journal's snapshots
**read-only** (it never claims or mutates a distill job, so memory distill is
unaffected), drafts via the backend, and stages pending drafts through the
idempotent sink for the provider-aware installer. Composition remains
three-guarded (Codex node **and** no explicit opt-out **and** a distill journal),
so Claude startup is unchanged. A sweep attempts one unprocessed job by default
(`CCC_CODEX_SKILL_COLLECTOR_MAX_JOBS_PER_SWEEP`, range 1–10), shares the
autonomous Codex usage meter, takes a non-blocking per-job lease, refunds
reservations abandoned before provider start, and durably backs off failed or
canceled attempts. See
[`codex-skill-collector-activation.md`](codex-skill-collector-activation.md)
for deployment verification and opt-out rollback. This changes candidate
staging only; `CCC_SKILL_AUTOSAVE_MODE` remains `approve` by default.

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
owner-only `.autosave-meta.json` v2 provenance marker. Failing drafts are
**never dropped**: they stay
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
  Every layer that the switch stops or gates (skill-autosave sweep, autoinstall,
  distill) appends one **body-free** line — `{ts, layer, state, detail}`, no
  payload — to a shared fleet ledger at
  `~/.claude/state/autonomy-ledger.jsonl` (owner-only `0600`, newest
  `CCC_AUTONOMY_LEDGER_MAX` lines, default 500), so an operator can see in one
  place what the kill/dry-run switch actually blocked across the node:
  `tail ~/.claude/state/autonomy-ledger.jsonl`.
- **Rollback, always**: every autosave-created install is reversible,
  individually or in bulk — archives to
  `~/.claude/state/skill-autosave-rollback/`, never deletes, and uses the
  shared ownership classifier rather than marker presence alone. Adopted,
  pinned, managed/bundled, conflicting, drifted, and unreadable skills are
  refused:

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

## Autonomous mutation ownership contract (#750)

This contract establishes who a future background curator may modify. It does
not generate or apply a skill patch yet. Foreground user-approved installs and
the transactional `ccc_codex_skills.py` setup/self-update path remain separate
authority domains and do not call this autonomous write gate.

The classifier uses this fail-closed precedence:

1. unsafe/unreadable path or metadata → `unknown/unreadable`;
2. a valid `.ccc-node-managed.json` → `managed/bundled` (wins over an
   autosave marker; a dual-marker conflict is reported and remains read-only);
3. valid current or legacy `.autosave-meta.json` → `autosave-managed`;
4. a verifiable repository/external marker → `external/repo-installed`;
5. a normal readable skill with no autonomous provenance → `user-owned`;
6. the separate pin control overlays any otherwise valid local class as
   `pinned`.

Only `autosave-managed` and unpinned is eligible for an autonomous write
proposal. Every other row is read-only to autonomous work:

| Base ownership | Unpinned autonomous write | Pinned autonomous write | Authority that may change it |
|---|---:|---:|---|
| `autosave-managed` | exact-read guard required | denied | future autosave curator / explicit owner control |
| `user-owned` | denied | denied | foreground owner; `adopt` may transfer control |
| `managed/bundled` | denied | denied | setup/self-update only |
| `external/repo-installed` | denied | denied | its external installer/foreground owner |
| `unknown/unreadable` | denied | denied | repair provenance/path first |

Operator controls are provider-scoped and return JSON with no skill body or
secret. State lives under `~/.claude/state/` in an owner-only directory:
`skill-autosave-control.json` is the pin overlay and
`skill-autosave-ownership.jsonl` is a `0600` body-free audit ledger.
Every owner-state mutation first fsyncs a `prepared` transaction row before
publishing its marker, control file, or receipt, then records a terminal row.
The prepared row is durable evidence even if a later terminal append is
interrupted, so published ownership state is never created without an audit
record.

```bash
AUTO=~/.claude/hooks/skill-review/autoinstall.sh

$AUTO ownership-status                 # all skills
$AUTO ownership-status <name>          # one skill
$AUTO list-unmanaged
$AUTO adopt <name> --dry-run
$AUTO adopt <name>
$AUTO pin <name> --dry-run
$AUTO pin <name>
$AUTO unpin <name>
```

`adopt` accepts exactly one safe, owner-held, provider-root-local user skill.
It refuses traversal, symlinks, hardlinks, managed/external markers, corrupt
metadata, and unsafe roots. Adoption writes `.autosave-meta.json` v2 with
`created_by=operator-adopt` and `rollback_eligible=false`; rollback can never
archive an adopted user skill. Pin is a separate overlay, so unpin restores
the original ownership rather than guessing it. Dry-run performs the same
classification but does not create state, metadata, receipts, or ledger rows.

### Provenance and legacy migration

New autosave markers have `schema_version=2`,
`manager=ccc-node-skill-autosave`, provider/name/opaque target identity,
`created_by` (`ccc-node` or `operator-adopt`), `skill_sha256`,
`provenance_revision`, `rollback_eligible`, and a timestamp. They are regular,
single-link, owner-held `0600` files. The marker contains no skill body.

Migration is conservative and does not rewrite existing files during status:

- a legacy `installed_by=autosave` marker is recognized as revision `0` only
  when it has no `schema_version`, and its name/path shape and recorded
  SHA-256 match the current `SKILL.md`;
- legacy revision `0` remains visible for migration, but destructive rollback
  requires an explicit v2 `created_by=ccc-node` and
  `rollback_eligible=true` marker;
- unknown/future `schema_version` values never downgrade to the legacy parser;
- legacy SHA drift, corrupt metadata, unsafe permissions, or unreadable files
  become `unknown/unreadable`, never an inferred owner;
- a missing autosave marker on an otherwise safe local skill means
  `user-owned`, not autosave-managed;
- repository/external ownership is classified only from verifiable signals;
  it is never guessed from a name;
- valid bundled provenance remains setup/self-update-owned even if an autosave
  marker is also present.

### Exact read-before-write receipt

The future curator must read every exact file it proposes to change through
the deployed ownership tool. The read returns the content to that review
attempt and stores a short-lived owner-only receipt; only the audit metadata is
body-free:

```bash
OWN=~/.claude/hooks/skill-review/ownership.py

python3 "$OWN" read-target <skill> SKILL.md \
  --attempt-id <review-attempt-id> --operation patch > read.json

python3 "$OWN" guard-proposal --proposal proposal.json
```

`proposal.json` has schema version `1` and copies the receipt's `attempt_id`,
`receipt_id`, operation (`patch`, `edit`, or `write_file`), provider, name,
opaque target id, exact root-relative target, expected SHA-256, and expected
provenance revision/hash. The guard reopens the target no-follow from the
validated skill root and compares file device/inode/size/mtime, content hash,
ownership, pin state, and provenance. Mismatch, expiry, cross-attempt or
cross-operation use, path drift, and replay are denied. Every receipt is
single-use and is consumed on an authorization decision or checked denial.
The successful response is a validation result for the exact proposal, not a
reusable capability: a future apply engine must perform this validation
immediately in its own write path. Actual patch/write generation, application,
and that atomic apply integration remain out of scope for #750.

## Migration & rollback (Claude ↔ Codex)

Skills are **not** mirrored across providers automatically — the install target
is chosen from the active provider, never both. To move an autosave-installed
skill between providers, roll it back on the source and let the target node
re-draft/install it, or copy the `SKILL.md` by hand (Codex reads the same
frontmatter/dir layout). Rollback is provider-scoped and marker-driven, so it
works identically on either surface and always refuses hand-authored, adopted,
pinned, legacy, or invalid-provenance skills. Eligibility validation and the
archive rename run under the same ownership lock:

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
default `${CODEX_HOME:-~/.codex}/skills`),
`CCC_CODEX_SKILL_COLLECTOR` (Codex-only candidate collection, default true),
`CCC_CODEX_SKILL_COLLECTOR_MAX_JOBS_PER_SWEEP` (default 1, range 1–10).
