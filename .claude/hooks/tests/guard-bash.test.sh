#!/bin/sh
# guard-bash 훅 시험. settings.json 의 PreToolUse(Bash) command 를 그대로 꺼내 실행한다.
# 위험 문자열은 JSON 데이터로만 쓰며, 이 파일은 그 명령을 실행하지 않는다.

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
HOOK_CMD=$(jq -r '.hooks.PreToolUse[] | select(.matcher == "Bash") | .hooks[0].command' "$ROOT/.claude/settings.json") || exit 1
SH=$(command -v sh)
TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
trap 'exit 1' INT TERM

PASS=0
FAIL=0

# check <이름> <기대 코드> <설명> <실제 코드> <stderr 파일>
check() {
  if [ "$4" = "$2" ]; then
    PASS=$((PASS + 1))
    echo "PASS $1 $3"
  else
    FAIL=$((FAIL + 1))
    echo "FAIL $1 기대 $2 실제 $4 ($3)"
  fi
}

# 훅 실행 결과를 ACTUAL(종료 코드)·$TMP/err(stderr)에 둔다. 인자: 프로젝트 디렉터리, 입력 파일
run_hook() {
  (unset CLAUDE_TOOL_INPUT; CLAUDE_PROJECT_DIR="$1" "$SH" -c "$HOOK_CMD") <"$2" >/dev/null 2>"$TMP/err"
  ACTUAL=$?
}

# 차단 사례: 종료 코드 2 가 훅의 차단인지 stderr 의 BLOCKED 로 확인한다(스크립트를 못 열어 난 2 를 거른다).
check_blocked() {
  if ! grep -q 'BLOCKED' "$TMP/err"; then
    FAIL=$((FAIL + 1)); echo "FAIL $1-stderr 「BLOCKED」 없음"
  else
    PASS=$((PASS + 1)); echo "PASS $1-stderr BLOCKED 출력"
  fi
}

cmd_json() {
  jq -n --arg c "$1" '{tool_name: "Bash", tool_input: {command: $c}}' >"$TMP/in"
}

# a~e: 명령별 판정
case_cmd() {
  cmd_json "$2"
  run_hook "$ROOT" "$TMP/in"
  check "$1" "$3" "$4" "$ACTUAL"
  if [ "$3" = 2 ]; then
    check_blocked "$1"
  fi
}

case_cmd a 'rm -rf /tmp/x' 2 'rm -rf 차단'
case_cmd b 'git push --force origin main' 2 'force push 차단'
case_cmd c 'git reset --hard HEAD~1' 2 'reset --hard 차단'
case_cmd d 'psql -c "DROP TABLE users"' 2 'DROP TABLE 차단'
case_cmd e 'ls -la' 0 '안전한 명령 통과'

# f: 환경변수 CLAUDE_TOOL_INPUT 은 읽지 않는다
cmd_json 'ls'
env CLAUDE_TOOL_INPUT='rm -rf /' CLAUDE_PROJECT_DIR="$ROOT" "$SH" -c "$HOOK_CMD" <"$TMP/in" >/dev/null 2>"$TMP/err"
check f 0 '환경변수 무시, stdin 만 검사' $?

# g~j: 읽지 못한 입력은 경고 후 통과(exit 1)
case_bad() {
  run_hook "$ROOT" "$TMP/in"
  check "$1" 1 "$2" "$ACTUAL"
  if ! grep -q '검사하지 못했습니다' "$TMP/err"; then
    FAIL=$((FAIL + 1)); echo "FAIL $1-stderr 「검사하지 못했습니다」 없음"
  else
    PASS=$((PASS + 1)); echo "PASS $1-stderr 경고 문구 출력"
  fi
}
printf '%s' '{not json' >"$TMP/in"; case_bad g '깨진 JSON'
: >"$TMP/in"; case_bad h '빈 stdin'
printf '%s' '{"tool_name":"Bash","tool_input":{}}' >"$TMP/in"; case_bad i 'command 없음'
printf '%s' '{"tool_name":"Bash","tool_input":{"command":123}}' >"$TMP/in"; case_bad j 'command 가 숫자'

# k: jq 없음. settings.json 읽기는 위에서 끝났고, 훅 실행 때만 PATH 를 바꾼다.
#    훅 커맨드가 sh 를 PATH 에서 찾으므로 grep 과 sh 만 링크한다.
NOJQ="$TMP/nojq"
if ! { mkdir "$NOJQ" && ln -s "$(command -v grep)" "$NOJQ/grep" && ln -s "$SH" "$NOJQ/sh"; }; then
  FAIL=$((FAIL + 1)); echo "FAIL k 시험 준비 실패"
elif env PATH="$NOJQ" "$SH" -c 'command -v jq' >/dev/null 2>&1; then
  FAIL=$((FAIL + 1)); echo "FAIL k 시험 준비 실패: 임시 PATH 에서 jq 가 보임"
else
  cmd_json 'ls'
  (unset CLAUDE_TOOL_INPUT; PATH="$NOJQ" CLAUDE_PROJECT_DIR="$ROOT" "$SH" -c "$HOOK_CMD") <"$TMP/in" >/dev/null 2>"$TMP/err"
  check k 1 'jq 없음' $?
  if ! grep -q 'jq' "$TMP/err"; then
    FAIL=$((FAIL + 1)); echo "FAIL k-stderr 「jq」 없음"
  else
    PASS=$((PASS + 1)); echo "PASS k-stderr jq 언급"
  fi
fi

# m: grep 없음. 훅 커맨드가 sh 를 PATH 에서 찾으므로 jq 와 sh 만 링크한다.
NOGREP="$TMP/nogrep"
if ! { mkdir "$NOGREP" && ln -s "$(command -v jq)" "$NOGREP/jq" && ln -s "$SH" "$NOGREP/sh"; }; then
  FAIL=$((FAIL + 1)); echo "FAIL m 시험 준비 실패"
elif env PATH="$NOGREP" "$SH" -c 'command -v grep' >/dev/null 2>&1; then
  FAIL=$((FAIL + 1)); echo "FAIL m 시험 준비 실패: 임시 PATH 에서 grep 이 보임"
else
  cmd_json 'ls'
  (unset CLAUDE_TOOL_INPUT; PATH="$NOGREP" CLAUDE_PROJECT_DIR="$ROOT" "$SH" -c "$HOOK_CMD") <"$TMP/in" >/dev/null 2>"$TMP/err"
  check m 1 'grep 없음' $?
  if ! grep -q 'grep' "$TMP/err"; then
    FAIL=$((FAIL + 1)); echo "FAIL m-stderr 「grep」 없음"
  else
    PASS=$((PASS + 1)); echo "PASS m-stderr grep 언급"
  fi
fi

# l: 경로에 공백
SPACED="$TMP/a b"
if ! { mkdir -p "$SPACED/.claude/hooks" && cp "$ROOT/.claude/hooks/guard-bash.sh" "$SPACED/.claude/hooks/guard-bash.sh"; }; then
  FAIL=$((FAIL + 1)); echo "FAIL l 시험 준비 실패"
else
  cmd_json 'rm -rf /tmp/x'
  run_hook "$SPACED" "$TMP/in"
  check l 2 '경로에 공백이 있어도 차단' "$ACTUAL"
  check_blocked l
fi

echo "통과 $PASS / 실패 $FAIL"
[ "$FAIL" -eq 0 ]
