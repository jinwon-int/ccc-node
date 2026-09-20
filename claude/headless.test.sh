#!/usr/bin/env bash
# claude/headless.test.sh — fake-claude tests for ccc-headless failure diagnostics.
#
# 회귀 방지 대상: 실패 경로에서 stdout 을 버려 진단 근거가 사라지는 것.
# `claude --output-format json` 은 오류 본문을 stdout 으로 내므로, stderr 만
# 보존하면 "claude exited 1" 한 줄만 남는다 (agent-cron prompt 태스크 4건 실측).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$HERE/headless.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
ok() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; fi; }

# stdout 으로만 오류를 내고 종료코드 1 로 죽는 가짜 claude — 실제 실패 모양이다.
FAKE_FAIL="$TMP/claude-fail"
cat > "$FAKE_FAIL" <<'SH'
#!/usr/bin/env bash
printf '%s\n' '{"type":"result","is_error":true,"error":"OAuth token expired"}'
exit 1
SH
chmod +x "$FAKE_FAIL"

# shellcheck disable=SC2034 # consumed through eval in ok()
err="$(CCC_CLAUDE_BIN="$FAKE_FAIL" bash "$RUNNER" 'probe' 2>&1 >/dev/null)"
# shellcheck disable=SC2034 # consumed through eval in ok()
rc=$?
ok "failed run mirrors the claude exit code" '[ "$rc" = 1 ]'
ok "failed run names the binary and exit code" 'printf "%s" "$err" | grep -q "exited 1"'
ok "failed run surfaces the stdout error body" 'printf "%s" "$err" | grep -q "OAuth token expired"'
ok "failed run labels the stdout capture" 'printf "%s" "$err" | grep -q "ccc-headless: stdout (first"'
ok "failed run keeps the error body off stdout" '[ -z "$(CCC_CLAUDE_BIN="$FAKE_FAIL" bash "$RUNNER" "probe" 2>/dev/null)" ]'

# 캡 적용: 큰 stdout 은 잘려야 한다(스풀/런히스토리 범람 방지).
#
# 길이는 **표식 문자를 세지 말고** 라벨 줄 뒤의 본문을 잘라서 잰다. 전체 stderr
# 에서 글자를 세면 러너 자신의 메시지와 경로가 같이 잡혀 틀린다 — 'x' 는
# "exited" 에 들어 있고(이 테스트가 처음 그렇게 틀렸다), 'Z' 는 mktemp 경로에
# 섞여 들어온다(실측 200회 중 28회). 후자는 로컬에서는 통과하고 CI 에서만
# 깨지는 flaky 로 나타났다.
FAKE_BIG="$TMP/claude-big"
cat > "$FAKE_BIG" <<'SH'
#!/usr/bin/env bash
head -c 5000 /dev/zero | tr '\0' 'Z'
exit 1
SH
chmod +x "$FAKE_BIG"
# shellcheck disable=SC2034 # consumed through eval in ok()
errbig="$(CCC_HEADLESS_FAIL_STDOUT_BYTES=64 CCC_CLAUDE_BIN="$FAKE_BIG" bash "$RUNNER" 'probe' 2>&1 >/dev/null)"
# 라벨 줄 다음부터가 캡처 본문이다.
# shellcheck disable=SC2034 # consumed through eval in ok()
bigbody="$(printf '%s' "$errbig" | sed -n '/^ccc-headless: stdout (first/,$p' | tail -n +2)"
ok "stdout capture honours the byte cap" '[ "${#bigbody}" = 64 ]'
ok "stdout capture body is exactly the payload head" '[ "$bigbody" = "$(head -c 64 /dev/zero | tr "\0" "Z")" ]'
ok "stdout capture reports the full size" 'printf "%s" "$errbig" | grep -q "of 5000B"'

# 0 은 캡처를 끈다 — 민감 출력이 우려되는 호출자용 탈출구.
# shellcheck disable=SC2034 # consumed through eval in ok()
erroff="$(CCC_HEADLESS_FAIL_STDOUT_BYTES=0 CCC_CLAUDE_BIN="$FAKE_FAIL" bash "$RUNNER" 'probe' 2>&1 >/dev/null)"
ok "zero cap disables the stdout capture" '! printf "%s" "$erroff" | grep -q "OAuth token expired"'
ok "zero cap still reports the failure" 'printf "%s" "$erroff" | grep -q "exited 1"'

# 비정상 캡 값은 기본값으로 안전하게 되돌아간다.
# shellcheck disable=SC2034 # consumed through eval in ok()
errbad="$(CCC_HEADLESS_FAIL_STDOUT_BYTES=abc CCC_CLAUDE_BIN="$FAKE_FAIL" bash "$RUNNER" 'probe' 2>&1 >/dev/null)"
ok "invalid cap falls back to the default" 'printf "%s" "$errbad" | grep -q "OAuth token expired"'

# stderr 보존은 기존 계약이다 — 함께 지킨다.
FAKE_ERR="$TMP/claude-stderr"
cat > "$FAKE_ERR" <<'SH'
#!/usr/bin/env bash
echo "boom on stderr" >&2
exit 3
SH
chmod +x "$FAKE_ERR"
# shellcheck disable=SC2034 # consumed through eval in ok()
err3="$(CCC_CLAUDE_BIN="$FAKE_ERR" bash "$RUNNER" 'probe' 2>&1 >/dev/null)"
# shellcheck disable=SC2034 # consumed through eval in ok()
rc3=$?
ok "stderr is still preserved" 'printf "%s" "$err3" | grep -q "boom on stderr"'
ok "empty stdout is reported as such" 'printf "%s" "$err3" | grep -q "stdout was empty"'
ok "non-one exit codes are mirrored" '[ "$rc3" = 3 ]'

# 성공 경로는 바뀌지 않는다.
FAKE_OK="$TMP/claude-ok"
cat > "$FAKE_OK" <<'SH'
#!/usr/bin/env bash
printf '%s\n' '{"type":"result","session_id":"s1","total_cost_usd":0.01,"result":"done"}'
SH
chmod +x "$FAKE_OK"
# shellcheck disable=SC2034 # consumed through eval in ok()
out="$(CCC_CLAUDE_BIN="$FAKE_OK" bash "$RUNNER" 'probe' 2>/dev/null)"
# shellcheck disable=SC2034 # consumed through eval in ok()
rcok=$?
ok "successful run still prints the result text" '[ "$rcok" = 0 ] && [ "$out" = "done" ]'

# validate-harness.sh 는 이 형식의 요약 줄을 스위트 성공의 증거로 읽는다.
# 소문자로 내면 테스트가 전부 통과해도 "no 'PASS=<n> FAIL=<n>' summary line"
# 으로 실패한다.
echo "PASS=$pass FAIL=$fail"
[ "$fail" = 0 ]
