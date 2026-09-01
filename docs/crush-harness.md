# crush 하네스 (kimi k3 · GLM-5.2 전용)

> 도입: issue #923 · 파일럿: soonwook (2026-08-04)

ccc-node의 세 번째 하네스. [crush](https://github.com/charmbracelet/crush)(Charm, FSL-1.1-MIT)는
**Kimi K3 노드와 GLM-5.2 노드 전용** 러너다. 기본 하네스(claude)와 codex는 그대로 두고,
비-Anthropic 모델을 쓰는 노드에만 얹는다.

## 왜 crush인가

- 단일 Go 바이너리 (기동 user time ~0.36s; Node 기반 CLI의 1/6)
- Z.ai·Moonshot 내장/커스텀 프로바이더 — K3와 GLM-5.2를 한 하네스로
- `~/.claude/skills` 네이티브 스캔 — 레포 스킬 29개(`skills/` 통합 루트, #1393) 무이식 재사용
- `crush run` 비대화형 모드 + bash 스타일 `crushrc`

## 설치 (노드 옵트인)

```bash
npm install -g @charmland/crush@0.88.0
crush --version
```

키 파일 배치 (fleet 관례 — raw secret은 이 파일들에만):

| 용도 | 기본 경로 | 재지정 env |
|---|---|---|
| K3 (kimi.com/coding) | `~/.hermes/kimi-api-key` | `CCC_KIMI_KEY_FILE` |
| GLM (z.ai coding plan) | `~/.hermes/zai-api-key` | `CCC_ZAI_KEY_FILE` |

`crush/crushrc.readonly`가 로드 시점 `$()` 확장으로 이 파일들을 읽는다. 키 파일이
없으면 crush 설정 로드가 실패하고 실행은 fail-closed로 멈춘다.

## 헤드리스 실행

```bash
CCC_CRUSH_MODEL=kimi/k3    crush/headless.sh "작업 프롬프트"
CCC_CRUSH_MODEL=zai/glm-5.2 crush/headless.sh "작업 프롬프트"
```

- `CCC_CRUSH_WORKDIR` — 작업 디렉터리 (기본 `$PWD`)
- `CCC_HEADLESS_TIMEOUT` — 벽시계 상한 초 (기본 1500, 0=해제)
- `CCC_CRUSH_CONFIG` — 권한 설정 재지정. **기본은 레포의 `crushrc.readonly`**로,
  bash/edit/write/download가 숨겨진 읽기 전용이다. 쓰기가 필요한 작업은 검토된
  설정 파일을 만들고 이 변수로 가리킨다. yolo 경로는 의도적으로 없다.

agent-cron 연동은 기존 설치기의 `--headless /opt/ccc-node/crush/headless.sh`처럼
경로 주입으로 된다(러너는 레포 내 위치에서 직접 참조된다).

## 스킬·컨텍스트 호환

- 스킬: `~/.claude/skills` 자동 인식 (매핑은 `crush/compatibility.json`)
- 컨텍스트: `~/.config/AGENTS.md` + 프로젝트 `AGENTS.md`. 우리 `CLAUDE.md`는
  심링크/생성본으로 이어야 한다 (자동 아님)
- 가드 훅(settings.json PreToolUse 계열)은 **이식되지 않는다** — crush hooks는
  preliminary 단계라 별도 검토 과제. 그 전까지 crush 레인은 읽기 전용 기본값과
  프롬프트 수준 제약으로 운용한다

## 메트릭 옵트아웃

crush는 익명 사용 메트릭(PostHog)이 기본 활성이다. `headless.sh`가 실행마다
`CRUSH_DISABLE_METRICS=1`을 고정 주입한다. 대화형 사용 시에도 셸 환경에
`export CRUSH_DISABLE_METRICS=1`을 두는 것을 권장한다.

## 모델 메모

- 엔드포인트의 모델 id는 `k3`다. claude 하네스의 `k3[1m]` 표기는 그 하네스의
  별칭이며 crush/직접 호출에서는 `k3`를 쓴다
- `glm-5.2` 컨텍스트 1,000,000 (z.ai 문서 기준)
- GLM Coding Plan 키는 **공식 지원 도구에서만** 쓰는 약관이 있다 — crush는 지원
  목록에 포함된다. 지원外 도구(예: codex)로는 종량제 키만 사용
- 소형 모델 슬롯은 `model small <provider>/<id>`로 명시 고정한다. 미지정 시
  crush가 첫 활성 프로바이더의 기본 소형 모델을 고르므로, 상속된 외부 키가
  보이는 환경에서는 타이틀 생성 같은 보조 호출이 의도 밖의 백엔드로 나간다
  (canary4에서 anthropic 기본값으로 라우팅돼 401 노이즈가 난 사례)
- `option provider-auto-update false`는 정상 문법이다. 다만 crush의
  `"Option set in shell config"` 로그는 사용자가 입력한 값이 아니라 **저장된
  값**(`disable_provider_auto_update`, 반전 적용 후)을 찍으므로
  `"value":true`가 자동 갱신 ON을 뜻하는 것이 **아니다**(OFF가 맞다).
  로그만 보고 오독하지 않도록 주의
- 대화형 `question` 도구는 비대화형 경로(헤드리스·텔레그램 브릿지)에서 반드시
  `permissions deny question`으로 숨긴다. 응답 경로가 없어 모델이 이 도구를
  부르면 턴이 실패한다 — 방통 브릿지에서 k3가 2개 질문 배치를 보낸 뒤
  `"Processing failed: tool_use"`로 종료된 사례(2026-08-04)

## 한계

- 텔레그램 브릿지 제1급 프로바이더(AgentRuntime 어댑터)는 아니다 — 별도 과제
- crush는 활발히 개발 중이라 플래그/설정 변동 가능. 버전 핀으로 대응한다
