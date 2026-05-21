"""Tests targeting previously uncovered branches in sikula.py."""

from __future__ import annotations

import argparse
import importlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_sikula = importlib.import_module("sikula")
_parse_agent_llm_overrides = _sikula._parse_agent_llm_overrides
_fmt_time = _sikula._fmt_time
_build_tool_class = _sikula._build_tool_class
_reset_failed_state = _sikula._reset_failed_state
_print_review_summary = _sikula._print_review_summary
cmd_run = _sikula.cmd_run
cmd_show = _sikula.cmd_show
cmd_status = _sikula.cmd_status
cmd_review = _sikula.cmd_review
cmd_init = _sikula.cmd_init
main = _sikula.main


# ---------------------------------------------------------------------------
# _parse_agent_llm_overrides error branches
# ---------------------------------------------------------------------------


class TestParseAgentLlmOverrides:
    def test_unknown_agent_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_agent_llm_overrides(["bogus=claude-3"], None, None)
        assert exc.value.code == 1
        assert "Unknown agent" in capsys.readouterr().out

    def test_missing_equals_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_agent_llm_overrides(["analyst"], None, None)
        assert exc.value.code == 1
        assert "Expected format" in capsys.readouterr().out

    def test_empty_value_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_agent_llm_overrides(["analyst="], None, None)
        assert exc.value.code == 1
        assert "Expected format" in capsys.readouterr().out

    def test_bad_timeout_cast_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse_agent_llm_overrides(None, None, ["analyst=abc"])
        assert exc.value.code == 1
        assert "expected int" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _fmt_time ≥60s branch
# ---------------------------------------------------------------------------


class TestFmtTime:
    def test_under_60s(self):
        assert _fmt_time(45) == "45s"

    def test_exactly_60s(self):
        assert _fmt_time(60) == "1m 00s"

    def test_90s(self):
        assert _fmt_time(90) == "1m 30s"

    def test_multi_minute(self):
        assert _fmt_time(3661) == "61m 01s"


# ---------------------------------------------------------------------------
# _build_tool_class cargo / xcodebuild branches
# ---------------------------------------------------------------------------


class TestBuildToolClass:
    def test_cargo(self):
        from tools.cargo_tool import CargoTool

        assert _build_tool_class({"project": {"build_tool": "cargo"}}) is CargoTool

    def test_xcodebuild(self):
        from tools.xcode_tool import XcodeTool

        assert _build_tool_class({"project": {"build_tool": "xcodebuild"}}) is XcodeTool

    def test_python(self):
        from tools.python_tool import PythonTool

        assert _build_tool_class({"project": {"build_tool": "python"}}) is PythonTool

    def test_gradle_android(self):
        from tools.gradle_android_tool import AndroidGradleTool

        assert _build_tool_class({"project": {"build_tool": "gradle-android"}}) is AndroidGradleTool

    def test_gradle_jvm(self):
        from tools.gradle_jvm_tool import JvmGradleTool

        assert _build_tool_class({"project": {"build_tool": "gradle-jvm"}}) is JvmGradleTool

    def test_maven(self):
        from tools.maven_tool import MavenTool

        assert _build_tool_class({"project": {"build_tool": "maven"}}) is MavenTool

    def test_default_fallback_is_android_gradle(self):
        from tools.gradle_android_tool import AndroidGradleTool

        assert _build_tool_class({"project": {}}) is AndroidGradleTool


# ---------------------------------------------------------------------------
# _reset_failed_state
# ---------------------------------------------------------------------------


class TestResetFailedState:
    def _store(self, tmp_path: Path):
        from core.state import JsonStateStore

        return JsonStateStore(tmp_path)

    def test_task_not_found_exits(self, tmp_path: Path, capsys):
        store = self._store(tmp_path)
        cfg = {"project": {"root_path": str(tmp_path)}, "sandbox": {}}
        with pytest.raises(SystemExit) as exc:
            _reset_failed_state("nonexistent", cfg, store)
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().out

    def test_not_failed_returns_without_exit(self, tmp_path: Path, capsys):
        from core.state import TaskState

        store = self._store(tmp_path)
        s = TaskState(task_id="t1", task_description="task")
        s.failed = False
        store.save(s)
        cfg = {"project": {"root_path": str(tmp_path)}, "sandbox": {}}
        _reset_failed_state("t1", cfg, store)
        assert "not in failed state" in capsys.readouterr().out

    def test_reset_clears_flags_and_counters(self, tmp_path: Path, capsys):
        from core.state import TaskState

        store = self._store(tmp_path)
        s = TaskState(task_id="t1", task_description="task")
        s.failed = True
        s.files_changed = ["src/foo.py"]
        s.review_iterations = 3
        s.security_review_iterations = 2
        s.build_iterations = 5
        s.errors = ["err"]
        s.test_errors = ["terr"]
        s.check_errors = ["cherr"]
        store.save(s)
        cfg = {"project": {"root_path": str(tmp_path)}, "sandbox": {}}
        _reset_failed_state("t1", cfg, store)

        loaded = store.load("t1")
        assert not loaded.failed
        assert loaded.review_iterations == 0
        assert loaded.security_review_iterations == 0
        assert loaded.build_iterations == 0
        assert loaded.errors == []
        assert loaded.test_errors == []
        assert loaded.check_errors == []
        out = capsys.readouterr().out
        assert "reset" in out

    def test_auto_detects_changed_files_from_git_diff(self, tmp_path: Path, capsys):
        from core.state import TaskState

        store = self._store(tmp_path)
        s = TaskState(task_id="t1", task_description="task")
        s.failed = True
        s.files_changed = []
        store.save(s)
        cfg = {
            "project": {"root_path": str(tmp_path)},
            "sandbox": {"allowed_write_paths": ["src/"]},
        }
        modified = MagicMock(stdout="src/foo.py\n")
        untracked = MagicMock(stdout="")
        with patch("sikula.subprocess.run", side_effect=[modified, untracked]):
            _reset_failed_state("t1", cfg, store)

        loaded = store.load("t1")
        assert loaded.files_changed == ["src/foo.py"]
        assert "Auto-detected" in capsys.readouterr().out

    def test_no_changed_files_prints_warning(self, tmp_path: Path, capsys):
        from core.state import TaskState

        store = self._store(tmp_path)
        s = TaskState(task_id="t1", task_description="task")
        s.failed = True
        s.files_changed = []
        store.save(s)
        cfg = {
            "project": {"root_path": str(tmp_path)},
            "sandbox": {"allowed_write_paths": ["src/"]},
        }
        empty = MagicMock(stdout="")
        with patch("sikula.subprocess.run", side_effect=[empty, empty]):
            _reset_failed_state("t1", cfg, store)

        loaded = store.load("t1")
        assert loaded.files_changed == []
        assert "implement phase will run again" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_run error branches
# ---------------------------------------------------------------------------


class TestCmdRunErrors:
    def _cfg(self, tmp_path: Path, build_tool="python") -> dict:
        return {
            "project": {"root_path": str(tmp_path), "build_tool": build_tool},
            "tasks": {"state_dir": str(tmp_path / "state")},
        }

    def test_unsupported_build_tool_exits(self, tmp_path: Path, capsys):
        args = argparse.Namespace(
            task_file=None,
            task_file_pos=None,
            task_id=None,
            no_isolate=True,
            reset_failed=False,
            build=None,
            presync=None,
            presync_clean=None,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_run(args, self._cfg(tmp_path, build_tool="unsupported"))
        assert exc.value.code == 1
        assert "Unsupported build_tool" in capsys.readouterr().out

    def test_reset_failed_without_task_id_exits(self, tmp_path: Path, capsys):
        args = argparse.Namespace(
            task_file=None,
            task_file_pos=None,
            task_id=None,
            no_isolate=True,
            reset_failed=True,
            build=None,
            presync=None,
            presync_clean=None,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_run(args, self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "--reset-failed requires --task-id" in capsys.readouterr().out

    def test_task_file_not_found_exits(self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        args = argparse.Namespace(
            task_file="nonexistent_task.md",
            task_file_pos=None,
            task_id=None,
            no_isolate=True,
            reset_failed=False,
            build=None,
            presync=None,
            presync_clean=None,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_run(args, self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "Task file not found" in capsys.readouterr().out

    def test_task_file_pos_used_when_task_file_absent(self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
        """Positional task_file_pos is promoted to task_file when task_file is None."""
        monkeypatch.chdir(tmp_path)
        args = argparse.Namespace(
            task_file=None,
            task_file_pos="nonexistent_pos.md",
            task_id=None,
            no_isolate=True,
            reset_failed=False,
            build=None,
            presync=None,
            presync_clean=None,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_run(args, self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "Task file not found" in capsys.readouterr().out

    def test_presync_clean_override_reaches_task_file_check(
        self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ):
        """presync_clean override path is exercised before the task_file-not-found exit."""
        monkeypatch.chdir(tmp_path)
        args = argparse.Namespace(
            task_file="nonexistent_task.md",
            task_file_pos=None,
            task_id=None,
            no_isolate=True,
            reset_failed=False,
            build=None,
            presync=None,
            presync_clean=True,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_run(args, self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "Task file not found" in capsys.readouterr().out

    def test_task_id_not_found_exits(self, tmp_path: Path, capsys):
        args = argparse.Namespace(
            task_file=None,
            task_file_pos=None,
            task_id="doesnotexist",
            no_isolate=True,
            reset_failed=False,
            build=None,
            presync=None,
            presync_clean=None,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_run(args, self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().out

    def test_worktree_creation_failure_exits(self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
        """Covers lines 598-599: worktree creation fails → clear error."""
        import subprocess

        monkeypatch.chdir(tmp_path)
        task_file = tmp_path / "task.md"
        task_file.write_text("Add feature.")
        # Set up a real git repo so _find_git_root succeeds
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "ci@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "CI"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        args = argparse.Namespace(
            task_file=str(task_file),
            task_file_pos=None,
            task_id=None,
            no_isolate=False,  # isolation enabled — will try to create worktree
            reset_failed=False,
            build=None,
            presync=None,
            presync_clean=None,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        with patch("sikula._create_worktree", return_value=(False, "git error: already exists")):
            with pytest.raises(SystemExit) as exc:
                cmd_run(args, self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "Failed to create git worktree" in capsys.readouterr().out

    def test_missing_worktree_exits(self, tmp_path: Path, capsys):
        """Resume a task whose worktree has been deleted → clear error."""
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path / "state")
        s = TaskState(task_id="t1", task_description="task")
        s.worktree_path = str(tmp_path / "nonexistent" / "worktree")
        s.worktree_base = s.worktree_path
        store.save(s)
        args = argparse.Namespace(
            task_file=None,
            task_file_pos=None,
            task_id="t1",
            no_isolate=True,
            reset_failed=False,
            build=None,
            presync=None,
            presync_clean=None,
            planner=None,
            review=None,
            security_review=None,
            test_writing=None,
            tests=None,
            build_per_step=None,
            checks=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        cfg = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "tasks": {"state_dir": str(tmp_path / "state")},
        }
        with pytest.raises(SystemExit) as exc:
            cmd_run(args, cfg)
        assert exc.value.code == 1
        assert "Worktree no longer exists" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_status — all status label branches
# ---------------------------------------------------------------------------


class TestCmdStatusLabels:
    def _make_state(self, tmp_path: Path, task_id: str, **kwargs):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id=task_id, task_description=f"task {task_id}")
        for k, v in kwargs.items():
            setattr(s, k, v)
        store.save(s)
        return {"tasks": {"state_dir": str(tmp_path)}}

    def _run(self, cfg: dict, capsys) -> str:
        cmd_status(cfg)
        return capsys.readouterr().out

    def test_no_tasks(self, tmp_path: Path, capsys):
        cfg = {"tasks": {"state_dir": str(tmp_path / "empty")}}
        cmd_status(cfg)
        assert "No tasks" in capsys.readouterr().out

    def test_done_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1", done=True)
        assert "DONE" in self._run(cfg, capsys)

    def test_failed_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1", failed=True)
        assert "FAILED" in self._run(cfg, capsys)

    def test_build_failed_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1", plan_decided=True, files_changed=["a.py"], build_status="failed")
        assert "build failed" in self._run(cfg, capsys)

    def test_building_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1", plan_decided=True, files_changed=["a.py"], build_iterations=1)
        assert "building" in self._run(cfg, capsys)

    def test_testing_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(
            tmp_path,
            "t1",
            plan_decided=True,
            files_changed=["a.py"],
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )
        assert "testing" in self._run(cfg, capsys)

    def test_writing_tests_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(
            tmp_path, "t1", plan_decided=True, files_changed=["a.py"], review_approved=True, security_approved=True
        )
        assert "writing tests" in self._run(cfg, capsys)

    def test_security_review_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1", plan_decided=True, files_changed=["a.py"], review_approved=True)
        assert "security review" in self._run(cfg, capsys)

    def test_reviewing_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1", plan_decided=True, files_changed=["a.py"])
        assert "reviewing" in self._run(cfg, capsys)

    def test_analyzing_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1", presync_done=True)
        assert "analyzing" in self._run(cfg, capsys)

    def test_starting_label(self, tmp_path: Path, capsys):
        cfg = self._make_state(tmp_path, "t1")
        assert "starting" in self._run(cfg, capsys)


# ---------------------------------------------------------------------------
# cmd_show
# ---------------------------------------------------------------------------


class TestCmdShow:
    def test_task_not_found_exits(self, tmp_path: Path, capsys):
        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        with pytest.raises(SystemExit) as exc:
            cmd_show("nonexistent", cfg)
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().out

    def test_found_task_prints_json(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        store = JsonStateStore(tmp_path)
        s = TaskState(task_id="t1", task_description="my task")
        store.save(s)
        cfg = {"tasks": {"state_dir": str(tmp_path)}}
        cmd_show("t1", cfg)
        out = capsys.readouterr().out
        import json

        data = json.loads(out)
        assert data["task_id"] == "t1"
        assert data["task_description"] == "my task"


# ---------------------------------------------------------------------------
# _print_review_summary
# ---------------------------------------------------------------------------


class TestPrintReviewSummary:
    def _make_state(self, review_approved=True, security_approved=True, files_changed=None):
        from core.state import TaskState

        s = TaskState(task_id="r1", task_description="review")
        s.review_approved = review_approved
        s.security_approved = security_approved
        s.files_changed = files_changed or ["src/a.py", "src/b.py"]
        return s

    def test_approved_result(self, capsys):
        s = self._make_state(review_approved=True, security_approved=True)
        _print_review_summary(s, "feature/x", "main", 42.5)
        out = capsys.readouterr().out
        assert "APPROVED" in out
        assert "feature/x" in out
        assert "42s" in out

    def test_issues_found_result(self, capsys):
        s = self._make_state(review_approved=False, security_approved=True)
        _print_review_summary(s, "feature/x", "main", 120.0)
        out = capsys.readouterr().out
        assert "ISSUES FOUND" in out

    def test_security_not_approved_shows_issues(self, capsys):
        s = self._make_state(review_approved=True, security_approved=False)
        _print_review_summary(s, "feature/x", "main", 10.0, run_security_review=True)
        out = capsys.readouterr().out
        assert "ISSUES FOUND" in out

    def test_security_skipped_approved_when_review_approved(self, capsys):
        s = self._make_state(review_approved=True, security_approved=False)
        _print_review_summary(s, "feature/x", "main", 10.0, run_security_review=False)
        out = capsys.readouterr().out
        assert "APPROVED" in out


# ---------------------------------------------------------------------------
# cmd_review error branches
# ---------------------------------------------------------------------------


class TestCmdReviewErrors:
    def _cfg(self, tmp_path: Path) -> dict:
        return {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "tasks": {"state_dir": str(tmp_path / "state")},
        }

    def _args(self, **kwargs) -> argparse.Namespace:
        defaults = dict(
            branch="feature/x",
            base_branch="main",
            description=None,
            description_file=None,
            fix=False,
            security_review=None,
            agent_model=None,
            agent_provider=None,
            agent_timeout=None,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_fix_with_unsupported_build_tool_exits(self, tmp_path: Path, capsys):
        cfg = self._cfg(tmp_path)
        cfg["project"]["build_tool"] = "unsupported"
        with pytest.raises(SystemExit) as exc:
            cmd_review(self._args(fix=True), cfg)
        assert exc.value.code == 1
        assert "Unsupported build_tool" in capsys.readouterr().out

    def test_description_file_not_found_exits(self, tmp_path: Path, capsys):
        with pytest.raises(SystemExit) as exc:
            cmd_review(self._args(description_file="nonexistent_desc.md"), self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "Description file not found" in capsys.readouterr().out

    def test_not_in_git_repo_exits(self, tmp_path: Path, capsys):
        with patch("sikula._find_git_root", return_value=None):
            with pytest.raises(SystemExit) as exc:
                cmd_review(self._args(description="Review branch changes"), self._cfg(tmp_path))
        assert exc.value.code == 1
        assert "git repository" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_init edge cases not covered by test_sikula_helpers.py
# ---------------------------------------------------------------------------


class TestCmdInitEdgeCases:
    def _args(self, **kwargs) -> argparse.Namespace:
        defaults = dict(force=False, guidelines=False, provider=None, model=None)
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def _scan_result(self, **kwargs):
        from tools.scanner import ScanResult

        defaults = dict(
            build_tool="python",
            language="Python",
            guidelines_files=[],
            write_paths=["src/"],
            test_write_paths=["tests/"],
        )
        defaults.update(kwargs)
        return ScanResult(**defaults)

    def test_config_exists_without_force_exits(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        sikula_dir = tmp_path / ".sikula"
        sikula_dir.mkdir()
        (sikula_dir / "config.yaml").write_text("existing config")
        with pytest.raises(SystemExit) as exc:
            cmd_init(self._args())
        assert exc.value.code == 1
        assert "Config already exists" in capsys.readouterr().out

    def test_init_loads_project_env(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result()
        with (
            patch("sikula._load_project_env") as mock_load_env,
            patch("tools.scanner.scan", return_value=scan_result),
        ):
            cmd_init(self._args())
        mock_load_env.assert_called_once_with(tmp_path)

    def test_init_adds_env_to_root_gitignore_inside_git_repo(self, tmp_path: Path, monkeypatch):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result()
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert ".env" in lines
        assert ".claude/" not in lines
        assert ".gemini/" not in lines

    def test_init_adds_only_env_to_root_gitignore_outside_git_repo(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result()
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert ".env" in lines
        assert ".claude/" not in lines
        assert ".gemini/" not in lines

    def test_init_guidelines_adds_only_used_provider_settings_to_gitignore(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result()
        with (
            patch("tools.scanner.scan", return_value=scan_result),
            patch("sikula._generate_guidelines_for_init", return_value="# Generated guidelines"),
        ):
            cmd_init(self._args(guidelines=True, provider="gemini", model="gemini-2.5-pro"))
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert ".env" in lines
        assert ".gemini/" in lines
        assert ".claude/" not in lines

    def test_guidelines_only_adds_used_provider_settings_to_gitignore(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sikula_dir = tmp_path / ".sikula"
        sikula_dir.mkdir()
        (sikula_dir / "config.yaml").write_text(
            """\
project:
  name: custom-project
  language: Python
llm:
  provider: claude
  model: claude-sonnet-4-6
"""
        )
        with patch("sikula._generate_guidelines_for_init", return_value="# Generated guidelines"):
            cmd_init(self._args(guidelines=True))
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert ".claude/" in lines
        assert ".gemini/" not in lines

    def test_guidelines_without_provider_exits(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            cmd_init(self._args(guidelines=True, provider=None, model="gpt-5.5"))
        assert exc.value.code == 1
        assert "--provider and --model are both required" in capsys.readouterr().out

    def test_guidelines_without_model_exits(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            cmd_init(self._args(guidelines=True, provider="codex", model=None))
        assert exc.value.code == 1
        assert "--provider and --model are both required" in capsys.readouterr().out

    def test_ambiguous_build_tools_printed(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result(ambiguous_tools=["gradle-android", "cargo"])
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        assert "Multiple build tools detected" in capsys.readouterr().out

    def test_platform_printed_when_detected(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result(build_tool="gradle-android", language="Kotlin", platform="Android")
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        assert "platform" in capsys.readouterr().out

    def test_no_build_tool_detected_message(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result(build_tool=None, language=None)
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        assert "No build tool detected" in capsys.readouterr().out

    def test_guidelines_generation_failure_continues(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result()
        with (
            patch("tools.scanner.scan", return_value=scan_result),
            patch("core.llm_client.create_llm_client", return_value=MagicMock()),
            patch("agents.init_agent.InitAgent") as mock_cls,
        ):
            mock_cls.return_value.generate_guidelines.side_effect = RuntimeError("API error")
            cmd_init(self._args(guidelines=True, provider="codex", model="gpt-5.5"))
        out = capsys.readouterr().out
        assert "Warning: guidelines generation failed" in out
        assert (tmp_path / ".sikula" / "config.yaml").exists()

    def test_existing_guidelines_md_kept_in_config(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        sikula_dir = tmp_path / ".sikula"
        sikula_dir.mkdir()
        (sikula_dir / "guidelines.md").write_text("# Existing guidelines")
        scan_result = self._scan_result()
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args(force=True))
        config_text = (sikula_dir / "config.yaml").read_text()
        assert ".sikula/guidelines.md" in config_text

    def test_guidelines_only_preserves_existing_config(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        sikula_dir = tmp_path / ".sikula"
        sikula_dir.mkdir()
        config_path = sikula_dir / "config.yaml"
        config_path.write_text(
            """\
project:
  name: custom-project
  language: Python
  build_tool: python
llm:
  provider: codex
  model: gpt-5.5
guidelines:
  context_files:
    - README.md
custom_section:
  keep: true
"""
        )

        with patch("sikula._generate_guidelines_for_init", return_value="# Generated guidelines") as mock_generate:
            cmd_init(self._args(guidelines=True))

        config_text = config_path.read_text()
        assert "custom_section:" in config_text
        assert "  keep: true" in config_text
        assert "README.md" in config_text
        assert ".sikula/guidelines.md" in config_text
        assert "Sikula project configuration" not in config_text
        assert (sikula_dir / "guidelines.md").read_text() == "# Generated guidelines"
        mock_generate.assert_called_once()
        out = capsys.readouterr().out
        assert "without rewriting" in out

    def test_guidelines_only_does_not_duplicate_existing_reference(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        sikula_dir = tmp_path / ".sikula"
        sikula_dir.mkdir()
        config_path = sikula_dir / "config.yaml"
        config_path.write_text(
            """\
project:
  name: custom-project
  language: Python
llm:
  provider: codex
  model: gpt-5.5
guidelines:
  context_files:
    - .sikula/guidelines.md
    - README.md
"""
        )

        with patch("sikula._generate_guidelines_for_init", return_value="# Generated guidelines"):
            cmd_init(self._args(guidelines=True))

        config_text = config_path.read_text()
        assert config_text.count(".sikula/guidelines.md") == 1
        assert "already references" in capsys.readouterr().out

    def test_guidelines_only_requires_llm_config_when_missing(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        sikula_dir = tmp_path / ".sikula"
        sikula_dir.mkdir()
        (sikula_dir / "config.yaml").write_text("project:\n  name: custom-project\n")

        with pytest.raises(SystemExit) as exc:
            cmd_init(self._args(guidelines=True))

        assert exc.value.code == 1
        assert "unless llm.provider/model exist in config" in capsys.readouterr().out

    def test_guidelines_only_rejects_invalid_existing_config(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        sikula_dir = tmp_path / ".sikula"
        sikula_dir.mkdir()
        (sikula_dir / "config.yaml").write_text("project:\n  name: [broken\n")

        with pytest.raises(SystemExit) as exc:
            cmd_init(self._args(guidelines=True, provider="codex", model="gpt-5.5"))

        assert exc.value.code == 1
        assert "Invalid config YAML" in capsys.readouterr().out
        assert not (sikula_dir / "guidelines.md").exists()

    def test_xcode_scheme_printed(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result(build_tool="xcodebuild", language="Swift", xcode_scheme="MyApp")
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        assert "scheme" in capsys.readouterr().out

    def test_todos_for_gradle_build_tool(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result(build_tool="gradle-android", language="Kotlin")
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        out = capsys.readouterr().out
        assert "compile_task" in out or "TODO" in out or "build.compile_task" in out

    def test_todos_for_xcodebuild_no_scheme(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result(build_tool="xcodebuild", language="Swift", xcode_scheme=None)
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args())
        out = capsys.readouterr().out
        assert "scheme" in out

    def test_no_provider_emits_llm_todo(self, tmp_path: Path, monkeypatch, capsys):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        scan_result = self._scan_result()
        with patch("tools.scanner.scan", return_value=scan_result):
            cmd_init(self._args(provider=None))
        assert "llm.provider" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main() — CLI dispatch
# ---------------------------------------------------------------------------


class TestMain:
    """Tests for the main() argparse dispatch function."""

    def _cfg(self, tmp_path: Path) -> dict:
        return {"project": {"root_path": str(tmp_path), "build_tool": "python"}, "tasks": {}}

    def _patch_config(self, tmp_path: Path):
        """Context managers to mock config loading in main()."""
        return (
            patch("sikula._resolve_config", return_value=(tmp_path / "config.yaml", tmp_path)),
            patch("sikula.load_config", return_value=self._cfg(tmp_path)),
            patch("sikula._resolve_root_path", return_value=tmp_path),
        )

    def test_no_command_prints_help_and_exits(self, tmp_path: Path, capsys):
        with patch("sys.argv", ["sikula"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_run_command_dispatches_to_cmd_run(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "run", "--task-id", "t1"]):
            with p1, p2, p3:
                with patch("sikula.cmd_run") as mock_run:
                    main()
        mock_run.assert_called_once()

    def test_main_loads_project_env_before_dispatch(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "run", "--task-id", "t1"]):
            with p1, p2, p3:
                with (
                    patch("sikula._load_project_env") as mock_load_env,
                    patch("sikula.cmd_run"),
                ):
                    main()
        mock_load_env.assert_called_once_with(tmp_path)

    def test_status_command_dispatches_to_cmd_status(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "status"]):
            with p1, p2, p3:
                with patch("sikula.cmd_status") as mock_status:
                    main()
        mock_status.assert_called_once()

    def test_status_command_passes_output_and_filter_flags(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "status", "--active", "--json", "--verbose"]):
            with p1, p2, p3:
                with patch("sikula.cmd_status") as mock_status:
                    main()
        mock_status.assert_called_once()
        args = mock_status.call_args.args[1]
        assert args.json is True
        assert args.verbose is True
        assert args.status_filter == ["active"]

    def test_show_command_dispatches_to_cmd_show(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "show", "abc123"]):
            with p1, p2, p3:
                with patch("sikula.cmd_show") as mock_show:
                    main()
        mock_show.assert_called_once()

    def test_cleanup_command_dispatches_to_cmd_cleanup(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "cleanup", "abc123"]):
            with p1, p2, p3:
                with patch("sikula.cmd_cleanup") as mock_cleanup:
                    main()
        mock_cleanup.assert_called_once()
        assert mock_cleanup.call_args.args[0].delete_state is False

    def test_delete_command_dispatches_to_cmd_cleanup_with_delete_state(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "delete", "abc123"]):
            with p1, p2, p3:
                with patch("sikula.cmd_cleanup") as mock_cleanup:
                    main()
        mock_cleanup.assert_called_once()
        assert mock_cleanup.call_args.args[0].delete_state is True

    def test_review_command_dispatches_to_cmd_review(self, tmp_path: Path):
        p1, p2, p3 = self._patch_config(tmp_path)
        argv = ["sikula", "review", "--branch", "feature/x", "--description", "Review branch changes"]
        with patch("sys.argv", argv):
            with p1, p2, p3:
                with patch("sikula.cmd_review") as mock_review:
                    main()
        mock_review.assert_called_once()

    def test_init_command_dispatches_to_cmd_init(self, tmp_path: Path):
        with patch("sys.argv", ["sikula", "init"]):
            with patch("sikula.cmd_init") as mock_init:
                main()
        mock_init.assert_called_once()

    def test_run_without_task_file_or_task_id_calls_error(self, tmp_path: Path, capsys):
        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "run"]):
            with p1, p2, p3:
                with pytest.raises(SystemExit):
                    main()

    def test_version_not_installed_falls_back_to_dev(self, tmp_path: Path):
        from importlib.metadata import PackageNotFoundError

        p1, p2, p3 = self._patch_config(tmp_path)
        with patch("sys.argv", ["sikula", "status"]):
            with patch("sikula._pkg_version", side_effect=PackageNotFoundError):
                with p1, p2, p3:
                    with patch("sikula.cmd_status"):
                        main()
