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
| [`skill-autosave.md`](skill-autosave.md) | Hermes-style auto-skillification: skill-review hook, daily sweep cron, Telegram approval flow. |
| [`skill-registry.md`](skill-registry.md) | Generated single registry over all repo skill sources; CI-enforced freshness; lifecycle `status` field (#1338). |
| [`skill-graduation.md`](skill-graduation.md) | fleet-skills → ccc-node graduation: criteria, procedure, precedence contract across the skill tools (#1344). |
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
| [`piri-runtime-contract.md`](piri-runtime-contract.md) | PiriRuntime provider contract. |
| [`pr-status-poll.md`](pr-status-poll.md) | PR/issue status poll lane (#962). |
| [`quality-baseline.md`](quality-baseline.md) | Bridge quality gates and measured coverage baseline (#348). |
| [`service-control.md`](service-control.md) | Service control and the real enforcement split (post TM-1306). |
| [`version-and-provenance.md`](version-and-provenance.md) | Version anchor, provenance, and self-update identity. |
| [`examples/`](examples/) | Non-secret example env/scripts. |
| [`archive/`](archive/) | Historical roadmap and closeout records. |
