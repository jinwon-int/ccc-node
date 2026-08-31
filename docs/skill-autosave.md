# Skill autosave (Hermes-style auto-skillification)

ccc-node learns frequently-repeated procedures as skill drafts automatically.
By default a strict human approval gate applies; the opt-in **auto mode**
(#355) replaces it with machine gates + after-the-fact notification and
rollback, Hermes-style. Three layers cooperate:

| Layer | Trigger | What it does |
|---|---|---|
| `claude/hooks/skill-review.sh` | SessionEnd hook (interactive `claude` sessions) | LLM reviews the session transcript and stages `SKILL.md` drafts under `~/.claude/state/pending-skills/`. In auto mode it then hands fresh drafts to `skill-review/autoinstall.sh`. |
| `scripts/ccc-skill-autosave.sh` | daily cron (this doc) | Covers what hooks cannot: Telegram-bridge / SDK sessions never fire SessionEnd, so the sweep pushes their recent transcripts through the same skill-review pipeline, refreshes the deterministic candidate report (`skillsuggest/scan.sh`), and queues an owner Telegram notification — an approval reminder in approve mode, or the autoinstall install/block notice in auto mode. |
| `/skillsuggest` skill | operator (terminal or Telegram) | approve mode: reviews pending drafts + ranked candidates and installs approved skills into `~/.claude/skills/`. auto mode: post-hoc review — list, audit and roll back auto-installed skills. |
| `scripts/ccc-skill-promotion.py` | daily sweep, explicit opt-in | Every node rescans and stages owner-only local envelopes; only the central publisher collects over SSH and opens bounded **draft intake PRs** in private `jinwon-int/fleet-skills`. It never merges or publishes generated content to ccc-node. |

## Provider support (Claude / Codex)

The install/gate/ledger/rollback pipeline (`skill-review/autoinstall.sh`) is
provider-neutral: it screens a `SKILL.md` and installs the passing draft into a
skills directory. Only the **install target** and a **compatibility screen**
differ per provider. `skill-review/provider.sh` resolves both.

| Capability | Claude | Codex | Piri |
|---|---|---|---|
| Install target | `~/.claude/skills/<name>/` (`CLAUDE_SKILLS_DIR`) | `${CODEX_HOME:-~/.codex}/skills/<name>/` (`CODEX_SKILLS_DIR`) | `${PIRI_CODING_AGENT_DIR:-~/.piri/agent}/skills/<name>/` (`PIRI_SKILLS_DIR`) |
| Machine gates (secret / node-fact / dedup / lint / claims) | ✅ identical | ✅ identical | ✅ identical |
| Mode / daily cap / off-switch / ledger / rollback | ✅ identical | ✅ identical | ✅ identical |
| Codex-compat screen (rejects `claude -p`, `~/.claude`, `CLAUDE_*`) | n/a | ✅ isolates Claude-only drafts as pending | ✅ same screen (shared non-Claude coupling rules) |
| Secure install dir (0700, no-symlink leaf, fail-closed) | existing dir untouched | ✅ created owner-only | ✅ created owner-only |
| Candidate **drafting/collection** (SessionEnd → draft) | ✅ (`skill-review.sh` + `extract.sh`) | ✅ v2 create/patch/write_file/noop engine + real `codex exec` backend + Codex-only default-ON collector (`CCC_CODEX_SKILL_COLLECTOR=false` opts out) | ✅ same collector engine over Piri distill jobs via `RuntimeCliSkillCandidateBackend`, default-ON (`CCC_PIRI_SKILL_COLLECTOR=false` opts out) |

Select the provider explicitly with `CCC_SKILL_PROVIDER=claude|codex|piri`. When
unset it auto-detects: a node with a Codex home but no `~/.claude` and no
`claude` binary resolves to `codex`; everything else stays `claude`
(back-compatible — existing Claude nodes are unchanged). **Piri is
explicit-only**: bridge nodes commonly carry a `~/.piri/agent` tree for A2A
workers while their interactive lane stays Claude, so set
`CCC_SKILL_PROVIDER=piri` in the collector/installer environment (cron line or
systemd drop-in) for the piri install target to engage.

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

### Incremental proposals (#751)

The Codex collector now prefers improving an eligible existing skill over
creating a near-duplicate. Its bounded, redacted inventory contains at most
eight unpinned `autosave-managed` skills, four safe files per skill, 16 KiB per
file and 64 KiB total content. Read-only overlap metadata enters model input
only after credential, endpoint, node-fact and prompt-injection sanitization.
Inventory is advisory: apply always reclassifies and reopens the exact target
under the ownership lock.

Each owner-only pending directory contains exactly one `proposal.json` plus
`meta.json`. The action is one of:

- `create`: a complete new `SKILL.md`;
- `patch`: one unique old-text replacement in `SKILL.md` or an allowlisted
  `references/`, `scripts/` or `templates/` file, bound to its exact SHA-256;
- `write_file`: no-replace creation below an allowlisted support directory,
  bound to provenance revision/hash and `expected_absent=true`;
- `noop`: records the job complete without staging a draft.

Patch and support-file apply run entirely inside the ownership mutation lock.
The engine rechecks ownership, pin state, hashes, path components, link count,
content gates and support caps. It fsyncs a body-free `prepared` ledger row
before mutation and a terminal row after it. An owner-only backup supports
crash recovery; the existing whole-skill archive rollback remains available
for autosave-created skills. Replay after an interrupted terminal append
reconciles the exact target and marker and records one terminal outcome.
Automatic daily-cap reservation uses this same locked ledger, so concurrent
workers cannot consume the final slot twice. Automatic mutation is limited to
rollback-eligible autosave-created skills; an operator-adopted skill can be
improved through explicit approval but is never changed unattended.
Existing-file updates use a Linux `renameat2(RENAME_NOREPLACE)` claim and
no-replace publish sequence. A non-cooperating same-owner rename can cause a
brief unavailable/conflict state, but its entry is preserved rather than
silently overwritten; recovery then fails closed for operator resolution.

Review and apply a v2 draft without copying files by hand:

```bash
AUTO="${CCC_CLAUDE_DIR:-$HOME/.claude}/hooks/skill-review/autoinstall.sh"
bash "$AUTO" render <draft-id>
bash "$AUTO" apply <draft-id>
```

`render` shows target, expected hashes, provenance and exact diff/content.
`apply` routes through the locked ownership engine and archives the reviewed
draft. A directory containing both legacy `SKILL.md` and v2 `proposal.json`
fails closed. Schema-v1 create-only backend output remains accepted and is
migrated in memory; new backend output uses strict schema v2.

## Codex rollout drafting (#1353, opt-in)

The sweep's main loop drafts only from the Claude transcript tree
(`~/.claude/projects/**.jsonl`). Codex sessions live in
`${CODEX_HOME:-~/.codex}/sessions/YYYY/MM/DD/rollout-*.jsonl` with a different
record shape, so their procedures never reach the drafting brain. When opted
in, the sweep runs a **codex branch** right after the Claude draft loop:

1. `codex-rollout-normalize.py` projects each rollout into the Claude
   transcript shape (user/assistant text rows + Bash `tool_use` rows; the
   `bash -lc` wrapper is unwrapped to the script payload) into a branch-local
   tree at `<CCC_STATE_DIR>/codex-normalized/<encoded-cwd>/<session-id>.jsonl`.
   Injected noise (session_meta/base_instructions, developer-role instruction
   blocks, turn_context/world_state/token_count/reasoning) is discarded.
   Headless rollouts that record the conversation only as `event_msg` fall
   back to those events; when real `response_item` messages exist they are
   skipped so turns are not duplicated.
2. The projected transcript is pushed through the SAME `skill-review.sh`
   pipeline with `CCC_SKILL_PROVIDER=codex` and
   `CLAUDE_PROJECTS_DIR=<normalized tree>` — provider.sh routes installs to
   `$CODEX_HOME/skills`, promotion staging reads the branch provider from
   `.autosave-meta.json`, and `scan.sh` reuses the normalized tree via
   `CLAUDE_PROJECTS_DIR` unchanged. The pending queue and the autoinstall
   daily-cap ledger stay shared (`CCC_SKILL_REVIEW_STATE_DIR`), so install
   counts sum across both branches.
3. A separate regrowth ledger (`skill-autosave.codex-seen`, same 16 KiB
   semantics and `MAX_SESSIONS` per-run budget as the Claude branch) prevents
   re-processing unchanged rollouts.

**Opt in** with `CCC_SKILL_CODEX_DRAFTING=1` (or `1` in
`<CCC_STATE_DIR>/skill-autosave.codex-drafting`). Default is off: nodes without
the flag pay nothing — the sessions tree is not even walked.

Safety rails:

- Sessions with `originator=="codex_exec"` are excluded at projection time —
  machine-driven runs must not self-reference into skill drafts (the same bias
  control as promotion's self-review ban). `CCC_SKILL_CODEX_INCLUDE_EXEC=1`
  (or `--include-exec`) lifts it.
- Projection is capped at 512 KiB per session (`--max-bytes`); the downstream
  tail bounds (extract.sh 500 lines / 60 KiB) apply unchanged.

Canary guidance: enable on the heaviest codex-using (hybrid) node first,
observe 1–2 weeks of draft quality, cost, and zero codex_exec self-reference
before rolling fleet-wide.

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

Ask the bot to run `/skillsuggest` (or "스킬 후보 검토해줘"). It lists pending
drafts and ranked candidates. Legacy create-only drafts retain their existing
review path; v2 proposals are rendered and applied through `autoinstall.sh`
as shown above. In the default approve mode nothing is installed or mutated
without approval.

An owner-operated Claude bridge using audience-scoped memory keeps SDK
`setting_sources=[]` so host hooks and unscoped memory settings stay isolated.
For an explicit `/skillsuggest`, `/skill skillsuggest`, or equivalent
`/command`, the bridge instead resolves only the matching owner-owned,
not-group/world-writable, non-symlinked `SKILL.md` under its trusted skill
roots and asks Claude to read that exact file. Other filesystem settings and
unknown slash commands remain disabled/native respectively.

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
4. **Structure lint** (Hermes HARDLINE-style + Agent Skills spec): frontmatter
   with kebab-case `name` (≤64, no leading/trailing/consecutive hyphens),
   routing-friendly `description` (20–1024 chars), non-trivial body with
   headings, and an optional `compatibility` field of at most 500 chars
   (agentskills.io spec).
5. **Body size** (progressive disclosure): a `SKILL.md` over 500 lines is
   isolated as `size oversized-body` — the author splits it into
   `references/` with read-when pointers per the official guidance.
6. **Codex-compat** (Codex provider only): a draft that hard-codes the Claude
   CLI (`claude -p`), the `~/.claude` tree, or `CLAUDE_*` env can't run on a
   Codex node, so it is isolated as pending (`codex-incompat <label>`) instead
   of installed. Prose that merely mentions "Claude Code" is untouched.
7. **Unverified factual claims**: a draft that asserts an exit code, an HTTP
   status, or a pinned version while giving the reader **no way to re-derive
   it** is isolated as pending (`unverified-claim <exit-code|http-status|version-pin>`).
   Any one of a URL, a `file.ext:line` source reference, a shown
   `--help`/`--version` invocation, or a dated "verified" marker satisfies it.
   A cited URL answering 404/410 blocks as `dead-citation http-404`; template
   placeholders (`OWNER`/`REPO`/`NUM`) and private GitHub resources (re-checked
   through `gh`) are exempt, and any network trouble fails **open** so an
   offline cron never blocks on it. Disable with `CCC_SKILL_GATE_CLAIMS=0`;
   skip only the URL probe with `CCC_SKILL_GATE_URLCHECK=0`.

   This gate checks **citability, not truth** — a machine cannot know whether a
   claim is correct, only whether a reader could check it. It is a floor, not a
   guarantee: a fabricated CLI flag, an inverted rule, or a `grep` pattern that
   can never match all pass it. Auto mode still needs periodic factual audit of
   what it has installed.

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

- **Node-local by default**: auto mode itself never touches the ccc-node
  template repo. The separate promotion boundary below is explicit opt-in and
  PR-first; without that opt-in every generated skill remains local.
- **Concurrency-safe**: an atomic single-runner lock means the same checkpoint
  processed many times at once installs exactly once — no duplicate
  candidate/ledger/install rows.

## Private central intake through draft PRs

The intake helper closes the fleet-sharing gap without granting generated
content or every node direct GitHub authority. Enable local staging per node
with an owner-only state file:

```bash
printf 'true\n' > ~/.claude/state/skill-promotion.enabled
chmod 600 ~/.claude/state/skill-promotion.enabled
python3 ~/.claude/hooks/ccc-skill-promotion.py status
python3 ~/.claude/hooks/ccc-skill-promotion.py run --dry-run
```

`run` never calls GitHub or SSH. It writes a `0600` content-addressed envelope
under `~/.claude/state/skill-promotion/outbox/`; `export` is a read-only SSH
transport command, and a successful central publication moves the retained
envelope to the owner-only `sent/` directory.

Exactly one central node is separately enabled as publisher. Its collector
file contains canonical SSH aliases, one per line:

```bash
printf 'true\n' > ~/.claude/state/skill-promotion.publisher
printf '%s\n' dungae nosuk soonwook > ~/.claude/state/skill-promotion.collect-nodes
chmod 600 ~/.claude/state/skill-promotion.publisher \
  ~/.claude/state/skill-promotion.collect-nodes
python3 ~/.claude/hooks/ccc-skill-promotion.py collect --dry-run
```

The existing daily skill-autosave cron runs both local staging and collection.
On ordinary nodes `collect` reports `publisher_enabled=false` without GitHub or
SSH access. The central live collector authenticates with `gh`, checks that
`jinwon-int/fleet-skills` is `PRIVATE`, pulls at most a bounded number of
envelopes with batch SSH, and opens at most one draft PR by default
(`CCC_SKILL_PROMOTION_MAX_PRS_PER_RUN`, range 1–3). A public or internal target
fails before any remote export, clone, push, or PR operation.

Only unpinned, rollback-eligible schema-v2 skills with
`created_by=ccc-node`, an exact current ownership hash, safe owner-only paths,
and a bounded UTF-8 file tree are eligible. The envelope includes `SKILL.md` and
only `references/`, `scripts/`, and `templates/`; local provenance markers are
not published. A fresh scan rejects credential-shaped data, node-specific
paths/addresses/accounts, redaction markers, and Claude/Codex runtime coupling.
Runtime-coupled skills stay local for manual adaptation rather than being
misclassified as fleet-shared. The private approved snapshot is also checked
for normalized-name and description-similarity duplicates before a branch is
published.

Each proposal writes exactly one candidate under
`intake/<node>/<provider>/<candidate-id>/` on a content-addressed branch. Raw
intake PRs are private and intentionally non-mergeable. An independent reviewer
must generalize and sanitize the candidate, then create a clean PR from current
private `main` under `approved/shared`, `approved/claude`, or `approved/codex`.
No path automatically copies generated content into public ccc-node. If the
same branch and PR already exist, the next sweep reuses it and acknowledges the
local envelope. `CCC_AUTONOMY=dry-run` previews staging/collection and
`CCC_AUTONOMY=kill` stops both with the rest of the autosave sweep.

### Drop-recommendation sweep report (#1363)

When the auto-revision gate's reviser answers `outcome: drop_recommendation`,
the publisher records it in its ledger (`kind a2a-revise-result`, status
`drop-recommended`) and leaves the intake PR open. Drop execution is always a
human decision (#1357 open decision (b)); the `drop-report` subcommand is the
read-only periodic sweep over those records:

```bash
python3 ~/.claude/hooks/ccc-skill-promotion.py drop-report
# weekly, optionally with human-processed bookkeeping:
python3 ~/.claude/hooks/ccc-skill-promotion.py drop-report --ack <task-id>
```

Each pending item joins the drop record with its revise dispatch and carries
skill name, author node, provider, intake PR link, revise round,
`dropRecommendation.reason`, and the first recorded time. The summary reports
new items (recorded since the previous sweep), total pending items, pending
lineages, and per-node distribution. The report never deletes a ledger record,
comments, or touches a PR; GitHub automation is intentionally absent.
Two `0600` owner-only state files beside the ledger carry the bookkeeping under
the same safety invariants: `drop-report.acked.jsonl` (task ids a human marked
processed via `--ack`, repeatable and idempotent) and
`drop-report.sweeps.jsonl` (task ids prior sweeps already showed, so `new`
stays accurate). Both reads fail closed on unsafe state, the report stays
viewable under `CCC_AUTONOMY=kill`/`dry-run`, and a failed sweep recording only
degrades the `new` computation (reported as `sweep_recorded: false`). The
publisher node runs the report on a weekly cron (see the deployment record for
the installed schedule).

### Canonical intake review dispatcher/handler

The review lane's node-side runtime is canonical in this repo:
`scripts/a2a-intent-dispatcher.sh` (intent routing) and
`scripts/skills-intake-review-handler.sh` (rubric execution + verdict result
composition), deployed to each worker by
`scripts/install-a2a-review-handler.sh [--dest DIR]` with an automatic backup
of any previous copy. Historically these were hand-copied per node and forked;
two 2026-08 fleet bugs trace to that drift: a composer that omitted the
snake_case `head_sha` binding (the publisher discarded every verdict as
malformed) and a hardcoded `claude` invocation (review capacity died with one
provider's quota).

Node configuration lives in the worker env file the handler child inherits:

- `REVIEW_AGENT_BIN` / `REVIEW_AGENT_ARGS` — the reviewer executable and
  arguments (default `claude` / `-p --disallowed-tools *`); a node whose main
  bridge runs another agent pins its own, e.g. a grok node sets
  `REVIEW_AGENT_BIN=/opt/piri/pi-test.sh` and
  `REVIEW_AGENT_ARGS="-p --no-tools --model xai/grok-4.6"`;
- `REVIEW_TIMEOUT_SEC` — reviewer wall clock (default 480);
- `INTAKE_REVIEW_HANDLER` / `DEFAULT_TASK_HANDLER` — dispatcher routing
  overrides (defaults resolve next to the installed dispatcher);
- reviewer identity resolves from `WORKER_ID`, then `A2A_WORKER_ID`.

The result contract binds `head_sha` (snake_case) to the dispatched head —
the publisher's `_verdict_from_task` discards unbound outputs as malformed by
design, so the snake_case keys are load-bearing; camelCase mirrors exist only
for older receipts tooling. Negative verdicts (`revise`/`reject`) currently
terminate the broker task as failed without a preserved result
(a2a-nexus#2016), so only approve verdicts reach the publisher until that gap
closes.

### Installing approved private skills

Setup installs the consumer beside the autosave hooks, but does not run it.
After an `approved/*` PR has passed independent review and merged, choose its
exact 40-character commit SHA and preview the node-local transaction:

```bash
SYNC=~/.claude/hooks/ccc-fleet-skills-sync.py
python3 "$SYNC" plan --ref <exact-commit-sha>
python3 "$SYNC" apply --ref <exact-commit-sha>
```

The consumer authenticates read-only, rechecks that `jinwon-int/fleet-skills`
is private, checks out exactly that commit, and independently validates the
approved tree without executing repository-provided code. `shared` installs to
both Claude and Codex; provider-specific audiences install only to that target.
Floating `main`/tags, symlinks, body limits, scanner failures, duplicate names,
invalid approvals, and existing user-owned targets fail closed before any
installation. Managed updates use an atomic replacement with retained local
backup and exact-commit marker. Automatic pruning is intentionally absent.

Applying or changing a fleet pin is a foreground rollout action. The intake
cron never installs a candidate or advances an approved commit on its own.

## Autonomous mutation ownership contract (#750)

This contract establishes who a background curator may modify. The #751
incremental engine above consumes this classification and exact-target
contract. Foreground user-approved installs and the transactional
`ccc_codex_skills.py` setup/self-update path remain separate authority domains
and do not bypass this autonomous write gate.

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
reusable capability. The #751 apply path performs equivalent exact target and
provenance validation again under the mutation lock; it never treats a prior
read or inventory snapshot as write authority.

## Usage telemetry and deterministic lifecycle curator (#752)

`skill-review/curator.py` adds a Hermes-style lifecycle on top of the #750
ownership contract. It manages **only `autosave-managed` skills** — provenance
is declared by the marker contract, never inferred. Everything else
(user-owned, managed/bundled, external/repo-installed, unknown) is observed
for telemetry but can never be transitioned.

**Telemetry (body-free).** `~/.claude/state/skill-autosave-usage.json`
(0600) records per-skill `view_count`, `use_count`, `patch_count`,
`last_*_at` timestamps, lifecycle `state` and `archived_at` — counters and
ISO timestamps only, never content. Sources: a `PostToolUse(Skill)` hook
(`curator-bump.sh`, fail-open — a non-blocking lock degrades the bump instead
of ever stalling a foreground skill call), plus patch/install events
recomputed from the ownership ledger. Pin state stays authoritative in
`skill-autosave-control.json`; reports merge it live. Lifecycle mutations
fail closed when telemetry or provenance is unreadable, and every mutating
command loads the usage store inside the mutation lock so a concurrent bump
can never be silently overwritten.

**Deterministic lifecycle.** States are `active → stale → archived` with
reactivation on fresh activity; the anchor is the latest activity timestamp
or the first-sight seed time (never epoch — a newly seen skill gets a full
fresh window). `stale` is display-only; `archived` is an atomic same-filesystem
move into the owner-only `~/.claude/state/skill-autosave-archive/` (the run
fails closed on a cross-device archive root). There is **no permanent delete**
anywhere: every archived skill restores with `restore`, and a restored skill
keeps its old anchor, so pin it if it must stay live. Pinned, non-autosave
provenance, and never-used skills younger than the stale window are always
protected. The first `--auto` run only seeds the interval timer; `--auto`
also skips while any skill activity is newer than `min_idle_hours` (a session
may be live). Every mutation is a prepared→terminal transaction in the
ownership ledger, and a crash mid-move is reconciled on the next locked
command (`prepared` rows are finished `archived`/`restored`/`aborted` or
fail closed as `conflict`).

**Backup / rollback.** A mutating `run` first snapshots every autosave-managed
skill plus usage/control metadata into
`~/.claude/state/skill-autosave-curator-backups/<utc-id>/` (owner-only,
body-free manifest); a systemic backup failure aborts the run. A single
unsafe skill (symlink members, oversized file) is **quarantined** instead:
excluded from the snapshot, never transitioned that run, and reported in the
run's `quarantined` list — repair the member and the next run proceeds.
Retention keeps the newest `backup_keep` snapshots (default 5) — pruning
never touches them. `rollback [--id]` restores a snapshot (usage/control
metadata, skills archived since the backup move back, drifted live content is
restored after staging) and takes a safety snapshot of the current state
first, so a rollback is itself undoable. A rollback that fails mid-plan is
recorded `conflict` with the applied count (never a silent "aborted"), and a
crash-interrupted rollback recovers to `conflict` on the next locked command.
The curator backup is lifecycle insurance and is **separate
from the install rollback ledger** (`autoinstall.sh rollback` reverts a single
install; `curator.py rollback` restores a whole pre-run snapshot — they share
no state and never move each other's files).

**LLM consolidation is not implemented.** The curator never calls a provider;
setting `CCC_SKILL_CURATOR_CONSOLIDATE=true` makes `run` fail closed
(`consolidation_not_implemented`) so a future phase-3 cannot activate by
accident.

```bash
CUR=~/.claude/hooks/skill-review/curator.py
python3 "$CUR" status [name]     # per-skill classification + telemetry (JSON)
python3 "$CUR" report            # aggregate owner-only report
python3 "$CUR" run --dry-run     # preview transitions, writes nothing
python3 "$CUR" run               # operator-explicit transitions (backs up first)
python3 "$CUR" pin <name>        # protect (delegates to the ownership contract)
python3 "$CUR" archive <name> / restore <name>
python3 "$CUR" list-archived
python3 "$CUR" backup --reason manual / list-backups / rollback [--id <id>]
```

**Sweep integration is opt-in and fleet-gated.** The daily sweep runs the
curator only when `CCC_SKILL_CURATOR_ENABLED=true` (default off — enabling is
a rollout decision). Under autonomy `dry-run` the sweep passes `--dry-run`;
under `kill` the sweep never starts. Tuning (env):
`CCC_SKILL_CURATOR_STALE_AFTER_DAYS` (30), `CCC_SKILL_CURATOR_ARCHIVE_AFTER_DAYS`
(90), `CCC_SKILL_CURATOR_MIN_IDLE_HOURS` (2), `CCC_SKILL_CURATOR_INTERVAL_HOURS`
(24), `CCC_SKILL_CURATOR_BACKUP_KEEP` (5), `CCC_SKILL_CURATOR_NOW` (test-only
clock pin, UTC). Codex nodes share the same contract (`CCC_SKILL_PROVIDER=codex`);
only the `PostToolUse` bump is Claude-only, so Codex telemetry is ledger-derived
(patch/install events) — the lifecycle math is identical.

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

## Dual-broker review dispatch (#2024)

The intake review lane and its R2 revision round are broker-routed:

- **Primary broker** — `CCC_SKILL_PROMOTION_BROKER_URL` (default
  `http://127.0.0.1:8787`) with `A2A_EDGE_SECRET` from the local environment.
  Reviewers are keyring workers minus the author, intersected with the
  broker's online workers (unchanged behavior).
- **Remote brokers** — optional registry in
  `CCC_SKILL_PROMOTION_REMOTE_BROKERS` (JSON array). Each entry:
  `{"name", "ssh_host", "broker_url", "nexus_dir", "secret_cmd"}` where
  `nexus_dir` is the dispatcher checkout on that host and `secret_cmd` is a
  remote shell command that prints that broker's edge secret. The secret is
  evaluated only on the remote host — dispatch and polling run over SSH
  there, and the value never transits the publisher node.

Review dispatch prefers the primary broker and falls through to the remote
brokers when no eligible reviewer is online there (e.g. a whole-broker outage
or an exhausted reviewer pool). The R2 revision round is dispatched to the
broker where the AUTHOR node is online, so an author homed on the secondary
broker receives its revision task there; when the author is online on no
configured broker the round is skipped with `revise_author_offline` as before.

Verdict and revision-result polling route through the broker recorded on each
ledger row (`"broker": "primary" | <name>`); rows written before #2024
continue to poll the primary broker. Every recorded verdict still lands in
the promotion ledger, so a broker outage delays consumption but loses nothing.

The rotation tools carry the same dual-broker discipline:

- `scripts/a2a-rescreen-rotation.py` — `probe` also queries each registry
  broker over SSH (secret sourced remotely, never transiting the planner
  node) and tags every worker with its home broker; `plan` carries the
  broker through to each assignment (multi-broker duplicates collapse to
  one reviewer, primary preferred); `manifests --dispatch` builds each
  manifest for the assignment's broker and dispatches remote reviewers via
  the publisher's `_remote_dispatch_round` path. Successful dispatches
  append an `a2a-dispatch` ledger row with the broker recorded, so the
  nightly `collect` consumes rescreen verdicts on the right broker.
- `scripts/rescreen-rotation.py` — the pool already spans primary + registry
  brokers; successful dispatches now append the same `a2a-dispatch` ledger
  row (`"broker"` field, `"rescreen": true`), closing the gap where a
  rescreen verdict landed on the broker but was never collected.

### Rescreen rotation — standard procedure (#2028)

Re-review rounds (e.g. after a reviewer-pool outage or a rubric revision) are
generated by `scripts/rescreen-rotation.py`, never by hand-written rotation
scripts:

```
python3 scripts/rescreen-rotation.py --cases CASES.json [--names a,b] \
  [--dry-run] [--output OUT.json] [--exclude-hours 6]
```

- The reviewer pool is **live broker state**: keyring workers ∩ online
  workers per broker (primary first, then the #2024 remote brokers), minus
  the author node. No hand-maintained healthy lists.
- Nodes with a `task.failed` audit event inside `--exclude-hours` are skipped
  automatically and the exclusion (node, broker, failure count) is recorded
  in the results JSON.
- Assignments are **deterministic** and provider-diverse: the pool rotates
  through provider groups (from `implementationCapability` on the broker
  projection; nodes without it report `unknown`) starting at the case
  ordinal, so identical state produces identical distribution.
- `--dry-run` (or `RESCREEN_DRYRUN=1`) builds the rotation without
  dispatching. Every result entry records the reviewer, broker, provider,
  model tier, rotation reason, and any skipped nodes — the assignment
  rationale is auditable, matching #2027 provenance.
