#!/bin/sh
# Stop 훅: 프로젝트 루트에서 컴파일을 돌린다. 실패하면 stdout 에 {"decision":"block","reason":…} 을 내고 exit 0(차단).
# exit 1 = 검사 못함(경고 후 통과). stdout 에는 그 JSON 말고 아무것도 쓰지 않는다.
if ! command -v jq >/dev/null 2>&1; then
  echo 'compile-gate: jq 가 없어 컴파일을 검사하지 못했습니다(통과).' >&2
  exit 1
fi
# stop_hook_active 가 true 면 이미 Stop 훅의 차단으로 이어진 응답이다. 다시 막으면 재개가 반복되므로 통과시킨다.
# jq 가 값을 낸 뒤 실패하는 입력(잘린 JSON 등)도 읽기 실패로 본다.
if ! active=$(jq -r '.stop_hook_active | select(type == "boolean")' 2>/dev/null); then
  active=
fi
case $active in
  true)
    exit 0
    ;;
  false)
    ;;
  *)
    echo 'compile-gate: 입력을 읽지 못해 컴파일을 검사하지 못했습니다(통과).' >&2
    exit 1
    ;;
esac
if ! cd -- "$CLAUDE_PROJECT_DIR"; then
  echo 'compile-gate: 프로젝트 루트로 이동하지 못해 컴파일을 검사하지 못했습니다(통과).' >&2
  exit 1
fi
if [ -f ./gradlew ]; then
  out=$(sh ./gradlew -q compileJava compileTestJava 2>&1)
elif [ -f ./mvnw ]; then
  out=$(sh ./mvnw -q test-compile 2>&1)
else
  exit 0
fi
rc=$?
if [ "$rc" -eq 0 ]; then
  exit 0
fi
# 컴파일 출력은 exit 2 + stderr 로 보내지 않는다. 그 경로는 stderr 에 「파일 없음」 문구가 있으면 차단되지 않는다.
# 마지막 명령이므로 jq 가 실패하면 그 종료 코드와 오류문이 그대로 나간다.
printf '%s\n' "$out" | tail -n 200 |
  jq -Rsc --arg head "compile-gate: 컴파일 실패(종료 코드 $rc). 출력의 끝 200줄:" '{decision: "block", reason: ($head + "\n" + rtrimstr("\n"))}'
