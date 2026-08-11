# Harness Framework (Java · Spring)

Claude Code로 Java/Spring 프로젝트를 step 단위로 자동 개발하는 오케스트레이션 프레임워크.

작업을 여러 step으로 쪼개 `phases/`에 정의하면, `execute.py`가 각 step을 독립된 Claude 세션에
순차로 전달하고, 검증 실패 시 자동 재시도하며, 성공하면 커밋 후 다음 step으로 넘어간다.

## 동작 방식

1. `phases/{ticket}/step{N}.md`에 step을 정의한다
2. `execute.py`가 각 step을 Claude Code(`claude -p`)에 순차 전달한다
3. Claude가 코드를 작성하고 **`CLAUDE.md`의 `## 명령어`에 적힌 빌드 커맨드**로 검증한다
4. 실패하면 이전 에러를 프롬프트에 피드백해 최대 3회 재시도한다
5. 성공하면 코드/메타데이터를 분리 커밋하고 다음 step으로 넘어간다

빌드툴·Java/Spring 버전·영속성·아키텍처 패턴은 고정하지 않는다. 프로젝트마다 다르므로
`/bootstrap`이 기존 소스를 분석해 문서를 채운다.

## 사전 요구사항

- [Claude Code](https://claude.ai/code) CLI
- Python 3
- Java + Spring 프로젝트 (Gradle Wrapper 또는 Maven Wrapper 포함)

## 설치

프로젝트 루트에 파일을 복사한다.

```bash
git clone https://github.com/yeochul-jeon/harness_java_framework.git
cp -r harness_java_framework/.claude   my-project/
cp -r harness_java_framework/docs      my-project/
cp -r harness_java_framework/scripts   my-project/
cp    harness_java_framework/CLAUDE.md my-project/
```

## 설정

프로젝트에서 Claude Code를 열고 **한 번만** 실행한다.

```
/bootstrap
```

`/bootstrap`이 기존 소스를 스캔해 빌드툴·Java/Spring 버전·영속성·패키지 구조·사내 프레임워크를
판별하고, `CLAUDE.md`와 `docs/`의 placeholder를 채운 뒤 결과를 보고한다.

빌드 커맨드는 **실제로 1회 실행해보고 성공했을 때만** 기록한다. 실행할 수 없으면 `# 미검증`으로
표시하고 확인을 요청한다. 이 값이 틀리면 모든 step의 AC가 틀리기 때문이다.

`docs/PRD.md`는 코드에서 알 수 없으므로 직접 작성한다.
사내 공용 프레임워크를 쓰지 않으면 `docs/STACK_PROFILE.md`를 삭제한다.

## 사용

```
/harness
```

문서를 읽고 → 논의하고 → step으로 쪼갠 계획을 승인받아 → `phases/` 파일을 생성한다.
그다음 실행한다.

```bash
python3 scripts/execute.py ABC-1234          # 순차 실행
python3 scripts/execute.py ABC-1234 --push   # 완료 후 push
```

변경 사항 리뷰는 `/review`.

## 구성

```
CLAUDE.md                     # 프로젝트 규칙 + 명령어 (검증 커맨드의 단일 진실원)
docs/
├── PRD.md                    # 무엇을 왜 만드는가
├── ARCHITECTURE.md           # 디렉토리 구조·패턴·데이터 흐름
├── ADR.md                    # 기술 선택과 트레이드오프
├── API_GUIDE.md              # URI·상태코드·에러 포맷·페이징 규약
├── GIT.md                    # 브랜치·커밋 규칙
└── STACK_PROFILE.md          # 사내/공용 프레임워크 설정 (선택 — 미사용 시 삭제)
.claude/
├── commands/{bootstrap,harness,review}.md
└── settings.json             # Stop hook(컴파일 검증) + 위험 커맨드 차단
scripts/
├── execute.py                # step 실행기
└── test_execute.py           # 실행기 테스트
```

`docs/*.md`는 **매 step 프롬프트에 전부 주입된다.** 문서를 늘릴수록 토큰을 쓰고 신호가 희석되므로
필요한 것만 남긴다.

## 브랜치·커밋 규칙 바꾸기

기본값은 브랜치 `feature/{ticket}`, 커밋 `[{ticket}] {message}`다.
`phases/{ticket}/index.json`에 `git` 블록을 넣으면 바꿀 수 있다.

```json
"git": { "branch_prefix": "feat/", "commit_format": "feat({ticket}): {message}" }
```

자세한 내용은 `docs/GIT.md` 참조.

## 프레임워크 자체 테스트

```bash
python3 -m pytest scripts/test_execute.py
```
