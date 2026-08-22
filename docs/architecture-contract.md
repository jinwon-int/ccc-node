# 실행 가능한 아키텍처 계약

`architecture/architecture-contract-v1.json`은 ccc-node의 중요한 Python import
경계를 선언하는 versioned source of truth다. `scripts/ccc_architecture_contract.py`가
stdlib AST로 실제 import를 읽고, 선언된 금지 방향과 다른 edge를 rule 이름·source
path·import target과 함께 실패시킨다. 소스 본문이나 런타임 payload는 출력하지 않는다.
검사 대상은 선언된 Python root 아래의 Git-tracked regular `.py` 파일뿐이다. 따라서
설치 트리에 남은 untracked virtualenv·cache는 결과를 바꾸지 않으며, tracked symlink나
root 밖으로 해석되는 경로는 안전하게 fail closed한다. 검사는 Git worktree에서 실행한다.

첫 계약은 #896의 단계적 분해가 provider adapter 안으로 Telegram UI 구현을 다시
끌어들이지 못하게 한다. provider adapter는 typed runtime event를 노출하고,
Telegram rendering과 handler 구현은 presentation layer가 소유한다.

```bash
python3 scripts/ccc_architecture_contract.py --repo-root .
```

## 변경 규칙

- 새 모듈은 자동으로 금지되지 않는다. 위험 경계에 포함시킬 때 contract diff로
  layer를 명시한다. 이 계약은 디렉터리 구조 전체를 얼리는 module freeze가 아니다.
- 기존 module이 둘 이상의 layer pattern에 걸리거나 rule이 존재하지 않는 layer를
  참조하면 contract 자체가 잘못된 것으로 fail closed한다.
- 금지 edge가 필요해졌다면 검사 예외를 추가하기보다 책임 방향이 맞는지 먼저
  검토한다. 의도한 아키텍처 변경이면 contract와 이유를 같은 PR에서 갱신한다.
- 이 파일은 import 경계의 설명서다. runtime authorization이나 permission gate를
  대체하지 않는다.

외부 부수효과 inventory, recovery drill, 생성된 운영 표는
`side-effect-contract.md`와 별도의 versioned contract가 같은 source-of-truth
원칙으로 관리한다.
