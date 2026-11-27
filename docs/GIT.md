# 브랜치 & 커밋 규칙

이 문서는 사람이 읽는 규칙이자, `execute.py`가 자동 생성하는 브랜치·커밋의 기준이다.
실제 동작 값은 `phases/{ticket}/index.json`의 `git` 블록에서 읽으며, 없으면 아래 기본값을 쓴다.

## 브랜치

**형식:** `{branch_prefix}{ticket}` — 기본값 `feature/ABC-1234`

`ticket`은 `phases/` 아래 task 디렉토리명이며, 곧 실행 인자다.

```bash
python3 scripts/execute.py ABC-1234   # → feature/ABC-1234 브랜치에서 작업
```

## 커밋 메시지

**형식:** `{commit_format}` — 기본값 `[{ticket}] {message}`

메시지 본문은 자연스러운 한국어로 간결하게 쓴다.

```
[ABC-1234] 주문 취소 API 추가
[ABC-1234] step 2 order-service
```

step 커밋은 `execute.py`가 한다. step을 수행하는 세션은 커밋하지 않는다.
세션이 커밋하면 `execute.py`가 그 step을 재시도 없이 error로 끝내고 멈춘다.
빌드를 통과한 step은 두 번(코드·메타) 커밋하고, 실패(error)하거나 중단(blocked)된 step은 `phases/` 상태 파일만 한 번 커밋한다. 빌드를 통과하지 않은 코드는 커밋하지 않는다.

| 커밋 | 대상 | 메시지 |
|------|------|--------|
| 코드 | step에서 작성/수정한 소스 | `[ABC-1234] step {N} {step-name}` |
| 메타 | `phases/` 아래 상태 파일 | `[ABC-1234] step {N} 산출물 기록` |
| 실패 | `phases/` 아래 상태 파일 | `[ABC-1234] step {N} {step-name} 실패 기록` |
| 중단 | `phases/` 아래 상태 파일 | `[ABC-1234] step {N} {step-name} 중단 기록` |

## 설정 — 다른 규칙을 쓰는 경우

`phases/{ticket}/index.json`에 `git` 블록을 넣으면 형식을 바꿀 수 있다.

```json
{
  "project": "my-api",
  "phase": "ABC-1234",
  "git": {
    "branch_prefix": "feat/",
    "commit_format": "feat({ticket}): {message}"
  },
  "steps": []
}
```

| 필드 | 기본값 | 설명 |
|------|--------|------|
| `branch_prefix` | `feature/` | 브랜치명 접두사. 빈 문자열이면 티켓명이 곧 브랜치명 |
| `commit_format` | `[{ticket}] {message}` | `{ticket}`, `{message}` 두 placeholder를 지원한다 |

Jira를 쓰지 않는 프로젝트는 티켓 대신 임의의 slug를 디렉토리명으로 쓰면 된다
(`phases/order-cancel/` → `feature/order-cancel`).

## PR 설명

```markdown
### Note
{변경 요약 — 여러 개면 `-` 목록, 하나면 한 줄}

### Commit
{커밋 메시지 목록}
```
