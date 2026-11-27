"""
execute.py 리팩터링 안전망 테스트.
리팩터링 전후 동작이 동일한지 검증한다.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "ABC-1234"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "ABC-1234",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "ABC-1234", "status": "pending"},
            {"dir": "ABC-5678", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2))
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("ABC-1234")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "ABC-1234"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text()
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text()
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------

class TestLoadGuardrails:
    def test_loads_claude_md_and_docs(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_claude_md(self, executor, tmp_project):
        (tmp_project / "CLAUDE.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "CLAUDE.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        import shutil
        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        with patch.object(ex, "ROOT", tmp_path):
            # executor가 필요 없는 static-like 동작이므로 임시 인스턴스
            phases_dir = tmp_path / "phases" / "dummy"
            phases_dir.mkdir(parents=True)
            idx = {"project": "T", "phase": "t", "steps": []}
            (phases_dir / "index.json").write_text(json.dumps(idx))
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    def test_includes_completed_with_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "이전 Step 산출물" in result

    def test_no_commit_example(self, executor):
        # 세션은 커밋하지 않는다 — 커밋 예시를 주지 않는다.
        result = executor._build_preamble("", "")
        assert "[ABC-1234] step N" not in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("", "")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("", "")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", "", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result

    def test_includes_max_retries(self, executor):
        result = executor._build_preamble("", "")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_includes_index_path(self, executor):
        result = executor._build_preamble("", "")
        assert "/phases/ABC-1234/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "ABC-1234")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "ABC-1234")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "ABC-1234")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        polish = next(p for p in data["phases"] if p["dir"] == "ABC-5678")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text())
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text())
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feature/ABC-1234\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    def test_two_phase_commit(self, executor):
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert commit_calls[0][2] == "[ABC-1234] step 2 ui"
        assert commit_calls[1][2] == "[ABC-1234] step 2 산출물 기록"

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert commit_msgs[0] == "[ABC-1234] step 2 산출물 기록"


# ---------------------------------------------------------------------------
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------

def _fake_popen(stdout='{"permission_denials": []}', returncode=0):
    proc = MagicMock(returncode=returncode)
    proc.communicate.return_value = (stdout, "")
    return proc


class TestInvokeClaude:
    def test_invokes_claude_with_correct_args(self, executor):
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.Popen", return_value=_fake_popen('{"result": "ok"}')) as mock_popen:
            output = executor._invoke_claude(step, preamble)

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert "--output-format" in cmd
        assert "PREAMBLE" in cmd[-1]
        assert "UI를 구현하세요" in cmd[-1]

    def test_saves_output_json(self, executor):
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.Popen", return_value=_fake_popen('{"ok": true}')):
            executor._invoke_claude(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_is_1800(self, executor):
        step = {"step": 2, "name": "ui"}
        proc = _fake_popen()

        with patch("subprocess.Popen", return_value=proc):
            executor._invoke_claude(step, "preamble")

        assert proc.communicate.call_args[1]["timeout"] == 1800


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_context_manager(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0

    def test_no_frames_when_stderr_is_not_a_terminal(self, monkeypatch):
        import io, time
        buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", buf)
        with ex.progress_indicator("test"):
            time.sleep(0.4)
        assert "\r" not in buf.getvalue()

    def test_frames_when_stderr_is_a_terminal(self, monkeypatch):
        import io, time

        class Tty(io.StringIO):
            def isatty(self):
                return True

        buf = Tty()
        monkeypatch.setattr(sys, "stderr", buf)
        with ex.progress_indicator("test"):
            assert _wait_until(lambda: buf.getvalue().count("\r") >= 2, 5)
        assert "test [" in buf.getvalue()  # 경과 초 값은 스레드 시작 시점에 따라 달라 보지 않는다

    def test_start_line_on_stdout_when_not_a_terminal(self, capsys):
        with ex.progress_indicator("Step 2/2 (2 done): ui"):
            pass
        out, err = capsys.readouterr()
        assert out == "  ▶ Step 2/2 (2 done): ui\n"
        assert err == ""

    def test_no_start_line_when_a_terminal(self, monkeypatch, capsys):
        import io

        class Tty(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stderr", Tty())
        with ex.progress_indicator("test"):
            pass
        assert capsys.readouterr().out == ""

    def test_start_line_each_attempt_with_retry_mark(self, executor, capsys):
        _session_does(executor, "completed", summary="UI 완료")
        executor._commit_step = MagicMock()
        executor._build_cmd = "test -f marker || { touch marker; exit 1; }"
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        starts = [l for l in capsys.readouterr().out.splitlines() if l.startswith("  ▶ ")]
        assert len(starts) == 2
        assert starts[0].endswith(": ui")
        assert starts[1].endswith(": ui [retry 2/3]")


class TestStepElapsedShown:
    """결과 줄의 소요 시간은 세션이 끝난 뒤 진행 표시기가 잰 값이다."""

    @pytest.fixture(autouse=True)
    def _indicator_measures_42s(self, monkeypatch):
        import contextlib, types

        @contextlib.contextmanager
        def fake(label):
            info = types.SimpleNamespace(elapsed=0.0)
            try:
                yield info
            finally:
                info.elapsed = 42.0  # 실제 표시기처럼 블록이 끝날 때 채운다

        monkeypatch.setattr(ex, "progress_indicator", fake)

    def test_completed_line(self, executor, capsys):
        _session_does(executor, "completed", summary="UI 완료")
        executor._commit_step = MagicMock()
        executor._build_cmd = "true"
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        assert "✓ Step 2: ui [42s]" in capsys.readouterr().out

    def test_blocked_line(self, executor, capsys):
        _session_does(executor, "blocked", blocked_reason="키 없음")
        executor._commit_state = MagicMock()
        assert _run_step(executor) == 2
        assert "⏸ Step 2: ui blocked [42s]" in capsys.readouterr().out

    def test_failed_line(self, executor, capsys):
        _session_does(executor, "completed", summary="UI 완료")
        executor._commit_state = MagicMock()
        executor._build_cmd = "exit 1"
        assert _run_step(executor) == 1
        assert f"failed after {ex.StepExecutor.MAX_RETRIES} attempts [42s]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self):
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index))

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# git 설정 (branch_prefix / commit_format)
# ---------------------------------------------------------------------------

class TestGitConfig:
    def _make(self, tmp_project, git_cfg=None):
        d = tmp_project / "phases" / "ABC-1234"
        if not d.exists():
            d.mkdir(parents=True)
        index = {"project": "T", "phase": "ABC-1234", "steps": []}
        if git_cfg is not None:
            index["git"] = git_cfg
        (d / "index.json").write_text(json.dumps(index, ensure_ascii=False))
        with patch.object(ex, "ROOT", tmp_project):
            return ex.StepExecutor("ABC-1234")

    def test_default_branch_name(self, tmp_project):
        inst = self._make(tmp_project)
        assert inst._branch_name() == "feature/ABC-1234"

    def test_default_commit_msg(self, tmp_project):
        inst = self._make(tmp_project)
        assert inst._commit_msg("주문 취소 추가") == "[ABC-1234] 주문 취소 추가"

    def test_custom_branch_prefix(self, tmp_project):
        inst = self._make(tmp_project, {"branch_prefix": "feat/"})
        assert inst._branch_name() == "feat/ABC-1234"

    def test_empty_branch_prefix(self, tmp_project):
        inst = self._make(tmp_project, {"branch_prefix": ""})
        assert inst._branch_name() == "ABC-1234"

    def test_custom_commit_format(self, tmp_project):
        inst = self._make(tmp_project, {"commit_format": "feat({ticket}): {message}"})
        assert inst._commit_msg("주문 취소 추가") == "feat(ABC-1234): 주문 취소 추가"

    def test_partial_git_block_falls_back(self, tmp_project):
        inst = self._make(tmp_project, {"branch_prefix": "hotfix/"})
        assert inst._branch_name() == "hotfix/ABC-1234"
        assert inst._commit_msg("수정") == "[ABC-1234] 수정"

    def test_invalid_commit_format_falls_back(self, tmp_project, capsys):
        inst = self._make(tmp_project, {"commit_format": "[{tickett}] {message}"})
        assert inst._commit_msg("수정") == "[ABC-1234] 수정"
        assert "WARN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# AC 빌드 게이트 — CLAUDE.md `## 명령어` 빌드 줄을 세션 뒤 직접 실행
# ---------------------------------------------------------------------------

import signal
import time


def _commands_md(block_lines, close_fence=True):
    fence = "```"
    tail = fence + "\n\n## 다음 절\n" if close_fence else "\n## 다음 절\n"
    return "# Rules\n\n## 명령어\n\n> 설명\n\n" + fence + "\n" + "\n".join(block_lines) + "\n" + tail


FILLED = [
    "sh ./mvnw spring-boot:run      # 실행 — 미검증(DB 필요)",
    "sh ./mvnw verify               # 빌드 — 검증됨(BUILD SUCCESS)",
    "sh ./mvnw test                 # 테스트",
    "sh ./mvnw -q test-compile      # 컴파일 — 검증됨",
]


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(cond, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _read_pid(path):
    try:
        return int(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class TestLoadBuildCommand:
    def _load(self, executor, tmp_project, block_lines, close_fence=True):
        (tmp_project / "CLAUDE.md").write_text(_commands_md(block_lines, close_fence))
        return executor._load_build_command()

    def _exits(self, executor, tmp_project, block_lines, close_fence=True):
        with pytest.raises(SystemExit) as e:
            self._load(executor, tmp_project, block_lines, close_fence)
        assert e.value.code == 1

    def test_returns_whole_second_line(self, executor, tmp_project):
        assert self._load(executor, tmp_project, FILLED) == FILLED[1].strip()

    def test_line_without_comment(self, executor, tmp_project):
        lines = ["./gradlew bootRun", "./gradlew build", "./gradlew test", "./gradlew compileJava"]
        assert self._load(executor, tmp_project, lines) == "./gradlew build"

    def test_template_placeholder_exits(self, executor, tmp_project):
        self._exits(executor, tmp_project, [
            "{실행}      # 예: ./gradlew bootRun",
            "{빌드}      # 예: ./gradlew build",
            "{테스트}    # 예: ./gradlew test",
            "{컴파일}    # 예: ./gradlew compileJava",
        ])

    def test_placeholder_with_flag_exits(self, executor, tmp_project):
        lines = list(FILLED)
        lines[1] = "{빌드} --offline   # 빌드"
        self._exits(executor, tmp_project, lines)

    def test_shell_group_is_not_placeholder(self, executor, tmp_project):
        lines = list(FILLED)
        lines[1] = "{ ./gradlew build; }   # 빌드"
        assert self._load(executor, tmp_project, lines) == lines[1]

    def test_unverified_anywhere_exits(self, executor, tmp_project):
        lines = list(FILLED)
        lines[1] = "true;# 미검증"
        self._exits(executor, tmp_project, lines)

    def test_unverified_inside_quotes_exits(self, executor, tmp_project):
        lines = list(FILLED)
        lines[1] = "./gradlew build -Pnote='a # 미검증'"
        self._exits(executor, tmp_project, lines)

    def test_json_braces_are_not_placeholder(self, executor, tmp_project):
        lines = list(FILLED)
        lines[1] = "./gradlew build -Pmetadata='{\"ci\":true}'   # 빌드"
        assert self._load(executor, tmp_project, lines) == lines[1]

    def test_dollar_brace_is_not_placeholder(self, executor, tmp_project):
        lines = list(FILLED)
        lines[1] = "${MVN:-sh ./mvnw} verify   # 빌드"
        assert self._load(executor, tmp_project, lines) == lines[1]

    def test_placeholder_in_comment_exits(self, executor, tmp_project):
        lines = list(FILLED)
        lines[1] = "./gradlew build   # {빌드} 아직 안 채움"
        self._exits(executor, tmp_project, lines)

    def test_comment_only_line_exits(self, executor, tmp_project, capsys):
        # `미검증`·placeholder 가 없는 주석 줄 — 다른 분기가 대신 잡지 못하게 한다.
        lines = list(FILLED)
        lines[1] = "# ./gradlew build"
        self._exits(executor, tmp_project, lines)
        assert "주석만" in capsys.readouterr().out

    def test_unverified_mark_exits(self, executor, tmp_project, capsys):
        lines = list(FILLED)
        lines[1] = "./mvnw verify   # 미검증 — 기존 실패 있음"
        self._exits(executor, tmp_project, lines)
        assert "미검증" in capsys.readouterr().out

    def test_tests_not_run_warns(self, executor, tmp_project, capsys):
        lines = list(FILLED)
        lines[1] = "sh ./mvnw verify   # 빌드 — 검증됨. 주의: 테스트 미실행"
        assert self._load(executor, tmp_project, lines) == lines[1]
        assert "테스트 미실행" in capsys.readouterr().out

    def test_missing_section_exits(self, executor, tmp_project):
        (tmp_project / "CLAUDE.md").write_text("# Rules\n- rule one\n")
        with pytest.raises(SystemExit) as e:
            executor._load_build_command()
        assert e.value.code == 1

    def test_wrong_line_count_exits(self, executor, tmp_project):
        self._exits(executor, tmp_project, FILLED[:3])

    def test_unclosed_fence_exits(self, executor, tmp_project):
        self._exits(executor, tmp_project, FILLED, close_fence=False)


class TestRunBuild:
    def test_pass_returns_none_and_runs(self, executor, tmp_project):
        executor._build_cmd = "touch built-marker"
        assert executor._run_build() is None
        assert (tmp_project / "built-marker").exists()

    def test_fail_returns_exit_code_and_tail(self, executor):
        executor._build_cmd = "for i in $(seq 1 100); do echo line$i; done; exit 3"
        err = executor._run_build()
        assert "exit 3" in err
        assert "line100" in err
        assert "line1\n" not in err

    def test_runs_in_project_root(self, executor, tmp_project):
        (tmp_project / "root-marker-p5").write_text("")
        executor._build_cmd = "test -f root-marker-p5"
        assert executor._run_build() is None

    def test_comment_and_quoted_hash_go_to_shell(self, executor, tmp_project):
        executor._build_cmd = "printf '%s\\n' \"a #b\" > quoted.out   # 빌드 — 주석은 셸이 버린다; exit 1"
        assert executor._run_build() is None
        assert (tmp_project / "quoted.out").read_text() == "a #b\n"

    def test_undecodable_output_does_not_crash(self, executor):
        executor._build_cmd = "printf '\\377\\376 broken'; exit 2"
        err = executor._run_build()
        assert "exit 2" in err

    def test_timeout_kills_child_processes(self, executor, tmp_project):
        executor._build_cmd = "sleep 20 & echo $! > child.pid; wait"
        pid = None
        try:
            with patch.object(ex.StepExecutor, "BUILD_TIMEOUT", 3):
                err = executor._run_build()
            pid = int((tmp_project / "child.pid").read_text())
            assert "시간 초과" in err
            assert _wait_until(lambda: not _pid_alive(pid)), f"자식 {pid} 가 남았다"
        finally:
            pid = pid or _read_pid(tmp_project / "child.pid")
            if pid:
                _kill_quietly(pid)

    def test_drain_after_timeout_is_bounded(self, executor, tmp_project):
        # 그룹 밖(새 세션)으로 나간 손자가 출력 파이프를 쥐고 있어도 정리 단계가 끝없이 기다리지 않는다.
        executor._build_cmd = (
            f"{sys.executable} -c \"import os,time; os.setsid(); "
            f"open('escaped.pid','w').write(str(os.getpid())); time.sleep(15)\" & wait"
        )
        pid = None
        try:
            t0 = time.monotonic()
            with patch.object(ex.StepExecutor, "BUILD_TIMEOUT", 3), \
                 patch.object(ex.StepExecutor, "DRAIN_TIMEOUT", 0.5):
                err = executor._run_build()
            elapsed = time.monotonic() - t0
            pid = int((tmp_project / "escaped.pid").read_text())
            assert elapsed < 10, f"{elapsed:.1f}초 걸렸다"
            assert "시간 초과" in err
            assert "출력 회수" in err
        finally:
            pid = pid or _read_pid(tmp_project / "escaped.pid")
            if pid:
                _kill_quietly(pid)

    def test_interrupt_kills_child_processes(self, tmp_path):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import execute as ex\n"
            "e = ex.StepExecutor.__new__(ex.StepExecutor)\n"
            "e._root = sys.argv[2]; e._build_cmd = 'sleep 20 & echo $! > child.pid; wait'\n"
            "e._run_build()\n"
        )
        p = subprocess.Popen(
            [sys.executable, "-c", code, str(Path(ex.__file__).parent), str(tmp_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pid_file = tmp_path / "child.pid"
        pid = None
        try:
            assert _wait_until(lambda: pid_file.exists() and pid_file.read_text().strip()), "빌드가 시작되지 않았다"
            pid = int(pid_file.read_text())
            p.send_signal(signal.SIGINT)
            p.wait(timeout=10)
            assert _wait_until(lambda: not _pid_alive(pid)), f"자식 {pid} 가 남았다"
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            pid = pid or _read_pid(pid_file)
            if pid:
                _kill_quietly(pid)


class TestRunOrder:
    def test_build_command_read_after_checkout(self, executor):
        order = []
        for name in ["_print_header", "_check_clean_tree", "_check_blockers", "_checkout_branch",
                     "_load_guardrails", "_ensure_created_at", "_execute_all_steps", "_finalize"]:
            setattr(executor, name, (lambda n: lambda *a, **k: order.append(n))(name))
        executor._load_build_command = lambda: order.append("_load_build_command") or "true"
        executor.run()
        assert order.index("_load_build_command") > order.index("_checkout_branch")
        # 트리 검사는 checkout 전(부작용 없이 멈춤), 차단 판정은 checkout 뒤(전환된 브랜치의 index).
        assert order.index("_check_clean_tree") < order.index("_checkout_branch") < order.index("_check_blockers")


class TestExecuteSingleStepBuildGate:
    """세션이 completed 를 적어도 빌드가 통과하기 전에는 completed 로 남기지 않는다."""

    def _session_writes(self, executor, status, **extra):
        calls = []

        def fake_invoke(step, preamble):
            calls.append(preamble)
            idx = json.loads(executor._index_file.read_text())
            for s in idx["steps"]:
                if s["step"] == step["step"]:
                    s["status"] = status
                    s.update(extra)
            executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
            return {}

        executor._invoke_claude = fake_invoke
        executor._commit_step = MagicMock()
        executor._commit_state = MagicMock()
        return calls

    def _step2(self, executor):
        idx = json.loads(executor._index_file.read_text())
        return next(s for s in idx["steps"] if s["step"] == 2)

    def test_build_passes_completes(self, executor, tmp_project):
        calls = self._session_writes(executor, "completed", summary="UI 완료")
        executor._build_cmd = "touch built-marker"
        assert executor._execute_single_step(self._step2(executor), "") is True
        assert len(calls) == 1
        assert (tmp_project / "built-marker").exists()
        assert self._step2(executor)["status"] == "completed"
        assert "completed_at" in self._step2(executor)
        executor._commit_step.assert_called_once()

    def test_build_fail_then_pass_retries_with_build_error(self, executor, tmp_project):
        calls = self._session_writes(executor, "completed", summary="UI 완료")
        executor._build_cmd = "test -f marker || { touch marker; echo BUILD-BOOM; exit 1; }"
        assert executor._execute_single_step(self._step2(executor), "") is True
        assert len(calls) == 2
        assert "BUILD-BOOM" in calls[1]
        executor._commit_step.assert_called_once()

    def test_build_fails_every_attempt_marks_error(self, executor):
        calls = self._session_writes(executor, "completed", summary="UI 완료")
        executor._build_cmd = "echo BUILD-BOOM; exit 1"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step(self._step2(executor), "")
        assert e.value.code == 1
        assert len(calls) == ex.StepExecutor.MAX_RETRIES
        s = self._step2(executor)
        assert s["status"] == "error"
        assert "BUILD-BOOM" in s["error_message"]
        assert f"[{ex.StepExecutor.MAX_RETRIES}회 시도 후 실패]" in s["error_message"]
        assert "summary" not in s
        assert executor._commit_step.call_count == 0
        assert executor._commit_state.call_count == 1

    def test_blocked_does_not_run_build(self, executor, tmp_project):
        self._session_writes(executor, "blocked", blocked_reason="API 키")
        executor._build_cmd = "touch build-ran"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step(self._step2(executor), "")
        assert e.value.code == 2
        assert not (tmp_project / "build-ran").exists()

    def test_session_error_does_not_run_build(self, executor, tmp_project):
        self._session_writes(executor, "error", error_message="컴파일 실패")
        executor._build_cmd = "touch build-ran"
        with pytest.raises(SystemExit):
            executor._execute_single_step(self._step2(executor), "")
        assert not (tmp_project / "build-ran").exists()

    @pytest.mark.parametrize("exc", [KeyboardInterrupt, OSError])
    def test_crash_during_build_leaves_no_completed(self, executor, exc):
        self._session_writes(executor, "completed", summary="UI 완료")
        executor._build_cmd = "true"

        def boom():
            raise exc("빌드 중 중단")

        executor._run_build = boom
        with pytest.raises(exc):
            executor._execute_single_step(self._step2(executor), "")
        assert self._step2(executor)["status"] == "pending"
        executor._commit_step.assert_not_called()

    def test_build_timeout_ends_step_without_retry(self, executor):
        calls = self._session_writes(executor, "completed", summary="UI 완료")
        executor._build_cmd = "sleep 20"
        with patch.object(ex.StepExecutor, "BUILD_TIMEOUT", 1):
            with pytest.raises(SystemExit) as e:
                executor._execute_single_step(self._step2(executor), "")
        assert e.value.code == 1
        assert len(calls) == 1
        s = self._step2(executor)
        assert s["status"] == "error"
        assert s["error_message"].startswith("AC 빌드 시간 초과(1초)")
        assert "summary" not in s
        assert executor._commit_step.call_count == 0
        assert executor._commit_state.call_count == 1

    def test_build_failure_clears_summary_before_retry(self, executor):
        seen = []
        calls = self._session_writes(executor, "completed", summary="UI 완료")
        inner = executor._invoke_claude

        def recording_invoke(step, preamble):
            seen.append(self._step2(executor).get("summary"))
            return inner(step, preamble)

        executor._invoke_claude = recording_invoke
        executor._build_cmd = "test -f marker || { touch marker; exit 1; }"
        assert executor._execute_single_step(self._step2(executor), "") is True
        assert seen == [None, None]


class TestUnverifiedCompleted:
    """completed_at 은 빌드 통과 뒤 실행기만 쓴다 — 없으면 검증되지 않은 completed 다."""

    def test_completed_without_completed_at_runs_again(self, executor):
        idx = json.loads(executor._index_file.read_text())
        idx["steps"][0]["completed_at"] = "2026-11-26T09:00:00+0900"
        idx["steps"][1]["status"] = "completed"
        idx["steps"][1].pop("completed_at", None)
        executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        ran = []

        def fake_single(step, guardrails):
            cur = json.loads(executor._index_file.read_text())
            s = next(x for x in cur["steps"] if x["step"] == step["step"])
            ran.append((step["step"], s["status"], s.get("summary")))
            s["status"] = "completed"
            s["completed_at"] = "2026-11-26T10:00:00+0900"
            executor._index_file.write_text(json.dumps(cur, ensure_ascii=False))
            return True

        executor._execute_single_step = fake_single
        executor._execute_all_steps("")
        assert [r[0] for r in ran] == [1, 2]
        assert ran[0][1] == "pending" and ran[0][2] is None


# ---------------------------------------------------------------------------
# 실패 경로 — 미검증 코드 커밋, 작업 트리 확인, 세션 시간 초과, 차단 판정
# ---------------------------------------------------------------------------

SUCCESS_MSG = "[ABC-1234] step 2 ui"


def _git_init(tmp_project, monkeypatch, *add_paths):
    """사용자 git 설정과 분리된 실제 저장소. add_paths 를 첫 커밋에 넣는다."""
    for k, v in {
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        monkeypatch.setenv(k, v)

    def g(*args):
        return subprocess.run(["git", *args], cwd=tmp_project, capture_output=True,
                              text=True, check=True).stdout

    g("init", "-q", "-b", "main")
    g("add", "--", *add_paths)
    g("commit", "-q", "-m", "init")
    return g


@pytest.fixture
def git_repo(tmp_project, phase_dir, monkeypatch):
    """픽스처 파일 전부를 첫 커밋에 넣은 실제 저장소."""
    return _git_init(tmp_project, monkeypatch, ".")


def _session_does(executor, status, write=None, stage=False, **extra):
    """세션 흉내: 코드 파일을 쓰고(선택, staged 까지) index 의 step 2 status 를 적는다."""
    calls = []

    def fake_invoke(step, preamble):
        calls.append(preamble)
        root = Path(executor._root)
        if write:
            p = root / write
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"attempt {len(calls)}\n")
            if stage:
                subprocess.run(["git", "add", write], cwd=root, check=True)
        idx = json.loads(executor._index_file.read_text())
        for s in idx["steps"]:
            if s["step"] == step["step"]:
                s["status"] = status
                s.update(extra)
        executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        return {}

    executor._invoke_claude = fake_invoke
    return calls


def _committed_files(g):
    return set(filter(None, g("log", "--name-only", "--format=").splitlines()))


def _head_step2(g):
    idx = json.loads(g("show", "HEAD:phases/ABC-1234/index.json"))
    return next(s for s in idx["steps"] if s["step"] == 2)


class TestFailureDoesNotCommitUnverifiedCode:
    """빌드가 통과하지 않은 코드는 성공 메시지 커밋에 들어가지 않는다."""

    def _fail(self, executor):
        executor._build_cmd = "exit 1"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")
        return e.value.code

    def test_final_failure_has_no_success_commit(self, executor, git_repo):
        _session_does(executor, "completed", write="src/Ui.java", summary="UI")
        assert self._fail(executor) == 1
        assert SUCCESS_MSG not in git_repo("log", "--format=%s").splitlines()

    def test_final_failure_leaves_code_uncommitted_and_commits_state(self, executor, git_repo):
        _session_does(executor, "completed", write="src/Ui.java", summary="UI")
        assert self._fail(executor) == 1
        assert "src/Ui.java" not in _committed_files(git_repo)
        assert (Path(executor._root) / "src/Ui.java").exists()
        assert _head_step2(git_repo)["status"] == "error"
        assert git_repo("log", "-1", "--format=%s").strip() == f"{SUCCESS_MSG} 실패 기록"

    def test_staged_code_stays_out_of_state_commit(self, executor, git_repo):
        _session_does(executor, "completed", write="src/Ui.java", stage=True, summary="UI")
        assert self._fail(executor) == 1
        assert "src/Ui.java" not in _committed_files(git_repo)
        assert _head_step2(git_repo)["status"] == "error"
        assert "src/Ui.java" in git_repo("diff", "--cached", "--name-only")

    def test_blocked_commits_state_only(self, executor, git_repo):
        _session_does(executor, "blocked", write="src/Ui.java", blocked_reason="API 키")
        executor._build_cmd = "true"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert e.value.code == 2
        assert "src/Ui.java" not in _committed_files(git_repo)
        assert _head_step2(git_repo)["status"] == "blocked"
        assert git_repo("log", "-1", "--format=%s").strip() == f"{SUCCESS_MSG} 중단 기록"

    def test_success_still_commits_code_with_step_message(self, executor, git_repo):
        _session_does(executor, "completed", write="src/Ui.java", summary="UI")
        executor._build_cmd = "true"
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        assert SUCCESS_MSG in git_repo("log", "--format=%s").splitlines()
        assert git_repo("status", "--porcelain") == ""

    def test_preamble_tells_session_not_to_commit(self, executor):
        p = executor._build_preamble("", "")
        assert "모든 변경사항을 커밋하라" not in p
        assert "커밋하지 마라" in p


class TestCleanTreeBeforeRun:
    """phases/ 밖에 커밋되지 않은 변경이 있으면 실행 전에 목록을 보이고 멈춘다."""

    STEPS = ["_print_header", "_check_blockers", "_checkout_branch", "_load_guardrails",
             "_ensure_created_at", "_execute_all_steps", "_finalize"]

    def _run(self, executor):
        order = []
        for name in self.STEPS:
            setattr(executor, name, (lambda n: lambda *a, **k: order.append(n))(name))
        executor._load_build_command = lambda: order.append("_load_build_command") or "true"
        code = None
        try:
            executor.run()
        except SystemExit as e:
            code = e.code
        return code, order

    def _stops(self, executor, capsys, *paths):
        code, order = self._run(executor)
        out = capsys.readouterr().out
        assert code == 1
        assert "_checkout_branch" not in order
        for p in paths:
            assert p in out
        return out

    def test_clean_tree_runs(self, executor, git_repo):
        code, order = self._run(executor)
        assert code is None and "_execute_all_steps" in order

    def test_modified_file_outside_phases_stops(self, executor, git_repo, capsys):
        (Path(executor._root) / "CLAUDE.md").write_text("changed")
        self._stops(executor, capsys, "CLAUDE.md")

    def test_staged_file_outside_phases_stops(self, executor, git_repo, capsys):
        (Path(executor._root) / "docs/arch.md").write_text("changed")
        git_repo("add", "docs/arch.md")
        self._stops(executor, capsys, "docs/arch.md")

    def test_untracked_file_in_new_dir_listed_by_path(self, executor, git_repo, capsys):
        p = Path(executor._root) / "src/main/New.java"
        p.parent.mkdir(parents=True)
        p.write_text("x")
        self._stops(executor, capsys, "src/main/New.java")

    def test_path_with_space_and_hangul_is_shown_unquoted(self, executor, git_repo, capsys):
        (Path(executor._root) / "docs/새 문서.md").write_text("x")
        self._stops(executor, capsys, "docs/새 문서.md")

    def test_rename_out_of_phases_stops(self, executor, git_repo, capsys):
        git_repo("mv", "phases/ABC-1234/step2.md", "moved.md")
        self._stops(executor, capsys, "moved.md")

    def test_rename_into_phases_stops_on_old_path(self, executor, git_repo, capsys):
        # rename 쌍을 한 항목으로 읽으면 원래 경로(phases/ 밖)가 깨져 보이거나 빠진다.
        git_repo("mv", "docs/arch.md", "phases/ABC-1234/arch.md")
        self._stops(executor, capsys, "docs/arch.md")

    def test_changes_inside_phases_only_run(self, executor, git_repo):
        (Path(executor._root) / "phases/ABC-1234/step3.md").write_text("new step")
        (Path(executor._root) / "phases/ABC-1234/step2.md").write_text("edited")
        code, order = self._run(executor)
        assert code is None and "_execute_all_steps" in order

    def test_not_a_git_repo_stops(self, executor, capsys):
        code, order = self._run(executor)
        assert code == 1
        assert "_checkout_branch" not in order


def _fake_claude(bin_dir, pid_file, calls_file, index_file):
    """index 에 completed 를 적고, 자식 하나를 띄운 뒤 매달리는 가짜 claude."""
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, subprocess, time
        open({str(calls_file)!r}, "a").write("x")
        p = {str(index_file)!r}
        idx = json.load(open(p))
        for s in idx["steps"]:
            if s["step"] == 2:
                s["status"] = "completed"; s["summary"] = "UI"
        json.dump(idx, open(p, "w"))
        child = subprocess.Popen(["sleep", "300"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        time.sleep(300)
        """))
    script.chmod(0o755)


class TestSessionTimeout:
    """세션 시간 초과는 traceback 없이, 재시도 없이 error 로 끝나고, 세션이 띄운 프로세스도 끝낸다."""

    def test_session_timeout_ends_step_as_error(self, executor, tmp_project, monkeypatch):
        bin_dir, pid_file, calls_file = tmp_project / "bin", tmp_project / "child.pid", tmp_project / "calls"
        _fake_claude(bin_dir, pid_file, calls_file, executor._index_file)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        executor._commit_state = MagicMock()
        executor._commit_step = MagicMock()
        executor._build_cmd = "touch build-ran"
        real_run = subprocess.run

        def short_run(*a, **k):  # 4b9a22f 는 subprocess.run(timeout=1800) — 시험에서는 1초로 줄인다.
            if "timeout" in k:
                k["timeout"] = 1
            return real_run(*a, **k)

        try:
            with patch("subprocess.run", short_run), \
                 patch.object(ex.StepExecutor, "SESSION_TIMEOUT", 1, create=True):
                with pytest.raises(SystemExit) as e:
                    executor._execute_single_step({"step": 2, "name": "ui"}, "")
            assert e.value.code == 1
            assert calls_file.read_text() == "x"
            assert not (tmp_project / "build-ran").exists()
            s = json.loads(executor._index_file.read_text())["steps"][2]
            assert s["status"] == "error"
            assert s["error_message"].startswith("세션 시간 초과(1초)")
            assert "summary" not in s
            executor._commit_state.assert_called_once()
            executor._commit_step.assert_not_called()
            child = _read_pid(pid_file)
            assert child is not None
            assert _wait_until(lambda: not _pid_alive(child), 5), "세션이 띄운 자식이 남았다"
        finally:
            pid = _read_pid(pid_file)
            if pid:
                _kill_quietly(pid)


class TestCheckBlockersAllSteps:
    """뒤쪽 step 이 completed 여도 앞쪽 error·blocked 를 본다."""

    def _make(self, tmp_project, steps):
        return TestCheckBlockers()._make_executor_with_steps(tmp_project, steps)

    def test_error_before_completed_exits_1(self, tmp_project):
        inst = self._make(tmp_project, [
            {"step": 0, "name": "a", "status": "error", "error_message": "x"},
            {"step": 1, "name": "b", "status": "completed"},
        ])
        with pytest.raises(SystemExit) as e:
            inst._check_blockers()
        assert e.value.code == 1

    def test_blocked_before_completed_exits_2(self, tmp_project):
        inst = self._make(tmp_project, [
            {"step": 0, "name": "a", "status": "blocked", "blocked_reason": "x"},
            {"step": 1, "name": "b", "status": "completed"},
            {"step": 2, "name": "c", "status": "pending"},
        ])
        with pytest.raises(SystemExit) as e:
            inst._check_blockers()
        assert e.value.code == 2

    def test_first_problem_in_step_order_wins(self, tmp_project):
        # 뒤에서부터 보면 step 1 의 error(exit 1)가 먼저 걸린다.
        inst = self._make(tmp_project, [
            {"step": 0, "name": "a", "status": "blocked", "blocked_reason": "x"},
            {"step": 1, "name": "b", "status": "error", "error_message": "y"},
        ])
        with pytest.raises(SystemExit) as e:
            inst._check_blockers()
        assert e.value.code == 2

    def test_error_on_switched_branch_is_seen(self, executor, git_repo, tmp_project, capsys):
        """main 에서 띄웠는데 feature 브랜치 index 에 error 가 있으면 step 을 돌리지 않고 멈춘다."""
        idx = json.loads(executor._index_file.read_text())
        for s in idx["steps"][:2]:
            s["completed_at"] = "2026-11-26T09:00:00+0900"  # 검증된 completed (재실행 대상 아님)
        executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        git_repo("commit", "-q", "-am", "step 0·1 완료")
        git_repo("checkout", "-q", "-b", "feature/ABC-1234")
        idx = json.loads(executor._index_file.read_text())
        idx["steps"][2].update(status="error", error_message="빌드 실패")
        executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        git_repo("commit", "-q", "-am", "step 2 실패 기록")
        git_repo("checkout", "-q", "main")

        executor._load_build_command = lambda: "true"
        executor._load_guardrails = lambda: ""
        executor._invoke_claude = MagicMock()
        with pytest.raises(SystemExit) as e:
            executor.run()
        assert e.value.code == 1
        executor._invoke_claude.assert_not_called()
        assert "completed_at" not in json.loads(executor._index_file.read_text())
        # checkout 실패로 멈춘 것이 아니라, 전환된 브랜치의 index 를 보고 멈췄다.
        assert git_repo("rev-parse", "--abbrev-ref", "HEAD").strip() == "feature/ABC-1234"
        assert "빌드 실패" in capsys.readouterr().out


class TestStateCommitWithIgnoredOutput:
    """상태 커밋은 무시 규칙(phases/**/step*-output.json)이 있어도·없어도, phases/ 가 미추적이어도 남는다."""

    IGNORE = "phases/**/step*-output.json\n"

    def _repo(self, tmp_project, monkeypatch, ignore):
        if ignore:
            (tmp_project / ".gitignore").write_text(self.IGNORE)
        paths = ["CLAUDE.md", "docs"] + ([".gitignore"] if ignore else [])
        return _git_init(tmp_project, monkeypatch, *paths)  # phases/ 는 한 번도 커밋하지 않는다

    def _session(self, executor, status, **extra):
        inner = _session_does(executor, status, write="src/Ui.java", **extra)
        fake = executor._invoke_claude

        def with_output(step, preamble):
            (executor._phase_dir / f"step{step['step']}-output.json").write_text("{}")
            return fake(step, preamble)

        executor._invoke_claude = with_output
        return inner

    def _check(self, g, tmp_project, status, title):
        assert g("log", "-1", "--format=%s").strip() == f"{SUCCESS_MSG} {title}"
        idx = json.loads(g("show", "HEAD:phases/ABC-1234/index.json"))
        assert idx["steps"][2]["status"] == status
        top = json.loads(g("show", "HEAD:phases/index.json"))
        assert next(p for p in top["phases"] if p["dir"] == "ABC-1234")["status"] == status
        committed = _committed_files(g)
        for f in ("src/Ui.java", "phases/ABC-1234/step2.md", "phases/ABC-1234/step2-output.json"):
            assert f not in committed
        assert (tmp_project / "src/Ui.java").exists()
        assert (tmp_project / "phases/ABC-1234/step2.md").exists()

    @pytest.mark.parametrize("ignore", [True, False], ids=["ignore-rule", "no-ignore-rule"])
    def test_final_failure_commits_both_indexes(self, executor, tmp_project, top_index, monkeypatch, ignore):
        g = self._repo(tmp_project, monkeypatch, ignore)
        self._session(executor, "completed", summary="UI")
        executor._build_cmd = "exit 1"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert e.value.code == 1
        self._check(g, tmp_project, "error", "실패 기록")

    def test_blocked_commits_both_indexes(self, executor, tmp_project, top_index, monkeypatch):
        g = self._repo(tmp_project, monkeypatch, True)
        self._session(executor, "blocked", blocked_reason="API 키")
        executor._build_cmd = "true"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert e.value.code == 2
        self._check(g, tmp_project, "blocked", "중단 기록")


class TestSessionTimeoutTruncatedIndex:
    """세션이 index.json 을 쓰다 시간 초과로 끝나도 실행기는 죽지 않고 error 를 기록한다."""

    def test_truncated_index_after_timeout_records_error(self, executor):
        def truncating_invoke(step, preamble):
            text = executor._index_file.read_text()
            executor._index_file.write_text(text[: len(text) // 2])
            return {"timedOut": True}

        executor._invoke_claude = truncating_invoke
        executor._commit_state = MagicMock()
        executor._commit_step = MagicMock()
        executor._build_cmd = "true"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert e.value.code == 1
        s = json.loads(executor._index_file.read_text())["steps"][2]
        assert s["status"] == "error"
        assert s["error_message"].startswith("세션 시간 초과")
        executor._commit_state.assert_called_once()
        executor._commit_step.assert_not_called()


# ---------------------------------------------------------------------------
# 세션 커밋 감지, 판정 기준 변경 경고
# ---------------------------------------------------------------------------

def _session_acts(executor, actions, status="completed", **extra):
    """세션 흉내: 시도마다 actions[i](root) 를 돌린 뒤(마지막 것을 반복) index 의 step 2 status 를 적는다.
    action 이 dict 를 돌려주면 그것을 _invoke_claude 반환값으로 쓴다(timedOut 등)."""
    calls = []

    def fake_invoke(step, preamble):
        calls.append(preamble)
        act = actions[min(len(calls), len(actions)) - 1]
        ret = act(Path(executor._root)) or {}
        if executor._index_file.exists():
            idx = json.loads(executor._index_file.read_text())
            for s in idx["steps"]:
                if s["step"] == step["step"]:
                    s["status"] = status
                    s.update(extra)
            executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        return ret

    executor._invoke_claude = fake_invoke
    return calls


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _seed(g, root, files):
    for rel, text in files.items():
        _write(root, rel, text)
    g("add", "-A")
    g("commit", "-q", "-m", "seed")


def _step2_now(executor):
    return json.loads(executor._index_file.read_text())["steps"][2]


def _run_step(executor):
    with pytest.raises(SystemExit) as e:
        executor._execute_single_step({"step": 2, "name": "ui"}, "")
    return e.value.code


class TestSessionCommitDetected:
    """세션이 커밋하거나 브랜치를 바꾸면 재시도 없이 error. 세션 커밋은 되돌리지 않는다."""

    def _commit_in_session(self, g):
        def act(root):
            _write(root, "src/A.java", "class A {}\n")
            g("add", "src/A.java")
            g("commit", "-q", "-m", "session commit")
        return act

    def test_commit_is_error_without_retry_or_build(self, executor, git_repo, tmp_project):
        before = git_repo("rev-parse", "HEAD").strip()
        calls = _session_acts(executor, [self._commit_in_session(git_repo)], "completed", summary="UI")
        executor._build_cmd = "touch build-ran"
        assert _run_step(executor) == 1
        assert len(calls) == 1
        assert not (tmp_project / "build-ran").exists()
        s = _head_step2(git_repo)
        assert s["status"] == "error"
        assert s["error_message"].startswith("세션이 커밋함")
        assert before in s["error_message"]
        assert "summary" not in s
        subjects = git_repo("log", "--format=%s").splitlines()
        assert subjects[0] == f"{SUCCESS_MSG} 실패 기록"
        assert "session commit" in subjects  # 되돌리지 않는다

    def test_blocked_and_commit_is_error(self, executor, git_repo):
        _session_acts(executor, [self._commit_in_session(git_repo)], "blocked", blocked_reason="API 키")
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        s = _head_step2(git_repo)
        assert s["status"] == "error"
        assert "세션이 커밋함" in s["error_message"] and "API 키" in s["error_message"]

    def test_timeout_and_commit_reports_both(self, executor, git_repo):
        commit = self._commit_in_session(git_repo)

        def act(root):
            commit(root)
            return {"timedOut": True}

        _session_acts(executor, [act], "completed")
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        msg = _head_step2(git_repo)["error_message"]
        assert "세션 시간 초과" in msg and "세션이 커밋함" in msg

    def test_amend_is_detected_as_head_change(self, executor, git_repo):
        _session_acts(executor, [lambda root: git_repo("commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")])
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        assert "HEAD 가 바뀜" in _head_step2(git_repo)["error_message"]

    def test_branch_switch_is_error_and_records_nothing(self, executor, git_repo, capsys):
        _session_acts(executor, [lambda root: git_repo("switch", "-q", "-c", "other")])
        executor._build_cmd = "touch build-ran"
        head_other = git_repo("rev-parse", "HEAD").strip()
        assert _run_step(executor) == 1
        assert "세션이 브랜치를 바꿈" in capsys.readouterr().out
        assert git_repo("rev-parse", "--abbrev-ref", "HEAD").strip() == "other"
        assert git_repo("rev-parse", "HEAD").strip() == head_other  # 상태 커밋을 하지 않았다

    def test_switch_to_branch_without_phase_dir_does_not_crash(self, executor, git_repo, capsys):
        _session_acts(executor, [lambda root: git_repo("switch", "-q", "--orphan", "empty")])
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        assert "세션이 브랜치를 바꿈" in capsys.readouterr().out
        assert not executor._index_file.exists()  # 엉뚱한 브랜치 트리에 phases 파일을 만들지 않았다

    def test_recovery_rerun_continues_on_session_commit(self, executor, git_repo):
        """복구: error 뒤 pending 으로 바꿔 재실행하면 트리가 깨끗해 트리 검사를 지나고, 세션 커밋 위에서 이어 돈다."""
        _session_acts(executor, [self._commit_in_session(git_repo)], "completed", summary="UI")
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        session_commit = git_repo("rev-parse", "HEAD~1").strip()

        idx = json.loads(executor._index_file.read_text())
        idx["steps"][2]["status"] = "pending"
        idx["steps"][2].pop("error_message", None)
        executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        executor._check_clean_tree()  # phases/ 밖 변경이 없어 멈추지 않는다

        _session_acts(executor, [lambda root: _write(root, "src/B.java", "class B {}\n")], "completed", summary="UI")
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        git_repo("merge-base", "--is-ancestor", session_commit, "HEAD")  # check=True — 조상이 아니면 실패


TEST_SRC = "src/test/java/app/FooTest.java"
FOO_TEST = "class FooTest {\n    @Test\n    void a() {}\n\n    @Test\n    void b() {}\n}\n"
POM = "<project>\n  <properties>\n    <java.version>17</java.version>\n  </properties>\n</project>\n"
GRADLE = "plugins { id 'java' }\n\ndependencies {\n}\n"


class TestCriteriaWarnings:
    """판정 기준을 바꾸는 변경을 step 항목 criteria_warnings 에 적는다. 완료 판정은 그대로."""

    @pytest.fixture
    def seeded(self, executor, git_repo, tmp_project):
        _seed(git_repo, tmp_project, {TEST_SRC: FOO_TEST, "pom.xml": POM, "build.gradle": GRADLE})
        executor._build_cmd = "true"
        return git_repo

    def _complete(self, executor, *actions):
        _session_acts(executor, list(actions), "completed", summary="UI")
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        s = _step2_now(executor)
        assert s["status"] == "completed"
        return s.get("criteria_warnings")

    def test_deleted_test_file_warns(self, executor, seeded, tmp_project, capsys):
        w = self._complete(executor, lambda root: (root / TEST_SRC).unlink(missing_ok=True))
        assert w and any("테스트 파일 삭제" in x and TEST_SRC in x for x in w)
        assert "판정 기준 변경" in capsys.readouterr().out

    def test_deleted_hangul_test_file_shown_unquoted(self, executor, seeded, tmp_project):
        rel = "src/test/java/app/한글Test.java"
        _seed(seeded, tmp_project, {rel: FOO_TEST.replace("FooTest", "HanTest")})
        w = self._complete(executor, lambda root: (root / rel).unlink())
        assert w and any(rel in x for x in w)

    def test_renamed_test_file_does_not_warn(self, executor, seeded, tmp_project):
        def act(root):
            (root / TEST_SRC).rename(root / "src/test/java/app/BarTest.java")
        assert self._complete(executor, act) is None

    @pytest.mark.parametrize("line", ["    @Disabled", "    @Disabled(\"flaky\")", "    @Ignore",
                                      "    @DisabledOnOs(OS.WINDOWS)", "    @Test @Disabled"])
    def test_added_disable_annotation_warns(self, executor, seeded, tmp_project, line):
        new = FOO_TEST.replace("    @Test\n    void a()", f"{line}\n    @Test\n    void a()")
        w = self._complete(executor, lambda root: _write(root, TEST_SRC, new))
        assert w and any("테스트 비활성화" in x and TEST_SRC in x for x in w)

    def test_new_test_file_with_disabled_warns(self, executor, seeded, tmp_project):
        rel = "src/test/java/app/NewTest.java"
        w = self._complete(executor, lambda root: _write(root, rel, "class NewTest {\n  @Disabled\n  @Test void x() {}\n}\n"))
        assert w and any(rel in x for x in w)

    @pytest.mark.parametrize("line", ["    // @Disabled", "    /* @Disabled */", "     * @Disabled 는 쓰지 않는다"])
    def test_commented_annotation_does_not_warn(self, executor, seeded, tmp_project, line):
        new = FOO_TEST.replace("    @Test\n    void a()", f"{line}\n    @Test\n    void a()")
        assert self._complete(executor, lambda root: _write(root, TEST_SRC, new)) is None

    def test_moved_annotation_does_not_warn(self, executor, seeded, tmp_project):
        _seed(seeded, tmp_project, {TEST_SRC: FOO_TEST.replace("    @Test\n    void a()", "    @Disabled\n    @Test\n    void a()")})
        moved = FOO_TEST.replace("    @Test\n    void b()", "    @Disabled\n    @Test\n    void b()")
        assert self._complete(executor, lambda root: _write(root, TEST_SRC, moved)) is None

    def test_annotation_outside_test_sources_does_not_warn(self, executor, seeded, tmp_project):
        assert self._complete(executor, lambda root: _write(root, "src/main/java/app/Entity.java",
                                                            "class Entity {\n  @Ignore\n  int x;\n}\n")) is None

    @pytest.mark.parametrize("rel,old,new", [
        ("pom.xml", "<java.version>17</java.version>", "<java.version>17</java.version>\n    <skipTests>true</skipTests>"),
        ("pom.xml", "<java.version>17</java.version>", "<java.version>17</java.version>\n    <maven.test.skip>true</maven.test.skip>"),
        ("pom.xml", "<java.version>17</java.version>", "<java.version>17</java.version>\n    <testFailureIgnore>true</testFailureIgnore>"),
        ("build.gradle", "dependencies {", "test { enabled = false }\n\ndependencies {"),
        ("build.gradle", "dependencies {", "test {\n    ignoreFailures = true\n}\n\ndependencies {"),
        ("build.gradle", "dependencies {", "tasks.named('build') { dependsOn 'assemble' } // -x test\n\ndependencies {"),
    ])
    def test_build_file_test_skip_warns(self, executor, seeded, tmp_project, rel, old, new):
        text = (tmp_project / rel).read_text().replace(old, new)
        w = self._complete(executor, lambda root: _write(root, rel, text))
        assert w and any("빌드 파일" in x and rel in x for x in w)

    def test_skip_tests_false_does_not_warn(self, executor, seeded, tmp_project):
        text = POM.replace("<java.version>17</java.version>", "<java.version>17</java.version>\n    <skipTests>false</skipTests>")
        assert self._complete(executor, lambda root: _write(root, "pom.xml", text)) is None

    def test_build_file_unrelated_change_does_not_warn(self, executor, seeded, tmp_project):
        text = GRADLE.replace("dependencies {\n", "dependencies {\n    implementation 'x:y:1'\n")
        assert self._complete(executor, lambda root: _write(root, "build.gradle", text)) is None

    @pytest.mark.parametrize("rel", ["CLAUDE.md", ".claude/settings.json", "sub/CLAUDE.md"])
    def test_rule_file_change_warns(self, executor, seeded, tmp_project, rel):
        w = self._complete(executor, lambda root: _write(root, rel, "changed\n"))
        assert w and any("규칙 파일 변경" in x and rel in x for x in w)

    def test_no_change_has_no_field_even_if_session_planted_one(self, executor, seeded):
        _session_acts(executor, [lambda root: None], "completed", summary="UI", criteria_warnings=["심은 값"])
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        assert "criteria_warnings" not in _step2_now(executor)

    def test_retry_counts_inherited_change_once(self, executor, seeded, tmp_project):
        executor._build_cmd = "test -f marker || { touch marker; exit 1; }"
        w = self._complete(executor, lambda root: (root / TEST_SRC).unlink(missing_ok=True), lambda root: None)
        assert w is not None and len([x for x in w if TEST_SRC in x]) == 1

    def test_retry_that_restores_test_clears_field(self, executor, seeded, tmp_project):
        executor._build_cmd = "test -f marker || { touch marker; exit 1; }"
        w = self._complete(executor, lambda root: (root / TEST_SRC).unlink(missing_ok=True),
                           lambda root: _write(root, TEST_SRC, FOO_TEST))
        assert w is None

    def test_warning_recorded_on_final_failure_too(self, executor, seeded, tmp_project):
        _session_acts(executor, [lambda root: (root / TEST_SRC).unlink(missing_ok=True)], "completed")
        executor._build_cmd = "exit 1"
        assert _run_step(executor) == 1
        s = _head_step2(seeded)
        assert s["status"] == "error" and s.get("criteria_warnings")

    def test_scan_leaves_real_index_and_tree_alone(self, executor, seeded, tmp_project):
        base = seeded("rev-parse", "HEAD").strip()
        (tmp_project / TEST_SRC).unlink()
        _write(tmp_project, "src/main/java/app/Staged.java", "class Staged {}\n")
        seeded("add", "src/main/java/app/Staged.java")
        _write(tmp_project, "src/main/java/app/Untracked.java", "class U {}\n")
        cached, status = seeded("diff", "--cached", "--name-only"), seeded("status", "--porcelain", "-uall")
        w = executor._scan_criteria(base)
        assert any(TEST_SRC in x for x in w)
        assert seeded("diff", "--cached", "--name-only") == cached
        assert seeded("status", "--porcelain", "-uall") == status

    def test_scan_outside_git_returns_empty(self, executor):
        assert executor._scan_criteria(None) == []


class TestSessionCheckEdgeCases:
    """코덱스 검수 반영: 깨진 index·임시 파일 오류·빌드가 HEAD 를 바꾼 경우."""

    def test_violation_with_broken_index_still_records_error(self, executor, git_repo):
        def act(root):
            _write(root, "src/A.java", "class A {}\n")
            git_repo("add", "src/A.java")
            git_repo("commit", "-q", "-m", "session commit")
            executor._index_file.write_text('{"steps": null}')

        executor._invoke_claude = lambda step, preamble: act(Path(executor._root)) or {}
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        s = _head_step2(git_repo)
        assert s["status"] == "error" and "세션이 커밋함" in s["error_message"]

    def test_scan_temp_file_error_does_not_block(self, executor, git_repo, capsys):
        with patch("tempfile.mkstemp", side_effect=OSError("디스크 가득")):
            assert executor._scan_criteria(git_repo("rev-parse", "HEAD").strip()) == []
        assert "판정 기준 검사 실패" in capsys.readouterr().out

    def test_head_moved_by_build_is_not_blamed_on_next_session(self, executor, git_repo):
        # 첫 빌드가 (잘못 만든 빌드 스크립트로) 커밋하고 실패 → 다음 시도의 세션은 커밋하지 않았다.
        executor._build_cmd = ("test -f marker || { touch marker; git commit -q --allow-empty -m build-commit; exit 1; }")
        _session_acts(executor, [lambda root: None], "completed", summary="UI")
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True


class TestSessionCheckFailures:
    """관찰용 검사의 예외·HEAD 삭제·브랜치 전환 뒤 출력 저장이 본 흐름을 끊거나 다른 브랜치를 건드리지 않는다."""

    @pytest.mark.parametrize("exc", [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        OSError("임시 index 를 지우지 못함"),
    ], ids=["unicode", "oserror"])
    def test_scan_exception_does_not_block_completion(self, executor, exc, capsys):
        _session_acts(executor, [lambda root: None], "completed", summary="UI")
        executor._commit_step = MagicMock()
        executor._scan_criteria = MagicMock(side_effect=exc)
        executor._build_cmd = "true"
        assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        assert _step2_now(executor)["status"] == "completed"
        assert "판정 기준 검사 실패" in capsys.readouterr().out

    def test_scan_exception_does_not_block_failure_record(self, executor):
        _session_acts(executor, [lambda root: None], "completed", summary="UI")
        executor._commit_state = MagicMock()
        executor._scan_criteria = MagicMock(side_effect=OSError("임시 index 를 지우지 못함"))
        executor._build_cmd = "exit 1"
        assert _run_step(executor) == 1
        assert _step2_now(executor)["status"] == "error"
        executor._commit_state.assert_called_once()

    def test_head_deleted_on_same_branch_is_violation(self, executor):
        msg = executor._session_git_violation(("a" * 40, "main"), (None, "main"))
        assert msg is not None and "HEAD 가 사라짐" in msg

    def test_head_deleted_in_session_records_error(self, executor, git_repo):
        _session_acts(executor, [lambda root: git_repo("update-ref", "-d", "refs/heads/main")])
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        s = _step2_now(executor)
        assert s["status"] == "error" and "HEAD 가 사라짐" in s["error_message"]

    def test_branch_switch_does_not_write_output_on_other_branch(self, executor, git_repo, tmp_project, monkeypatch, capsys):
        bin_dir = tmp_project / "bin"
        bin_dir.mkdir()
        script = bin_dir / "claude"
        script.write_text(f"#!{sys.executable}\nimport subprocess\n"
                          f"subprocess.run(['git', 'switch', '-q', '-c', 'other'], cwd={str(tmp_project)!r}, check=True)\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        executor._build_cmd = "true"
        assert _run_step(executor) == 1
        assert git_repo("rev-parse", "--abbrev-ref", "HEAD").strip() == "other"
        assert not (executor._phase_dir / "step2-output.json").exists()
        assert "세션이 브랜치를 바꿈" in capsys.readouterr().out


class TestSessionOutputNotCommitted:
    """세션 출력 파일(stepN-output.json)은 무시 규칙이 있든 없든 실행기가 만드는 커밋에 들어가지 않는다."""

    IGNORE = "phases/**/step*-output.json\n"

    def _repo(self, tmp_project, monkeypatch, ignore):
        if ignore:
            (tmp_project / ".gitignore").write_text(self.IGNORE)
        return _git_init(tmp_project, monkeypatch, ".")

    def _step2_done(self, executor, tmp_project, g):
        """step 2 를 마친 직후 모습: 코드·index 변경, 앞 step 과 이번 step 의 출력 파일, 세션이 해 둔 git add -A."""
        _write(tmp_project, "src/Ui.java", "class Ui {}\n")
        (executor._phase_dir / "step1-output.json").write_text("{}")
        (executor._phase_dir / "step2-output.json").write_text("{}")
        other = tmp_project / "phases" / "ABC-5678"  # 무시 규칙 없는 저장소에서 앞서 끝난 다른 phase
        other.mkdir()
        (other / "step0-output.json").write_text("{}")
        idx = json.loads(executor._index_file.read_text())
        idx["steps"][2]["status"] = "completed"
        executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        g("add", "-A")

    @staticmethod
    def _subjects(g):
        return g("log", "--format=%s").splitlines()

    @pytest.mark.parametrize("ignore", [True, False], ids=["ignore-rule", "no-ignore-rule"])
    def test_step_commits(self, executor, tmp_project, monkeypatch, ignore):
        g = self._repo(tmp_project, monkeypatch, ignore)
        self._step2_done(executor, tmp_project, g)
        executor._commit_step(2, "ui")
        subjects = self._subjects(g)
        assert SUCCESS_MSG in subjects
        assert "[ABC-1234] step 2 산출물 기록" in subjects
        committed = _committed_files(g)
        assert "src/Ui.java" in committed
        assert "phases/ABC-1234/index.json" in committed
        assert "phases/ABC-1234/step1-output.json" not in committed
        assert "phases/ABC-1234/step2-output.json" not in committed
        assert "phases/ABC-5678/step0-output.json" not in committed
        assert (executor._phase_dir / "step2-output.json").exists()

    @pytest.mark.parametrize("ignore", [True, False], ids=["ignore-rule", "no-ignore-rule"])
    def test_phase_done_commit(self, executor, tmp_project, monkeypatch, ignore):
        g = self._repo(tmp_project, monkeypatch, ignore)
        self._step2_done(executor, tmp_project, g)
        executor._finalize()
        assert self._subjects(g)[0] == "[ABC-1234] phase 완료"
        committed = _committed_files(g)
        assert "src/Ui.java" in committed
        assert "phases/ABC-1234/step1-output.json" not in committed
        assert "phases/ABC-1234/step2-output.json" not in committed
        assert "phases/ABC-5678/step0-output.json" not in committed


DEFAULT_TOOLS = ["Bash(./gradlew *)", "Bash(./mvnw *)"]


def _tools_in(cmd):
    i = cmd.index("--allowedTools")
    j = cmd.index("--output-format")
    return cmd[i + 1:j]


def _set_index(executor, **fields):
    idx = json.loads(executor._index_file.read_text())
    idx.update(fields)
    executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))


def _denials_json(*items):
    return json.dumps({"type": "result", "subtype": "success", "permission_denials": [
        {"tool_name": n, "tool_input": i} for n, i in items]})


_REAL_POPEN = subprocess.Popen


def _popen_session(executor, stdout, argvs, mutate=None):
    """claude 호출만 흉내: argv 를 적고, index 의 step 2 를 completed 로 적은 뒤 stdout 을 돌려준다(git 등은 실제로 돈다)."""
    def fake(cmd, **kw):
        if cmd[0] != "claude":
            return _REAL_POPEN(cmd, **kw)
        argvs.append(cmd)
        idx = json.loads(executor._index_file.read_text())
        idx["steps"][2]["status"] = "completed"
        idx["steps"][2]["summary"] = "UI"
        if mutate:
            mutate(idx)
        executor._index_file.write_text(json.dumps(idx, ensure_ascii=False))
        return _fake_popen(stdout)
    return fake


class TestSessionPermissions:
    """세션은 편집 자동 승인 모드 + 승인 창 없음 + 빌드 래퍼 허용 목록으로 뜨고, phase 가 목록을 늘릴 수 있다."""

    def test_default_argv(self, executor):
        with patch("subprocess.Popen", return_value=_fake_popen()) as m:
            executor._invoke_claude({"step": 2, "name": "ui"}, "PRE\n")
        cmd = m.call_args[0][0]
        assert cmd[:7] == ["claude", "-p", "--permission-mode", "acceptEdits",
                           "--permission-prompts", "none", "--allowedTools"]
        assert _tools_in(cmd) == DEFAULT_TOOLS
        assert cmd[-3:-1] == ["--output-format", "json"]
        assert cmd[-1].startswith("PRE\n")

    def test_phase_tools_appended(self, executor):
        _set_index(executor, allowed_tools=["Bash(docker compose *)"])
        executor._allowed_tools = executor._load_allowed_tools()
        with patch("subprocess.Popen", return_value=_fake_popen()) as m:
            executor._invoke_claude({"step": 2, "name": "ui"}, "PRE\n")
        assert _tools_in(m.call_args[0][0]) == DEFAULT_TOOLS + ["Bash(docker compose *)"]

    def test_no_field_gives_defaults(self, executor):
        assert executor._load_allowed_tools() == DEFAULT_TOOLS

    @pytest.mark.parametrize("bad", ["Bash(x)", ["Bash(x)", 3], {"a": 1}, None, 5])
    def test_not_string_array_stops(self, executor, capsys, bad):
        _set_index(executor, allowed_tools=bad)
        with pytest.raises(SystemExit) as e:
            executor._load_allowed_tools()
        assert e.value.code == 1
        assert "ERROR" in capsys.readouterr().out

    @pytest.mark.parametrize("bad", ["", "  ", "--dangerously-skip-permissions", " -x"])
    def test_option_like_or_empty_entry_stops(self, executor, capsys, bad):
        _set_index(executor, allowed_tools=["Bash(make *)", bad])
        with pytest.raises(SystemExit) as e:
            executor._load_allowed_tools()
        assert e.value.code == 1
        assert "ERROR" in capsys.readouterr().out

    def test_run_loads_after_checkout(self, executor):
        order = []
        for name in ["_print_header", "_check_clean_tree", "_check_blockers", "_checkout_branch",
                     "_load_guardrails", "_ensure_created_at", "_execute_all_steps", "_finalize"]:
            setattr(executor, name, (lambda n: lambda *a, **k: order.append(n))(name))
        executor._load_build_command = lambda: "true"
        loaded = DEFAULT_TOOLS + ["Bash(make *)"]
        executor._load_allowed_tools = lambda: order.append("_load_allowed_tools") or loaded
        executor.run()
        assert executor._allowed_tools is loaded
        assert order.index("_checkout_branch") < order.index("_load_allowed_tools") < order.index("_execute_all_steps")
        # 잘못된 목록이 blocked(exit 2) 판정을 가리지 않게 차단 판정 뒤에 읽는다.
        assert order.index("_check_blockers") < order.index("_load_allowed_tools")

    def test_session_cannot_widen_its_own_list(self, executor):
        argvs = []
        plant = lambda idx: idx.update(allowed_tools=["Bash(*)"])
        executor._commit_step = MagicMock()
        executor._build_cmd = "test -f marker || { touch marker; exit 1; }"
        with patch("subprocess.Popen", side_effect=_popen_session(executor, _denials_json(), argvs, plant)):
            assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        assert len(argvs) == 2
        assert all(_tools_in(c) == DEFAULT_TOOLS for c in argvs)

    def test_rule_against_workarounds_in_prompt(self, executor):
        pre = executor._build_preamble("G", "")
        assert "우회하지 마라" in pre
        assert "blocked_reason" in pre.split("우회하지 마라")[1]


class TestPermissionDenialsRecorded:
    """거부 내역은 화면과 step 필드에 남고, 판정은 바뀌지 않는다."""

    def _run(self, executor, stdout, mutate=None):
        argvs = []
        executor._commit_step = MagicMock()
        executor._build_cmd = "true"
        with patch("subprocess.Popen", side_effect=_popen_session(executor, stdout, argvs, mutate)):
            assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        return json.loads(executor._index_file.read_text())["steps"][2]

    def test_denials_shown_and_stored(self, executor, capsys):
        s = self._run(executor, _denials_json(
            ("Bash", {"command": "git commit -m x"}),
            ("Write", {"file_path": "/elsewhere/x.txt", "content": "FILE-BODY-NOT-STORED"}),
        ))
        out = capsys.readouterr().out
        assert "⚠ 권한 거부 2건 — Bash: git commit -m x; Write: /elsewhere/x.txt" in out
        assert s["status"] == "completed"
        assert s["permission_denials"] == ["Bash: git commit -m x", "Write: /elsewhere/x.txt"]
        assert "FILE-BODY-NOT-STORED" not in executor._index_file.read_text()

    def test_odd_items_do_not_break(self, executor):
        s = self._run(executor, _denials_json(("Bash", {"command": 3}), ("WebFetch", {"url": "https://example.invalid/a"}),
                                              ("Task", "not-a-dict")))
        assert s["permission_denials"] == ["Bash", "WebFetch: https://example.invalid/a", "Task"]

    def test_blocked_keeps_denials_and_exits_2(self, executor):
        executor._commit_state = MagicMock()
        def mutate(idx):
            idx["steps"][2]["status"] = "blocked"
            idx["steps"][2]["blocked_reason"] = "git push 거부됨"
        with patch("subprocess.Popen", side_effect=_popen_session(
                executor, _denials_json(("Bash", {"command": "git push"})), [], mutate)):
            with pytest.raises(SystemExit) as e:
                executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert e.value.code == 2
        s = json.loads(executor._index_file.read_text())["steps"][2]
        assert s["permission_denials"] == ["Bash: git push"]

    def test_retry_replaces_previous_attempt(self, executor, capsys):
        outs = iter([_denials_json(("Bash", {"command": "make clean"})), _denials_json()])
        argvs = []
        def fake(cmd, **kw):
            if cmd[0] != "claude":
                return _REAL_POPEN(cmd, **kw)
            return _popen_session(executor, next(outs), argvs)(cmd, **kw)
        executor._commit_step = MagicMock()
        executor._build_cmd = "test -f marker || { touch marker; exit 1; }"
        with patch("subprocess.Popen", side_effect=fake):
            assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        assert len(argvs) == 2
        assert "⚠ 권한 거부 1건 — Bash: make clean" in capsys.readouterr().out  # 1회차 거부는 화면에 남는다
        assert "permission_denials" not in json.loads(executor._index_file.read_text())["steps"][2]

    def test_lone_surrogate_is_written(self, executor):
        # JSON 이스케이프된 단독 서로게이트는 디코딩하면 UTF-8 로 쓸 수 없는 문자가 된다.
        stdout = '{"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "echo \\ud800 x"}}]}'
        s = self._run(executor, stdout)
        assert s["status"] == "completed"
        assert s["permission_denials"][0].startswith("Bash: echo ")
        assert s["permission_denials"][0].encode("utf-8")
        out = json.loads((executor._phase_dir / "step2-output.json").read_text(encoding="utf-8"))
        assert out["permissionDenials"] == s["permission_denials"]

    def test_unexpected_error_warns_and_continues(self, executor, capsys):
        executor.DENIAL_ITEM_MAX = None  # 요약 중 예상 밖 예외(TypeError)를 낸다
        s = self._run(executor, _denials_json(("Bash", {"command": "git push"})))
        assert "WARN: 권한 거부 기록을 읽지 못했다" in capsys.readouterr().out
        assert s["status"] == "completed"
        assert "permission_denials" not in s

    def test_long_item_is_cut(self, executor):
        s = self._run(executor, _denials_json(("Bash", {"command": "echo " + "a" * 500})))
        assert len(s["permission_denials"][0]) <= 120

    def test_none_removes_planted_field(self, executor, capsys):
        plant = lambda idx: idx["steps"][2].update(permission_denials=["심은 값"])
        s = self._run(executor, _denials_json(), plant)
        assert "permission_denials" not in s
        assert "권한 거부" not in capsys.readouterr().out

    @pytest.mark.parametrize("stdout", ["not json", "{}", '{"permission_denials": "x"}', '[1]'],
                             ids=["not-json", "no-key", "not-list", "not-object"])
    def test_unreadable_output_warns_and_continues(self, executor, capsys, stdout):
        s = self._run(executor, stdout)
        assert "WARN: 권한 거부 기록을 읽지 못했다" in capsys.readouterr().out
        assert s["status"] == "completed"
        assert "permission_denials" not in s

    def test_timeout_does_not_parse(self, executor, capsys):
        proc = _fake_popen("")
        proc.communicate.side_effect = [subprocess.TimeoutExpired("claude", 1), ("", "")]
        with patch("subprocess.Popen", return_value=proc), patch.object(ex.StepExecutor, "_kill_group"):
            out = executor._invoke_claude({"step": 2, "name": "ui"}, "PRE\n")
        assert out["timedOut"] is True
        assert "권한 거부 기록" not in capsys.readouterr().out


class TestOldClaudeCli:
    """호출 인자를 모르는 Claude Code 는 재시도 없이 멈춘다."""

    @staticmethod
    def _claude_failing_with(tmp_path, monkeypatch, stderr_line):
        """rc 1·빈 stdout·stderr 한 줄을 내는 가짜 claude. 호출 횟수 파일을 돌려준다."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        calls = tmp_path / "calls"
        script = bin_dir / "claude"
        script.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            open({str(calls)!r}, "a").write("x")
            sys.stderr.write({stderr_line + chr(10)!r})
            sys.exit(1)
            """))
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return calls

    def test_other_programs_unknown_option_is_not_old_cli(self, executor, tmp_path, monkeypatch, capsys):
        calls = self._claude_failing_with(tmp_path, monkeypatch, "helper: unknown option 'x'")
        executor._commit_state = MagicMock()
        executor._build_cmd = "true"
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert e.value.code == 1
        assert calls.read_text() == "x" * ex.StepExecutor.MAX_RETRIES  # 기존 재시도 경로
        assert "v2.1.259" not in capsys.readouterr().out
        assert json.loads(executor._index_file.read_text())["steps"][2]["status"] == "error"

    def test_unknown_option_stops_without_retry(self, executor, tmp_path, monkeypatch, capsys):
        calls = self._claude_failing_with(tmp_path, monkeypatch, "error: unknown option '--permission-prompts'")
        executor._commit_state = MagicMock()
        executor._commit_step = MagicMock()
        executor._build_cmd = "true"
        before = executor._index_file.read_text()
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step({"step": 2, "name": "ui"}, "")
        assert e.value.code == 1
        assert calls.read_text() == "x"
        out = capsys.readouterr().out
        assert "error: unknown option '--permission-prompts'" in out
        assert "v2.1.259" in out
        assert executor._index_file.read_text() == before
        executor._commit_state.assert_not_called()


class TestSessionCannotChangeAllowedTools:
    """세션이 phase index 의 allowed_tools 를 바꾸면 실행기가 세션 전 값으로 되돌리고 경고한다. 판정은 그대로."""

    def _run(self, executor, mutate, status="completed"):
        def m(idx):
            mutate(idx)
            if status == "blocked":
                idx["steps"][2]["status"] = "blocked"
                idx["steps"][2]["blocked_reason"] = "키 없음"
        executor._commit_step = MagicMock()
        executor._commit_state = MagicMock()
        executor._build_cmd = "true"
        with patch("subprocess.Popen", side_effect=_popen_session(executor, _denials_json(), [], m)):
            if status == "blocked":
                with pytest.raises(SystemExit) as e:
                    executor._execute_single_step({"step": 2, "name": "ui"}, "")
                assert e.value.code == 2
            else:
                assert executor._execute_single_step({"step": 2, "name": "ui"}, "") is True
        return json.loads(executor._index_file.read_text())

    def test_added_field_is_removed(self, executor, capsys):
        idx = self._run(executor, lambda i: i.update(allowed_tools=["Bash(*)"]))
        assert "allowed_tools" not in idx
        assert idx["steps"][2]["status"] == "completed"
        assert "⚠ 세션이 allowed_tools 를 바꿨다" in capsys.readouterr().out

    def test_changed_field_is_restored(self, executor, capsys):
        _set_index(executor, allowed_tools=["Bash(make *)"])
        idx = self._run(executor, lambda i: i["allowed_tools"].append("Bash(*)"))
        assert idx["allowed_tools"] == ["Bash(make *)"]
        assert "⚠ 세션이 allowed_tools 를 바꿨다" in capsys.readouterr().out

    def test_removed_field_is_restored(self, executor):
        _set_index(executor, allowed_tools=["Bash(make *)"])
        idx = self._run(executor, lambda i: i.pop("allowed_tools"))
        assert idx["allowed_tools"] == ["Bash(make *)"]

    def test_restored_on_blocked(self, executor):
        idx = self._run(executor, lambda i: i.update(allowed_tools=["Bash(*)"]), status="blocked")
        assert "allowed_tools" not in idx
        assert idx["steps"][2]["status"] == "blocked"

    def test_untouched_gives_no_warning(self, executor, capsys):
        _set_index(executor, allowed_tools=["Bash(make *)"])
        idx = self._run(executor, lambda i: None)
        assert idx["allowed_tools"] == ["Bash(make *)"]
        assert "allowed_tools 를 바꿨다" not in capsys.readouterr().out
