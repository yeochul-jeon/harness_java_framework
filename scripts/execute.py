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
import signal
import subprocess
import sys
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

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_RETRIES = 3
    BUILD_TIMEOUT = 1800
    BUILD_TAIL_LINES = 40
    DRAIN_TIMEOUT = 10
    BUILD_TIMEOUT_HEAD = "AC 빌드 시간 초과"
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
        self._check_blockers()
        self._checkout_branch()
        self._build_cmd = self._load_build_command()
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

    def _commit_step(self, step_num: int, step_name: str):
        output_rel = f"phases/{self._phase_dir_name}/step{step_num}-output.json"
        index_rel = f"phases/{self._phase_dir_name}/index.json"

        self._run_git("add", "-A")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self._commit_msg(f"step {step_num} {step_name}")
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: 코드 커밋 실패: {r.stderr.strip()}")

        self._run_git("add", "-A")
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
        commit_example = self._commit_msg("step N <step-name>")
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
            f"6. 모든 변경사항을 커밋하라:\n"
            f"   {commit_example}\n\n---\n\n"
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step: dict, preamble: str) -> dict:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text()
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json", prompt],
            cwd=self._root, capture_output=True, text=True, timeout=1800,
        )

        if result.returncode != 0:
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")

        output = {
            "step": step_num, "name": step_name,
            "exitCode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }
        out_path = self._phase_dir / f"step{step_num}-output.json"
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return output

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
        for s in reversed(index["steps"]):
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
            if s["status"] != "pending":
                break

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

        for attempt in range(1, self.MAX_RETRIES + 1):
            index = self._read_json(self._index_file)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            with progress_indicator(tag) as pi:
                self._invoke_claude(step, preamble)
                elapsed = int(pi.elapsed)

            index = self._read_json(self._index_file)
            status = next((s.get("status", "pending") for s in index["steps"] if s["step"] == step_num), "pending")
            ts = self._stamp()

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
                sys.exit(2)

            err_msg = build_err or next(
                (s.get("error_message", "Step did not update status") for s in index["steps"] if s["step"] == step_num),
                "Step did not update status",
            )

            # 빌드 시간 초과는 코드보다 환경(JAVA_HOME·네트워크 등) 탓일 때가 많아 재시도해도 같다 — 그 자리에서 끝낸다.
            timed_out = build_err is not None and build_err.startswith(self.BUILD_TIMEOUT_HEAD)

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
                self._commit_step(step_num, step_name)
                print(f"  ✗ Step {step_num}: {step_name} failed after {attempt} attempts [{elapsed}s]")
                print(f"    Error: {err_msg}")
                self._update_top_index("error")
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

        self._run_git("add", "-A")
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
