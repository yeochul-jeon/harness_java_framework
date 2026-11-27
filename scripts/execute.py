#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push]
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    # 터미널이 아니면(파일로 돌린 로그 등) 프레임 대신 시작 줄 하나만 쓴다 — \r 이 덮어쓰지 못해 프레임마다 한 줄씩 쌓인다.
    # 시작 줄은 결과 줄과 같은 stdout 에 flush 해 로그에서 순서가 뒤바뀌지 않게 한다.
    th = threading.Thread(target=_animate, daemon=True) if sys.stderr.isatty() else None
    if th:
        th.start()
    else:
        print(f"  ▶ {label}", flush=True)
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        if th:
            th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    BUILD_TIMEOUT = 1800
    BUILD_TAIL_LINES = 40
    DRAIN_TIMEOUT = 10
    BUILD_TIMEOUT_HEAD = "AC 빌드 시간 초과"
    SESSION_TIMEOUT = 1800
    SESSION_TIMEOUT_HEAD = "세션 시간 초과"
    _UNCHECKED = object()  # _session_branch 를 _execute_single_step 이 아직 적지 않았다(브랜치 비교를 하지 않는다).
    EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    BUILD_FILES = {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
                   "gradle.properties", "maven.config"}
    # 판정 기준을 느슨하게 만드는 설정. 정규식 수준 관찰이라 오탐·누락이 있다(경고만 하고 막지 않는다).
    DISABLE_RE = re.compile(r"@(Disabled\w*|Ignore)\b")
    BUILD_SKIP_RE = re.compile(
        r"<skipTests>\s*true|skipTests\s*=\s*true|-DskipTests\b(?!=false)"
        r"|maven\.test\.skip\s*(>|=)\s*true|-Dmaven\.test\.skip\b(?!=false)"
        r"|testFailureIgnore\s*(>|=)\s*true|ignoreFailures\s*=\s*true|\benabled\s*=\s*false"
        r"|-x\s+test\b|--exclude-task[=\s]+test\b"
    )
    # 세션 권한: acceptEdits 가 프로젝트 안 편집·파일 명령을 자동 승인하고, --permission-prompts none 이 승인이
    # 필요한 나머지 호출(프로젝트 밖 쓰기·git commit 등)을 묻지 않고 거부한다. 허용 목록은 빌드 래퍼만 더한다.
    DEFAULT_ALLOWED_TOOLS = ["Bash(./gradlew *)", "Bash(./mvnw *)"]
    MIN_CLAUDE_VERSION = "v2.1.259"  # --permission-prompts 를 처음 지원한 판(CLI 문서)
    DENIAL_ITEM_MAX = 120
    DEFAULT_BRANCH_PREFIX = "feature/"
    DEFAULT_COMMIT_FORMAT = "[{ticket}] {message}"
    TZ = timezone(timedelta(hours=9))

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False):
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push
        self._session_branch = self._UNCHECKED  # 세션 직전 브랜치 — 세션이 브랜치를 바꿨으면 출력을 저장하지 않는다.
        self._allowed_tools = list(self.DEFAULT_ALLOWED_TOOLS)  # run() 이 checkout 뒤 phase 값을 더해 다시 정한다.

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])

        git_cfg = idx.get("git") or {}
        self._branch_prefix = git_cfg.get("branch_prefix", self.DEFAULT_BRANCH_PREFIX)
        self._commit_format = git_cfg.get("commit_format", self.DEFAULT_COMMIT_FORMAT)

    def run(self):
        self._print_header()
        self._check_clean_tree()
        self._checkout_branch()
        self._check_blockers()
        self._build_cmd = self._load_build_command()
        # 실행마다 한 번만 읽는다 — 세션이 index 에 적은 값이 이번 실행의 다음 시도·다음 step 을 넓히지 않는다.
        self._allowed_tools = self._load_allowed_tools()
        guardrails = self._load_guardrails()
        self._ensure_created_at()
        self._execute_all_steps(guardrails)
        self._finalize()

    # --- timestamps ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        cmd = ["git"] + list(args)
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True)

    def _branch_name(self) -> str:
        return f"{self._branch_prefix}{self._phase_name}"

    def _commit_msg(self, message: str) -> str:
        try:
            return self._commit_format.format(ticket=self._phase_name, message=message)
        except (KeyError, IndexError):
            # commit_format 오타로 step 실행 도중 죽는 것을 막는다.
            print(f"  WARN: commit_format '{self._commit_format}' 형식 오류 — 기본값 사용")
            return self.DEFAULT_COMMIT_FORMAT.format(ticket=self._phase_name, message=message)

    def _checkout_branch(self):
        branch = self._branch_name()

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print(f"  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print(f"  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _check_clean_tree(self):
        """phases/ 밖에 커밋되지 않은 변경(미추적 포함)이 있으면 목록을 보이고 멈춘다.

        성공 커밋은 git add -A 라서, 실행 전부터 있던 변경이 step 커밋에 섞인다. 실패한 step 의 코드도
        작업 트리에 남으므로(_commit_state) 사용자가 이어 쓸지(커밋)·보관(stash)·버릴지 정한 뒤 다시 돌린다.
        """
        # 경로는 표시만 한다 — 로캘로 못 읽는 이름 하나 때문에 검사가 죽지 않게 바꿔 읽는다.
        r = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
            cwd=self._root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            print(f"  ERROR: git status 실패 — git 저장소인지 확인하라.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)
        dirty = [e for e in r.stdout.split("\0") if e and not e[3:].startswith("phases/")]
        if dirty:
            print(f"  ERROR: phases/ 밖에 커밋되지 않은 변경이 있다 — 커밋·stash·폐기 중 하나로 정리한 뒤 다시 실행하라.")
            for e in dirty:
                print(f"    {e[:2]} {e[3:]}")
            sys.exit(1)

    def _git_state(self) -> tuple:
        """(HEAD 커밋, 브랜치 이름). git 이 아니거나 커밋이 없으면 None, detached HEAD 면 브랜치 None."""
        head = self._run_git("rev-parse", "--verify", "-q", "HEAD")
        branch = self._run_git("symbolic-ref", "-q", "--short", "HEAD")
        return (head.stdout.strip() if head.returncode == 0 else None,
                branch.stdout.strip() if branch.returncode == 0 else None)

    def _session_git_violation(self, before: tuple, after: tuple) -> Optional[str]:
        """세션이 커밋하거나 HEAD·브랜치를 바꿨으면 그 문구. 실행기는 세션 앞뒤로 커밋하지 않는다."""
        (h0, b0), (h1, b1) = before, after
        if b0 != b1:
            return f"세션이 브랜치를 바꿈({b0 or 'detached'} → {b1 or 'detached'}, HEAD {h0} → {h1})"
        if h0 == h1:
            return None
        if h1 is None:
            return f"세션 중 HEAD 가 사라짐(브랜치 {b1 or 'detached'} 의 ref 삭제 등, HEAD {h0} → 없음)"
        if h0 is None or self._run_git("merge-base", "--is-ancestor", h0, h1).returncode == 0:
            n = self._run_git("rev-list", "--count", f"{h0}..{h1}" if h0 else h1).stdout.strip()
            return f"세션이 커밋함(HEAD {h0} → {h1}, 새 커밋 {n}개)"
        return f"세션 중 HEAD 가 바뀜(되감기·amend 등, HEAD {h0} → {h1})"

    def _scan_criteria(self, base: Optional[str]) -> list:
        """base 커밋 대비 지금 작업 트리(미추적 포함)에서 판정 기준을 바꾸는 변경을 찾아 문구 목록으로 돌려준다.

        실제 index 를 복사한 임시 index 에만 git add -A 한다 — 실제 index·작업 트리·ref 는 건드리지 않는다.
        """
        def warn(r):
            print(f"  WARN: 판정 기준 검사 실패 — {r.stderr.strip()[:200]}")
            return []

        real = self._run_git("rev-parse", "--git-path", "index")
        if real.returncode != 0:
            return warn(real)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix="harness-index-")
            os.close(fd)
            os.unlink(tmp)
            real_index = Path(self._root) / real.stdout.strip()
            if real_index.exists():
                shutil.copyfile(real_index, tmp)
            env = {**os.environ, "GIT_INDEX_FILE": tmp}

            def git(*args):
                return subprocess.run(["git", *args], cwd=self._root, env=env, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace")

            r = git("add", "-A")
            if r.returncode != 0:
                return warn(r)
            base = base or self.EMPTY_TREE
            r = git("diff", "--cached", "-M", "-z", "--name-status", base)
            if r.returncode != 0:
                return warn(r)

            def count(text, pattern):
                n = 0
                for line in text.splitlines():
                    if line.strip().startswith(("//", "*", "/*", "<!--", "#")):
                        continue
                    n += len(pattern.findall(line))
                return n

            def is_test(path):
                return bool(re.search(r"(^|/)src/test/", path) or re.search(r"(Test|Tests|IT)\.(java|kt|groovy)$", path))

            def is_rule(path):
                return path == "CLAUDE.md" or path.endswith("/CLAUDE.md") or path.startswith(".claude/")

            found = []
            tokens = r.stdout.split("\0")
            i = 0
            while i < len(tokens) and tokens[i]:
                code = tokens[i]
                if code[0] in "RC":
                    old, new = tokens[i + 1], tokens[i + 2]
                    i += 3
                else:
                    old = new = tokens[i + 1]
                    i += 2
                shown = new if old == new else f"{old} → {new}"
                if is_rule(old) or is_rule(new):
                    found.append(f"규칙 파일 변경: {shown}")
                if code == "D" and is_test(old):
                    found.append(f"테스트 파일 삭제: {old}")
                if code[0] == "R" and is_test(old) and not is_test(new):
                    found.append(f"테스트 파일 이동(테스트 경로 밖): {shown}")
                if code == "D":
                    continue
                checks = []
                if is_test(new) and new.endswith((".java", ".kt", ".groovy")):
                    checks.append(("테스트 비활성화 추가", self.DISABLE_RE))
                if Path(new).name in self.BUILD_FILES:
                    checks.append(("빌드 파일 테스트 생략 설정 추가", self.BUILD_SKIP_RE))
                if not checks:
                    continue
                old_text = git("show", f"{base}:{old}").stdout if code[0] not in "AC" else ""  # C 는 새 파일
                new_text = git("show", f":{new}").stdout
                for label, pattern in checks:
                    gained = count(new_text, pattern) - count(old_text, pattern)
                    if gained > 0:
                        found.append(f"{label}(+{gained}): {new}")
            return found
        except OSError as e:
            # 관찰용 검사다 — 임시 파일 문제로 완료·재시도·실패 기록을 막지 않는다.
            print(f"  WARN: 판정 기준 검사 실패 — {e}")
            return []
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def _commit_state(self, step_num: int, note: str):
        """phases/ 상태 파일만 커밋한다. 빌드를 통과하지 않은 코드는 staged 여도 커밋에 넣지 않고 작업 트리에 둔다."""
        # phase index 는 늘 있다(__init__ 이 확인) — 경로 목록이 비어 `--` 뒤가 빈 전체 커밋이 되지 않는다.
        # stepN-output.json 은 넣지 않는다 — .gitignore 가 무시하는 경로를 git add 에 명시하면 add 가 실패해 커밋 전체가 빠진다.
        paths = [f"phases/{self._phase_dir_name}/index.json"]
        if (Path(self._root) / "phases/index.json").exists():
            paths.append("phases/index.json")
        r = self._run_git("add", "--", *paths)
        if r.returncode != 0:
            print(f"  WARN: 상태 커밋 실패(git add): {r.stderr.strip()}")
            return
        if self._run_git("diff", "--cached", "--quiet", "--", *paths).returncode == 0:
            return
        msg = self._commit_msg(f"step {step_num} {note}")
        r = self._run_git("commit", "-m", msg, "--", *paths)
        if r.returncode == 0:
            print(f"  Commit: {msg}")
        else:
            print(f"  WARN: 상태 커밋 실패: {r.stderr.strip()}")

    def _add_all(self):
        """작업 트리 전체를 stage 하되 모든 phase 의 세션 출력 파일(stepN-output.json)은 뺀다.
        설치한 프로젝트에 무시 규칙이 없어도 커밋에 들어가지 않게 하고, 세션이 미리 stage 해 둔 것도 내린다."""
        outputs = "phases/*/step*-output.json"
        self._run_git("add", "-A", "--", ".", f":(exclude,glob){outputs}")
        self._run_git("reset", "-q", "HEAD", "--", f":(glob){outputs}")

    def _commit_step(self, step_num: int, step_name: str):
        index_rel = f"phases/{self._phase_dir_name}/index.json"

        self._add_all()
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self._commit_msg(f"step {step_num} {step_name}")
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: 코드 커밋 실패: {r.stderr.strip()}")

        self._add_all()
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self._commit_msg(f"step {step_num} 산출물 기록")
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"  WARN: housekeeping 커밋 실패: {r.stderr.strip()}")

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- guardrails & context ---

    def _load_guardrails(self) -> str:
        sections = []
        claude_md = ROOT / "CLAUDE.md"
        if claude_md.exists():
            sections.append(f"## 프로젝트 규칙 (CLAUDE.md)\n\n{claude_md.read_text()}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                sections.append(f"## {doc.stem}\n\n{doc.read_text()}")
        return "\n\n---\n\n".join(sections) if sections else ""

    def _load_build_command(self) -> str:
        """CLAUDE.md `## 명령어` 코드 블록의 둘째 줄(실행·빌드·테스트·컴파일 순)을 빌드 커맨드로 꺼낸다.

        줄 전체를 셸에 넘긴다(뒤 주석은 셸이 버린다). 표시·placeholder 판정은 셸 문법을 해석하지 않고
        줄 전체 문자열에서 찾는다 — 따옴표 안에 있어도 거부하는 쪽(안전한 쪽)으로 틀린다.
        """
        def stop(reason: str):
            print(f"  ERROR: CLAUDE.md 빌드 커맨드를 쓸 수 없다 — {reason}")
            print(f"  /bootstrap 을 먼저 실행하라.")
            sys.exit(1)

        claude_md = Path(self._root) / "CLAUDE.md"
        if not claude_md.exists():
            stop("CLAUDE.md 가 없다")
        lines = claude_md.read_text(encoding="utf-8").splitlines()
        if "## 명령어" not in lines:
            stop("`## 명령어` 절이 없다")

        block, inside, closed = [], False, False
        for line in lines[lines.index("## 명령어") + 1:]:
            if line.startswith("```"):
                if inside:
                    closed = True
                    break
                inside = True
            elif line.startswith("## "):
                break
            elif inside and line.strip():
                block.append(line.strip())
        if not closed:
            stop("`## 명령어` 의 코드 블록이 없거나 닫히지 않았다")
        if len(block) != 4:
            stop(f"코드 블록 줄이 4개(실행·빌드·테스트·컴파일)가 아니다: {len(block)}개")

        line = block[1]
        if line.startswith("#"):
            stop(f"커맨드 없이 주석만 있다: {line}")
        if re.search(r"\{[가-힣 ]+\}", line):
            stop(f"placeholder 그대로다: {line}")
        if "미검증" in line:
            stop(f"`미검증` 표시가 있다: {line}")
        if "테스트 미실행" in line:
            print(f"  WARN: 빌드 줄에 `테스트 미실행` 표시가 있다 — AC 빌드는 테스트를 돌리지 않는다: {line}")
        return line

    def _load_allowed_tools(self) -> list:
        """기본 허용 목록 뒤에 phase index 의 선택 필드 allowed_tools(문자열 배열)를 붙인다."""
        extra = self._read_json(self._index_file).get("allowed_tools", [])
        if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
            print(f"ERROR: {self._index_file} 의 allowed_tools 는 문자열 배열이어야 한다 — 받은 값: {json.dumps(extra, ensure_ascii=False)}")
            sys.exit(1)
        # 빈 값·'-' 로 시작하는 값은 claude 인자 해석에서 옵션으로 읽혀 권한 플래그를 바꿀 수 있다.
        bad = [x for x in extra if not x.strip() or x.lstrip().startswith("-")]
        if bad:
            print(f"ERROR: {self._index_file} 의 allowed_tools 에 빈 값이나 '-' 로 시작하는 값이 있다: {bad}")
            sys.exit(1)
        return self.DEFAULT_ALLOWED_TOOLS + extra

    def _permission_denials(self, stdout: str) -> list:
        """세션 출력 JSON 의 permission_denials 를 「도구: 명령 또는 경로」 요약 목록으로. 읽지 못하면 WARN 후 빈 목록.
        tool_input 전체는 남기지 않는다 — Write 는 파일 내용을 통째로 담고, phase index 는 커밋된다.
        관찰용 검사다 — 어떤 예외도 세션 커밋 감지·판정·빌드를 막지 않게 WARN 으로 남긴다(판정 기준 검사와 같다)."""
        try:
            data = json.loads(stdout)
            denials = data["permission_denials"]
            if not isinstance(denials, list) or not all(isinstance(d, dict) for d in denials):
                raise TypeError("permission_denials 가 객체 배열이 아니다")
            items = []
            for d in denials:
                tool = str(d.get("tool_name", "?"))
                inp = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
                target = next((inp[k] for k in ("command", "file_path", "path", "url", "pattern")
                               if isinstance(inp.get(k), str)), "")
                item = " ".join(f"{tool}: {target}".split()) if target else tool
                if len(item) > self.DENIAL_ITEM_MAX:
                    item = item[:self.DENIAL_ITEM_MAX - 1] + "…"
                # JSON 의 단독 서로게이트(\ud800 등)는 UTF-8 로 쓸 수 없다 — 출력 파일·index 쓰기가 끊기지 않게 바꾼다.
                items.append(item.encode("utf-8", "replace").decode("utf-8"))
            return items
        except Exception as e:
            print(f"  WARN: 권한 거부 기록을 읽지 못했다 — {type(e).__name__}: {e}")
            return []

    @staticmethod
    def _build_step_context(index: dict) -> str:
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    def _build_preamble(self, guardrails: str, step_context: str,
                        prev_error: Optional[str] = None) -> str:
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
                f"{prev_error}\n\n---\n\n"
            )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{retry_section}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"3. 기존 테스트를 깨뜨리지 마라.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라.\n"
            f"5. /phases/{self._phase_dir_name}/index.json의 해당 step status를 업데이트하라:\n"
            f"   - AC 통과 → \"completed\" + \"summary\" 필드에 이 step의 산출물을 한 줄로 요약\n"
            f"   - {self.MAX_RETRIES}회 수정 시도 후에도 실패 → \"error\" + \"error_message\" 기록\n"
            f"   - 사용자 개입이 필요한 경우 (API 키, 인증, 수동 설정 등) → \"blocked\" + \"blocked_reason\" 기록 후 즉시 중단\n"
            f"6. 커밋하지 마라. 실행기가 AC 빌드를 다시 돌려 통과하면 커밋한다.\n"
            f"7. 권한이 거부된 명령을 다른 방법으로 우회하지 마라. 거부 때문에 진행할 수 없으면 \"blocked\" + "
            f"\"blocked_reason\" 에 거부된 명령을 적어라.\n\n---\n\n"
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step: dict, preamble: str) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text()
        # 세션이 띄운 빌드·셸이 시간 초과 뒤에도 남지 않게 별도 프로세스 그룹으로 띄운다(_run_build 와 같은 이유).
        proc = subprocess.Popen(
            # --allowedTools 는 값을 여럿 받는다 — 다음 옵션(--output-format)이 목록을 끝내므로 순서를 바꾸지 않는다.
            ["claude", "-p", "--permission-mode", "acceptEdits", "--permission-prompts", "none",
             "--allowedTools", *self._allowed_tools,
             "--output-format", "json", prompt],
            cwd=self._root, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=self.SESSION_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=self.DRAIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.stdout.close()
                proc.stderr.close()
                proc.wait()
                stdout, stderr = "", ""
        except BaseException:
            self._kill_group(proc)
            proc.wait()
            raise

        if timed_out:
            print(f"\n  WARN: {self.SESSION_TIMEOUT_HEAD}({self.SESSION_TIMEOUT}초) — 세션 프로세스 그룹을 끝냈다")
        elif proc.returncode != 0:
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {proc.returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")

        output = {
            "step": step_num, "name": step_name,
            "exitCode": proc.returncode, "timedOut": timed_out,
            "stdout": stdout, "stderr": stderr,
            # 시간 초과면 결과 JSON 이 없다. 인자를 모르는 옛 CLI 도 출력이 없어 읽지 않는다(_execute_single_step 이 멈춘다).
            "permissionDenials": [] if timed_out or self._unknown_option(proc.returncode, stdout, stderr)
                                 else self._permission_denials(stdout),
        }
        out_path = self._phase_dir / f"step{step_num}-output.json"
        switched = self._session_branch is not self._UNCHECKED and self._git_state()[1] != self._session_branch
        if switched or not self._phase_dir.is_dir():
            # 세션이 브랜치를 바꿨다 — 다른 브랜치의 작업 트리에 파일을 만들거나 덮어쓰지 않는다.
            print(f"  WARN: 세션이 브랜치를 바꿨거나 {self._phase_dir} 가 없어 세션 출력을 저장하지 않았다")
            return output
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return output

    @staticmethod
    def _unknown_option(returncode, stdout, stderr) -> Optional[str]:
        """claude 가 호출 인자를 몰라 시작도 못 했으면 그 stderr 줄을 돌려준다.
        claude 의 문구(`error: unknown option '…'`)로 시작하는 줄만 본다 — 다른 프로그램의 진단은 옛 CLI 가 아니다."""
        if returncode == 0 or (stdout or "").strip():
            return None
        return next((l.strip() for l in (stderr or "").splitlines() if l.startswith("error: unknown option")), None)

    def _run_build(self) -> Optional[str]:
        """빌드 커맨드를 돌린다. 통과면 None, 실패·시간 초과면 다음 시도에 넘길 에러 문구."""
        print(f"  AC 빌드: {self._build_cmd}")
        proc = subprocess.Popen(
            self._build_cmd, shell=True, cwd=self._root, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            out, _ = proc.communicate(timeout=self.BUILD_TIMEOUT)
            head = f"AC 빌드 실패(exit {proc.returncode})"
        except subprocess.TimeoutExpired:
            # shell 만 죽이면 그 아래 빌드(JVM 등)가 남아 다음 시도와 겹친다 — 프로세스 그룹째 끝낸다.
            self._kill_group(proc)
            head = f"{self.BUILD_TIMEOUT_HEAD}({self.BUILD_TIMEOUT}초)"
            try:
                out, _ = proc.communicate(timeout=self.DRAIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                # 그룹 밖으로 나간 프로세스가 출력 파이프를 쥐고 있다 — 출력 없이 진행한다.
                proc.stdout.close()
                proc.wait()  # 직접 자식은 이미 SIGKILL 을 받았다.
                out = ""
                head += f" — 출력 회수 {self.DRAIN_TIMEOUT}초 초과, 그룹 밖 프로세스가 파이프를 쥐고 있다"
        except BaseException:
            # 빌드는 별도 세션이라 실행기의 Ctrl-C 를 받지 못한다 — 실행기가 멈추면 함께 끝낸다.
            self._kill_group(proc)
            proc.wait()
            raise
        else:
            if proc.returncode == 0:
                return None
        tail = "\n".join(out.splitlines()[-self.BUILD_TAIL_LINES:])
        return f"{head}: {self._build_cmd}\n{tail}"

    @staticmethod
    def _kill_group(proc: subprocess.Popen):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # 그룹이 이미 끝났다.

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total}")
        if self._auto_push:
            print(f"  Auto-push: enabled")
        print(f"{'='*60}")

    def _check_blockers(self):
        index = self._read_json(self._index_file)
        for s in index["steps"]:
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print(f"  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print(f"  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 실행 루프 ---

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """단일 step 실행 (재시도 포함). 완료되면 True, 실패/차단이면 False."""
        step_num, step_name = step["step"], step["name"]
        done = sum(1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed")
        prev_error = None
        # 판정 기준 경고는 step 시작 HEAD 대비 누적 변경으로 본다.
        step_base = self._git_state()[0]

        no_field = object()
        for attempt in range(1, self.MAX_RETRIES + 1):
            index = self._read_json(self._index_file)
            tools_before = index.get("allowed_tools", no_field)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            # 세션 커밋 감지는 시도마다 직전 값과 비교한다 — 앞 시도의 빌드가 HEAD 를 바꿨어도 다음 세션 탓으로 돌리지 않는다.
            git_before = self._git_state()
            self._session_branch = git_before[1]
            with progress_indicator(tag) as pi:
                out = self._invoke_claude(step, preamble)
            elapsed = int(pi.elapsed)

            git_after = self._git_state()
            violation = self._session_git_violation(git_before, git_after)
            if git_before[1] != git_after[1]:
                # 브랜치가 바뀐 트리에는 phase 상태를 쓰지도 커밋하지도 않는다. 되돌리기는 사용자가 한다.
                print(f"  ✗ Step {step_num}: {step_name} — {violation}")
                back = git_before[1] or f"--detach {git_before[0]}"
                print(f"    index·커밋에 기록하지 않았다. `git switch {back}` 로 돌아가 정리한 뒤 다시 실행하라.")
                sys.exit(1)

            bad_cli = None if out.get("timedOut") or violation else self._unknown_option(
                out.get("exitCode"), out.get("stdout"), out.get("stderr"))
            if bad_cli:
                # 세션이 시작도 못 했다 — 환경 문제라 재시도해도 같다. step 상태는 건드리지 않는다(올린 뒤 그대로 다시 실행).
                print(f"  ✗ Step {step_num}: {step_name} — 설치된 Claude Code 가 실행기 호출 인자를 모른다: {bad_cli}")
                print(f"    `claude --version` 을 확인하고 Claude Code {self.MIN_CLAUDE_VERSION} 이상으로 올린 뒤 다시 실행하라.")
                sys.exit(1)

            session_err = None
            if out.get("timedOut"):
                session_err = f"{self.SESSION_TIMEOUT_HEAD}({self.SESSION_TIMEOUT}초)"
            if violation:
                note = ""
                if not out.get("timedOut"):
                    try:
                        disk = next(s for s in self._read_json(self._index_file)["steps"] if s["step"] == step_num)
                        if disk.get("status") == "blocked":
                            note = f" — 세션은 blocked 로 보고: {disk.get('blocked_reason', '')}"
                    except (OSError, ValueError, StopIteration, TypeError, KeyError, AttributeError):
                        pass  # 세션이 index 를 깨뜨렸다 — 사유 없이 위반만 적는다.
                session_err = "; ".join(x for x in (violation + note, session_err) if x)
            if session_err:
                # 끝나지 않았거나 규칙을 어긴 세션이 적은 completed·blocked·summary 는 믿지 않는다. 쓰다 잘린 index 일 수도
                # 있어 다시 읽지 않고, 세션 호출 전에 읽어 둔 index 에 기록한다.
                status = "session-error"
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s.pop("summary", None)
            else:
                index = self._read_json(self._index_file)
                status = next((s.get("status", "pending") for s in index["steps"] if s["step"] == step_num), "pending")
                # 세션이 자기 권한 목록을 넓혀 다음 실행에 남기지 못하게 세션 전 값으로 되돌린다(판정은 그대로).
                # session_err 경로는 세션 전 index 를 쓰므로 따로 되돌릴 것이 없다.
                if index.get("allowed_tools", no_field) != tools_before:
                    if tools_before is no_field:
                        index.pop("allowed_tools", None)
                    else:
                        index["allowed_tools"] = tools_before
                    self._write_json(self._index_file, index)
                    print("  ⚠ 세션이 allowed_tools 를 바꿨다 — 세션 전 값으로 되돌렸다")
            ts = self._stamp()

            # 판정 기준 변경 경고(관찰만). 세션이 이 필드를 적었어도 실행기 값으로 덮어쓴다.
            try:
                warnings = self._scan_criteria(step_base)
            except Exception as e:
                # 관찰용 검사다 — 어떤 예외도 빌드·재시도·실패 기록을 막지 않게 하고, 실패는 WARN 으로 남긴다.
                print(f"  WARN: 판정 기준 검사 실패 — {type(e).__name__}: {e}")
                warnings = []
            for s in index["steps"]:
                if s["step"] == step_num:
                    if warnings:
                        s["criteria_warnings"] = warnings
                    else:
                        s.pop("criteria_warnings", None)
            if warnings:
                print(f"  ⚠ 판정 기준 변경 {len(warnings)}건 — " + "; ".join(warnings))

            # 권한 거부 내역(관찰만 — 판정은 바꾸지 않는다). 세션이 이 필드를 적었어도 실행기 값으로 덮어쓴다.
            denials = out.get("permissionDenials") or []
            for s in index["steps"]:
                if s["step"] == step_num:
                    if denials:
                        s["permission_denials"] = denials
                    else:
                        s.pop("permission_denials", None)
            if denials:
                print(f"  ⚠ 권한 거부 {len(denials)}건 — " + "; ".join(denials))

            build_err = None
            if status == "completed":
                # 빌드가 통과하기 전에는 completed 를 남기지 않는다 — 빌드 중 실행기가 죽으면 다음 실행이 이 step 을 다시 돈다.
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                self._write_json(self._index_file, index)
                build_err = self._run_build()
                if build_err is not None:
                    # 빌드가 통과하지 않은 세션의 요약은 다음 시도·다음 step 에 넘기지 않는다.
                    for s in index["steps"]:
                        if s["step"] == step_num:
                            s.pop("summary", None)

            if status == "completed" and build_err is None:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "completed"
                        s["completed_at"] = ts
                self._write_json(self._index_file, index)
                self._commit_step(step_num, step_name)
                print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                return True

            if status == "blocked":
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["blocked_at"] = ts
                self._write_json(self._index_file, index)
                reason = next((s.get("blocked_reason", "") for s in index["steps"] if s["step"] == step_num), "")
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {reason}")
                self._update_top_index("blocked")
                self._commit_state(step_num, f"{step_name} 중단 기록")
                sys.exit(2)

            err_msg = build_err or session_err or next(
                (s.get("error_message", "Step did not update status") for s in index["steps"] if s["step"] == step_num),
                "Step did not update status",
            )

            # 빌드 시간 초과는 코드보다 환경(JAVA_HOME·네트워크 등) 탓일 때가 많아 재시도해도 같다 — 그 자리에서 끝낸다.
            # 세션 시간 초과도 같다 — 재시도하면 최악 MAX_RETRIES × SESSION_TIMEOUT 을 기다린다.
            # 세션 커밋도 재시도하지 않는다 — 커밋은 이미 이력에 있고, 재시도는 그 위에 쌓는다.
            timed_out = session_err is not None or (
                build_err is not None and build_err.startswith(self.BUILD_TIMEOUT_HEAD))

            if attempt < self.MAX_RETRIES and not timed_out:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                        s.pop("error_message", None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "error"
                        s["error_message"] = err_msg if timed_out else f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                        s["failed_at"] = ts
                self._write_json(self._index_file, index)
                self._update_top_index("error")
                # 빌드를 통과하지 않은 코드는 커밋하지 않는다 — 상태만 남기고 코드는 작업 트리에 둔다.
                self._commit_state(step_num, f"{step_name} 실패 기록")
                print(f"  ✗ Step {step_num}: {step_name} failed after {attempt} attempts [{elapsed}s]")
                print(f"    Error: {err_msg}")
                print(f"    빌드를 통과하지 않은 변경은 커밋하지 않고 작업 트리에 남겼다(git status).")
                sys.exit(1)

        return False  # unreachable

    def _execute_all_steps(self, guardrails: str):
        while True:
            index = self._read_json(self._index_file)
            # completed_at 은 빌드 통과 뒤 실행기만 쓴다. 없으면 검증 전에 실행기가 멈춘 step 이다 — 다시 돈다.
            unverified = [s for s in index["steps"] if s["status"] == "completed" and "completed_at" not in s]
            for s in unverified:
                s["status"] = "pending"
                s.pop("summary", None)
            if unverified:
                self._write_json(self._index_file, index)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                print("\n  All steps completed!")
                return

            step_num = pending["step"]
            for s in index["steps"]:
                if s["step"] == step_num and "started_at" not in s:
                    s["started_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    break

            self._execute_single_step(pending, guardrails)

    def _finalize(self):
        index = self._read_json(self._index_file)
        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._add_all()
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self._commit_msg("phase 완료")
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  ✓ {msg}")

        if self._auto_push:
            branch = self._branch_name()
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, auto_push=args.push).run()


if __name__ == "__main__":
    main()
