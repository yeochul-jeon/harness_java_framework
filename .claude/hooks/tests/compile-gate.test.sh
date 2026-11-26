#!/bin/sh
# compile-gate 훅 시험. settings.json 의 Stop command 를 그대로 꺼내 실행한다.
# 실제 gradle/maven 은 돌리지 않는다. 임시 디렉터리에 정해진 종료 코드로 끝나는 가짜 wrapper 를 만든다.
#
# 사용: sh compile-gate.test.sh [프로젝트 루트] [settings.json 경로]
#   프로젝트 루트 기본값은 이 파일 기준 ../../.. , settings.json 기본값은 <루트>/.claude/settings.json
#   HOOK_SH=<셸 경로> 를 주면 훅과 가짜 wrapper 를 그 셸로 돌린다(기본값: PATH 의 sh).

ROOT=${1:-$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)}
SETTINGS=${2:-$ROOT/.claude/settings.json}
HOOK_CMD=$(jq -er '.hooks.Stop[0].hooks[0].command' "$SETTINGS") || exit 1
SH=${HOOK_SH:-$(command -v sh)}
TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
trap 'exit 1' INT TERM

# 훅 커맨드와 훅 안의 「sh ./gradlew」가 PATH 에서 찾는 sh 를 $SH 로 고정한다.
mkdir "$TMP/bin" && ln -s "$SH" "$TMP/bin/sh" || exit 1

PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); printf '%s\n' "PASS $1"; }
ng() { FAIL=$((FAIL + 1)); printf '%s\n' "FAIL $1"; }

# check <이름> <기대 코드> <설명>  — ACTUAL 과 비교
check() {
  if [ "$ACTUAL" = "$2" ]; then ok "$1 $3"; else ng "$1 기대 $2 실제 $ACTUAL ($3)"; fi
}
# 훅 stderr 에 문구가 있는지 / 없는지 (고정 문자열)
err_has() {
  if grep -qF -- "$2" "$TMP/err"; then ok "$1-stderr 「$2」 출력"; else ng "$1-stderr 「$2」 없음"; fi
}
# 훅 stdout 은 항상 비어야 한다(stdout 내용은 훅 판정을 바꿀 수 있다).
out_empty() {
  if [ -s "$TMP/out" ]; then ng "$1-stdout 비어 있지 않음"; else ok "$1-stdout 비어 있음"; fi
}
err_empty() {
  if [ -s "$TMP/err" ]; then ng "$1-stderr 비어 있지 않음"; else ok "$1-stderr 비어 있음"; fi
}
called() {
  if [ -f "$2" ]; then ok "$1 $3 호출됨"; else ng "$1 $3 호출 흔적 없음"; fi
}
not_called() {
  if [ -f "$2" ]; then ng "$1 $3 호출됨"; else ok "$1 $3 호출 안 됨"; fi
}
# 차단 출력: rc 0, stderr 비어 있음, stdout 이 JSON 객체 정확히 한 개이고 decision 이 block, reason 이 문자열.
# reason 을 $TMP/reason 에 풀어 둔다.
blocked() {
  check "$1" 0 "$2"
  err_empty "$1"
  if jq -es 'length == 1 and (.[0] | type == "object" and .decision == "block" and (.reason | type == "string"))' "$TMP/out" >/dev/null 2>&1; then
    ok "$1-stdout block JSON 한 개"
  else
    ng "$1-stdout block JSON 한 개가 아님"
  fi
  jq -r '.reason' "$TMP/out" >"$TMP/reason" 2>/dev/null
}
# reason 에 문구가 있는지 (고정 문자열)
reason_has() {
  if grep -qF -- "$2" "$TMP/reason"; then ok "$1-reason 「$2」 있음"; else ng "$1-reason 「$2」 없음"; fi
}

# 호출 흔적 파일에 적힌 인자가 기대와 같은지
args_are() {
  if [ "$(cat "$2" 2>/dev/null)" = "$3" ]; then ok "$1 인자 「$3」"; else ng "$1 인자 기대 「$3」 실제 「$(cat "$2" 2>/dev/null)」"; fi
}

# 가짜 프로젝트 디렉터리를 만든다. 대상 루트에 훅 스크립트가 있으면 같은 상대 경로로 복사한다.
# (스크립트가 없는 판은 settings.json 의 인라인 커맨드만으로 돈다)
new_proj() {
  mkdir -p "$1/.claude/hooks" || return 1
  if [ -f "$ROOT/.claude/hooks/compile-gate.sh" ]; then
    cp "$ROOT/.claude/hooks/compile-gate.sh" "$1/.claude/hooks/compile-gate.sh" || return 1
  fi
}

# 가짜 wrapper. 인자: 경로, 종료 코드. 셸 내장 명령만 쓴다.
# 실행된 디렉터리에 <이름>.called 를 남기고(내용 = 받은 인자), stdout·stderr 에 서로 다른 표식을 낸다.
make_wrapper() {
  cat >"$1" <<EOF
echo "\$*" >"./${1##*/}.called"
echo 'FAKE-OUT: Foo.java:3: error: cannot find symbol'
echo 'FAKE-ERR: BUILD FAILED' >&2
exit $2
EOF
}

input() {
  printf '%s' "$1" >"$TMP/in"
}
stop_json() {
  jq -n --arg cwd "$1" --argjson active "$2" \
    '{session_id: "s", cwd: $cwd, hook_event_name: "Stop", stop_hook_active: $active}' >"$TMP/in"
}

# 훅 실행 결과를 ACTUAL(종료 코드)·$TMP/out·$TMP/err 에 둔다.
# 인자: CLAUDE_PROJECT_DIR, 프로세스 작업 디렉터리, [PATH]
run_hook() {
  (cd "$2" && PATH="${3:-$TMP/bin:$PATH}" CLAUDE_PROJECT_DIR="$1" "$SH" -c "$HOOK_CMD") <"$TMP/in" >"$TMP/out" 2>"$TMP/err"
  ACTUAL=$?
}

# 기본 사례는 CLAUDE_PROJECT_DIR · 프로세스 작업 디렉터리 · 입력 cwd 를 같은 디렉터리로 맞춘다.
run_same() {
  run_hook "$1" "$1"
}

# a~c: 컴파일 실패 → 차단(exit 0 + stdout 의 block JSON), 컴파일 출력은 reason 에
case_fail() {
  d="$TMP/$1"
  new_proj "$d" && make_wrapper "$d/$2" "$3" || { ng "$1 시험 준비 실패"; return; }
  stop_json "$d" false
  run_same "$d"
  blocked "$1" "$4"
  reason_has "$1" 'FAKE-OUT: Foo.java:3: error: cannot find symbol'
  reason_has "$1" 'FAKE-ERR: BUILD FAILED'
  called "$1" "$d/$2.called" "$2"
}
case_fail a gradlew 1 'gradlew rc 1 → 차단'
case_fail b gradlew 7 'gradlew rc 7 → 차단'
case_fail c mvnw 1 'mvnw rc 1 → 차단'
args_are c "$TMP/c/mvnw.called" '-q test-compile'

# d: 컴파일 성공 → 통과, 출력 없음(가짜 wrapper 는 rc 0 이어도 표식을 낸다)
d="$TMP/d"
if new_proj "$d" && make_wrapper "$d/gradlew" 0; then
  stop_json "$d" false
  run_same "$d"
  check d 0 'gradlew rc 0 → 통과'
  out_empty d
  err_empty d
  called d "$d/gradlew.called" gradlew
  args_are d "$d/gradlew.called" '-q compileJava compileTestJava'
else
  ng 'd 시험 준비 실패'
fi

# e: stop_hook_active 가 true 면 wrapper 를 부르지 않고 통과
d="$TMP/e"
if new_proj "$d" && make_wrapper "$d/gradlew" 1; then
  stop_json "$d" true
  run_same "$d"
  check e 0 'stop_hook_active true → 통과'
  not_called e "$d/gradlew.called" gradlew
  out_empty e
else
  ng 'e 시험 준비 실패'
fi

# f: wrapper 없음 → 통과
d="$TMP/f"
if new_proj "$d"; then
  stop_json "$d" false
  run_same "$d"
  check f 0 'wrapper 없음 → 통과'
  out_empty f
else
  ng 'f 시험 준비 실패'
fi

# g: gradlew·mvnw 둘 다 있으면 gradlew 만 호출
d="$TMP/g"
if new_proj "$d" && make_wrapper "$d/gradlew" 0 && make_wrapper "$d/mvnw" 1; then
  stop_json "$d" false
  run_same "$d"
  check g 0 '둘 다 있음 → gradlew 결과(rc 0)를 따름'
  called g "$d/gradlew.called" gradlew
  not_called g "$d/mvnw.called" mvnw
else
  ng 'g 시험 준비 실패'
fi

# h~m: 읽지 못한 입력은 경고 후 통과(exit 1). 컴파일은 돌리지 않는다.
case_bad() {
  d="$TMP/$1"
  new_proj "$d" && make_wrapper "$d/gradlew" 1 || { ng "$1 시험 준비 실패"; return; }
  run_same "$d"
  check "$1" 1 "$2"
  err_has "$1" '검사하지 못했습니다'
  out_empty "$1"
  not_called "$1" "$d/gradlew.called" gradlew
}
input '{not json'; case_bad h '깨진 JSON'
: >"$TMP/in"; case_bad i '빈 stdin'
input '"문자열"'; case_bad j '객체가 아닌 입력'
input '{"hook_event_name":"Stop"}'; case_bad k 'stop_hook_active 없음'
input '{"hook_event_name":"Stop","stop_hook_active":"true"}'; case_bad m 'stop_hook_active 가 문자열'
# t, u: 값 하나를 낸 뒤 jq 가 실패하는 잘린 입력
input '{"stop_hook_active":true}{'; case_bad t '잘린 입력(true 뒤)'
input '{"stop_hook_active":false}{'; case_bad u '잘린 입력(false 뒤)'

# n: jq 없음. settings.json 읽기는 위에서 끝났고, 훅 실행 때만 PATH 를 바꾼다.
#    훅 커맨드가 sh 를 PATH 에서 찾으므로 sh 만 링크한다.
d="$TMP/n"
NOJQ="$TMP/nojq"
if ! { new_proj "$d" && make_wrapper "$d/gradlew" 1 && mkdir "$NOJQ" && ln -s "$SH" "$NOJQ/sh"; }; then
  ng 'n 시험 준비 실패'
elif env PATH="$NOJQ" "$SH" -c 'command -v jq' >/dev/null 2>&1; then
  ng 'n 시험 준비 실패: 임시 PATH 에서 jq 가 보임'
else
  stop_json "$d" false
  run_hook "$d" "$d" "$NOJQ"
  check n 1 'jq 없음'
  err_has n 'jq'
  err_has n '검사하지 못했습니다'
  out_empty n
  not_called n "$d/gradlew.called" gradlew
fi

# o: 빌드 위치. CLAUDE_PROJECT_DIR · 프로세스 작업 디렉터리 · 입력 cwd 가 서로 다르면
#    CLAUDE_PROJECT_DIR 의 wrapper 만 호출한다.
d="$TMP/o"
if new_proj "$d" && make_wrapper "$d/gradlew" 1 \
  && mkdir "$TMP/o-proc" "$TMP/o-cwd" && make_wrapper "$TMP/o-proc/gradlew" 0 && make_wrapper "$TMP/o-cwd/gradlew" 0; then
  stop_json "$TMP/o-cwd" false
  run_hook "$d" "$TMP/o-proc"
  blocked o 'CLAUDE_PROJECT_DIR 에서 컴파일'
  called o "$d/gradlew.called" 'CLAUDE_PROJECT_DIR 의 gradlew'
  not_called o "$TMP/o-proc/gradlew.called" '프로세스 작업 디렉터리의 gradlew'
  not_called o "$TMP/o-cwd/gradlew.called" '입력 cwd 의 gradlew'
else
  ng 'o 시험 준비 실패'
fi

# p: 경로에 공백
d="$TMP/p q"
if new_proj "$d" && make_wrapper "$d/gradlew" 1; then
  stop_json "$d" false
  run_same "$d"
  blocked p '경로에 공백이 있어도 차단'
  reason_has p 'FAKE-ERR: BUILD FAILED'
else
  ng 'p 시험 준비 실패'
fi

# q: 긴 출력은 끝 200줄만 전달한다(가짜 wrapper 가 300줄을 낸다)
d="$TMP/q"
if new_proj "$d" && cat >"$d/gradlew" <<'EOF'
i=1
while [ "$i" -le 300 ]; do
  echo "LINE-$i-END"
  i=$((i + 1))
done
exit 1
EOF
then
  stop_json "$d" false
  run_same "$d"
  blocked q '300줄 출력 → 차단'
  reason_has q 'LINE-300-END'
  reason_has q 'LINE-101-END'
  n=$(grep -c '^LINE-[0-9]*-END$' "$TMP/reason")
  if [ "$n" = 200 ]; then ok 'q-reason 컴파일 출력 200줄'; else ng "q-reason 컴파일 출력 기대 200줄 실제 ${n}줄"; fi
  n=$(grep -c '' "$TMP/reason")
  if [ "$n" = 201 ]; then ok 'q-reason 머리말 1줄 + 200줄'; else ng "q-reason 전체 기대 201줄 실제 ${n}줄"; fi
else
  ng 'q 시험 준비 실패'
fi

# r, s, w: 출력 내용이 차단 판정이나 JSON 을 깨지 않는다. 인자: 이름, wrapper rc, 설명, 확인할 문구(없으면 생략)
#    wrapper 본문은 stdin 으로 받는다.
case_text() {
  d="$TMP/$1"
  new_proj "$d" && cat >"$d/gradlew" || { ng "$1 시험 준비 실패"; return; }
  echo "exit $2" >>"$d/gradlew"
  stop_json "$d" false
  run_same "$d"
  blocked "$1" "$3"
  if [ -n "$4" ]; then reason_has "$1" "$4"; fi
}
case_text r 7 '「파일 없음」 문구 + rc 7 → 차단' 'foo.java: No such file or directory' <<'EOF'
echo 'foo.java: No such file or directory' >&2
EOF
case_text s 1 '큰따옴표·역슬래시·한글 → 유효한 JSON' 'error: "기호"를 찾을 수 없습니다 C:\src\Foo.java \n' <<'EOF'
printf '%s\n' 'error: "기호"를 찾을 수 없습니다 C:\src\Foo.java \n'
EOF
case_text w 1 'UTF-8 이 아닌 바이트 → 유효한 JSON' 'AFTER-BYTE' <<'EOF'
printf 'BEFORE-BYTE \377 AFTER-BYTE\n'
EOF

# v: 프로젝트 루트로 이동 실패. settings 커맨드로는 스크립트를 못 찾으므로 스크립트를 직접 실행한다.
if [ ! -f "$ROOT/.claude/hooks/compile-gate.sh" ]; then
  ng 'v 대상 루트에 compile-gate.sh 없음'
else
  stop_json "$TMP/v-none" false
  (PATH="$TMP/bin:$PATH" CLAUDE_PROJECT_DIR="$TMP/v-none" "$SH" "$ROOT/.claude/hooks/compile-gate.sh") <"$TMP/in" >"$TMP/out" 2>"$TMP/err"
  ACTUAL=$?
  check v 1 '없는 CLAUDE_PROJECT_DIR'
  err_has v '검사하지 못했습니다'
  out_empty v
fi

echo "통과 $PASS / 실패 $FAIL"
[ "$FAIL" -eq 0 ]
