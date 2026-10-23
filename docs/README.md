# ccc-node documentation index

Living operator docs stay at the top level of `docs/`; historical closeouts and planning records live under `docs/archive/`.

| Doc | Scope |
|---|---|
| [`memory.md`](memory.md) | SessionStart/PostCompact memory injection, cache refresh, local hot-memory index, eval/benchmark flows. |
| [`auto-distill.md`](auto-distill.md) | TM-2380 session-to-Wiki canary, provider resolution, managed installation, and rollout boundary. |
| [`harness.md`](harness.md) | Settings, hooks, status line, output style, plugin/standalone mode, path overrides. |
| [`doctor.md`](doctor.md) | Doctor diagnostics, guarded repair, rollback, fleet matrix. |
| [`security-audit.md`](security-audit.md) | Read-only security audit and fleet matrix reporting. |
| [`agent-cron.md`](agent-cron.md) | Durable local task definitions, due/lock/run/scheduler commands. |
| [`a2a-broker-worker-watch.md`](a2a-broker-worker-watch.md) | Broker-side worker staleness watch: pages long-stale A2A workers from `GET /workers` (#2086). |
| [`fleet-merge-metrics.md`](fleet-merge-metrics.md) | Read-only merge-pipeline metrics per repo (merge-queue wait, effective batch size, CI runs by event); baseline for the 2026-10 batching change (#2302). |
| [`skill-autosave.md`](skill-autosave.md) | Hermes-style auto-skillification: skill-review hook, daily sweep cron, Telegram approval flow. |
| [`nunchi-recall.md`](nunchi-recall.md) | State-aware recall for `nunchi.py recall`/`dialectic`: recency re-rank, recent-state window, optional model2vec vectors, the STALE regression bench (#2464), `--as-of` point-in-time recall, `[#id]` citation footer and `why` provenance (#2466). |
| [`nunchi-conflict-sweep.md`](nunchi-conflict-sweep.md) | `nunchi.py conflict-sweep`: batched-LLM pass that flags vocabulary-disjoint state conflicts `review=1` only (never closes), with an idempotent pair ledger and judge-batch protection (#2464 제안 2). |
| [`nunchi-curation-authority.md`](nunchi-curation-authority.md) | Curation authority tiers for `peer_facts`: no path deletes a fact; unattended passes only add, close by deterministic gate and flag; bulk closes and per-fact edits are owner commands; enforced by `curation-authority.test.sh` (#2464 제안 4). |
| [`trajectory-render.md`](trajectory-render.md) | Shared session rendering grammar (`[USER]`/`[ACTION]`/`[OBSERVATION]`…), the `…(+N chars — render truncation)` marker contract, and the `obs_trunc >= 10,000` guard for distill, skill-review and nunchi (#2388). |
| [`skill-registry.md`](skill-registry.md) | Generated single registry over all repo skill sources; CI-enforced freshness; lifecycle `status` field (#1338). |
| [`skill-listing-budget.md`](skill-listing-budget.md) | Deterministic skill-listing budget: core + recently used skills keep descriptions, the rest `name-only` via owned `skillOverrides` (#2011). |
| [`skill-validate.md`](skill-validate.md) | Read-only skill pre-validation: name == directory, description + banned measurement words, core-skill trigger lines, SKILL.md `scripts/` refs + py_compile, hooks schema; repo and node (`--root`) modes (#2386). |
| [`skill-graduation.md`](skill-graduation.md) | fleet-skills → ccc-node graduation: criteria, procedure, precedence contract across the skill tools (#1344). |
| [`skill-usage-report.md`](skill-usage-report.md) | Read-only per-node skill usage report + fleet aggregation (archive/graduation/retire/unused candidates); `ccc-skill-retire.py` fleet retirement PR generator; two-stage curator archive opt-in; `fleet-managed` ownership class (#2239). |
| [`codex-managed-skills.md`](codex-managed-skills.md) | Static Codex operating-skill catalog, safe provisioning, collision policy, and rollback. |
| [`lifecycle-observability.md`](lifecycle-observability.md) | Provider-neutral lifecycle observation contract, shared redaction, owner-only audit ledger (#645). |
| [`approval-audit.md`](approval-audit.md) | Provider-neutral approval snapshots, request/display binding, and body-free owner audit metrics (#870). |
| [`side-effect-contract.md`](side-effect-contract.md) | Typed external-effect inventory, code registration markers, and deterministic recovery drills (#872). |
| [`self-update.md`](self-update.md) | Pre-approved node maintenance: pull + setup + allowlisted service restarts without loosening the guard. |
| [`bridge-ops.md`](bridge-ops.md) | Telegram bridge operations and boundaries. |
| [`provider-capability-matrix.md`](provider-capability-matrix.md) | Generated per-provider capability states (runtime + memory parity) with the conformance-gate contract. |
| [`bridge-upstream-i18n-policy.md`](bridge-upstream-i18n-policy.md) | Bridge upstream relationship and Korean/i18n policy. |
| [`a2a-claude-worker.md`](a2a-claude-worker.md) | A2A Claude Code worker lane and native Termux worker preflight. |
| [`a2a-piri-memory.md`](a2a-piri-memory.md) | A2A Piri lane memory snapshot producer. |
| [`android-termux-claude.md`](android-termux-claude.md) | Android/Termux Claude Code constraints. |
| [`termux-vps-parity.md`](termux-vps-parity.md) | Termux/VPS parity rules. |
| [`architecture-contract.md`](architecture-contract.md) | Executable architecture import contract (#872). |
| [`branch-protection.md`](branch-protection.md) | Branch protection and CODEOWNERS policy. |
| [`ci-governance.md`](ci-governance.md) | CI required-check governance. |
| [`codex-skill-collector-activation.md`](codex-skill-collector-activation.md) | Codex skill-candidate collector default-ON operations. |
| [`continuation.md`](continuation.md) | Yield-and-continue agent contract for the continuation queue (#1113). |
| [`crush-harness.md`](crush-harness.md) | Crush harness for Kimi-K3/GLM nodes (#923). |
| [`github-transport.md`](github-transport.md) | GitHub transport policy (local git + gh CLI first). |
| [`danso-telegram.md`](danso-telegram.md) | Select Danso with Astra medium and explicit host/bubblewrap execution. |
| [`piri-runtime-contract.md`](piri-runtime-contract.md) | PiriRuntime provider contract. |
| [`pr-status-poll.md`](pr-status-poll.md) | PR/issue status poll lane (#962). |
| [`timed-test-deadline-scan.md`](timed-test-deadline-scan.md) | Expired-unjudged timed-test scanner cron and its owner spool notice (#1870). |
| [`quality-baseline.md`](quality-baseline.md) | Bridge quality gates and measured coverage baseline (#348). |
| [`service-control.md`](service-control.md) | Service control and the real enforcement split (post TM-1306). |
| [`version-and-provenance.md`](version-and-provenance.md) | Version anchor, provenance, and self-update identity. |
| [`examples/`](examples/) | Non-secret example env/scripts. |
| [`archive/`](archive/) | Historical roadmap and closeout records. |
