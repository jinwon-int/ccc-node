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

### Deterministic recovery matrix

| Operation | Before intent | Intent → call | Success → ACK | ACK → terminal | Duplicate/restart |
|---|---|---|---|---|---|
| `telegram.send_text` | safe-replay | safe-replay | manual-review | safe-replay | manual-review |
| `honcho.deliver_distill` | safe-replay | safe-replay | safe-replay | safe-replay | safe-replay |
| `self_update.apply` | safe-replay | safe-replay | reconcile | safe-replay | reconcile |
| `agent_cron.spool_notify` | safe-replay | safe-replay | reconcile | safe-replay | reconcile |
<!-- ccc-side-effect-contract:end -->

초기 범위는 이슈 #872의 제안 순서에 따라 Telegram text delivery, Honcho distill
delivery, self-update apply였고, 이번 슬라이스가 agent-cron owner spool을 같은
marker와 policy gate로 추가한다. 실제 Telegram 전송은 계속 `telegram.send_text`다.
Wiki handoff, external-wait, autosave, service lifecycle, GitHub write는 후속
inventory 확장 때 같은 방식으로 추가한다.
