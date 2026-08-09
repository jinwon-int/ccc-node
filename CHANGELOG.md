# Changelog — ccc-node harness

All notable changes to the Claude Code node harness. Dates are KST.

## [Unreleased]

### Added
- Opt-in fleet skill intake now connects node-local autosave output to the
  private `jinwon-int/fleet-skills` review boundary without giving every node a
  GitHub write credential. Each daily node sweep reclassifies and rescans
  rollback-eligible `autosave-managed` skills, then writes only owner-only,
  content-addressed local outbox envelopes. An independently enabled central
  publisher collects them over batch SSH and opens bounded private draft PRs
  under `intake/` only after verifying repository visibility is `PRIVATE`.
  Raw intake is never merged, installed, or written to public ccc-node;
  approved content must be rebuilt from `main` under `approved/*` after human
  semantic review. Secret-shaped data, node facts, runtime coupling, unsafe
  paths, adopted/pinned skills, and approved name/description collisions fail
  closed. Fleet autonomy kill/dry-run and body-free local receipts remain in
  force.
- Approved private skills can be consumed with
  `ccc-fleet-skills-sync.py plan|apply --ref <exact-commit>`. The consumer
  re-verifies private visibility and the complete approved tree, refuses
  floating refs, symlinks, sensitive/node-specific content, and user-owned
  target conflicts, installs shared/provider-scoped copies atomically with
  exact-commit provenance, and retains rollback backups plus a body-free local
  receipt. It is installed by setup but never applied automatically.

### Changed
- Codex skill dedup and gh-pr-flow path cleanup (TM-2331 follow-ups). The
  zero/low-drift Codex ports `gh-ci-wait` and `fleet-disk-constraint-triage`
  are deleted from `codex/skills/` and now provision straight from
  `skills/shared/` — the collector's managed-skill contract accepts
  `skills/shared/<name>` sources, and the shared skills carry the Codex
  `agents/openai.yaml` interface recovered from the deleted ports. The
  shared fleet-disk text no longer names a runtime-specific Wiki skill.
  `ccc-wiki-record` stays a genuine adapted port (distinct name and prose),
  documented in docs/codex-managed-skills.md. `gh-pr-flow` helper paths
  resolve via `GH_PR_FLOW_DIR` (installed copy → template checkout
  fallback) instead of a hardcoded `~/.claude` path, so the skill text is
  correct under `CCC_CLAUDE_DIR` overrides and stale installs.

### Fixed
- Memory distill: `CCC_MEMORY_ASSISTANT_LABEL`'s built-in default was
  hardcoded to `"dungae, a Hermes Team2 worker"` in three places
  (`bridge/utils/settings_memory.py`, `claude/hooks/distill/extract.sh`,
  `claude/hooks/distill/pending_journal.py`), diverging from the documented
  `.env.example` default of `"ccc-node assistant"`. Any node that never set
  this explicitly — confirmed live on gongyung, nosuk, and soonwook — was
  telling the memory distill model (and therefore Honcho/Wiki-bound extracts)
  that it was dungae, regardless of which node actually ran the turn. All
  three defaults now match `.env.example`. Nodes should still set an explicit
  per-node `CCC_MEMORY_ASSISTANT_LABEL` for accurate identity, but no longer
  silently misattribute to another node when it's unset.

### Changed
- Repo skills now live in two trees and install as refreshed, near-atomic
  copies. `skills/shared/` holds runtime-agnostic skills (wiki-record,
  gh-ci-wait, fleet-disk-constraint-triage, debug-long-running-agent-tasks,
  moved out of `claude/skills/`, which keeps harness-coupled ones); the codex
  compatibility catalog and the collector's asset roots follow the move.
  setup.sh replaces the overlay `cp -r` with a per-skill stage+rename refresh
  plus a manifest (`state/repo-skills.manifest`): skills the repo no longer
  ships are pruned when the installed copy is unmodified, kept with a warning
  when the node edited it, and node-local skills are never touched. Copies
  stay real dirs — the managed-artifact guard refuses symlinks in managed
  paths by design, so freshness comes from self-update running setup.sh
  (2026-08-07 gongmyoung's stale gh-pr-flow was a dead-updater drift, not a
  copy-format flaw). Supersedes the fleet-skills repo plan (Wiki TM-2331).
- SessionStart audience search is parallel under one global budget (#897 step
  2). The local/recent/shared/legacy lanes used to run serially (3+1+3+2s,
  up to ~9s of the 15s hook budget); they now run concurrently with a global
  3s wait budget (`CCC_MEMORY_SEARCH_GLOBAL_TIMEOUT_SEC`), each lane keeping
  its own inner timeout so a cut wait never orphans an unbounded search.
  Measured on a 2s-per-lane fixture: 8.1s → 3.0s. `CCC_MEMORY_SEARCH_PARALLEL=0`
  restores the serial path; non-audience mode is unchanged.

### Added
- Claude runtime: registered the `fable` model alias (`claude-fable-5`) in
  `CURATED_CLAUDE_MODELS` and made it the new default, ahead of `sonnet`,
  `opus`, and `haiku`. The Claude CLI already resolved this alias
  (`claude --model fable`); the bridge's static curated set was the only
  place it was missing, so `/model` and new sessions still defaulted to
  `sonnet`.

### Fixed
- Dependabot pip updates now cover the CI lock directory too. The pip entry
  scanned only `/bridge`, so each bump edited `bridge/requirements.lock.txt`
  without its pair `.github/requirements/bridge-ci.txt` and failed the #349
  drift test (the tqdm/openai/cffi PRs each needed a manual sync commit).
  The update config now lists both directories so one bump PR carries the
  pair, and the drift assertion message names the remediation.
- Docs: `CCC_NODE_ISOLATION_PROFILE=external` is now described as what it is
  — a memory-source gate that forces Family Wiki paths off — not a
  "non-bypassable PreToolUse guard" (that hook was removed, TM-1306), with
  README/memory.md pointing at service-control.md for the real enforcement
  split (#900). Per-provider feature status is owned by
  provider-capability-matrix.md (README links instead of generalizing), and
  the README env table now exposes `CCC_CODEX_MEMORY_LOADER` and the
  default-on `CCC_CODEX_SKILL_COLLECTOR` opt-out.
- Piri nunchi extraction was silently dead on real nodes: `piri-feed.sh`
  resolves its extractor CLI from `CCC_PIRI_CLI_PATH`/`PATH`, cron's bare
  PATH has no `piri` entry, and the guard failed silently — no Piri session
  had ever been ingested since the lane shipped. `install-nunchi.sh
  --apply --piri` now resolves a runnable CLI (env-first: `CCC_PIRI_REAL_CLI_PATH`,
  `/opt/piri/piri-ccc.sh`, `CCC_PIRI_CLI_PATH`, then `piri` on PATH) and pins
  `CCC_PIRI_CLI_PATH` into the feed cron line, warning loudly when none is
  found; the feed's CLI guard now logs the skip and no longer passes on
  searchable directories named `piri` (checkout-root false positive).

### Added
- SessionStart stage timing instrumentation (#897 step 1). `load-memory.sh`
  now appends one body-free JSON line per run (fixed stage names + integer
  milliseconds + total) to `$CCC_STATE_DIR/memory-timing.jsonl`, so the
  static latency hypotheses (serial local/recent/shared/legacy search chain,
  over-split render pipeline) can be verified against real fleet data before
  any optimization lands. Default on, `CCC_MEMORY_TIMING=0` opts out; the log
  is self-bounding (~256KiB) and timing never touches stdout or job state.
- Session-close nunchi kick for audience-scoped Piri nodes. When a distill
  local-sink job lands, the bridge now fires one detached, body-free scoped
  `piri-feed.sh` run for that route, so the just-closed session reaches the
  scope's nunchi DB/snapshot before the next session starts instead of
  waiting for the 10-minute cron. The cron dispatcher stays the owner of
  record; feed-side flock and seen-file keep overlap idempotent, the kick
  never changes job state, and an absent/unsafe feed path skips silently.
- Audience-scoped Piri/Nunchi/MemPalace collection and recall (#950). The
  bridge now supplies one canonical Nunchi DB/snapshot and isolated MemPalace
  HOME per opaque memory audience. Private recall is private + shared +
  private-only pre-scope legacy; shared recall is shared-only. The Piri feed
  and MemPalace cron wrappers provide bounded, owner-only scope dispatch while
  rejecting symlinks, unsafe entries and non-canonical scope names. Body-free
  readiness reports partition counts without scope names or memory bodies;
  unscoped collection remains unchanged.
- nunchi/MemPalace collection lane for the Piri provider. Piri nodes had no
  nunchi lane (Piri sessions are not read by the Claude/Codex distill journal),
  so `ccc-doctor` always reported `nunchi collection` DRIFT and `memory cache`
  degraded on a Piri node. `install-nunchi.sh --apply --piri` now wires a
  `hooks/nunchi/piri-feed.sh` extractor (scans new
  `~/.piri/agent/sessions/**/*.jsonl`, asks the configured Piri CLI for
  distill-style facts, ingests them into the nunchi DB; the extractor's own
  session is isolated under `NUNCHI_HOME/.piri-feed-extractor-sessions` via
  `PIRI_CODING_AGENT_SESSION_DIR` so it never re-enters the scanned tree), a
  `mempalace mine --mode convos --wing piri` refresh, and the weekly bench
  cron. `mempalace-refresh.sh`, `ccc-doctor`, `ccc_memory_probe`, and
  `install-termux-mempalace.sh` recognize the piri lane (configured=piri
  matches runtime=piri).
- `ccc-doctor` now surfaces the nunchi MemPalace collection lane (#920, completes
  the doctor half of #865). A new `nunchi collection` finding (text + `--json`,
  body-free — paths/versions/state only, never transcript body/excerpts/ids/
  credentials) reports the configured collection lane (from the managed cron) vs
  the runtime `CCC_AGENT_PROVIDER` with a `match=ok|DRIFT` flag, the source
  kind/path, the MemPalace binary/version (or peer-facts-only degrade), and the
  last collection state/exit/timestamp from `mempalace-refresh.status.json`.
  Severity is 정상 when the lane matches the runtime and the last run did not
  end in error; otherwise 경고 (warning, non-fatal — never flips the exit code).
  The parsing mirrors `install-nunchi.sh status` so the two reports cannot
  diverge.
- Request lifecycle leak diagnostics (#860): Three-layer defense against zombie
  requests that cause `active_requests` to exceed actual turn count:
  1. WARN-level race condition logging in `register_active()` — emits
     diagnostic details (user/chat IDs, existing token, age, turn kind) when a
     turn registration is refused due to an already-active turn, helping identify
     background-task notification vs user turn races.
  2. WARN-level token mismatch logging in `deactivate_if_same()` — logs when
     deactivate fails due to token mismatch, exposing abandoned turns that fail to
     clean up and become zombies.
  3. Emergency cleanup: `force_cleanup_stale_turns(max_age_seconds)` — last-resort
     cleanup mechanism for zombie turns older than a threshold, callable from health
     monitor when `active_requests` grows unexpectedly. Returns count of cleaned-up
     zombies for observability.

  This addresses the gwakga incident where `active_requests=4` but only 1 turn was
  executing, with the oldest zombie lasting 22401s (6.2 hours) beyond the
  21600s `CLAUDE_PROCESS_TIMEOUT` deadline.
- Error message diagnostics (#901): When `is_error=True` and `result` is empty,
  the bridge now includes `subtype`, `api_error_status`, and `terminal_reason`
  fields in the user-facing error message instead of the generic "Claude turn failed".
  This helps operators diagnose rate limits, API errors, and other failures without
  digging into logs.
- nunchi write gate (#890, graph-engineering review): G1 progress→done
  update detection auto-closes stale in-flight facts (reversible, `supersedes`
  link kept) with a `review-stale` retro CLI; G2 verified source rank
  (user-stated 3 / measured 2 / inferred 1 — the claimed rank counts only
  when its transcript quote verifies, and a lower rank can never close a
  higher one); G3 ambiguous high-overlap conflicts are flagged `review=1`
  and surfaced in the snapshot instead of auto-resolved (`review` CLI to
  list/clear); G4 new `kind=constraint` stored near-verbatim and always
  injected by `snapshot` regardless of the recency limit. Distill extraction
  now preserves numbers/ids/paths verbatim, requires the reason inside every
  `decision`, and grounds user-stated/measured facts with a verbatim quote.
  Weekly bench gains a body-free `metrics` section (stale-suspect ratio,
  review queue, constraint count) and two reason-preservation queries
  (q6/q7). Measured motivation on dungae: 75 facts, ~30% stale, 0 supersedes
  used, confidence constant 0.7.
- New skill `fleet-disk-constraint-triage`: platform-aware fleet disk audit
  (Android/Termux nodes must be judged by `df $HOME`, never `df /`), severity
  classification, root-cause investigation, and node-local cleanup delegation
  via durable Wiki tickets — no centralized remote deletes; destructive steps
  (e.g. `docker system prune`) are owner-approval gated. Shipped as `adapted`
  with a codex mirror (`codex/skills/fleet-disk-constraint-triage`).
- New skill `bridge-safe-detached-run`: run long-running commands as detached
  systemd transient units (`--collect`, mandatory `HOME`/`PATH` env injection,
  persistent log + `EXIT=` marker, polling watcher) so they survive
  bridge/session restarts — formalizes the #822 workaround. Classified
  `claude-only` (Claude bridge background-task lifecycle).
- Skill autosave now has a usage telemetry and deterministic lifecycle
  curator (`skill-review/curator.py`, #752). Body-free counters
  (view/use/patch, timestamps, state) feed `active → stale → archived`
  transitions that manage only `autosave-managed` skills: stale is
  display-only, archive is an atomic same-filesystem move into an owner-only
  archive root, restore is always possible, and nothing is ever permanently
  deleted. Pinned, non-autosave provenance, recently active, and
  never-used-young skills are protected; the first auto run only seeds the
  interval timer. Every mutating run takes a retention-capped owner-only
  backup first and can be rolled back (rollback itself takes a safety
  snapshot); prepared→terminal ledger transactions reconcile crash
  mid-states. A `PostToolUse(Skill)` hook bumps use telemetry fail-open, the
  daily sweep runs the curator only under the opt-in
  `CCC_SKILL_CURATOR_ENABLED=true` gate, and LLM consolidation is not
  implemented — `CCC_SKILL_CURATOR_CONSOLIDATE=true` fails closed and no
  provider is ever called. (#752)
- Skill autosave now has a provider-neutral autonomous-mutation ownership
  contract. A shared fail-closed classifier distinguishes autosave-managed,
  user-owned, bundled/managed, external/repo-installed, pinned, and
  unknown/unreadable skills; exposes owner-only `adopt`, `pin`, `unpin`,
  status, and list-unmanaged controls with dry-run support; and issues
  single-use exact-read receipts bound to the review attempt, operation,
  target identity, content hash, and provenance revision. Bundled provenance
  keeps priority over autosave, legacy markers require a current SHA match,
  and rollback now refuses adopted, pinned, conflicting, or invalid
  provenance. (#750)

### Changed
- Codex nodes now compose the skill-candidate collector by default, with
  `CCC_CODEX_SKILL_COLLECTOR=false` as an explicit node-local opt-out. Candidate
  collection remains separate from installation (`approve` is still the
  installer default), processes at most one historical snapshot per sweep by
  default, takes a non-blocking per-job lease, shares the autonomous Codex
  usage meter, refunds reservations abandoned before provider start, and
  durably backs off body-free backend failures and cancellations. Claude
  composition is unchanged. (#749)

### Fixed
- Self-update allowlists now support `user:` and `system:` service scopes, so
  user units restart and pass active verification inside the transactional
  updater instead of through an unaudited post-update wrapper.
- Setup now satisfies the agent-cron command-task contract when registering
  self-update and surfaces registration failures. Umask-sensitive distill,
  nunchi, and Codex memory fixtures no longer depend on the operator shell or
  checkout mode.
- `ccc_memory_probe` rejected a valid Piri MemPalace refresh status as
  `refresh-invalid` (and flagged `refresh-provider`): the refresh probe and the
  Termux install-metadata validator only accepted `provider` in `{claude,
  codex}`. Both now accept `piri`, so a Piri node with nunchi+MemPalace enabled
  reports a clean `memory cache` instead of a perpetual 경고. Follow-up to #946.
- `test_codex_keeps_typing_alive_and_shows_tool_heartbeat` no longer races on
  the heartbeat status. It waited only for the first `status_callback` then
  immediately asserted that the heartbeat "⏳ Working … Command: pwd" status had
  been emitted — but that heartbeat fires on a timer *after* the first status,
  so under load the assertion could run before the heartbeat arrived (intermittent
  `AssertionError`). The test now waits for that specific heartbeat condition
  (a dedicated event set only when it arrives), making the assertion deterministic.
- nunchi MemPalace collection is now provider-aware for Codex (#865). The Codex
  refresh ran `mempalace mine <sessions> --mode convos` without `--wing`, so
  `mine` attributed the ingested facts to the default wing name (`sessions`,
  the directory basename) instead of the `codex` provider — Codex transcripts
  were collected but mislabelled. The codex path now uses
  `mine <sessions> --mode convos --wing codex`. Related hardening in the same
  pass: the collection source honours `CODEX_HOME` (Codex) and `CCC_CLAUDE_DIR`
  (Claude) instead of hardcoded `$HOME/.codex` / `$HOME/.claude`; the refresh
  wrapper sets `umask 077` and `install-nunchi.sh --apply` makes `~/.nunchi`
  owner-only (0700); a missing or unsupported MemPalace CLI now degrades
  silently to the peer-facts-only path (state=`degraded`, exit 0) instead of
  failing every cron tick; and `install-nunchi.sh` status reports a body-free
  provider wiring block — `configured` vs runtime `CCC_AGENT_PROVIDER` with a
  `match=ok|DRIFT|n/a` flag, source kind/path, MemPalace binary+version, and the
  last collection state/exit/timestamp — without ever printing transcript body,
  excerpts, session ids or credentials. (ccc-doctor surfacing of this state is
  tracked separately.)
- bumped `cryptography` 49.0.0 → 50.0.0 in both hash-locked dependency files
  (`bridge/requirements.lock.txt` and `.github/requirements/bridge-ci.txt`) to
  clear CVE-2026-69247, which `pip-audit` (the wheel-smoke gate) began failing
  on once the advisory propagated. 50.0.0 has the same dependency set as 49.0.0,
  so this is a single-package bump (no other pins changed); locks were
  regenerated via `scripts/ccc-deps-lock.sh` with `--upgrade-package cryptography`.
  This was breaking CI repo-wide (every new run failed wheel-smoke) independent
  of any code change.
- provider approval requests on auto-approve profiles now leave a body-free
  trace. Previously a `can_use_tool` request (or a #838 no-active-turn deny)
  emitted no record of *what* was asked, so post-hoc diagnosis was impossible
  — the stall WARNING carried only user/chat/elapsed, the transcript had not
  flushed the pending tool_use, and the #879 audit ledger is approve-each
  only. `_handle_permission_request` now logs one INFO line per request:
  provider · tool name · `target_kind` (path/command/empty — never the value)
  · request id · turn state · outcome (`allowed`/`denied`/`denied-no-route`).
  Approval semantics are unchanged; this is pure observability (#889).
- self-update is now registered as an agent-cron task by `setup.sh` so the
  harness can actually auto-update. Previously no `self-update` task was ever
  registered, so a deferred run (bridge busy) had no scheduled tick to retry
  on, and a node could go days without receiving merged fixes — gwakga had
  the timer active but the task absent, so #908 (1 MiB NDJSON limit) never
  applied. `setup.sh` now idempotently adds a `self-update` task (opt out
  with `CCC_SELF_UPDATE_REGISTER_CRON=false`; schedule via
  `CCC_SELF_UPDATE_CRON`, default four times daily) using `--success-exit-codes
  0,8,11` so a clean update, a bridge-busy defer, and a no-allowlist degraded
  run do not raise on-failure alerts. The agent-cron timer is still installed
  separately. docs/self-update.md no longer claims "the next scheduled tick
  retries" without that precondition (#909).
- self-update no longer reports `result:"ok"` / "services restarted: 0" when
  the code changed but no service was restarted because the services allowlist
  file (`self-update.services`) was missing or empty. That silent code/runtime
  drift (checkout on NEW code, running processes on OLD) previously read as
  success; it now exits `11` with `result:"degraded-no-services"` and a
  notification warning the runtime may be stale (#910, measured on gwakga:
  persisted 4 runs / ~3 days as false-positive `ok`).
- agent-cron no longer mislabels a watch-type task that exits non-zero to
  signal findings as `failed` → `retry-exhausted`. A task-definition
  `successExitCodes` (CLI `--success-exit-codes 0,1`, default `[0]`) now
  classifies those exits as successful runs-with-findings; only codes outside
  the set (2+, 127 command-missing, 124 timeout) count as `failed`. A task
  that declared **no** `retryPolicy` has no retry concept and is never
  labelled `retry-exhausted` (its failures stay plain `failed`). `status`
  exposes `lastExitCode` so an operator can tell `1` (findings) from `127`
  (command missing) without digging into logs (#911, measured on gwakga
  2026-08-03: `adapter-fleet-watch`/`fleet-doctor-sweep` showed
  `retry-exhausted` while actually reporting 11/12 nodes OK).
- Telegram bridge turns no longer die on image-bearing tool results. The
  Claude adapter never set `ClaudeAgentOptions.max_buffer_size`, so the SDK
  used its own 1 MiB stdout NDJSON limit and one oversized line raised
  `SDKJSONDecodeError` inside the message reader task, which has no recovery
  path — the whole turn failed with "JSON message exceeded maximum buffer
  size of 1048576 bytes" (measured 2026-08-03 18:19:14 KST, line of
  1,056,854 bytes: a 510 KB PNG resized to 682x2000 / 528,000 base64 chars
  and emitted twice in one message, as `message.content[].source.data` and
  again as `toolUseResult.file.base64`, so a single image over ~524 KB of
  base64 was fatal). Every construction path — including bare, settings-free
  `ClaudeRuntime()` — now passes an explicit bound, configurable through the
  new `CCC_CLAUDE_MAX_BUFFER_SIZE` (default 16 MiB, range 1 MiB–256 MiB).
- `bridge/service-systemd.sh` install/reconcile now fail closed when the
  caller's HOME is missing or under the tmp tree (`$TMPDIR`, `/tmp`,
  `/var/tmp`, `/dev/shm`) and the `CCC_SYSTEMD_DIR` test seam is not set;
  an unset HOME is derived from the passwd database (setup.sh convention).
  A root `setup.test.sh` full-setup case had leaked its scratch
  `HOME=$TMP/wk-home` into the live
  `/etc/systemd/system/ccc-telegram-bridge.service`, sending session
  transcripts and memory hooks to `/tmp` (dungae 2026-08-03, #885).
  `setup.test.sh` now also exports a suite-wide
  `CCC_SYSTEMD_DIR`/`CCC_SYSTEMCTL` stub so no test-run `setup.sh` can reach
  the live systemd tree.
- Nunchi installation now follows the live provider and runtime user while
  preserving the managed Codex loader and the single Claude SessionStart hook.
  Audience-scoped runtimes fail closed until scope-local provenance exists,
  and `ccc-memory-check`/`ccc-doctor` expose body-free nunchi and MemPalace
  health.
- Isolated Codex subprocesses now support Termux's owner-controlled
  `PREFIX/bin` without inheriting `PREFIX` or ambient secrets. Unsafe prefix
  paths remain excluded, preventing distill and skill-candidate jobs from
  failing with exit 127 on Android nodes. (#844)
- Bridge autonomous budgets now evaluate only the durable autonomous-mode token
  ledger, so interactive provider traffic remains metered but cannot exhaust or
  block autonomous work. Budget decisions/reports expose that denominator, and
  dead-session wakeup health/status accumulates count-only active, locked,
  quarantine, cooldown, attempts-cap, and budget skip totals; skip-only scans
  also produce the lifecycle summary. Refs #798.
- Claude delegated runs now retain the exact active-turn approval route across
  intermediate SDK result frames while bounded local agent/workflow tasks are
  still running. Later identical approval requests in the same live run no
  longer fail with `No active turn accepts approval requests`; terminal,
  interrupted, replaced, drained, generation-mismatched, and cross-conversation
  callbacks remain fail-closed, and stale callbacks never rebind to a later
  turn. External-wait publication remains an independent active-turn contract.
  (#804)
- Linux bridge restarts now enter a bounded graceful drain instead of sending
  SIGTERM to the whole service cgroup at once. The bridge closes new-turn
  admission, waits up to 45 seconds for active provider turns, accepted run
  tasks, and tracked Claude background Bash tasks, then performs bounded
  runtime cleanup. Canonical root and user systemd units use `KillMode=mixed`
  with a 70-second whole-cgroup SIGKILL fail-safe, preserving explicit stop,
  `Restart=always`, transport-only reconnects, and #303 orphan cleanup. (#822)
- Agent-cron owner/chat failure notifications now distinguish fleet domain
  alerts from generic task failures by promoting only bounded counts of the
  redacted line-start tokens `DOWN`, `UNREACHABLE`, `DRIFT`, and `BOOTPATH`.
  Existing fleet-watch tasks gain the alert title without a store migration;
  arbitrary diagnostic details remain confined to the redacted body. (#829)
- Top-level setup/self-update now reconciles an already-installed, ccc-generated
  Telegram bridge systemd main unit against the canonical
  `bridge/service-systemd.sh` renderer. Identical units are untouched; drift is
  replaced atomically and followed only by `daemon-reload`, preserving active
  sessions and operator-stopped state. Dry-run is mutation-free and explicit,
  systemctl failures restore the previous unit fail-closed, user scope follows
  the invoking user, and bespoke/node-local policy remains in systemd drop-ins
  instead of being copied from arbitrary legacy main-unit lines. (#830; #831
  remains a separate live normalization decision.)
- Skill autosave auto-install no longer fails closed on nodes whose default
  umask is 0002 (#770). Both install paths (auto and owner-approved create)
  now pin the skill directory to mode 700 and SKILL.md to 600 instead of
  inheriting the ambient umask, matching the ownership contract that rejects
  group/other-writable skill dirs. The auto-mode run summary reports a
  `failed` counter so an install that fail-closed is distinguishable from a
  silent skip, and the four umask-sensitive test suites run a second pass
  under umask 0002 in validate-harness to hold the line. Test fixtures now
  model contract-compliant skills roots under any umask.
- The `ccc-codex-github-policy` and `setup` test suites no longer fail on
  umask 0002 nodes (#772): fixtures now pin contract-compliant permissions
  (700 codex homes, 600 config.toml) instead of inheriting the ambient
  umask, matching the policy fail-closed contract. Both suites joined the
  validate-harness umask-0002 second pass, which now covers six suites.

## [0.5.0] — 2026-07-26

### Added
- The Telegram bridge now has an opt-in, sole-owner `/restart` control plane
  for Linux systemd nodes. A delayed transient worker outside the bridge cgroup
  performs the restart, validates a new MainPID and fresh available health, and
  leaves a durable body-free completion receipt for the replacement bridge.
  The target remains restricted to `ccc-telegram-bridge*.service`; group,
  multi-owner, duplicate, unsupported, and default-off paths fail closed.
  (#708)
- Agent-cron can now install an ephemeral Codex headless runner with an
  explicit fail-closed sandbox choice. Bounded tasks support `notBefore`,
  `maxRuns`, and durable `runCount`, so a future one-time LLM job cannot catch
  up last year's cron occurrence or remain enabled after its allowed run.
- GitHub CLI-first fleet policy for Codex and Claude Code. `setup.sh` now
  atomically persists `[plugins."github@openai-curated-remote"] enabled = false`
  without re-rendering or printing the rest of node-local `config.toml`.
  Codex global guidance and `gh-pr-flow` require local `git` + authenticated
  `gh`, prohibit automatic connector fallback, and retain an explicit per-task
  opt-in path for connector use. A fail-closed policy helper and regression
  suite cover idempotence, comment preservation, invalid TOML, and symlinks.

### Fixed
- Claude bridge sessions now support `CCC_BRIDGE_MEMORY_MODE=audience-scoped`.
  Each Telegram route receives an opaque, validated hook environment: groups
  use only shared memory, while DMs add a per-user private store and private-only
  legacy recall. Sessions created before route labelling are reset instead of
  resumed across the new boundary, history reads validate the route first, and
  global Claude transcript `/resume` and `/revert` controls are disabled while
  scoped mode is active.
- In-turn bridge self-restarts now fail safely before stop. If a Claude/Codex
  Bash call runs `start.sh --restart` from below the serving bot or daemon
  supervisor, the command exits 5 with an external recovery hint instead of
  killing its own restart driver and leaving Telegram offline. (#706)
- The Claude bridge runtime now launches its injected web MCP servers via
  `node <abs cli>` on Termux/Android instead of `npx -y <pkg>`. `web_mcp.py`
  (consumed by `claude_runtime.py` as `options.mcp_servers`) hardcoded `npx`,
  whose `#!/usr/bin/env node` bin shebang is unresolved in the agent MCP spawn
  context there, so bridge sessions had no searxng/firecrawl. Falls back to
  `npx -y` on other platforms and when the global cli is unresolved; the
  Firecrawl key stays in `process_env`. Complements #664 (the CLI path). (#669)
- `claude/mcp-setup.sh` now registers stdio MCP servers as `node <abs cli>` on
  Termux/Android instead of `npx -y <pkg>`. The package bins start with
  `#!/usr/bin/env node`, and the agent's MCP spawn context does not carry
  termux-exec (Claude subprocess; Codex `rmcp` `env_clear()`), so `/usr/bin/env`
  was unresolved and every server failed to connect ("`<bin>`: not found").
  On Termux the package is installed globally and launched via `node`, which
  bypasses the shebang; Linux keeps `npx -y`. Platform-branching regression test
  added. (#663)
- The memory-audience 0700 guard now self-heals a bridge-owned key-parent
  directory instead of failing closed forever. `load_or_create_audience_key`
  used `Path.mkdir(mode=0o700, exist_ok=True)`, which only applies the mode when
  it *creates* the directory — so a `.telegram_bot` created earlier under the
  default umask 022 (→ 0755) stayed loose and the bot answered every message
  with "memory audience key parent must be bridge-owned and mode 0700" (observed
  on 8/12 fleet nodes, 2026-07-22). A bridge-owned parent is now tightened to
  0700 with an explicit `chmod`; a parent owned by another user is a real
  exposure and still fails closed. Regression tests added. (#659)
- Audience-scoped Honcho recall and write-back now use physically distinct
  server-side workspaces derived from opaque memory scopes. Shared routes can
  access only the shared workspace; private routes can access their private and
  shared workspaces plus private-only legacy recall. Outboxes are partitioned by
  scope, and a legacy unscoped job fails closed in audience mode.
- Audience-scoped Codex distillation can now produce human-review Wiki candidates
  without returning to the global queue. Candidate records are labelled `private`
  or `shared`, physically partitioned by opaque memory scope, and remain local and
  owner-only; the legacy unscoped queue layout is unchanged and Honcho remains
  disabled in audience-scoped mode.
- GitHub CLI-first setup now falls back to `tomli` on Python 3.10 nodes instead
  of importing the Python 3.11-only `tomllib` unconditionally. If neither parser
  exists, setup still fails closed with a body-free error code.
- Linux systemd installs now use `Restart=always` so a direct SIGTERM that the
  bridge handles as a clean exit cannot silently leave Telegram serving down.
  Explicit `systemctl stop` remains authoritative, and the documented
  single-supervisor rule still forbids combining systemd with `start.sh -d`.
- The daemon supervisor now clears a competing project-bot poller before an
  auto-restart. A crash caused by a Telegram getUpdates 409 Conflict — a stray
  or second instance still holding the token — previously relaunched straight
  back into the same conflict until the rapid-crash limit tripped and the
  supervisor gave up, leaving the bot unresponsive (observed 2026-07-21 on
  daegyo/gongmyoung; gongyung only recovered by timing). `reap_competing_pollers`
  (reusing the exact-argv `find_project_bot_pids` oracle) now terminates the
  surviving poller so the loop self-heals; it is a no-op when no competitor
  exists. A `--_reap-competing-pollers` internal seam plus regression tests
  cover kill, no-op, and cross-project isolation.

## [0.4.0] — 2026-07-18

### Removed
- **Semantic PreToolUse guard removed (TM-1306).** The node now runs the native
  Claude Code posture and Fresh Approval Required is **behavioral policy**
  (`CLAUDE.md`), not a hook-enforced boundary. Deleted `claude/hooks/guard.py` +
  `guard.sh` and their tests, the operational-relax / operator-trust guard
  profile machinery (`/etc/ccc-node/guard-profile`, the `--strict-guard` /
  `--operational-relax` setup flags, `docs/examples/guard-profile.example`), the
  managed-node / managed-service allowlists (`managed-nodes.allow`,
  `managed-services.allow` and their examples), and `RISK-PROFILES.md`. Stage 1
  (#576) unwired the PreToolUse hook and dropped `permissions.deny`; this stage
  deletes the now-dormant sources, tests, and profile provisioning. The real
  OS-level boundaries are unchanged: the unprivileged agent account and the
  root-owned `ccc-service-control` / `ccc-broker-reconcile` wrappers with their
  root-owned exact-unit allowlists remain the enforceable service-control
  boundary.

### Added
- `ccc-broker-reconcile` — a root-owned, operator-installed wrapper that
  encapsulates the fixed broker Compose runbook (`cd` project dir, export
  `A2A_BROKER_REVISION=$(git rev-parse HEAD)`, `docker compose up -d
  <allowlisted services>`) behind one entrypoint, mirroring `ccc-service-control`.
  Broker reconciliation runs through the wrapper instead of the PreToolUse
  guard's inline Compose ALLOW-grammar, so future runbook changes are reviewed in
  the wrapper rather than added as new runbook grammar. The guard permits only
  the direct absolute `/usr/local/libexec/ccc-broker-reconcile` entrypoint with
  exact service tokens; bare/PATH-shadowed, interpreter-mediated, compounded,
  daemon-overridden, and Compose-file-overridden forms stay gated. Raw
  `docker compose up` stays gated. This is additive and staged: the legacy
  inline grammar remains accepted during migration and is removed in a follow-up
  once the wrapper is deployed fleet-wide. The wrapper performs no privilege
  escalation — it provides wrapper/config and command-shape integrity, not a
  new privilege boundary or broker checkout/payload integrity.
  `scripts/ccc-broker-reconcile.test.sh` (19 cases: allowlist enforcement, token
  validation, ownership/writability/symlink integrity, environment isolation,
  privileged-shebang behavior, absolute-dir requirement) is wired into
  `validate-harness.sh`; operator install steps are in `docs/service-control.md`.

### Fixed
- Root-aware `bypassPermissions` default (fixes a root node bricking every new
  Claude session). Claude Code refuses `--dangerously-skip-permissions` — the
  flag `bypassPermissions` maps to — under root/sudo, so the `#552`
  `settings.base.json` default rejected new sessions on root-run nodes.
  `setup.sh` now drops the `permissions.defaultMode = bypassPermissions`
  default when the setup user is root (the PreToolUse guard remains the
  boundary); non-root nodes keep the no-prompt default. The opt-in bridge flag
  `CCC_BRIDGE_CLAUDE_UNRESTRICTED` is likewise ignored under root
  (`claude_unrestricted_enabled(..., is_root=True)` degrades to the guarded
  owner-operator path with a logged warning) so it cannot brick a root bridge.
  `validate-harness.sh`/`setup.test.sh` gate the root-neutralization (real +
  `id`-stubbed root), and the bridge gate/wiring carry root regressions.

### Security
- Keep the ccc-node PreToolUse guard as the authoritative semantic
  Fresh-Approval boundary and keep `CCC_BRIDGE_CLAUDE_UNRESTRICTED` opt-in with
  a default of `false`. Native Claude permission denies now add
  defense-in-depth for literal catastrophic root deletion and force-pushes to
  `main`, while `sudo` remains ask-gated. The native patterns do not replace the
  guard: `setup.test.sh` pins both layers and `ccc-security-audit` reports a
  missing native catastrophic backstop as a risk.

### Changed
- The autonomous broker Compose runbook now accepts one provenance
  `export A2A_BROKER_REVISION=$(git rev-parse HEAD)` companion (also `--short` and the
  backtick form) before the single `docker compose up -d` reconciliation, so
  the compose file can label the image with the deployed revision without a
  fresh-approval gate. `git rev-parse HEAD` is side-effect-free and is the
  ONLY substitution the runbook accepts: the sequence splitter rejects any
  top-level `;`/`|`/`&&` first, and a full-match on the exact command leaves
  no room for a hidden substitution — every other `$(...)`/backtick, a second
  export, an export after the reconciliation, a redirect, or any other variable
  name still fails closed (`guard.test.sh` carries the allow/deny regressions,
  365/0).
- Claude Code now defaults to native `bypassPermissions` mode, matching the
  no-prompt execution posture used by ccc-node/Codex. New installs and
  self-updates receive the mode through `settings.base.json`; the independent
  ccc-node PreToolUse guard continues to enforce Fresh Approval Required
  boundaries, including catastrophic local `rm`. Root-run nodes are the
  exception: Claude Code refuses `bypassPermissions` under root, so `setup.sh`
  drops the default there and the guard alone is the boundary.
- Opt-in Codex-parity ungoverned Claude execution
  (`CCC_BRIDGE_CLAUDE_UNRESTRICTED`, default **false**, `owner-operator` only).
  On a node that sets it true, the bridge's Claude SDK path runs with
  `permission_mode=bypassPermissions`, no OS sandbox, and no host settings
  chain — so the PreToolUse `guard.py` hook is not loaded — matching the
  Codex `never + dangerFullAccess` contract that `auto-approve` already maps
  to (`bot_access.py`). MEMORY/USER context is preserved through the curated
  settings block. The flag is fail-closed: `claude_unrestricted_enabled`
  ignores it on `strict-project`/`disabled` (which stay sandboxed) and honors
  only an explicit boolean-true, so it can never widen a non-owner node to
  host scope. Default keeps `guard.py` as the boundary; the change is
  per-node and reversible. This unifies the two providers' execution scope at
  the operator's explicit request without dropping the guard for nodes that
  do not opt in.
- Claude's service-lifecycle guard now treats detached Compose reconciliation
  (`docker compose up -d [services...]`, including `docker-compose` and
  `--detach`) as autonomous, matching the ccc-node/Codex recoverable path. The
  exact broker restart/rollback runbook may include literal `cd`, optional
  `docker tag`, one reconciliation, and read-only `inspect`/`sleep` (maximum
  300 seconds)/loopback-curl verification; direct SSH requires named fleet
  services. Other Compose lifecycle, multiple reconciliations, remote-daemon
  selection, wrappers, substitutions, arbitrary compounds, and
  external/mutating curl remain fail-closed. Refs #544.
- `gh-pr-flow` now handles protected merges that require an independent
  `jinon86` review of a `seoseo-ai` PR. A narrow helper uses Seoseo's existing
  authenticated session only after fresh, per-invocation explicit approval,
  validates remote actor/repo/author/base/state/reviewer scope, never extracts
  the token, and leaves the final squash merge on the normal account. Mock
  regression coverage verifies each gate.
- `gh-pr-flow` now includes a fail-closed Seoseo merge fallback for the case
  where local `seoseo-ai` lacks organization merge permission. Each use
  requires fresh PR-specific operator approval, keeps the existing `jinon86`
  credential on Seoseo, pins the exact head SHA, rejects draft/non-main/dirty
  merge state and pending or failed checks, and calls the squash-merge API
  without an admin bypass. The helper has deterministic no-approval,
  head-drift, failed-check, legacy-status, dry-run, and merge-path tests.
- Codex `/usage` now hides the `GPT-5.3-Codex-Spark` rate-limit bucket and
  account lifetime/daily token history, omits unavailable context/session rows,
  and renders reset timestamps deterministically in KST (UTC+9).
- Family Wiki log-writing guidance now allocates node/date-scoped
  `LOG-YYYYMMDD-<node>-<seq>` IDs under `[LOG-00]` instead of the collision-prone
  global `LOG-NNNN` max+1 scheme. The wiki skill, slash command, session
  cheatsheet, and durable-memory template share the rule, and harness validation
  rejects regressions to the old allocator. Refs
  `jinwon-int/seoyoon-family-wiki#2246`.
- PreToolUse guard rewritten from bash to `guard.py` (#452), invoked through a
  thin `guard.sh` shim so the hook/install contract is unchanged (stdin payload →
  exit 2 to deny). shlex tokenization replaces the hand-rolled bash word-splitting
  and its three derived string views; `guard.test.sh` (the executable-contract
  golden suite) drives the swap — every prior non-relaxed case passes unchanged.
  The shim fails OPEN only when python3/guard.py is unavailable (matching the
  historical missing-jq posture); `guard.py` fails CLOSED on internal errors and
  every matched gate. Supersedes the guard internals touched by the fleet-restart
  branch; `validate-harness.sh` now `py_compile`s guard.py and asserts setup.sh
  installs it.
- Service-lifecycle gate re-baselined for fleet operations: pure lifecycle verbs
  (start/restart/reload/stop/kill + try-/or- variants) on fleet units
  (`a2a`/`hermes`/`openclaw`/`broker`/`gateway`/`worker`/`ccc-telegram-bridge`) are
  autonomous, locally and toward a peer node (`ssh <node> systemctl restart <unit>`,
  `systemctl -H <node> …`). Still gated: non-fleet units, config verbs
  (enable/disable/mask/isolate/daemon-*) on the local node, `pm2 delete`,
  docker/podman/kubectl, and local host lifecycle. Compound commands are judged
  per statement; one non-fleet target denies the whole command.
- Remote fleet restart classification now inspects quoted SSH and shell-function
  bodies one command at a time, so read-only post-restart verification (`sleep`,
  `systemctl is-active`, `systemctl show`) no longer turns into false target names.
  Mixed non-fleet targets and config-changing verbs still fail closed. Refs #534.

### Added
- Node-local model-usage metering with daily budget caps (#388). The new
  `bridge/core/usage_meter.py` durably records body-free token/request
  counters per KST day × provider × interactive/autonomous mode in
  `.telegram_bot/usage-meter.json` (atomic owner-only writes, bounded
  retention, fail-open persistence). Spend sites wired: Claude interactive
  turns meter at the reader's `ResultMessage` using the complete validated
  input total (raw plus cache-creation and cache-read tokens), Codex
  interactive turns meter via a runtime usage recorder fed by cumulative
  `thread/tokenUsage/updated` deltas — threads the process created start
  from a zero baseline so their first turn is metered, and a resumed
  thread's first notification during a turn this process started derives an
  implied pre-turn baseline from the turn-scoped `last` block (total minus
  last) so the first post-resume turn is metered while prior-session
  history is never counted — plus one request per provider attempt,
  recorded at the runtime's spend boundary (the accepted `turn/start`) so
  every outcome including error terminals and turns cancelled before their
  first event charges exactly once while pre-boundary failures charge
  nothing, and the distill extraction worker charges every
  autonomous attempt with a worst-case pre-spend token reservation over the complete request
  (8192 prompt/schema overhead + the backend's hard output-size cap as the
  output allowance + six tokens per raw snapshot byte, covering canonical
  JSON escape expansion at ≤1 BPE token per serialized byte) until the exec
  backend can report actual usage, so repeated background work consumes —
  and eventually hits — the cap and complete valid input+output cost cannot
  exceed an admitted budget. Autonomous admission is atomic and prospective: the
  meter's `reserve_autonomous_spend` admits only when the whole bounded
  attempt cost (overhead + persisted snapshot size, reserved before the
  claim) fits under the cap and charges it in the same locked step, so
  concurrent attempts cannot jointly overrun the cap, a single oversized
  attempt is rejected outright, and the recorded autonomous total never
  exceeds the configured cap. Reservations are opaque day-pinned handles:
  the charge and any later refund target the accounting day captured at
  admission, so a midnight rollover can neither split a reservation across
  days nor let a refund erase another day's spend. No-op invocations (claim
  lost or job already done) refund their exact reservation, a tiny valid
  budget no longer warns at zero usage, and budgets must fit at least one
  maximal attempt or that work stays deferred by design. Every meter
  mutation additionally holds an exclusive interprocess file lock and
  re-reads the on-disk state before applying its delta, so overlapping
  meter instances or bridge processes merge spend instead of losing it to
  last-writer-wins (falling back to thread-only locking with a logged
  warning if the lock file is unavailable, and preserving unpersisted
  in-memory deltas across repeated save failures instead of reloading over
  them). The bridge
  composition root (`build_context`) now constructs the distill extraction
  worker itself through the handler factory with the shared meter, the
  running `TelegramBot` retains that gated instance and drives it from the
  bridge lifecycle: a fail-open sweep (default every 300s,
  `distill_extraction_poll_interval`) runs every ready snapshot job through
  the gated worker, so capped work is deferred before any provider call
  while job-creating trigger policy remains #465's phase, and the worker's `usage_meter` is an explicit required constructor
  decision.
  Optional per-provider daily token budgets
  (`CCC_USAGE_BUDGET_TOKENS_CLAUDE`/`_CODEX`, 0 = off) raise one warn (early
  alarm at `CCC_USAGE_BUDGET_WARN_PERCENT`, default 80%) and one enforce
  alert per provider-day; at the enforce threshold the distill worker defers
  autonomous extraction without claiming the job or burning an attempt while
  interactive user turns keep flowing by design.
  `ProjectChatHandler.build_distill_extraction_worker` is the composition
  root for #465's scheduling: it always injects the shared meter (callers
  cannot substitute their own gate), and budget alerts additionally queue an
  owner push through the opt-in push-notifier spool (log-only when
  `CCC_PUSH_ENABLED` is off). `/usage` now appends a compact 7-day local
  meter report with budget state. Metering never blocks or fails a turn
  (`CCC_USAGE_METER_ENABLED=false` disables it entirely).
- Provider conformance contract + capability matrix (#387). The new
  `bridge/core/provider_capabilities.py` is the single source of per-provider
  capability states (`supported`/`degraded`/`unsupported`/`unknown`, each with
  a machine-readable reason and issue dependencies) across 13 runtime axes and
  the 8 memory-parity axes; `docs/provider-capability-matrix.md` is rendered
  from it and golden-pinned. A shared `AgentRuntime` behavior suite
  (`bridge/tests/runtime_conformance.py`) now executes the contract — session
  lifecycle, streaming delivery with result-before-completion terminal
  ordering, tool-event pairing, fail-closed approvals, interrupt/liveness,
  error normalization, and per-session turn serialization — against the real
  `CodexRuntime` over a scripted fake app-server plus a normative reference
  runtime, with negative tests proving each contract violation fails the
  suite. Drift checkers pin the matrix to the session layer's provider set,
  the runtime/usage/memory-hook surfaces, and the executable coverage, so new
  provider adapters (#354 successors) must pass the suite and update the
  matrix to land.
- A durable provider-neutral Codex distill extraction worker now advances completed
  snapshot jobs through fenced extraction leases, atomically retains one strict result,
  classifies body-free retryable/terminal failures, and recovers cancellation or stale
  leases without re-reading the user thread. Runtime scheduling and memory sink writes
  remain deferred under #465. Refs #532.
- Hermes-style unattended skill-autosave **auto mode** (#355, opt-in via
  `CCC_SKILL_AUTOSAVE_MODE=auto` or the `skill-autosave.mode` state file;
  default `approve` keeps every existing node unchanged). New
  `claude/hooks/skill-review/autoinstall.sh` replaces the human approval gate
  with deterministic machine gates — secret scan (redaction pattern family,
  hard-fail), node-specific-fact scan (home/root paths, non-loopback IPs,
  user@host), dedup vs installed skills (never overwrites), and HARDLINE-style
  structure lint — then installs passing drafts into `~/.claude/skills/` with
  an `installed-by=autosave` ledger + in-dir marker, a daily install cap
  (`CCC_SKILL_AUTOSAVE_DAILY_CAP`, default 3), and a post-hoc (not approval)
  Telegram notice for installs and blocks. Gate failures are never dropped:
  drafts stay pending with a recorded reason for the normal human path. Both
  draft layers drive it (SessionEnd hook immediately, daily sweep as backstop);
  `/skill-suggest` gains the post-hoc role — `list` / `rollback <name>` /
  `rollback --all` (archive-only undo that refuses unmarked, hand-authored
  skills). Off-switch honored; template-repo skills remain PR-first.
  credential-redacted input with an explicit untrusted-content marker, recursive
  unknown-field rejection, bounded Honcho/Wiki/resume output, safe relative Wiki
  targets, directive/credential fail-closed gates, body-free diagnostics, and a
  checked-in JSON Schema. This source-only phase exposes `DistillBackend` but makes
  no provider call, journal transition, or sink mutation.
- Managed-nodes allowlist for owned-node writes (opt-in, fail-closed). Operator-owned
  `~/.claude/managed-nodes.allow` (override `CCC_MANAGED_NODES_ALLOW`) lists the remote
  hosts this node operates. For a Bash statement whose only remote reach (via
  ssh/scp/rsync/sftp/`systemctl -H`) is to a listed host, `guard.py` relaxes the
  blast-radius gates for that statement — secret/key deployment (`scp deploy.env node:`),
  remote cleanup (`ssh node "rm -rf /var/log/old"`), remote service config verbs
  (`ssh node "systemctl daemon-reload"`), and **reboot** of the host (`ssh node reboot`).
  Reboot-class (`reboot`/`shutdown -r`, recoverable) is also relaxed on the LOCAL node;
  the down-class (`poweroff`/`halt`/`shutdown` without `-r`) stays gated everywhere
  (incl. managed nodes) since a powered-off node stays offline unattended, and reboot of
  an unlisted host / interpreter-mediated forms stay gated. Fail-closed everywhere else:
  no allowlist → fleet-only baseline;
  an unlisted host → fully gated; curl/wget/nc excluded (secret-exfil keeps authority);
  review-gated classes (force-push to protected, history-rewrite, release, DB) never
  relaxed even via ssh; a local destructive op chained beside a managed remote op is
  judged on its own statement and denied. The allowlist is agent-write-gated
  (`managed-nodes-config`). Quote-aware statement splitting (a shlex-rewrite payoff) keeps
  a remote command chained inside quotes (`ssh node "a && b"`) intact as one managed
  statement. `guard.test.sh` grows managed-node/reboot/adversarial coverage;
  `docs/examples/managed-nodes.allow.example` documents the format. Refs #341, #452.
- Managed-services allowlist for local self-managed apps (opt-in, fail-closed).
  Operator-owned `~/.claude/managed-services.allow` (override `CCC_MANAGED_SERVICES_ALLOW`)
  lists the node's own non-fleet local units/containers/processes. `systemctl`/`service`/
  `pm2`/`docker`/`podman` lifecycle is relaxed when EVERY target of the command is listed;
  a mixed/unlisted target, targetless `daemon-reload`, Compose lifecycle other than the
  separately approved direct local detached `up`, a docker remote-daemon flag, and
  command-substitution targets stay gated, and `kubectl` is never relaxed — so
  `sshd`/`ufw`/`nginx` stay protected while the node's own apps become self-manageable.
  Trailing `.service` is tolerated in matching. Write-gated for agents
  (`managed-services-config`); `docs/examples/managed-services.allow.example` documents it.
- Fail-closed external-node memory isolation (#466):
  `CCC_NODE_ISOLATION_PROFILE=external` provides a higher-priority bridge-to-hook
  placement policy and PreToolUse Family-resource guard; `CCC_WIKI_MEMORY_ENABLED=0`
  disables Family Wiki injection, refresh, local
  indexing (including stale distill artifacts), extraction candidates, and queue
  writes while preserving built-in/local/Honcho/resume memory. Node-local
  `CCC_MEMORY_USER_LABEL` / `CCC_MEMORY_ASSISTANT_LABEL` replace hard-coded
  relationship identities without changing existing defaults.
- Tag-based versioning preparation for #251: `scripts/ccc-version.sh`, ccc-doctor
  harness-version reporting, fleet-matrix version extraction, release workflow,
  and CONTRIBUTING release policy. Actual tag/Release publishing remains a
  separate operator approval gate.

### Notes
- Future release tags should be `v0.MINOR.PATCH`. Historical changelog headings
  without a leading `v` are preserved as-is; the release workflow accepts either
  tagged (`v0.4.0`) or historical (`0.3.18`) headings when extracting notes.

### Fixed
- Codex assistant replies lost paragraph spacing in Telegram (turning the readable
  flags on changed nothing). Codex streams text as `item/agentMessage/delta` chunks
  joined with no separator (`"".join` in `project_chat_process`, and the streaming
  accumulator), and the `item/completed` boundary that ends each assistant message
  was dropped in the runtime — so consecutive messages fused (`…다.현재…`), unlike the
  Claude path which sources `msg.result` with its `\n\n` intact. Since the readable
  renderer only *widens existing* blank lines, it could not restore a break Codex
  never emitted. `core/codex_runtime.py` now emits a `\n\n` text delta on a completed
  `agentMessage`, gated by an `emitted_text` flag so the separator only follows real
  text, never leads, and an empty message cannot double it. It rides the existing
  delta stream, so both the non-streaming join and the streaming accumulator recover
  the boundary and the renderer can space paragraphs. Regression coverage in
  `tests/test_codex_runtime.py`.
- `ccc_doctor.py --json` stdout is now strictly machine-parseable (#404):
  `scripts/ccc_doctor.py --json` runs the whole diagnosis with the real stdout
  file descriptor redirected to stderr and writes the single JSON document to a
  preserved private copy of stdout. This structurally prevents a probe (or a
  descriptor-inheriting subprocess such as a Codex helper) from trailing
  non-JSON bytes after the report, so a strict `json.load` consumer no longer
  fails with `Extra data`. Probe diagnostics surface on stderr; exit-code
  semantics (0 for 정상/경고 only; 1 for 교정가능/수동필요) are documented in
  `--help` and the module docstring. Regression coverage in
  `scripts/ccc-doctor.test.sh` parses the Codex `--json` path repeatedly with
  `json.load` and proves the stdout guard diverts stray fd-1/print leaks to
  stderr.
- CodeQL/required-check drift (#350): Dependabot now groups the complete
  `github/codeql-action/*` family, CodeQL publishes the stable
  `codeql-python` context, and `.github/required-checks.json` plus regression
  tests bind all six desired `main` checks to explicit workflow job names.
  The CI governance runbook distinguishes runner infrastructure failures from
  source failures and documents the narrow post-merge protection update.
- Canonical provenance/update drift (#351): bridge install docs now clone
  `jinwon-int/ccc-node`; `bridge/start.sh --version` uses
  `scripts/ccc-version.sh`, and `--upgrade` validates the canonical origin,
  pins `main`, and delegates to `scripts/ccc-self-update.sh` while preserving
  its terminal exit code. Historical bridge/package versions no longer drive
  runtime update decisions.

Distill observability follow-up — closes #130, #133.

A2A mobile native worker first slice — refs #150.

Native worker accepts the single-shot patch bridge — refs #150, a2a-nexus #1020/#1021.

### Added
- `scripts/a2a_termux_native_worker.py` now accepts `claude-a2a-patch-bridge.mjs`
  (a2a-nexus #1021) for `OPENCLAW_BIN`/`A2A_OPENCLAW_ANALYSIS_BIN` as an
  intent-aware drop-in superset of the analysis bridge, and validates an opt-in
  `A2A_CLAUDE_CODE_PATCH_MODE=single-shot` (fail-closed if set without the patch
  bridge). `WORKER_METADATA_JSON` `adapter` must now match the wired bridge.
  Env example + `docs/a2a-claude-worker.md` document the single-shot path; new
  test cases cover the patch-bridge env, mode/bridge mismatch, and bad mode value.
- `scripts/a2a-termux-native-worker.sh` + Python checker: validates a
  systemd-style env file for running `a2a-broker-worker/dist/worker.js` under
  Termux native/glibc-runner Node, with fail-closed bridge metadata, local
  tunnel, and env-hygiene checks. `run` is explicit and no live cutover or
  restart is performed by default.
- `scripts/a2a-termux-native-worker.sh` now also owns the singleton
  SSH-tunnel + worker-respawn supervisor (`supervise`/`stop`/`status`
  subcommands) — canonical replacement for the hand-rolled
  `~/.hermes/scripts/native-worker-supervisor.sh` that gongyung and daegyo
  used to run. Wiki ND-1236: singleton via `flock -n` (second supervise
  exits rc=3), orphan-safe cleanup (`cleanup_orphans` sweeps parent=1 ssh
  on port 18790; `kill_tree` walks pgrep -P recursively; `sweep_lingering_ssh`
  is the belt-and-suspenders finalizer). Supervise inherits
  `A2A_TUNNEL_SSH_TARGET` and `A2A_TUNNEL_REMOTE` env keys.
- `scripts/a2a-termux-native-worker-health.sh` (new, cron-safe): read-only
  supervisor / tunnel / worker snapshot with optional `--self-heal` (spawns
  a supervisor via `setsid -f` when none is running) and a
  `--max-supervisors N` cap detector that flags the exact >1-supervisor
  pile-up that motivated ND-1236, matching BOTH the canonical script AND
  the legacy `native-worker-supervisor.sh` process name so a pre-migration
  node running both trips the check. Exit codes 0/2/3/4/5 are distinct so
  cron logs are self-explanatory; `--json` emits a single-line schema.v1
  summary for fleet log ingestion.
- `docs/examples/a2a-termux-native-worker.env.example` and
  `docs/a2a-claude-worker.md` native-Termux section documenting the PR-first
  mobile worker path.
- `scripts/validate-harness.sh`: OpenClaw runtime/bootstrap context-file guard
  for tracked `AGENTS.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `HEARTBEAT.md`,
  `IDENTITY.md`, and `.openclaw/**` paths.

### Changed
- `claude/hooks/distill.sh` (#130): the three `skip reason=…` log lines
  (`no-transcript`, `cwd-out-of-scope`, `too-few-turns`) now emit
  `trigger=$TRIGGER pid=$$` so `/distill stats` can attribute them to the
  correct trigger column instead of falling through to `unknown:`.
- `claude/skills/distill/SKILL.md` (#130): the stats awk now caches
  `pid → trigger` from `start trigger=… pid=…` lines (plus a parent→bg PID
  bridge from `spawned bg pid=…`) and falls back to that cache when a
  downstream line lacks an inline `trigger=` field. Truly historical
  orphan lines (no trigger, no pid) still bucket into `unknown:`.
- `claude/hooks/distill/wiki-queue.sh` (#133): replaced the title-only
  hash with `title_hash()`. Strategy A — if the title contains one or
  more `#NNN` tokens, the hash is determined by the sorted set of issue
  numbers (so `#82 ...`, `Issue #82: ...`, `이슈 #82: ...`, `... (#82) ...`
  all collapse). Strategy B — sigilless titles get aggressive
  normalization (lowercase, strip bilingual section prefixes with colon,
  strip space-bounded `r\d+` round tags, replace common punctuation with
  space, collapse whitespace). The `.seen` file format is unchanged — old
  rows live their 7-day TTL and roll off naturally.

### Tests
- `claude/hooks/distill/wiki-queue.test.sh` — new cases covering
  issue-anchored cluster collapse, multi-issue distinctness, sigilless
  variant dedup (round-tag + punctuation), section-prefix bilingual
  dedup, and a HOT-crossing chain proving the dedup signal now feeds
  the existing `#76` hot mechanism end-to-end.

## [0.3.18] — 2026-06-22

Distill Tier-1 follow-up bundle — closes #71, #72, #73 in one PR.

### Added
- `claude/hooks/distill/queue-drain.sh` (#71): SessionStart-backgrounded retry
  worker for `honcho-queue.jsonl`. Reads up to `CCC_DISTILL_DRAIN_BATCH`
  (default 20) entries per run, retries each with the same upsert-session +
  POST-messages sequence as `honcho-push.sh`. In-band `_attempts` counter
  on each line; entries that exceed `CCC_DISTILL_DRAIN_MAX_ATTEMPTS`
  (default 3) move to `honcho-queue.jsonl.dead` for manual review.
  Pre-flight `/health` probe (skips drain if Honcho is unreachable).
  Single-flight via `flock` so concurrent SessionStarts don't double-drain.
  Replayed messages carry `metadata.replay: true` and a `(replayed)` marker
  in content so they're identifiable in Honcho.

### Changed
- `claude/hooks/distill/extract.sh` (#72): timeout (ec=124) now triggers a
  one-shot retry path — rebuilds the transcript window with halved
  `MAX_TURNS` and `MAX_BYTES`, calls `claude -p` again with the existing
  `STRICT` system prompt. Transcript construction was factored into a
  `build_redacted()` function so both attempts share the same redact +
  byte-cap logic. JSON-drift retry path (#70) is preserved and now lives
  after the timeout-retry branch.
- `claude/hooks/distill.sh`, `distill/honcho-push.sh`, `distill/wiki-queue.sh`
  (#73): state-dir paths read from `CCC_STATE_DIR`
  (default `/root/.claude/state`) instead of being literal-hardcoded.
  `distill/honcho-push.sh` also reads `CCC_HONCHO_CFG`
  (default `/root/.hermes/honcho.json`) for non-root / alternate-install
  scenarios. Matches the pattern already used by `load-memory.sh`'s
  `CCC_MEMORY_CACHE_DIR` / `CCC_HOOK_DIR`.
- `claude/settings.base.json`: new SessionStart hook entry that fires
  `queue-drain.sh` in the background (`& 2>/dev/null`) so it never adds
  latency to startup.
- `setup.sh`: copies the new `claude/hooks/distill/queue-drain.sh` alongside
  the other distill sub-scripts; chmod glob already covers it.

### Verified (dungae)
- Empty queue path: `queue-drain.sh` returns immediately, no log noise.
- Loaded queue path: seeded a synthetic failed payload, ran drain,
  Honcho POST returned HTTP 201, queue file truncated to 0 lines, message
  visible in Honcho with `metadata.replay: true` and the `(replayed)` content
  prefix. DELETE 202 cleanup of the smoke session.
- LIVE manual distill on the working session: attempt 1 succeeded in ~75 s,
  no retry needed, 2 honcho facts + 1 wiki candidate.
- Concurrent natural SessionEnd from another cwd's session distilled
  successfully alongside (4 candidates added) — single-flight lock didn't
  interfere.

### Notes
- The `_attempts` field is added in-band to the JSON line on each retry
  failure. Old queue lines (pre-0.3.18) without this field default to 0,
  so they retry up to `MAX_ATTEMPTS` total — graceful migration.
- No plugin.json bump (drain runs in node-local SessionStart, not in the
  portable plugin surface).

## [0.3.17] — 2026-06-22

Follow-up to 0.3.15 — harden `distill/extract.sh` against Haiku occasionally
returning prose instead of strict JSON (observed once on a natural `SessionEnd`
trigger fired from a code-debugging session — fail-open, but worth recovering).

### Fixed
- `claude/hooks/distill/extract.sh`:
  - Stronger user-prompt output contract: explicit "first non-whitespace is `{`,
    last non-whitespace is `}`, no prose, no preamble, no fences" plus an empty
    schema fallback for trivial sessions.
  - New `--append-system-prompt` constraint (belt + suspenders) attached to the
    `claude -p` invocation, restating the strict-JSON contract at system level.
  - **Two-attempt strategy**: if attempt 1's response fails `jq` validation, the
    script retries once with an even more emphatic system prompt (`CRITICAL
    OUTPUT CONTRACT...`) instead of failing immediately. Most Haiku "prose drift"
    cases recover on this single strict retry.
  - On final failure, the first 1 KB of each attempt's raw response is logged to
    `distill.log` for debugging.

Live verified on the dungae node: trigger=manual, attempt 1 produced valid JSON
(no retry needed), honcho POST returned HTTP 201 with 2 facts, 1 wiki candidate
queued, pipeline completed in ~80 s.

## [0.3.16] — 2026-06-22

Follow-up to 0.3.15 — add operator-facing `/distill` skill for manual control of the
Session Distiller pipeline introduced in the previous release.

### Added
- `claude/skills/distill/SKILL.md`: dispatches on the slash-command argument:
  - (empty) / `manual` — fire `distill.sh manual` and wait (polling `distill.log` up to
    180 s) before reporting what was distilled.
  - `status` — non-mutating: print toggle state, last `distill-last.json` summary,
    last 5 `distill.log` lines, and the wiki-candidates queue size.
  - `dryrun` / `live` — toggle DRY-RUN mode; uses `mv` (not `rm`) so the
    `guard.sh` `rm-catastrophic` rule never trips.
  - `disable` / `enable` — toggle the OFF switch the same way.
- The skill is picked up by the existing `setup.sh` skills `cp -r` line; no
  `setup.sh` change required.

## [0.3.15] — 2026-06-22

Session Distiller — `PreCompact`/`SessionEnd` hook pipeline that distills the live transcript
via `claude -p --model haiku` (inherits parent OAuth, no `ANTHROPIC_API_KEY` needed) and
routes the result to **Honcho** (auto push of working/relational facts) + a **human-gated
wiki-candidates queue** (`~/.claude/state/wiki-candidates.md`) for durable wiki promotion via
the existing `wiki-record` skill. Closes the gap left by the Hermes consolidator after the
ccc-node harness moved to Claude Code, without re-bloating `MEMORY.md`/`USER.md`.

Design rationale and live-check evidence: seoyoon-family-wiki `pages/team/dungae/DECISIONS.md`
**[TM-1058]**, log **[LOG-1212]** / **[LOG-1220]**. Runbook sections: `pages/nodes/dungae/RUNBOOK.md`
**[ND-1059]** (overview), **[ND-1060]** (troubleshooting), **[ND-1061]** (`rm-catastrophic`
guard-bypass pattern for LIVE flip).

### Added
- `claude/hooks/distill.sh`: entry hook. Recursion-guarded
  (`CLAUDE_DISTILL_INFLIGHT=1`), off-switch (`~/.claude/state/distill.disabled`), dry-run
  (`~/.claude/state/distill.dryrun`), min-content gate, backgrounded so the foreground
  hook returns instantly; resolves its sub-script directory dynamically so the same file
  works in both standalone (`~/.claude/hooks/distill.sh`) and plugin
  (`${CLAUDE_PLUGIN_ROOT}/hooks/distill.sh`) install modes.
- `claude/hooks/distill/extract.sh`: pulls the last N user/assistant turns from
  `~/.claude/projects/<cwd-encoded>/<session-uuid>.jsonl`, applies a secret-regex redact
  pass on top of `redact.sh` patterns, invokes `claude -p --model haiku
  --no-session-persistence --output-format text`, validates the strict-JSON response, and
  tags it with `session_id`/`trigger`/`distilled_at` metadata.
- `claude/hooks/distill/honcho-push.sh`: upserts the Honcho session and POSTs distilled
  working/relational facts to `{baseUrl}/v3/workspaces/<ws>/sessions/<sid>/messages` as
  `peer_id: <aiPeer>`. Fail-open with retry-queue stub (`honcho-queue.jsonl`).
- `claude/hooks/distill/wiki-queue.sh`: appends durable wiki candidates to
  `~/.claude/state/wiki-candidates.md` with title-hash 7-day de-dup; auto-bootstraps the
  queue header on first run. No auto-PR (human-gated per [FW-03]).
- `claude/settings.base.json`: registers `distill.sh` on `PreCompact` (after `checkpoint.sh`)
  and `SessionEnd` (after `notify.sh`) for both standalone and plugin install modes.
- `claude/hooks/enforcement-overlay.json` + `claude/hooks/hooks.json`: register
  `distill.sh` on `SessionEnd` only. PreCompact is handled exclusively by `settings.base.json`
  (merged into `~/.claude/settings.json` by `setup.sh`).
- `claude/settings.base.json` env: `CLAUDE_DISTILL_TIMEOUT="180"` — bigger budget than the
  90 s default for transcripts that exceed Haiku's first-token latency on large sessions.
- `setup.sh`: copies `claude/hooks/distill.sh` and the `claude/hooks/distill/` directory
  into `~/.claude/hooks/`, and `chmod +x` covers both directories.

### Changed
- `claude/hooks/load-memory.sh`, `claude/hooks/load-tools.sh`, `claude/hooks/checkpoint.sh`,
  `claude/hooks/refresh-memory.sh`, `claude/hooks/evidence-gate.sh`: each gains a single
  guard line right after `set -uo pipefail`:
  ```
  [ -n "${CLAUDE_DISTILL_INFLIGHT:-}" ] && exit 0
  ```
  This prevents the child `claude -p` session spawned by the distiller from re-firing
  memory loads / cache refreshes / checkpoints / Stop-time evidence checks.

### Verified
- Guards: all six hooks (`load-memory`, `load-tools`, `checkpoint`, `refresh-memory`,
  `evidence-gate`, `distill`) exit 0 silently under `CLAUDE_DISTILL_INFLIGHT=1`.
- Live Honcho POST: `ensure-session` returned HTTP 201, message POST returned HTTP 201,
  read-back confirmed peer/content/metadata round-trip, DELETE 202 cleanup.
- LIVE end-to-end manual run: `claude -p` Haiku call ~28 s, valid JSON parsed,
  2 wiki candidates auto-queued (later promoted to RUNBOOK [ND-1059..1061] in Wiki PR
  jinwon-int/seoyoon-family-wiki#1916).

## [0.3.14] — 2026-06-21

Bridge — extend `CCC_TELEGRAM_PART_HEADERS` to the entity-renderer path so multi-chunk
responses actually get a `k/N` marker under the default config (GitHub issue #34 follow-up).

### Fixed
- `bridge/core/streaming.py`: previously, `apply_part_headers` only ran on the MarkdownV2
  fallback path. With both `CCC_TELEGRAM_ENTITY_RENDERER` and `CCC_TELEGRAM_PART_HEADERS`
  default-on (slices 4 & 5), the entity path returned first and emitted multi-chunk
  responses with no part marker. The `PART_HEADER_RESERVE` headroom is now applied to the
  split limit for both renderers, and entity chunks pass through `apply_part_headers`
  before send.

### Added
- `bridge/utils/tg_entities.py`: `apply_part_headers(chunks)` — entity-path counterpart to
  `tg_readable.apply_part_headers`. Prepends `"k/N\n"` to each chunk text and emits a bold
  `MessageEntity` over the `k/N` digits (no `parse_mode` is set in the entity path, so
  asterisks would otherwise render as literal text). Existing entity offsets are shifted
  by the UTF-16 length of the prefix.
- `bridge/tests/test_tg_entities.py`: unit coverage for single/empty/multi-chunk behavior,
  offset shifting, and UTF-16-safe ASCII marker length.
- `bridge/tests/test_streaming.py`: integration test — a >TELEGRAM_LIMIT draft on the
  entity path lands as multiple bubbles, each starting with `k/N\n` and carrying a bold
  marker `MessageEntity`.

## [0.3.13] — 2026-06-21

Guard — narrow Telegram bridge restart carve-out for issue #34 slice 4 canary operations.

### Changed
- `claude/hooks/guard.sh`: allow the low-risk local `ccc-telegram-bridge` restart path
  (`.service` suffix optional) while preserving approval gates for broker/Gateway/A2A worker
  and other bridge service controls discovered during issue #34 canary operations.

### Added
- `claude/hooks/guard.test.sh`: acceptance coverage for allowed `ccc-telegram-bridge`
  restarts and denied A2A/worker/broker service controls.

## [0.3.12] — 2026-06-21

Fix — setup.sh did not install evidence-gate.sh (added in 0.3.8 but omitted from the
install list), so a real install referenced a Stop hook that wasn't on disk.

### Fixed
- `setup.sh`: copy `claude/hooks/evidence-gate.sh` into `~/.claude/hooks/` alongside the
  other portable hooks.
- `scripts/validate-harness.sh`: new check — every hook referenced by settings/overlay must
  also be installed by `setup.sh` (catches referenced-but-not-installed hooks at CI time).

## [0.3.11] — 2026-06-21

Permissions model — document the allow-all + fail-closed-hook decision (#13 item #3).

### Changed
- `claude/hooks/RISK-PROFILES.md`: add a "Permissions vs hook enforcement" decision
  section. Audit analysis (~1k tool calls) shows Bash usage is overwhelmingly
  compound/multi-line, which prefix-matched permission entries (`Bash(cmd:*)`) cannot
  describe — a per-command allowlist would over-block autonomous A2A/cron/headless runs.
  **Decision:** keep the broad `Bash(*)` allow and rely on `guard.sh` (regex, full-command,
  fail-closed) as the real Fresh-Approval enforcement; #13 item #3's "replace `Bash(*)`
  allow-all" is **superseded** for this node. No code/permission change — documents the
  existing, intentional model.

## [0.3.10] — 2026-06-21

Guard — relax the force-push gate for a developer's own feature branches (operator-approved).

### Changed
- `claude/hooks/guard.sh`: a *single explicit* `git push --force`/`-f`/`--force-with-lease`
  (or `+refspec`) to a **non-protected feature branch** now proceeds autonomously instead of
  being review-gated — it only rewrites that branch's own history, not shared/published state.
  The gate still **DENIES** (fail-closed) when the destination is a protected branch
  (`main`/`master`/`develop`/`release*`/`hotfix/*`/`prod`/`production`/`stable`), is
  ambiguous/bare (no explicit dst, `HEAD`, current branch), uses multiple refspecs, or is part
  of a compound/chained command. Destination is parsed from the command's positional args;
  when it can't be parsed unambiguously, the push is denied.
- `claude/hooks/RISK-PROFILES.md`: document the relaxation under `operator_review_gated`.

### Added
- `claude/hooks/guard.test.sh`: allow/deny cases for the relaxation (feature-branch allow;
  protected/ambiguous/multi/compound deny), and made the suite **hermetic** by stripping any
  ambient `CCC_ALLOW_GATED` (which would otherwise turn every gated case into a false "allow").

## [0.3.9] — 2026-06-21

Self-update skill — safe harness drift control (issue #13 Tier 2, item #16).

### Added
- `claude/skills/self-update/`: a skill that updates a node's installed harness
  (`~/.claude`) to ccc-node latest. `check.sh` is **read-only** drift detection
  (fetch + commits/files/CHANGELOG delta vs origin/main). Applying is **approval-gated**
  and routed through `setup.sh` (auto-snapshot to `~/.claude/backups/`), validated with
  `scripts/validate-harness.sh`, with an explicit rollback path. SKILL.md documents that
  node identity (CLAUDE.md, memories, honcho.json) is preserved by setup.sh's `seed()`
  and that the Telegram bridge is out of scope.

## [0.3.8] — 2026-06-21

Evidence gate — "evidence before declaring" Stop hook (issue #13 Tier 1.5, item #8).

### Added
- `claude/hooks/evidence-gate.sh`: opt-in (`CCC_EVIDENCE_GATE=1`) Stop hook. If the
  current session changed files (Write/Edit/MultiEdit/NotebookEdit) but the audit log
  shows no verification activity (tests / `--dry-run` / `--check` / `git diff`·`status` /
  CI checks), it blocks the stop **once** and asks for evidence. Loop-safe
  (`stop_hook_active` passes through), session-scoped, fail-open. Off by default.
- `claude/hooks/audit.sh`: record `session_id` so the gate can scope to the current
  session.
- Wired the gate into `Stop` in both `claude/hooks/hooks.json` and
  `claude/hooks/enforcement-overlay.json` (parity preserved); 6 new tests in
  `observability.test.sh` (23/23 pass).

## [0.3.7] — 2026-06-21

Harness settings — pin two operational `settings.json` keys (issue #13 Tier 3).

### Added
- `claude/settings.base.json`: `includeCoAuthoredBy: true` (keep the `Co-authored-by`
  trailer on Claude-made commits, matching the gh-pr-flow convention) and
  `cleanupPeriodDays: 30` (explicit chat/transcript retention period). First slice of
  the #13 harness-maturity roadmap's Tier 3 settings keys; the `model` pin is
  intentionally deferred (operational impact, decided separately).

## [0.3.6] — 2026-06-20

Telegram rendering — fix the MarkdownV2 path silently dropping long/symbol-dense messages
(and tables) to plain text. Follow-up to 0.3.4.

### Fixed
- MarkdownV2 escaping expands text ~1.2x (more for tables/symbol-dense content), so a
  sub-limit raw chunk could exceed Telegram's 4096-char limit once escaped and was dropped to
  **plain text — losing all formatting**. Both delivery paths now convert to MarkdownV2 **first**
  and split on entity-safe boundaries with `tg_md.split_markdownv2`, instead of splitting raw and
  hoping the escaped form fits.
  - `bridge/core/bot.py`: `_deliver_markdown` converts the whole message then splits the
    MarkdownV2 (removes the fragile raw-3500 headroom heuristic; per-part plain fallback only on
    the rare `BadRequest`).
  - `bridge/core/streaming.py`: `finalize_draft` upgrades the draft to the first MarkdownV2 chunk
    and emits the overflow as follow-up MarkdownV2 messages, instead of dropping the whole draft to
    plain when the escaped form exceeds the limit.
- `bridge/core/streaming.py`: `_find_split_boundary` no longer cuts through a fenced code block or
  a contiguous pipe table when overflowing between draft messages (new `_avoid_block_split` guard,
  floored at `max_length // 2`), so a table renders as one block instead of two broken halves.

### Changed
- `bridge/tests/test_streaming.py`: fixtures accept `parse_mode` (mirrors the real telegram Bot
  signature); added regression tests for overflow splitting and the block-boundary guard.

## [0.3.5] — 2026-06-20

### Fixed
- Telegram bridge no longer surfaces "❌ Internal error: Message is not modified..." to the
  chat. Telegram rejects no-op edits (identical text + reply markup) with a harmless 400; the
  streaming draft path already swallowed it, but inline-button / callback edit paths
  (`query.edit_message_text(...)`) did not, so the exception reached the global error handler and
  was posted to the user. `_error_handler` now detects this case and logs it quietly. New
  `bridge/utils/tg_errors.py` (`is_not_modified`) + `bridge/tests/test_tg_errors.py`.

## [0.3.4] — 2026-06-20

Telegram rendering — make tables and special characters display correctly instead of
falling back to plain text.

### Added
- `bridge/utils/tg_md.py`: renders standard Markdown to Telegram **MarkdownV2** via
  `telegramify-markdown` — GFM pipe tables become aligned fixed-width **code blocks** (a real
  table on mobile), and reserved special characters (`_ * [ ] ( ) ~ \` > # + - = | { } . !`) are
  escaped correctly so messages no longer hit `BadRequest` and drop to plain text. Decorative
  heading emojis are stripped (structure kept via bold). Degrades gracefully (returns `None`) when
  the library is absent so callers keep the legacy path. New dep: `telegramify-markdown>=0.5.0`.
- `bridge/tests/test_tg_md.py`.

### Changed
- `bridge/core/bot.py`: `_reply_smart` / `_send_smart` now route text through a shared
  `_deliver_markdown` helper that renders MarkdownV2 (per-chunk plain-text fallback on parse
  error). HTML callers (`/skills` listing) keep HTML; if telegramify is unavailable the legacy
  `wrap_markdown_tables` + Markdown path is used.
- `bridge/core/streaming.py`: `finalize_draft` upgrades the streamed message to MarkdownV2 on
  finalize (live drafts stay plain), so streamed responses also render tables/formatting. Any
  parse/length edge case falls back to the original plain text — delivery is never lost.

## [0.3.3] — 2026-06-20

Node onboarding hardening — closes the P2–P4 gaps found bringing up `soonwook`/vps6 standalone
(issue #25). P1 shipped in #24, P5 in #27.

### Added
- **P2 — Linux reboot-persistence for the Telegram bridge.** `bridge/start.sh` gains
  `--install-systemd` / `--uninstall-systemd`. Run as root it writes a system unit to
  `/etc/systemd/system/ccc-telegram-bridge.service` and `systemctl enable --now`s it; run as a
  normal user it installs a `systemctl --user` unit. The unit runs the bridge in the foreground
  under systemd supervision (`Restart=on-failure`); name overridable via `BRIDGE_SERVICE_NAME`.
  No more hand-written units (cf. the manual `ccc-telegram-bridge.service` on soonwook).
- **P4 — node-identity seeding.** `setup.sh` accepts `--node`, `--display`, `--slot`,
  `--fleet-role`, `--lang`, `--user-name`, `--user-gh`, `--user-tz`, `--user-context` and
  substitutes the matching `<PLACEHOLDER>` tokens in freshly-seeded `CLAUDE.md` / `MEMORY.md` /
  `USER.md`. Omitted tokens are left intact for manual editing; existing files are never rewritten.

### Changed
- **P3 — `setup.sh` no longer overwrites `~/.claude` without a restore point.** Before clobbering
  `settings.json`, `settings.local.json`, and the hook/output-style/agent/command/skill dirs it
  tars them to `~/.claude/backups/ccc-node-setup-<ts>.tar.gz` (credentials excluded). Skip with
  `--no-backup`.
- `bridge/README.md`: documents Linux systemd install and lists Linux under Platform Support.

## [0.3.2] — 2026-06-20

A2A Claude Code worker lane docs — capture the `soonwook` follow-up conversion and remove a
few `nosuk`-only labels from portable harness messages.

### Added
- `docs/a2a-claude-worker.md`: documents the poller-service vs analysis-backend split for A2A
  lanes where `a2a-hermes-worker` remains the systemd poller name but `OPENCLAW_BIN` /
  `A2A_OPENCLAW_ANALYSIS_BIN` point at `claude-a2a-analysis-bridge.mjs` and broker metadata
  reports `runtime=claude-code`.
- `/a2a-claim` and `CLAUDE.md.template` now explicitly warn workers to classify A2A runtime from
  live env + broker metadata instead of service name.

### Changed
- Session memory status messages and injected heading are now node-generic (`CCC_NODE`,
  `/root/.claude/state/node.txt`, or hostname) instead of hard-coded `nosuk`.

## [0.3.1] — 2026-06-20

Plugin-mode install — resolve portable-hook double-firing between `setup.sh` and the plugin.

### Changed
- **Single owner per portable hook.** `settings.json` and an enabled plugin both register hooks and Claude Code does not de-duplicate them, so a node running both would fire guard/audit/redact/notify **twice**. `setup.sh` now composes `settings.json` from two sources and you pick one owner per node:
  - `claude/settings.base.json` — node-local hooks (SessionStart/Pre+PostCompact) + statusLine + outputStyle, always installed.
  - `claude/hooks/enforcement-overlay.json` — the portable hooks (guard/audit/redact/notify), absolute paths.
  - `./setup.sh` (standalone, default): merges base + overlay → `settings.json` owns everything.
  - `./setup.sh --with-plugin`: installs lean settings (base only); the **plugin** owns the portable hooks.
- The static `claude/settings.json` is removed; it is now generated at install (single source of truth, no drift).
- Validator: base/overlay hook events must be disjoint; the overlay must stay equivalent to the plugin's `hooks/hooks.json` (same events/matchers/script basenames, modulo the `${CLAUDE_PLUGIN_ROOT}` vs `/root/.claude` path prefix) so both modes enforce identically; the rendered standalone settings is validated.

### Notes
- Reference/bootstrap nodes (e.g. nosuk) stay **standalone**; plugin mode is for nodes that consume ccc-node via the marketplace. Mixing modes on one node is what double-fires — don't enable the plugin on a standalone install.

## [0.3.0] — 2026-06-20

Tier 1.5 follow-up — Telegram push delivery (token-isolated, owner-only, opt-in).

### Added
- **Bridge push notifier** (`bridge/core/push_notifier.py`): a background task in the Telegram bridge that delivers Claude Code lifecycle notifications to the **owner only**, decoupled from the hook via a filesystem spool. The bot **token never leaves the bridge** — the hook writes summaries, the bridge sends them. Owner target = `CCC_PUSH_CHAT_ID` or the sole `ALLOWED_USER_IDS`. Rate-limited, deduplicated, best-effort (delivery failure never crashes the bot). **Disabled by default** (`CCC_PUSH_ENABLED`); merging/restarting is a no-op until an operator opts in.
- **notify hook spool** (`claude/hooks/notify.sh`): with `CCC_NOTIFY_TELEGRAM=1`, Notification/Stop events also write a short, **redacted** summary (token-shaped runs masked, length-capped) into `~/.claude/state/telegram-spool`. Off by default; never touches the bot token.
- Config (`bridge/utils/config.py`): `CCC_PUSH_ENABLED` / `CCC_PUSH_CHAT_ID` / `CCC_PUSH_SPOOL` / `CCC_PUSH_POLL_INTERVAL` / `CCC_PUSH_MAX_PER_MINUTE`, documented in `bridge/.env.example`.
- Tests: `bridge/tests/test_push_notifier.py` (11 cases — disabled-by-default, owner target resolution, dedup, rate-limit, retry-on-failure, malformed-archive, format); observability suite +4 (spool off-by-default, opt-in, redaction, node label) → 17/17.

### Approval boundary
- Telegram delivery is an outbound provider send. It stays **opt-in + owner-only + token-isolated** by construction; first live activation (set `CCC_PUSH_*`, restart the bridge) remains a separate, explicitly-approved step.

## [0.2.0] — 2026-06-20

Tier 3 — presentation & headless surface.

### Added
- **Status line** (`claude/hooks/statusline.sh`): one-line at-a-glance bar — node · model · git branch+dirty · context % (color-coded: green/amber/red) · `⚠200k` token warning · session cost · A2A task marker · active output style. Reads Claude Code's stdin session JSON; degrades gracefully on empty/garbage input. Wired via the node-local `settings.json` `statusLine` field (the main status line is not applied from a plugin's `settings.json`).
- **Output style** (`claude/output-styles/ccc-report.md`): Korean structured-reporting default — 확정/변경/리스크/다음 절 구분, 진행 내레이션(짧은 분리 메시지), 번호형 질문, Fresh-Approval 경계, secret 비노출. `keep-coding-instructions: true` (coding behaviour unchanged). Ships via the plugin's `output-styles/` and is activated through `settings.json` `outputStyle: "ccc-report"`.
- **Headless runner** (`claude/headless.sh`): `claude -p` wrapper for cron / A2A / CI — JSON output, read-only tool baseline (override via `CCC_ALLOWED_TOOLS`/`CCC_PERMISSION_MODE`), stdin piping, session+cost logging. Guard enforcement still applies (non-`--bare`).
- Validator coverage: statusline smoke (sample + empty input), `settings.json` statusLine/outputStyle wiring resolves to shipped files, output-style frontmatter, headless/statusline `bash -n` + shellcheck.

### Notes
- Plugin `details` inventory does not list output styles as a category, but `output-styles/ccc-report.md` ships at the plugin root and loads when the plugin is enabled (verified by isolated install).

## [0.1.1] — 2026-06-20

### Fixed
- **Plugin now actually loads.** The 0.1.0 manifest passed `claude plugin validate` but failed at install time (`Status: ✘ failed to load`). Two distinct defects, both confirmed by a real isolated install on Claude Code 2.1.183:
  1. `plugin.json` referenced `./hooks/hooks.json` in its `hooks` field, but `hooks/hooks.json` is auto-loaded — the duplicate reference aborted the whole plugin load.
  2. `agents`/`commands` custom-path **arrays** (`./claude/...`) are schema-valid but silently load **0** components in this CLI; only default-location discovery is honoured.
- **Fix**: the marketplace entry now points the plugin root at the existing component tree via `source: "./claude"`. The manifest moves to `claude/.claude-plugin/plugin.json` with **no path fields** (agents/commands/skills/hooks auto-discovered), and the hook config moves to `claude/hooks/hooks.json` with `${CLAUDE_PLUGIN_ROOT}/hooks/*.sh` paths. No `claude/` restructure; `setup.sh` is unaffected.
- Verified real install: `Status: ✔ enabled` — Skills 7 (incl. 3 commands), Agents 4, Hooks 6, all hook scripts resolve.
- **Validator hardened** (`scripts/validate-harness.sh`): now resolves every `${CLAUDE_PLUGIN_ROOT}` hook path to an on-disk script, rejects silent-load path fields, asserts `source: "./claude"`, and runs `claude plugin validate` when the CLI is present — the checks that would have caught 0.1.0.

## [0.1.0] — 2026-06-20

First versioned/packaged release. Installable as a Claude Code **plugin** (`/plugin marketplace add jinwon-int/ccc-node` → `/plugin install ccc-node@ccc-node`) in addition to the existing `setup.sh` bootstrap.

### Added
- **Plugin packaging**: `.claude-plugin/plugin.json` manifest + `.claude-plugin/marketplace.json` catalog + `hooks/hooks.json` (enforcement + observability hooks via `${CLAUDE_PLUGIN_ROOT}`). Packages the node-agnostic surface (skills, slash commands, A2A agents, guard/audit/redact/notify hooks).
- **Tier 1 enforcement** — `guard.sh` PreToolUse fail-closed guard for the Fresh-Approval set, with `CCC_ALLOW_GATED=1` operator escape hatch; risk-profile mapping (`RISK-PROFILES.md`); `permissions.deny`/`ask`.
- **Tier 1.5 observability** — `audit.sh` (PostToolUse, secret-redacted JSONL), `redact.sh` (UserPromptSubmit secret-awareness), `notify.sh` (Notification/Stop/SessionEnd; approval-needed log + working-state archive).
- **Tier 2** — harness CI (`scripts/validate-harness.sh` + `.github/workflows/ci.yml`); slash commands `/node-status`, `/a2a-claim`, `/wiki-log`.
- Skills: `wiki-record`, `mcp-add`, `skill-suggest`, `gh-pr-flow`.
- A2A worker sub-agent roster: `a2a-explorer`, `a2a-researcher`, `a2a-implementer`, `a2a-verifier`.

### Notes
- **Node-local memory bootstrap** (SessionStart/PostCompact memory injection, working-state checkpoint) stays in `setup.sh` — it is inherently node-specific and not part of the portable plugin.
- Two install paths coexist: plugin (portable surface) + `setup.sh` (memory bootstrap + node templates).
- `scripts/bridge-watchdog.sh` serializes its down-detect → start critical
  section on an exclusive flock (#970). A dependency build longer than one
  tick no longer accumulates concurrent `start.sh`/`dependency_bootstrap`
  runs (cargo "Text file busy"); a tick that finds a start in flight skips.
- Termux/Android Rust toolchain is now a setup-managed prerequisite (#968):
  `setup.sh` installs `rust` + `rust-std-aarch64-linux-android` via `pkg` (or
  fails loudly with the exact line), and `dependency_bootstrap.py` warns
  upfront when cargo is absent and names it as the likely cause of a
  hash-locked build failure instead of a maturin backtrace.
