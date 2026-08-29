---
name: nclex-a2a-content-pipeline
description: Drive a jinwon-int/nclex content PR through the full narrow-gate A2A pipeline — terminology lane dispatch, result-preservation rerun on verdict fail, coverage review projection, CI recovery (stale base.sha), content 2-lane dispatch with keyring-trusted worker routing, signed receipt posting, and a2a/receipts verification. Use when authoring or shepherding an NCLEX 출제 PR (new_case/rewrite/remediation), when a terminology or content lane must be dispatched, when a lane returns review_verdict_failed, or when receipts must be projected to the PR. 확립 2026-08-13 RRP-003(#249)·PA-006(#251) 2회 완주.
---

# nclex A2A content pipeline (좁은 문 규율)

콘텐츠 PR은 **A2A exact-head PASS 전 머지 금지**. 전체 흐름:
출제 PR → ① terminology 레인 → 검토 기록 커밋 → ② CI green(자동 요청서) →
③ content 2-lane → ④ receipt 게시 → `a2a/receipts` green → 비작성자 승인·머지.

머지·A2A 디스패치·크로스계정 승인은 **각각 사용자 승인**을 받는다.

## 0. 전제

- 로컬 클론(예: `/root/work/nclex`), 디스패치 작업장 `/root/work/nclex-dispatch/`
  (기존 manifest들이 템플릿 — `manifest-pr249-*.json`, `manifest-pr251-*.json`).
- 디스패치 CLI: `a2a-nexus` 체크아웃의 `scripts/a2a-dispatch-round.mjs`
  (`--manifest F --dry-run` → `--verify`). **edge secret은 브로커가 있는 노드의
  로컬 env에서만 읽는다** — T1(Seoseo)=이 노드 `/etc/default/a2a-hermes-worker`의
  `BROKER_EDGE_SECRET`(터널 `http://127.0.0.1:18787`), T2(Gwakga)=gwakga의
  `/root/a2a-nexus/packages/broker/.env`의 `EDGE_SECRET`(**gwakga에서 SSH로 실행**,
  값 이동·출력 금지).
- 신뢰 워커(keyring `refs/a2a-public-keyring.json` 등록): sogyo·nosuk(T1),
  dungae·daegyo·gongmyoung(T2), yukson(저자 제척). **soonwook·jingun 미등록** —
  이들의 PASS는 `a2a/receipts`에서 무효. jingun은 Claude 인증 없음(핸들러 즉사).
- PR 본문에 기계 판독 8필드 필수(값=머지된 claim 레코드와 정확 일치):
  `- TASK_ID/TASK_KIND/AUTHOR_NODE/TARGET_IDS/SOURCE_PACKET/REFS_MANIFEST_SHA256/BASE_SHA/RISK_CLASS`.

## 1. 해시 규약 (자가검증 완료)

- `targetContentHash` = sha256(RFC8785-JCS 정규화한 케이스 객체).
  카탈로그 로드 순서는 **items.js 먼저, cases-* 정렬** (`tools/commercial_readiness.js dataFiles`).
- `diffHash` = sha256(`git diff <baseSha> <headSha>` 바이트).
- `intentHash`(terminology) = sha256(intentContract에서 intentHash 제외,
  json sort_keys+compact, ensure_ascii=False).
- content 레인의 intentHash는 **CI 자동 요청서 값을 그대로 사용**
  (PR 본문 8필드 블록의 sha256 — gate `a2a-dispatch` job이 게시).
- facts 생성기 스크립트 원형: 세션 기록 `/root/work/nclex-dispatch/` 참고
  (facts*.json을 만든 node 스크립트 — fingerprint·diffHash·changedRecords 75쌍 추출).

## 2. terminology 레인 (ko_coverage 신규쌍 해소의 유일 경로)

1. 신규 용어 후보(coverage 레코드 `candidates`)를 terms.json에 KMA 근거로 먼저 등록
   → `node tools/ko_coverage.js --build` → 후보 0 확인 → 커밋.
2. manifest: 스키마 `nclex.terminology-bilingual-review.v1`, lane
   `terminology_bilingual`, reviewer는 저자 아닌 신뢰 워커(선례 sogyo),
   `sourceBundle.files[0].content`에 계약+changedRecords(75쌍)+relevantRules+
   machineGate 내장. `terminalBrief.notificationOwnership` 필수.
3. dry-run → 디스패치 → 브로커 GET `/tasks/<url-enc-id>`로 감시.
4. **`review_verdict_failed`이면 먼저 GET `/tasks/:id` 와 `/tasks/:id/diagnostics`를 본다.**
   `sameSourceRedispatch.action=skip` 이거나 `negativeVerdictEvidence`에
   소견(findings/note)이 있으면 **같은 소스 diagnostic/결과보존 재생을 하지 않는다**
   (#1815 item 5, #1878 + redispatch classifier). merge gate는 그대로 fail-closed.
   증거가 비었거나 generic ack일 때만 `review: {required:false}` 재생으로 회수.
   회수 후 수정(수정 세대 1회) → 새 head로 정식 레인 재디스패치.
5. PASS 후 coverage.json에 review 객체 투영(각 레코드 `stage=reviewed` +
   `review{reviewer:"a2a:<node>", reviewed_at, note(태스크 id·resultHash 인용),
   evidence:["kuksiwon-item-writing-2018:…", "kma-terminology-6x:search=<개념>"…]}`)
   → `node tools/ko_coverage.js --check --base origin/main` PASS 확인 → 커밋·푸시.

## 3. CI 함정 2개

- **base.sha 고착**: pull_request 이벤트의 base.sha가 PR 생성 시점에 고정되어
  content_process 경로 검증이 오탐하면 `gh api -X PUT
  repos/<r>/pulls/<n>/update-branch`로 base 갱신(merge 커밋 추가, 콘텐츠 불변).
- `gh run rerun`은 **옛 이벤트를 재생**한다(수정된 PR 본문·base 미반영) —
  본문 수정 후에는 빈 커밋 푸시 또는 update-branch로 새 이벤트를 만든다.

## 4. content 2-lane

1. CI green이면 gate가 **자동 요청서**(A2A-EVALUATION-REQUEST 코멘트) 게시 —
   head FROZEN 선언·diffHash·intentHash·레인 목록(standard 2 / high 3). 이 값이 정본.
2. packet: 스키마 `nclex.content-pr-review.v1` — contract(요청서 값+reviewerNodeId)
   + intentContract(요청서 intentHash) + currentTargets(전체 케이스 객체) +
   structuredEvidence + evidenceExcerpts(원문 인용) + machineGate +
   terminologyReview(서명 태스크 참조) + exactHeadTextIntegrity(파일 sha256).
3. 라우팅: reviewer가 T2면 manifest를 gwakga로 scp → gwakga에서 CLI 실행,
   T1이면 로컬 실행. 레인별 reviewer는 서로 다르고 저자 제척·keyring 등록 필수.
4. 핸들러 크래시(`handler_exit_nonzero`)는 판정이 아니다 — 동일 packet 재시도 1회,
   반복 시 다른 신뢰 워커로 재라우팅. 워커가 클레임을 안 하면 워커 노드에서
   `a2a-hermes-worker` 저널 확인(취소 태스크 zombie heartbeat면 서비스 재시작 —
   fleet-service 자율 범위).

## 5. receipt 게시와 마감

1. 각 PASS 태스크의 전체 JSON에서 receipt 구성:
   `{task_id, head_sha, signed_head_prefix(=head 전체), lane, reviewer_node,
   author_node, result(<브로커 result 원본, provenance 포함>)}`.
2. PR 코멘트에 ```json 펜스로 게시 → `a2a-receipts` 워크플로가 keyring 검증 후
   commit status `a2a/receipts`를 exact-head에 투영. 확인:
   `gh api repos/<r>/commits/<head>/status`.
3. `success: "signed receipts complete: N lane(s), N reviewer(s), exact-head"`
   확인 후 **머지는 별도 사용자 승인** → gh-pr-flow(Direction B: jinon86
   exact-head squash via seoseo).

## 안전 규칙

- edge secret·토큰 값 출력/복사/이동 금지. 원격 브로커 작업은 그 노드에서 실행.
- head 동결 후 sync push 금지(모든 PASS 무효). 리베이스는 디스패치 전에만.
- 이 파이프라인은 GitHub 머지 승인·면허 RN 검토를 대체하지 않으며
  `source.verified=false`·paid-pool 자격 불변.
