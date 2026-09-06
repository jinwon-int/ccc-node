# ccc-node improvement roadmap

Tracking: https://github.com/jinwon-int/ccc-node/issues/1528

## 목표와 기준
2026-09-06 main `ad69065` 소스·격리 재현·GitHub 실제 설정을 대조한 개선 로드맵이다. 코드 완료, 배포 대기, 운영 증거 대기, 데이터/정책 결정을 구분한다. 이 이슈는 전체 추적용이며 일부 PR 머지로 자동 종료하지 않는다.

## 이번 구현 묶음
- [ ] #1523 업데이트 강제 재기동 및 실제 health deadline — 회귀/역검증 후 PR 머지.
- [ ] #1524 A2A payload 임시파일 수명주기 — stdin/exit/signal 보존 후 PR 머지.
- [ ] #1525 Python 3.14 CI와 Android 격리 검증 절차 — 첫 소스 단계.
- [ ] #1526 required-checks 설정 차이 읽기 전용 검출 — 첫 소스 단계.

## 다음 단계
| 과제 | 이슈 | 완료 판단 |
| --- | --- | --- |
| 정지 전 환경 준비·의존성 포함 복구 | #1527, #1041 | 준비비용 측정, 단일 poller 전환, 실패주입 복구, 중단시간 비교 |
| 플랫폼 실제 검증·패키지 필수 게이트 | #1525, #1526 | Android 자동 evidence, 실제 wheel-smoke 필수 적용 및 차이 0 |
| 노드 구성 외부화 | #1451 | private config schema → scripts → bridge; 미설정 fail-closed; doctor/dispatch/deploy 동일 데이터 |
| 상태 관측 통일 | #1451, #1527 | source/installed/serving SHA, dependency fingerprint, provider readiness와 검증 시각 분리 |
| 기억/스킬 평가 재현성 | #1521, #1360, #1264 | resolved model ID, 하네스/표본/예산 고정, N≥3 산포; 오주입·중복·비용·지연·재사용 측정 |
| 모듈 책임 분리 | #896 | 현재 bot_lifecycle/bot_commands/codex_runtime 기준 갱신; 동작 변경과 순수 이동 분리; 필요한 import 경계 확장 |
| 데이터 수명주기 | #873, #1468 | 기존 planner 활용; 저장소별 소유자·기간·검증 정의; 미분류 잔존량 보고 후 정책별 처리 |
| 점진 기술부채 | #1510, #1059 | 경고/리다이렉션 잔여를 분류하고 회귀검증하면서 축소 |
| 세션/검색 발전 | #1353, #827, #832 | 정규화 어댑터와 fleet 벤치; Termux FTS는 실제 miss 입증 시 착수 |

## 운영 증거가 남은 기존 이슈
- #1472: alias 소스는 이미 수정됨. 노드 반영/과거 데이터 재귀속 완료 여부 별도 검증.
- #1460: revise handler와 route는 이미 구현됨. 설치/실행 증거 확인.
- #1470: 서명 receipt publication/retry 소스 존재. 실제 발급/승격 end-to-end 증거 확인.
- #1483: 그룹/lock/문서 수정 반영됨. 실제 dependency bot 후속 실행 확인.

이슈가 열려 있다는 이유만으로 소스 미구현으로 재분류하지 않는다. 실제 배포·보호 설정·평가 실행·보존정책 변경은 각 단계의 검증 결과를 기록한다. 전체 플릿 재시작, 민감 백업 삭제, 모델 평가 영수증의 추정값 생성은 이 로드맵 PR의 완료 증거로 사용하지 않는다.

## 검증 원칙
PR별 구버전 재현 → 회귀검증 → 독립 리뷰 → exact-head CI/비작성자 승인 → squash merge. 소스 머지와 실제 배포 상태를 별도로 보고한다.
