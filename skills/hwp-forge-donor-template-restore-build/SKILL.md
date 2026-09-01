---
name: hwp-forge-donor-template-restore-build
description: Produce a Korean HWP/HWPX document that inherits an institutional form's exact formatting on Linux without Hangul Office, by choosing between template-inheritance build (hwp-forge hwpx_template_build.py) and donor-restore surgery (reuse a known-good prior .hwpx as donor and edit Contents/section0.xml while reusing its paraPr/charPr/borderFill IDs), then verifying with an independent parser. Use when asked to generate, revise, or re-issue a .hwp/.hwpx report or official form (기관 양식, 보고서, 붙임 서식) on a server, when a previous revision must be amended in place, or when hand-written zip/XML surgery scripts keep being re-invented per document.
---

# hwp-forge donor/template restore & build

기관 양식 서식을 **재현하지 않고 상속**해서 .hwp/.hwpx를 만든다. 두 경로가 있고,
잘못 고르면 서식이 미묘하게 어긋난 문서가 나온다.

## 경로 선택 (먼저 결정)

| 상황 | 경로 | 도구 |
|---|---|---|
| 기관 양식 `.hwp`만 있고 새 문서를 만든다 | **양식 상속** | `tools/hwpx_template_build.py` |
| 같은 문서의 **이전 정본 `.hwpx`**가 있고 개정본을 낸다 | **도너 복원 수술** | 도너 zip 열어 `Contents/section0.xml` 직접 편집 |
| 양식도 이전본도 없다 | from-scratch (서식 근사치) | `tools/build_hwp.py` |

도너 복원이 양식 상속보다 우선이다 — 이전 정본은 이미 양식 상속을 거쳤으므로
DocInfo·문단모양·표 테두리가 확정돼 있고, 재상속하면 그동안의 수동 교정이 날아간다.

## 절차 — 양식 상속

1. 환경: `pip install rhwp-python pyhwp`, `node >= 18`. rhwp 휠의 freetype 심볼 문제는
   도구가 `LD_PRELOAD=/lib/x86_64-linux-gnu/libfreetype.so.6`로 자동 우회한다(수동 실행 시에만 export).
2. 원문 추출: `python3 tools/extract_docx.py 원문.docx --text`
3. 양식 서식 실측: `python3 tools/inspect_hwp_styles.py '양식.hwp' --markdown`
   → 섹션 제목/ㅇ/-/세부/※ 각 위계의 `paraPr`·`charPr` ID를 스펙에 기록.
4. 생성: `python3 tools/hwpx_template_build.py <spec.json>` — 양식 DocInfo는 손대지 않고
   제목·날짜·요약 교체 + 데모 본문 제거 + 양식 스타일 ID를 참조하는 본문 삽입.
5. 검증(아래 공통 절차)으로 마감.

## 절차 — 도너 복원 수술 (이전 정본 개정)

1. 도너를 **복사본으로** 연다. 원본 `.hwpx`는 절대 in-place 수정하지 않는다:
   `SRC=이전정본.hwpx`, `TMP=<같은이름>.vN.hwpx` → 검증 통과 후에만 최종 파일명으로 교체.
2. `zipfile`로 `Contents/section0.xml`을 문자열로 읽는다. 편집은 문자열 치환이지만
   **모든 치환은 유일성 단언을 건다**:
   ```python
   def replace_once(old, new):
       assert x.count(old) == 1, f"count {x.count(old)} != 1 for: {old[:60]}"
   ```
   count가 0이면 도너가 예상과 다른 판(스테일 도너)이고, 2 이상이면 의도하지 않은 곳까지
   바뀐다. 둘 다 즉시 실패시킨다.
3. 새 문단/표를 넣을 때는 **도너의 스타일 ID를 그대로 재사용**한다. 새 ID를 만들면
   header.xml에도 등록해야 하므로, 필요 없으면 만들지 않는다. 위계별로 도너에서
   실측한 쌍을 헬퍼 함수로 고정한다(예: 섹션제목 `paraPr 39`/`charPr 12`,
   ㅇ 항목 `paraPr 28` + runs `charPr 10/18/10`, 빈 스페이서 `paraPr 38`/`charPr 17`).
   **ID 숫자는 문서마다 다르다 — 반드시 해당 도너에서 다시 실측하고 하드코딩 재사용 금지.**
4. 문단 단위 삭제·교체는 `<hp:p>` 중첩을 세는 span 계산으로 하고, 균형이 안 맞으면
   `RuntimeError("unbalanced")`로 중단한다. 정규식만으로 태그를 자르지 않는다.
5. 텍스트는 반드시 XML 이스케이프(`&`,`<`,`>`)한다.
6. zip을 다시 쓸 때 **도너의 나머지 엔트리를 전부 그대로 복사**한다(header.xml, DocInfo,
   BinData, mimetype 등). 빠뜨리면 한글에서 열리지 않는다.
7. 플레이스홀더 전량 소진 확인: 개정 목적이 `[OO일]` 같은 미확정 토큰 확정이면
   마지막에 `x.count("[OO")==0`을 단언한다.

## 검증 (양쪽 공통 — 생략 금지)

생성 측 자기신고는 증거가 아니다. 독립 파서로 다시 연다:

```bash
python3 tools/verify_hwp.py 출력.hwpx \
  --require '핵심문구1' --require '핵심문구2' \
  --expect-table 6x4 --expect-merge 1:0:2 \
  --render-dir /tmp/render
```

- ✅ 파싱·왕복 성공
- ✅ 개정으로 **추가된** 문구가 존재
- ✅ 개정으로 **제거된** 문구가 부재(요구 문구만 확인하면 반쪽 검증)
- ✅ 표 행×열·병합 기대치 일치
- ✅ `--render-dir` PNG 육안 검수 — 서식 어긋남은 텍스트 대조로 안 잡힌다
- ✅ 검증 통과 후에만 임시 파일 → 최종 파일명 교체

## 안전

- 도너/양식 원본은 읽기 전용. 실패 시 되돌릴 원본이 없으면 전부 잃는다.
- 스테일 도너 주의: 개정 요청이 온 파일과 손에 있는 도너의 판이 같은지
  (날짜 접미사·문구 sample) 먼저 확인. `replace_once`의 count==0 실패가 이 신호다.
- 문서 내용에 개인정보·계좌·내부 식별자가 있으면 로그·커밋에 본문을 남기지 않는다.
- 반복 문서라면 일회성 스크립트를 남기지 말고 `hwp-forge` 도구 쪽으로 흡수시킨다
  (이 스킬이 존재하는 이유가 매 문서마다 `make_v2.py`/`make_v3.py`가 새로 태어난 것).

## 참고

- 도구·스펙: hwp-forge 체크아웃(노드마다 경로 다름 — 예: `~/hwp-forge`). README와
  `specs/<form>/ANALYSIS.md`에 양식 실측 규칙이 있다. 없으면 이 스킬의 도구 단계는
  건너뛰고 도너 복원 수술 절차만 적용한다.
- 일반 zip/XML 수술 절차는 `archive-document-template-reverse-engineering`,
  생성물 내용 검증 일반론은 `generated-archive-document-content-verification` 참조.
  이 스킬은 그 위에 **한글 양식 상속 경로 선택**과 **도너 복원**을 얹은 것이다.
