#!/bin/bash
# ExecStartPre용 kill 스크립트 — 패턴을 외부 파일로 분리해 self-kill 방지
# pkill -f <pattern>을 ExecStartPre bash -c "..." inline으로 쓰면
# cmdline에 패턴 텍스트가 포함되어 자기 자신을 SIGTERM하는 버그 발생
PATTERN='python.*-m.*telegram_bot'
pkill -TERM -f "$PATTERN" 2>/dev/null || true
sleep 2
pkill -KILL -f "$PATTERN" 2>/dev/null || true
exit 0
