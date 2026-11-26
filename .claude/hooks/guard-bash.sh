#!/bin/sh
# PreToolUse(Bash) 훅: stdin JSON 의 tool_input.command 를 검사한다. exit 2 = 차단, exit 1 = 검사 못함(경고 후 통과).
if ! command -v jq >/dev/null 2>&1; then
  echo 'guard-bash: jq 가 없어 명령을 검사하지 못했습니다(통과).' >&2
  exit 1
fi
if ! cmd=$(jq -er '.tool_input.command | select(type == "string")' 2>/dev/null); then
  echo 'guard-bash: 입력을 읽지 못해 명령을 검사하지 못했습니다(통과).' >&2
  exit 1
fi
printf '%s\n' "$cmd" | grep -qE 'rm\s+-rf|git\s+push\s+--force|git\s+reset\s+--hard|DROP\s+TABLE'
case $? in
  0)
    echo 'BLOCKED: 위험한 명령어가 감지되었습니다.' >&2
    exit 2
    ;;
  1)
    exit 0
    ;;
  *)
    echo 'guard-bash: grep 실행에 실패해 명령을 검사하지 못했습니다(통과).' >&2
    exit 1
    ;;
esac
