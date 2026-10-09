# Matrix 대화별 병렬 턴 설계 (#2006)

Status: 설계 문서만 있음. **구현하지 않았고 운영 반영 승인도 없다.** 코드·설정·기본값은
바뀌지 않는다. 아래 줄 번호는 이 문서를 작성한 시점의 `origin/main` `ee7fc8e3`
(`bridge/` 기준)을 가리킨다.

관련 이슈: #2006 (본 설계), #1959 (`/stop` 락 항목), #1951 (self-update idle gate),
#2001 (held files), #1825/#1934/#1895 (self-job), #2003 (`/restart`).

---

## 1. 현황

### 1.1 Matrix: 모든 방을 통틀어 한 번에 한 턴

- **워커가 하나다.** `MatrixTransport.run()`은 `receive`·`send`·`work` 세 leg만 띄운다
  (`core/matrix/transport.py:2013-2017`). `work()`는 `store.claim()`으로 job 하나를 꺼내
  `await self.run_turn(job)`을 끝까지 기다린 뒤에야 다음 claim을 한다
  (`transport.py:1855-1867`). 따라서 동시에 도는 턴은 프로세스 전체에서 최대 1개다.
- **순서 규칙 자체는 이미 scope 단위다.** `Store.claim()` SQL은
  `state='queued' AND NOT EXISTS (… p.scope=q.scope AND p.seq<q.seq AND p.state!='done') ORDER BY seq LIMIT 1`
  이다 (`core/matrix/state.py:1079-1088`). 같은 scope 안에서 앞선 행이 `done`이 아니면
  (`running`·`uncertain`·`ready`(미발송 답) 포함) 뒤 job은 claim되지 않는다. 다른 scope의
  job은 SQL상 막히지 않는다. **전역 직렬은 SQL이 아니라 단일 `work()` 루프와 단일 슬롯
  속성(§2.1)에서 생긴다.**
- **scope = (계정, 방, 보낸 사람).** `scope_of()`는 `sha256([account, room_id, sender])`이다
  (`state.py:286-287`). 가족방에서는 구성원마다 scope가 따로 생긴다. 오너 DM과 오너의
  가족방 발화도 서로 다른 scope다.
- **턴 상한은 6시간이다.** `turn_timeout_minutes` 기본값은 360분이다 (`state.py:248-263`).
  `transport.py:464`의 "default 20 min" 주석은 현재 값과 맞지 않는다.
- **큐 한도**: 전체 128, scope당 32 (`state.py:953`, 검사는 `1051-1056`, self-job은
  `1149-1153`). 넘치면 `NOTICE_QUEUE_FULL`을 보낸다 (`transport.py:1183-1189`).

### 1.2 Telegram: 참고 모델 (정정 포함)

이슈 본문에 적힌 "대화별 큐, 최대 3개 병렬"은 **전역 동시 턴 3개**라는 뜻이 아니다.

- `UserTaskQueue(max_inflight=3)` (`core/task_queue.py:29`; `core/bot.py:136,157`)는
  **대화 키 하나당** 받아 둘 수 있는 in-flight task 수의 상한이다. 넘치면 거절한다
  (`task_queue.py:112`).
- 한 대화 안의 실제 실행은 `ProjectChatHandler._conversation_turn` 락으로 직렬화된다
  (`core/project_chat.py:1087-1137`, 사용처 `core/project_chat_process.py:1948`). 대화가
  바쁠 때 들어온 후속 메시지는 durable follow-up queue에 저장된다
  (`core/bot_followup_queue.py:306-381`, `occupied`는 351줄).
- **서로 다른 대화 사이에는 브리지 차원의 전역 상한이 없다.** 대화마다 병렬로 돈다.
  대화 키는 `storage_key`/`stream_key`로 정한다 (`core/session_scope.py:31-55`, 기본값
  `per-user-chat`: `utils/config.py:784-795`).
- Telegram은 이미 "대화 안은 직렬, 대화끼리는 병렬"로 운영 중이다. 그래서 provider
  런타임은 대화 간 동시 실행을 지원한다 (§2.8).

### 1.3 사용자에게 보이는 영향

- 가족방에서 긴 작업(최대 6h)이 돌면 오너 DM 요청은 그 턴이 끝날 때까지 claim되지 않는다.
- **대기 안내도 나오지 않는다.** `NOTICE_QUEUED`는 `pending_before()`가 0보다 클 때만
  보낸다 (`transport.py:1196-1202`). 그런데 `pending_before()`는 **같은 scope**의 앞선 행만
  센다 (`state.py:1069-1077`). 다른 scope의 턴 뒤에서 기다리는 오너 DM은 0으로 계산되어
  안내가 없다. 사용자가 보는 것은 early typing 8초(`transport.py:1193`, `587`)뿐이고, 그
  뒤로는 아무 반응이 없다.
- `/restart`(#2003)도 하나의 턴이다 (`core/matrix/bot.py:1873-1877`). 그래서 가족방 작업
  뒤로 밀린다. `/stop`·`/approve`는 sync 경로에서 바로 처리되므로 밀리지 않는다
  (`transport.py:1171-1173`).
- continuation self-job은 다른 방 작업 뒤에서 기다리다 `_continuation_wait_seconds`
  (턴 상한 + 1800s 여유, `bot.py:129`, `1288-1295`)를 넘기면 실패한 번들로 집계될 수 있다.

---

## 2. 제약·위험 목록

### 2.1 "활성 턴은 하나"를 전제로 하는 transport 공유 상태

| 속성 / 위치 | 현재 가정 | N>1이면 깨지는 점 |
|---|---|---|
| `self.active` (`transport.py:509`, 설정 1871, 해제 1924) | 유일한 실행 job | 두 번째 턴이 덮어쓴다. `_RoomSink._active()`가 `transport.active is self.job`인지 확인하므로 (`332-333`) 먼저 시작한 턴의 typing·interim·status·approval이 **조용히 무시된다**. |
| `self.turn_task` (`510`, `1885`, `1925`) | 유일한 runner task | `_turn_running()` (`1204-1205`)과 `/stop` 대상 (`1266-1275`)이 틀어진다. |
| `self.approvals` (`511`) | 턴 하나의 nonce→future | `run_turn` 시작 시 `self.approvals = {}`로 **초기화한다** (`1872`). finally에서는 **모든** future를 False로 닫는다 (`1920-1923`). 턴 B가 시작하거나 끝나면 턴 A의 승인 대기가 끊기거나 거절된다. 상한 `MAX_PENDING_APPROVALS=16`도 전역이다 (`194`, `426`). |
| `self.cancel_requested` (`513`, `1270`, `1873`) | 턴 하나 | 쓰기만 하고 읽는 곳이 없다 (grep 결과 0건). 제거하거나 슬롯별로 옮긴다. |
| `self.turn_timed_out` (`534`, `1874`, `1908`) | 턴 하나 | `MatrixBot._record_turn_health`가 전역으로 읽는다 (`bot.py:2313`). 턴 A가 타임아웃되면, 턴 B의 `/stop` 취소까지 agent 장애로 기록될 수 있다. |
| `_encrypted_raw`의 `first_sent` 표시 (`1802-1804`) | active 방 하나 | 같은 방에 활성 턴이 여러 개면 지연 기록이 섞인다. 기능 영향은 없고 계측만 흐려진다. |
| `control()`의 scope 비교 (`1224-1244`) | active scope 하나 | 활성 턴이 여럿이면 보낸 사람 scope로 슬롯을 찾아야 한다. `/cancel <tid>`, `/approve <tid> <nonce>`는 해당 슬롯의 tid와 nonce로 검증해야 한다. |
| `last_turn` meta (`1926-1928`) | 마지막 턴 하나 | 마지막으로 쓴 값만 남는다. 진단용이라 허용할 수 있다. |
| `_origin_ms` / `_batch` (`523`, `1354-1358`, `1301`) | 수신 측 상태 | **턴 상태가 아니다.** `process_pending` 안에서만, `matrix_lock` 아래 순차로 쓰인다 (`1311`). 병렬화해도 안전하다. |
| typing (`587`, `_early_typing` `613-619`, sink `335-342`) | — | 방 단위 PUT이라 겹쳐도 무해하다. 다만 sink의 활성 판정은 위 `self.active`에 묶여 있다. |

### 2.2 MatrixBot(runner)의 단일 턴 전제

- `_active_sink` / `_active_turn_id` (`bot.py:517-519`, 설정 `1845-1846`, 해제 `1888-1889`)는
  인스턴스 필드다. 병렬이면 서로 덮어쓴다.
  - **#2001 held files**: `_enqueue_deliverables`가 `_active_turn_id`를 파일의 parent로
    쓴다 (`bot.py:2186-2192` → `transport.enqueue_file(after=…)` `transport.py:1022-1026`).
    엉뚱한 턴이 parent가 되면 그 턴이 끝날 때 파일이 풀리거나, 그 턴이 취소될 때 파일이
    버려진다 (`state.py:1224-1232`).
  - **방을 넘는 유출 위험 (P0)**: `_dispatch_turn`은 `sink`가 없으면 `self._active_sink`로
    대신한다 (`bot.py:2264`). `_auto_resume_danso_recovery`는 `_active_sink is not None`을
    "지금 턴 안에 있다"는 뜻으로 해석한다 (`bot.py:2384`). 병렬 상태에서는 **다른 방의
    sink**로 interim·status·approval이 나갈 수 있다. 오너 DM 내용이 가족방에 게시될 수
    있다는 뜻이다.
- `_enqueue_user_task`의 주석은 "transport가 방마다 한 턴으로 직렬화한다"고 적혀 있다
  (`bot.py:2504-2509`). 실제로는 전역 직렬이다. 병렬화 뒤에는 "lane 안에서만 직렬"로
  고쳐 적어야 한다.
- `_runtime_active_sessions` (`bot.py:526`, `2022-2040`), `_task_resume_generations`
  (`516`), `_continuation_waiters` (`529`)는 대화 키나 cid를 키로 쓰는 dict/set이다. 같은
  키가 lane 안에서 직렬이면 안전하다.
- **대화 키와 transport scope가 항상 1:1은 아니다.** runner의 대화 키는
  `storage_key(scope, user_id, chat_id)`이고, `chat_id`는 DM이면 보낸 사람, 가족방이면 방이다
  (`core/matrix_ids.py:108-117`, `bot.py:2009-2011`). 기본 `per-user-chat`에서는
  (방, 보낸 사람)과 1:1이다. 하지만 `shared-groups`나 `shared-all`이면 transport scope 여러
  개가 **하나의 provider 세션**을 쓴다. 이때는 `_conversation_turn` 락이 직렬을 보장하므로
  데이터는 안전하다. 대신 워커 슬롯이 락을 기다리며 헛돈다(슬롯 기아).

### 2.3 crypto / outbox 순서

- 암호화 송신은 모두 `matrix_lock` 아래에서 한다: outbox part마다 (`transport.py:1731`),
  status bubble (`384`), 파일 (`1079`). `_encrypted_raw`는 megolm 세션 공유와 암호화를
  함께 한다 (`1780-1805`). nio olm 상태는 이 락 하나로만 보호된다. **병렬 턴이 직접
  암호화 송신을 추가하면 안 된다.** 모든 송신은 계속 락을 거치는 outbox나 sink를 통해야 한다.
- outbox는 **전역 seq 순서 단일 큐**이고 send leg도 하나다 (`state.py:1116-1117`,
  `transport.py:1685-1699`). 행마다 락 아래에서 `pin_devices`(keys/query POST, `772`)와
  `room_gate`(GET 2회, `900`, `905`)를 한다. 턴을 병렬화해도 **답 전달은 여전히 직렬**이다.
  1 MiB 답은 약 90 part이고 (`1714-1716`) 그동안 다른 방 답이 뒤에서 기다린다. send 중
  `ConnectionError`(예: `group-key-share-incomplete`, `1791`)가 나면 send leg 전체가 backoff
  재시작된다 (`1997-2011`). 한 방의 문제가 모든 방 전달을 멈춘다(`1088-1091` 주석도 인정).
  이 결합은 본 설계 범위 밖이다. §3 선택지 D로 기록만 한다.
- 방별 순서는 seq로 보존된다. 병렬 턴의 interim이 섞여도 방 안 순서는 유지된다.

### 2.4 `matrix_lock` 보유 시간

- `process_pending`은 sync 배치 **전체**를 락 아래에서 처리한다 (`transport.py:1311-1359`).
  여기에는 `pin_devices`, `receive_response`(복호화), 방마다 `room_gate`(네트워크),
  이벤트마다 `input()`이 들어간다. `input()`은 다시 `with_reply_context` →
  `_fetch_parent` GET (`1648-1661`)과 `control()` → `_cancel_active` →
  `await runner.cancel(job)` (`1266-1275`)을 부른다. 후자는 **타임아웃이 없다** (#1959 항목).
- 병렬 턴에서는 `/stop`이 더 자주 쓰이고 status bubble 경쟁도 늘어난다(턴마다 10초 간격,
  `bot.py:152`). 그러면 **#1959의 "/stop이 락을 쥔 채 무한 대기" 문제가 모든 lane을
  얼린다.** 병렬화 전에 반드시 먼저 고쳐야 한다(§4 PR-2).

### 2.5 uncertain / block 의미

- **지금 uncertain은 새 작업 전체를 멈추지 않는다.** 근거는 세 가지다.
  1. claim SQL은 uncertain 행이 **자기 scope만** 막게 되어 있다 (`state.py:1083`).
  2. uncertain은 오래 남지 않는다. `run_turn`의 finally가 `uncertain_job` 직후 곧바로
     `_close_interrupted`로 해소한다 (`transport.py:1914-1919`, `1934-1943`). 재시작으로
     남은 것은 store open에서 `running→uncertain`으로 바뀐 뒤 (`state.py:996`) `work()`
     루프 맨 앞에서 `NOTICE_RESTARTED`로 해소된다 (`transport.py:1860-1861`).
  3. `block()`은 "pauses the transport"라고 적혀 있지만 운영자 `unblock()`만 호출한다
     (`state.py:1236-1259`, grep상 다른 호출처 없음). 파일럿의 전역 정지 의미는 이미 없다.
- 병렬화 시 주의점이 있다. `work()` 루프 맨 앞의 "uncertain 전부 해소"를 워커 N개가
  돌리면 다른 슬롯이 방금 uncertain으로 만든 job을 가져갈 수 있다. 지금은
  `uncertain_job`→`_close_interrupted` 사이에 `await`가 없어서 끼어들 틈이 없다. 하지만
  이 불변식에 기대지 않도록 **startup 1회 sweep으로 옮긴다**(§4 PR-1).
- 종료 시에는 `shutting_down` 경로가 턴을 uncertain으로 남긴다 (`1899-1904`). N개 슬롯이면
  N개가 남고, 다음 기동에서 방마다 재시작 안내가 나간다. 동작은 지금과 같다.

### 2.6 self-job과 사용자 턴의 경합

- self-job(continuation `bot.py:1297-1353`, external-wait resume `1205-1249`, danso
  auto-resume `2377-2400`)은 `enqueue_self_job` (`transport.py:1144-1164`) →
  `Store.self_job` (`state.py:1127-1159`)를 거쳐 **같은 scope의 일반 job**이 된다. 따라서
  같은 scope 안에서는 사용자 턴과 계속 직렬이다. docstring의 "single-turn discipline"
  (`transport.py:1150-1151`)은 "lane discipline"으로 바꿔 적는다.
- **동작 변화**: 병렬화하면 한 대화의 자동 continuation이 다른 대화의 사용자 턴과
  **동시에** 돈다. 부하와 비용 모두 autonomous 쪽으로 늘어난다. self-job에 별도 상한을 둘지는
  열린 질문이다(§5 Q4).
- `/stop`은 idle scope에서 continuation만 취소한다 (`transport.py:1245-1250`,
  `bot.py:1952-1960`). 슬롯 조회를 scope 기준으로 바꿔도 의미는 그대로다.

### 2.7 health / workload 보고

- `MatrixBot._workload_snapshot`은 `project_chat.workload_snapshot`을 쓴다
  (`bot.py:838-845`). 이 값은 registry의 active session 수다 (`project_chat.py:1139-1170`).
  N개 병렬 턴을 제대로 센다. fallback인 `(1, 0.0) if transport.active`만 단일 가정이다.
- **claim 전 queued job은 어디에도 보고되지 않는다.** `waiting_for_turn`은 runtime
  admission 대기만 센다 (`project_chat.py:1226-1229`). 병렬화 뒤에는 "슬롯이 없어 대기 중인
  job 수"를 health에 추가해야 한다.
- self-update idle gate(#1951)는 health.json의 `workload.active_requests`를 읽는다
  (`scripts/ccc-self-update.sh:609-655`). 값의 출처가 registry라서 병렬에서도 정확하다.
  queued job은 durable하므로 재시작 뒤에도 남는다(유실 없음).

### 2.8 provider 세션 동시성

provider는 모두 **세션·스레드 단위 락**이다. 대화 간 병렬은 Telegram에서 이미 쓰이고 있다.

| provider | 동시성 단위 | 근거 |
|---|---|---|
| Claude | session_id별 `asyncio.Lock` | `core/claude_runtime.py:389-390` |
| Codex | audience마다 app-server 1개, thread별 락 | `core/codex_runtime_pool.py:35`, `core/codex_runtime.py:752-755` |
| Piri | 세션마다 프로세스 spawn, 세션별 `_turn_lock` | `core/piri_runtime.py:172`, `221`, `450` |
| Danso | 세션별 `_lock`, 턴마다 subprocess | `core/danso_worker.py:578`, `708` |

- 세션 획득 구간은 `_session_guard_lock`으로 잠깐만 직렬화된다
  (`project_chat_process.py:853`).
- **자원 한도**: `CCC_BRIDGE_MAX_RESIDENT_SESSIONS` 기본값은 2다 (`utils/config.py:687-696`).
  guard는 **idle 세션만** 내보낸다 (`core/project_chat_state.py:592-600`). N=3이면 active
  세션 3개가 동시에 상주한다. `CCC_BRIDGE_CODEX_MAX_ATTACHMENTS` 기본값 2 (`config.py:711`)도
  idle일 때만 recycle한다. RSS 워터마크는 `active_sessions == 0`일 때만 발동한다. 메모리가
  작은 노드(Termux)에서는 N>1이 위험하다.
- **Grok 프론트엔드**도 같은 `MatrixTransport`를 다른 `TurnRunner`로 쓴다
  (`core/grok_matrix_bot.py:146-148`, `369`). 병렬화는 runner가 명시적으로 선택할 때만
  켜져야 한다.

### 2.9 기타

- `/restart`(#2003) 순서가 바뀐다. 지금은 다른 방 턴이 끝난 뒤 실행된다. 병렬화하면 다른
  lane 턴이 도는 중에 재시작을 예약해 그 턴들을 끊는다(uncertain → 재시작 안내). 다른 lane이
  바쁠 때의 처리 방식은 §5 Q5에서 결정한다.
- 같은 가족방에서 두 구성원(다른 scope)의 턴이 동시에 돌면 status bubble 두 개와 답이
  한 방 타임라인에 섞인다. 기능상 문제는 없지만 UX 판단이 필요하다(§5 Q1).

---

## 3. 설계 선택지

### A. lane별 직렬 + 전역 워커 N개 (lane = scope)

**내용**: `work()`를 dispatcher로 바꾼다. 빈 슬롯이 있으면 `claim()`하고, job마다
`run_turn`을 별도 task로 띄운다. 기존 claim SQL이 이미 scope 직렬을 보장하므로 **스키마와
SQL은 바꾸지 않는다.** claim은 `await` 없이 SELECT→UPDATE를 한 트랜잭션으로 처리하므로
(`state.py:1080-1087`) 단일 이벤트 루프에서 원자적이다.

**필요한 변경**
- transport: `_TurnSlot`(job, task, sink, approvals, timed_out)과
  `self.turns: dict[scope, _TurnSlot]`를 둔다. `self.active`는 호환 property로 남긴다
  (슬롯이 정확히 1개면 그 job). `_RoomSink._active()`는 슬롯 생존 여부로 판정한다.
  `control()`은 `self.turns.get(req.scope)`로 찾는다. approvals는 슬롯 안에 두고 nonce가
  어느 슬롯 것인지로 검증한다. 전역 approvals 상한은 유지한다.
- runner: `_active_sink`/`_active_turn_id`를 `contextvars.ContextVar`로 바꾼다. `run_turn`이
  만든 task 안에서만 보이므로 restart scan 같은 외부 경로에서는 None이다.
  `turn_timed_out`은 슬롯 속성으로 두고 sink를 통해 읽는다.
- config: `CCC_MATRIX_MAX_PARALLEL_TURNS`(1..3, 기본 1). `saved_policy`에는 넣지 않는다
  (`state.py:800-808`에 넣으면 값을 바꿀 때 `saved-policy-changed`가 난다). runner가
  `supports_parallel_turns = True`를 선언해야만 1을 넘는다. Grok runner는 1로 고정한다.

**위험**: §2.1~2.2의 모든 속성을 빠짐없이 옮겨야 한다. 하나라도 놓치면 방을 넘는
sink 오용이 생긴다. shared-groups 노드에서 슬롯 기아가 생긴다(§2.2).
**테스트**: fake runner(이벤트로 진행을 제어)로 다음을 검증한다. ① 두 scope 동시 실행,
같은 scope 직렬 ② N 상한 ③ `/stop`·`/cancel`·`/approve`가 자기 슬롯에만 작용 ④ 턴 B 종료가
턴 A 승인에 영향 없음 ⑤ held file parent가 정확함 ⑥ 종료 시 슬롯 N개 모두 uncertain → 기동
시 방마다 안내 ⑦ N=1에서 기존 `test_matrix_transport.py`·`test_matrix_bot.py` 결과가 그대로.

### B. 방(room)별 lane

**내용**: lane 키를 scope가 아니라 `room_id`로 한다. 가족방 하나는 구성원이 누구든 한 번에
한 턴만 돌고, 방 간에만 병렬이다. 방마다 워커를 하나 두는 방식이다(방 수는 config
`rooms`로 제한됨).

**필요한 변경**: A의 슬롯 리팩터링에 더해 claim을 room 단위로 바꿔야 한다. SQL을
`p.room_id=q.room_id`로 바꾸거나, Python에서 바쁜 방을 건너뛴다. `pending_before`도 room
기준으로 바꾸면 가족방 대기 안내가 더 정확해진다.
**위험**: 오너가 가족방에서 호출한 것과 다른 구성원의 긴 작업이 여전히 서로 막는다.
방 수만큼 동시 실행되므로 N 상한을 따로 두어야 한다. 기존 scope 직렬 SQL과 이중 조건이 된다.
**테스트**: A와 같고, "같은 방 다른 발신자는 직렬"을 추가한다.

### C. 전역 직렬 유지 + 오너 DM 우선 lane

**내용**: 워커를 2개로 고정한다. 하나는 오너 direct room 전용, 하나는 그 외 전부.
**필요한 변경**: 동시 턴이 2개가 되므로 §2.1~2.2 리팩터링은 A와 **똑같이** 필요하다.
claim에 방 종류 필터(`room_id IN direct_rooms` / `NOT IN`)를 더한다.
**위험**: 비용은 A와 같은데 유연성이 없다. 가족방끼리는 여전히 서로 막는다.
**테스트**: A의 ①~⑦에 lane 분류 테스트를 더한다.

### (범위 밖) D. 방별 send lane

outbox head-of-line 문제(§2.3)는 A~C 어느 것으로도 풀리지 않는다. send leg를 방별
task로 나누려면 `matrix_lock`, 재시도 backoff, health `outbox_head_age_s`
(`transport.py:1966-1995`)를 다시 설계해야 한다. 별도 이슈로 다룬다.

---

## 4. 권장안과 단계별 구현 계획

### 4.1 권장안

**A(lane = scope, N개 워커)를 기본 N=1로 도입하고 opt-in으로 켠다.** 이유는 다음과 같다.
- claim SQL과 스키마를 바꾸지 않는다. 롤백은 설정값 하나로 끝나고 데이터 이관이 없다.
- C는 A와 리팩터링 비용이 같으므로, 필요하면 A 위의 **정책**으로 얹는다: "N≥2일 때 가족방
  job은 최대 N−1 슬롯만 쓴다"(오너 DM 예약 슬롯). §5 Q3에서 결정한다.
- B의 "같은 방 한 번에 한 턴"이 필요하면 lane 키 함수만 바꾸면 된다(PR-4의 `lane_key`).
- Telegram과 같은 모델("대화 안 직렬, 대화끼리 병렬")이고, 차이는 전역 상한 N뿐이다.

### 4.2 PR 분할

각 PR은 docs 동기화(`docs/matrix-frontend.md`)를 포함하고, `bridge/tests` 전체가 green이어야 한다.

**PR-1 — 슬롯 리팩터링 (동작 변화 없음, N=1 고정)**
- 변경: `_TurnSlot`와 `self.turns`를 도입한다. 호환용 `active`/`turn_task` property를 둔다.
  approvals를 슬롯으로 옮기고, `cancel_requested`를 제거하고, `turn_timed_out`을 슬롯으로
  옮긴다. `_encrypted_raw`의 first_sent는 방→슬롯 조회로 바꾼다. uncertain sweep을 startup
  1회로 옮긴다. runner 쪽은 `_active_sink`/`_active_turn_id`를 ContextVar로 바꾸고
  `_record_turn_health`가 슬롯 timeout 신호를 읽게 한다.
- 수용 기준: (a) 기존 Matrix 테스트를 **수정 없이** 통과한다. (b) 신규 테스트: 턴 밖(restart
  scan)에서 `_auto_resume_danso_recovery`가 self-job 경로를 탄다. ContextVar가 다른 task로
  새지 않는다. (c) `grep -n "self\.active\b" core/matrix/transport.py`가 property 정의와 호환
  경로에서만 나온다.

**PR-2 — `/stop` 취소를 `matrix_lock` 밖으로 (#1959 항목 해소)**
- 변경: `control()`은 슬롯 cancel을 `_spawn`으로 띄우고 곧바로 반환한다. `runner.cancel`에는
  `asyncio.timeout`(예: 10s) 상한을 두고, 초과하면 task.cancel이 우선한다.
- 수용 기준: runner.cancel이 영원히 걸리는 fake로 테스트해도 sync(`process_pending`)와 send가
  계속 진행한다. `/stop` 안내는 1회만 나간다.

**PR-3 — 워커 풀 (opt-in)**
- 변경: `work()`를 dispatcher로 바꾼다(빈 슬롯 수만큼 claim, 슬롯 종료 시 wake).
  `CCC_MATRIX_MAX_PARALLEL_TURNS`(1..3, 기본 1)와 runner capability 검사를 추가한다.
  대기 안내에 "다른 대화 작업 뒤 대기" 문구를 더한다(슬롯이 모두 차 있고 same-scope 대기가
  0일 때, §5 Q6). health에 `matrix_queued_jobs`와 `parallel_slots_in_use`를 넣는다(body 없음).
- 수용 기준: §3 A 테스트 ①~⑦. N=1이면 claim·실행 순서가 기존 테스트와 같다. 종료 시
  `TaskGroup` 취소가 슬롯 N개 모두의 `_join`을 30s(`TURN_JOIN_TIMEOUT_S`, `transport.py:192`)
  안에 마친다.

**PR-4 — 정책 (선택, Q1·Q3·Q4 결정 후)**
- `runner.lane_key(job)` seam을 둔다. 기본은 scope이고, MatrixBot은 `stream_key` 기반으로
  shared-groups 기아를 없앤다. 가족방 N−1 예약 슬롯, self-job 동시 상한(예: 1)을 추가한다.
- 수용 기준: 정책별 단위 테스트. 설정하지 않으면 PR-3과 같은 동작이다.

**PR-5 — 카나리 (운영, 승인 필요)**
- 노드 하나에서 `CCC_MATRIX_MAX_PARALLEL_TURNS=2`로 켠다. 적용에는 브리지 재시작이 필요하므로
  **fresh approval**을 받는다.
- 관측은 사람이 판정하는 날짜 단위 테스트다. 운영 규칙에 따라 #2006에 **시작·종료 일시(KST
  절대시각)·확인 지표·판정 주체**를 코멘트한다. 지표: `meta.turn_timings`의
  `admitted_to_claimed_s` 분포, health `outbox_head_age_s`, agent 오류율, RSS
  (`_session_guard_last_tree_rss_mb`), 방 간 유출 0건(수동 점검).
- 수용 기준: 종료 일시에 판정 주체가 확인하고, 기본값 변경 여부를 별도 PR로 결정한다.

### 4.3 기본값

- 코드 기본값은 **N=1**이다. PR-1~PR-4 동안 운영 동작은 지금과 byte-identical이다.
- opt-in 상한은 3이다. Telegram의 대화당 3이라는 숫자와 겹치지만 의미는 다르다. 메모리가
  작은 노드 보호가 목적이다.
- 기본값 상향은 카나리 판정 뒤 별도 결정한다(§5 Q2).

### 4.4 롤백

- 운영 롤백: `CCC_MATRIX_MAX_PARALLEL_TURNS` 제거(=1) 후 재시작(승인 필요). 스키마와 claim
  SQL이 그대로라서 inbox를 이관할 필요가 없다. 재시작 순간 `running`이던 job은 기존과
  똑같이 uncertain → "재시작으로 끊겼습니다" 안내로 끝난다.
- 코드 롤백: PR 단위 revert. PR-1은 동작 변화가 없으므로 PR-3만 되돌려도 N=1로 돌아간다.

---

## 5. 열린 질문 (오너 결정 필요)

1. **lane 단위**: scope(방+보낸 사람, 가족방에서 구성원끼리 병렬)로 할지, room(같은 방은
   한 번에 한 턴)으로 할지?
2. **N 기본값과 상한**: 코드 기본 1 + opt-in 상한 3이면 되는지? 카나리 뒤 기본값을 2로 올릴지?
   노드별(VPS / Termux)로 다르게 할지?
3. **오너 DM 예약 슬롯**: N≥2일 때 가족방 job을 최대 N−1 슬롯으로 제한해 오너 DM 자리를
   항상 남길지?
4. **self-job 동시성**: continuation, external-wait resume, danso auto-resume도 N을 똑같이
   쓸지, 별도 상한(예: 동시 1)이나 낮은 우선순위를 둘지?
5. **`/restart`와 다른 lane**: 다른 lane 턴이 돌 때 `/restart`를 (a) 그대로 예약해 끊을지,
   (b) 경고 후 확인을 받을지, (c) idle까지 기다릴지?
6. **교차 lane 대기 안내**: 슬롯이 모두 차서 기다리는 메시지에 "다른 대화 작업이 끝나면
   시작합니다" 안내를 보낼지? 가족방에서도 보낼지?
7. **카나리 노드, 관측 기간, 판정 주체**: 어느 노드에서, 언제까지(KST 절대시각), 누가 판정할지?
8. **방별 send lane(선택지 D)**: outbox head-of-line 결합을 이 이슈 범위에 넣을지, 별도 이슈로
   뺄지?
9. **shared-groups/shared-all 노드**: N>1을 금지(1로 강제)할지, PR-4의 `lane_key`로
   지원할지?
10. **자원 한도 연동**: N>1일 때 `CCC_BRIDGE_MAX_RESIDENT_SESSIONS`(기본 2)를 N 이상으로 자동
    맞출지, 운영자가 직접 맞추게 할지?
