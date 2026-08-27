# 외부 부수효과와 복구 계약

`architecture/side-effect-contract-v1.json`이 외부 효과의 canonical inventory다.
각 operation은 코드의 `ccc-side-effect` marker 및 구현 symbol과 결합되고,
idempotency·retry·ambiguous outcome·reconcile·compensation·audit·approval 경계를
typed enum과 bounded metadata로 선언한다. 계약은 token, credential, raw payload,
runtime state를 저장하지 않는다.

`scripts/ccc_side_effect_contract.py`는 Git-tracked regular Python/shell source만
검사한다. 코드 marker와 inventory가 어느 방향으로든 어긋나거나, non-idempotent
효과가 safe retry를 주장하거나, policy에서 계산한 recovery action과 표가 다르면
operation과 규칙만 출력하고 fail closed한다.

```bash
python3 scripts/ccc_side_effect_contract.py --repo-root .
```

recovery drill은 외부 네트워크 대신 in-memory fake sink를 사용한다. native key는
같은 key의 두 번째 attempt를 한 effect로 축약하고, local-ledger 또는 무키 효과의
ambiguous window는 계약대로 reconcile/manual/compensation에서 멈춘다. 이 검사는
exactly-once를 주장하지 않으며 실제 Telegram, Honcho, self-update를 실행하지 않는다.

아래 두 표는 JSON에서 생성된다. 직접 수정하지 않고 contract를 변경한 뒤
`--render-document` 출력으로 함께 갱신한다.

<!-- ccc-side-effect-contract:begin -->
### External-effect inventory

| Operation | Owner | Idempotency / key | Retry | Ambiguous window | Reconcile | Compensation | Audit | Approval boundary | Implementation |
|---|---|---|---|---|---|---|---|---|---|
| `telegram.send_text` | bridge delivery | none: `none` | conditional | request accepted before response or process exit before caller ACK | manual | delete | body-free bridge diagnostics and Telegram receipt when returned | authorized bridge delivery path | `bridge/utils/tg_robust.py::send_with_retry` |
| `honcho.deliver_distill` | memory Honcho sink worker | native: `ccc-distill-<job-id> and ccc-distill-<job-id>-session` | safe | HTTP success before durable outbox ACK and journal completion | none | none | owner-only outbox plus leased distill journal status | configured memory route and Honcho enable gate | `bridge/memory/distill_honcho_worker.py::CodexDistillHonchoSinkWorker.write_once` |
| `self_update.apply` | pre-approved node maintenance | local-ledger: `old and new commit SHA plus installer generation stamps` | conditional | repository or installed artifacts changed before terminal audit | query | restore-snapshot | body-free self-update audit record and notification result | reviewed procedure plus operator-owned service allowlist | `scripts/ccc-self-update.sh::<top-level>` |
| `agent_cron.spool_notify` | agent-cron owner notification | local-ledger: `agent-cron:<task-id>:<run-id>:<status>` | conditional | spool file created before lastRunAt/runHistory ACK | receipt | delete | body-free spool path and redacted owner text envelope | notify=telegram-owner or allowlisted telegram-chat | `scripts/agent_cron.py::write_owner_spool` |
| `external_wait.wake_resume` | bridge external-wait monitor | local-ledger: `registry pending-wake journal keyed by wait_id; terminal finish is journaled before any delivery` | safe | owner notice accepted by the push spool or continuation admitted before mark_wake persists, so a later drain may repeat it | query | none | external-wait registry wake record (delivered/resumed/skip_reason) plus body-free bridge diagnostics | auto-resume gate: resume_enabled flag, daily cap, and exact-head terminal pinning; gh transport stays read-only | `bridge/core/external_wait_monitor.py::ExternalWaitMonitor._deliver_wake` |
| `skill_autosave.sweep` | skill autosave cron sweep | local-ledger: `skill-autosave.seen and .notified ledgers keyed by transcript identity and growth snapshot` | conditional | auto-mode installer moved a passing draft before its post-hoc notice was queued or logged, leaving install state verifiable only via the installed-by ledger | manual | restore-snapshot | pending-skills queue, installed-by=autosave ownership ledger, rollback archive, and skill-autosave.log | approve mode by default installs nothing; auto install requires explicit mode opt-in plus the machine gate and daily cap in autoinstall.sh | `scripts/ccc-skill-autosave.sh::<top-level>` |
| `service_control.restart` | root-installed service restart wrapper | none: `none` | manual-only | systemctl accepted the restart before the unit reached its target state or the wrapper exited while the stop/start cycle was still in flight | query | none | wrapper stderr naming only the unit plus systemd journal records for the restarted unit | root-owned regular wrapper plus exact-unit allowlist /etc/ccc-node/service-control.allow with no group/world writable paths | `scripts/ccc-service-control.sh::<top-level>` |
| `telegram.terminal_cleanup` | bridge lifecycle terminal-outbox drain | local-ledger: `task ledger terminal_op records keyed by task_id and journaled before the first Telegram call, with an attempts cap of 20` | safe | Telegram accepted the edit or delete before resolve_terminal_op could purge the outbox entry, so a later drain repeats it until BadRequest confirms an already-gone or unmodified state | receipt | none | body-free task ledger terminal_op attempts counter plus bridge debug diagnostics naming task ids only | automatic housekeeping limited to bot-authored status messages recorded in the task ledger; user content is never deleted | `bridge/core/bot_lifecycle.py::BotLifecycleMixin._drain_terminal_ops` |

### Deterministic recovery matrix

| Operation | Before intent | Intent → call | Success → ACK | ACK → terminal | Duplicate/restart |
|---|---|---|---|---|---|
| `telegram.send_text` | safe-replay | safe-replay | manual-review | safe-replay | manual-review |
| `honcho.deliver_distill` | safe-replay | safe-replay | safe-replay | safe-replay | safe-replay |
| `self_update.apply` | safe-replay | safe-replay | reconcile | safe-replay | reconcile |
| `agent_cron.spool_notify` | safe-replay | safe-replay | reconcile | safe-replay | reconcile |
| `external_wait.wake_resume` | safe-replay | safe-replay | reconcile | safe-replay | reconcile |
| `skill_autosave.sweep` | safe-replay | safe-replay | manual-review | safe-replay | manual-review |
| `service_control.restart` | safe-replay | safe-replay | reconcile | safe-replay | reconcile |
| `telegram.terminal_cleanup` | safe-replay | safe-replay | reconcile | safe-replay | reconcile |
<!-- ccc-side-effect-contract:end -->


초기 범위는 이슈 #872의 제안 순서에 따라 Telegram text delivery, Honcho distill
delivery, self-update apply였고, 이후 슬라이스가 agent-cron owner spool,
external-wait wake, skill-autosave sweep, lifecycle terminal-outbox cleanup,
service-control restart를 같은 marker와 policy gate로 추가했다. 실제 Telegram
전송·드래프트 편집 중 전송 경로는 계속 `telegram.send_text`가 담당한다.
Honcho replay, callback query 직접 편집(단일 소유 symbol 부재), skill
install/rollback/archive 하위연산, GitHub write는 후속 inventory 확장 때 같은
방식으로 추가한다.
