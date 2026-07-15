# 도구/명령 치트시트 (auto-injected each session)
# 노드별 특화 항목은 <PLACEHOLDER>를 채우거나, 불필요하면 줄을 삭제하세요.
# 이 파일은 setup.sh가 ~/.claude/hooks/tools-cheatsheet.md 로 seed(미존재 시에만)합니다.

운영 사실은 mutable — 단정/변경 전 노드 live-check + Wiki 원문 검증.

## MCP 도구 (등록 시 — `claude/mcp-setup.sh`)
- **웹검색**: `mcp__searxng__*` (Seoyoon 공용 SearXNG, 1차 검색). 외부 API는 폴백.
- **웹 fetch/scrape**: `mcp__firecrawl__*` (URL→markdown/추출). 동적 페이지/추출에.
- **라이브러리 문서**: `mcp__context7__*` (SDK/라이브러리 최신 문서 주입).
- 등록/점검: `claude mcp list` · 재등록(멱등) `./claude/mcp-setup.sh`.

## A2A 워커 서브에이전트 roster (`~/.claude/agents/`) — 워커 역할 노드 전용
**워커 역할 노드에만 설치됨** (`CCC_A2A_ROLE=worker` 옵트인; setup.sh가 마커로 영속). 브로커/미설정 노드에는 이 roster가 **없음** — A2A는 넥서스/브로커 플로우로만 진입하고, 로컬 서브에이전트를 정문으로 쓰지 않는다.
A2A 태스크를 claim하면 업무량에 맞춰 소환(예산 0–3, 하드캡 4; 호스트 부하 시 축소). 워커=단일 finalizer.
- `a2a-explorer` — 읽기전용 조사(코드/이슈/로그). 편집·finalize 불가.
- `a2a-researcher` — explorer 변종: 외부 웹리서치(SearXNG/Firecrawl/Context7 MCP), 읽기전용·출처 인용.
- `a2a-implementer` — **분리된 write-set 내** 구현(최대 2개 병렬, 파일 비충돌).
- `a2a-verifier` — 테스트/CI/리스크/증거 검토(소스 미편집).
- 정책: a2a-nexus `worker-subagent-orchestration-policy.md` (Finalizer/Write-Set Rule, redaction, evidence-only).

## 커스텀 스킬 (`~/.claude/skills/`)
반복 절차 = 스킬. 설명 매칭 시 자동, 또는 `/<name>`으로 직접 호출.
- `wiki-record` — Wiki PR-first 기록(write-path→편집→pr) + ID 규칙(TM/ND/LOG).
- `mcp-add` — MCP 서버 등록(키는 `~/.hermes/.env`에서, secret 비노출) + allowlist.
- `skill-suggest` — 트랜스크립트 스캔→반복 절차 후보 제안(휴먼 승인 후 저작). 후보: `~/.claude/state/skill-candidates.md`.

## Family Wiki (가장 먼저 참조)
- 검색: `wiki-agent find "<query>"`
- 검증(운영 단정 전): `wiki-agent load --lines A:B <path>`
- 빠른 맥락: `wiki-agent prefetch "<query>"`
- 영속 업데이트(PR-first): `wiki-agent write-path` → 반환된 워크트리에서 편집 → `wiki-agent pr`
  - 워크트리: `$HOME/.wiki-agent/wiki-pr-work/seoyoon-family-wiki`
  - ID 규칙: 새 섹션 ID = `max(TM-/ND-)+1`; 새 로그 = `[LOG-YYYYMMDD-<node>-<same-day-seq>]`(노드별 1부터), `[LOG-00]` 규칙 블록 뒤에 prepend; 신규 `LOG-NNNN` 금지; **raw secret 금지**(위치/취급만)

## Honcho (관계/working memory)
- baseUrl은 `~/.hermes/honcho.json` (엔드포인트/크레덴셜 값 로그 금지)
- recall: `POST {baseUrl}/v3/workspaces/<WORKSPACE>/peers/<NODE>/chat`
  body `{"query":"…","target":"<USER_PEER>","reasoning_level":"low"}`

## Telegram bridge (이 노드가 채널을 운영할 때만)
- 배포: `/opt/ccc-node/bridge` (repo `jinwon-int/ccc-node`)
- 상태: `/opt/ccc-node/bridge/start.sh --path /root --status`
- import 링크 복구/운영 절차: 노드 Wiki RUNBOOK 참조
- 재시작 헬퍼(있으면): `$HOME/.telegram_bot/restart_bridge.sh`

## GitHub (PR-first)
- 생성: `gh pr create --repo <owner/repo> --base main --head <branch> --title .. --body ..`
- 머지가능 확인: `gh pr view <n> --repo <r> --json state,mergeable,mergeStateStatus,statusCheckRollup`
- green & mergeable → `gh pr merge <n> --repo <r> --squash --delete-branch`
- main 직접 푸시 금지(브랜치 먼저); 커밋에 `Co-Authored-By` trailer

## 진행상태 체크포인트 (멀티세션/장기 작업)
- `$HOME/.claude/state/working-state.md`를 **목표 / 진행 / 다음 단계**로 갱신 유지
- 이 파일은 PreCompact에서 스냅샷되고 PostCompact에서 자동 재주입됨
