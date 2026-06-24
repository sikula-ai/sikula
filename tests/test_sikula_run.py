"""Tests for sikula.py — run command helpers."""

from __future__ import annotations

import argparse
import importlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.state import JsonStateStore

_sikula = importlib.import_module("sikula")
_resolve_task_path = _sikula._resolve_task_path
_finalize_worktree = _sikula._finalize_worktree
_require_committed_config_for_isolated_run = _sikula._require_committed_config_for_isolated_run
cmd_cleanup = _sikula.cmd_cleanup
cmd_run = _sikula.cmd_run


def _git_commit_all(repo: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
    )


class TestFinalizeWorktree:
    def _state(self, **kwargs):
        defaults = dict(worktree_branch="sikula/my-task-abc123", task_id="abc123")
        defaults.update(kwargs)
        return MagicMock(**defaults)

    def test_removes_worktree_without_force_when_clean(self, tmp_path):
        calls = []

        def fake_run(cmd, **_):
            calls.append(cmd)
            r = MagicMock()
            if cmd == ["git", "status", "--porcelain"]:
                r.stdout = ""
            r.returncode = 0
            return r

        with patch("sikula.subprocess.run", side_effect=fake_run):
            success, committed, commit_sha = _finalize_worktree(tmp_path, tmp_path, self._state())

        assert success
        assert not committed
        assert commit_sha is None
        remove_calls = [c for c in calls if "worktree" in c and "remove" in c]
        assert remove_calls == [["git", "worktree", "remove", str(tmp_path)]]

    def test_falls_back_to_force_when_plain_remove_fails(self, tmp_path):
        calls = []

        def fake_run(cmd, **_):
            calls.append(cmd)
            r = MagicMock()
            if cmd == ["git", "status", "--porcelain"]:
                r.stdout = ""
            elif cmd == ["git", "worktree", "remove", str(tmp_path)]:
                r.returncode = 1
            else:
                r.returncode = 0
            return r

        with patch("sikula.subprocess.run", side_effect=fake_run):
            success, committed, commit_sha = _finalize_worktree(tmp_path, tmp_path, self._state())

        assert success
        assert not committed
        assert commit_sha is None
        remove_calls = [c for c in calls if "worktree" in c and "remove" in c]
        assert remove_calls == [
            ["git", "worktree", "remove", str(tmp_path)],
            ["git", "worktree", "remove", "--force", str(tmp_path)],
        ]

    def test_returns_failure_when_force_also_fails(self, tmp_path):
        def fake_run(cmd, **_):
            r = MagicMock()
            if cmd == ["git", "status", "--porcelain"]:
                r.stdout = ""
            elif "remove" in cmd:
                r.returncode = 1
            else:
                r.returncode = 0
            return r

        with patch("sikula.subprocess.run", side_effect=fake_run):
            success, _, commit_sha = _finalize_worktree(tmp_path, tmp_path, self._state())

        assert not success
        assert commit_sha is None

    def test_records_commit_sha_after_successful_commit(self, tmp_path):
        state = self._state()

        def fake_run(cmd, **_):
            r = MagicMock()
            if cmd == ["git", "status", "--porcelain"]:
                r.stdout = " M src/main.py\n"
                r.returncode = 0
            elif cmd == ["git", "rev-parse", "HEAD"]:
                r.stdout = "abc123\n"
                r.returncode = 0
            else:
                r.stdout = ""
                r.returncode = 0
            return r

        with patch("sikula.subprocess.run", side_effect=fake_run):
            success, committed, commit_sha = _finalize_worktree(tmp_path, tmp_path, state)

        assert success
        assert committed
        assert commit_sha == "abc123"
        assert state.result_commit == "abc123"


class TestCmdRunFinalizeWorktreeState:
    def test_successful_isolated_run_clears_removed_worktree_paths(self, tmp_path: Path):
        task_file = tmp_path / ".sikula" / "tasks" / "task.md"
        task_file.parent.mkdir(parents=True)
        task_file.write_text("do something")
        worktree_base = tmp_path / ".sikula" / "worktrees" / "abc123"
        worktree_base.mkdir(parents=True)

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["state_store"] = state_store
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            state.files_changed = ["sikula.py"]
            state.history = []
            state.build_iterations = 1
            mock = MagicMock()
            mock.run.return_value = state
            return mock

        def fake_create_worktree(git_root, worktree_path, branch):
            captured["worktree_base"] = worktree_path
            worktree_path.mkdir(parents=True, exist_ok=True)
            return True, ""

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("sikula._create_worktree", side_effect=fake_create_worktree),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sikula._finalize_worktree", return_value=(True, True, "abc123")),
            patch("sys.exit"),
        ):
            cmd_run(_run_args(task_file=str(task_file)), _run_cfg(tmp_path))

        store: JsonStateStore = captured["state_store"]
        task_id = store.list_tasks()[0]
        saved = store.load(task_id)
        assert saved.worktree_path is None
        assert saved.worktree_base is None
        assert saved.worktree_branch.startswith("sikula/")


class TestResolveTaskPath:
    def test_finds_file_in_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "task.md").write_text("do something")
        result = _resolve_task_path("task.md", tmp_path)
        assert result == tmp_path / "task.md"

    def test_finds_file_in_subdirectory_of_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tasks").mkdir()
        (tmp_path / "tasks" / "task.md").write_text("do something")
        result = _resolve_task_path("tasks/task.md", tmp_path)
        assert result == tmp_path / "tasks" / "task.md"

    def test_returns_none_when_not_found(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _resolve_task_path("missing.md", tmp_path)
        assert result is None

    def test_absolute_path_found(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        task = tmp_path / "task.md"
        task.write_text("do something")
        result = _resolve_task_path(str(task), tmp_path)
        assert result == task

    def test_absolute_path_not_found_returns_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _resolve_task_path(str(tmp_path / "nonexistent.md"), tmp_path)
        assert result is None

    def test_relative_path_resolved_from_cwd_not_project_root(self, tmp_path: Path, monkeypatch):
        project_root = tmp_path / "project"
        project_root.mkdir()
        subdir = project_root / "src"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        (subdir / "task.md").write_text("do something")
        result = _resolve_task_path("task.md", project_root)
        assert result == subdir / "task.md"

    def test_no_sikula_fallback(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".sikula" / "tasks").mkdir(parents=True)
        (tmp_path / ".sikula" / "tasks" / "task.md").write_text("do something")
        result = _resolve_task_path("tasks/task.md", tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# Isolated run config guard
# ---------------------------------------------------------------------------


class TestIsolatedRunConfigGuard:
    def _init_repo_with_config(self, tmp_path: Path, *, commit_config: bool) -> Path:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        config_path = tmp_path / ".sikula" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("project:\n  root_path: .\n  build_tool: python\n")
        if commit_config:
            _git_commit_all(tmp_path)
        return config_path

    def test_clean_committed_config_allows_isolated_run(self, tmp_path: Path):
        config_path = self._init_repo_with_config(tmp_path, commit_config=True)

        _require_committed_config_for_isolated_run({"_config_path": str(config_path)}, tmp_path)

    def test_untracked_config_exits_before_worktree(self, tmp_path: Path, capsys):
        config_path = self._init_repo_with_config(tmp_path, commit_config=False)

        with pytest.raises(SystemExit) as exc_info:
            _require_committed_config_for_isolated_run({"_config_path": str(config_path)}, tmp_path)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "requires Sikula config/context files to be committed" in out
        assert "not tracked by git" in out
        assert "git add .sikula/config.yaml" in out

    def test_modified_config_exits_before_worktree(self, tmp_path: Path, capsys):
        config_path = self._init_repo_with_config(tmp_path, commit_config=True)
        config_path.write_text("project:\n  root_path: .\n  build_tool: python\n  name: changed\n")

        with pytest.raises(SystemExit) as exc_info:
            _require_committed_config_for_isolated_run({"_config_path": str(config_path)}, tmp_path)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "has unstaged changes" in out
        assert "--no-isolate" in out

    def test_staged_config_exits_before_worktree(self, tmp_path: Path, capsys):
        config_path = self._init_repo_with_config(tmp_path, commit_config=True)
        config_path.write_text("project:\n  root_path: .\n  build_tool: python\n  name: staged\n")
        subprocess.run(["git", "add", ".sikula/config.yaml"], cwd=tmp_path, check=True, capture_output=True)

        with pytest.raises(SystemExit) as exc_info:
            _require_committed_config_for_isolated_run({"_config_path": str(config_path)}, tmp_path)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "has staged changes not committed to HEAD" in out

    def test_untracked_guidelines_context_exits_before_worktree(self, tmp_path: Path, capsys):
        config_path = self._init_repo_with_config(tmp_path, commit_config=True)
        guidelines_path = tmp_path / ".sikula" / "guidelines.md"
        guidelines_path.write_text("# Guidelines\n")

        with pytest.raises(SystemExit) as exc_info:
            _require_committed_config_for_isolated_run(
                {
                    "_config_path": str(config_path),
                    "project": {"root_path": str(tmp_path)},
                    "guidelines": {"context_files": [".sikula/guidelines.md"]},
                },
                tmp_path,
            )

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert ".sikula/guidelines.md (guidelines): not tracked by git" in out

    def test_missing_guidelines_context_exits_before_worktree(self, tmp_path: Path, capsys):
        config_path = self._init_repo_with_config(tmp_path, commit_config=True)

        with pytest.raises(SystemExit) as exc_info:
            _require_committed_config_for_isolated_run(
                {
                    "_config_path": str(config_path),
                    "project": {"root_path": str(tmp_path)},
                    "guidelines": {"context_files": [".sikula/missing-guidelines.md"]},
                },
                tmp_path,
            )

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert ".sikula/missing-guidelines.md (guidelines): does not exist" in out

    def test_modified_guidelines_context_exits_before_worktree(self, tmp_path: Path, capsys):
        config_path = self._init_repo_with_config(tmp_path, commit_config=True)
        guidelines_path = tmp_path / ".sikula" / "guidelines.md"
        guidelines_path.write_text("# Guidelines\n")
        _git_commit_all(tmp_path, "Add guidelines")
        guidelines_path.write_text("# Changed guidelines\n")

        with pytest.raises(SystemExit) as exc_info:
            _require_committed_config_for_isolated_run(
                {
                    "_config_path": str(config_path),
                    "project": {"root_path": str(tmp_path)},
                    "guidelines": {"context_files": [".sikula/guidelines.md"]},
                },
                tmp_path,
            )

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert ".sikula/guidelines.md (guidelines): has unstaged changes" in out

    def test_dirty_source_file_does_not_block_isolated_run(self, tmp_path: Path):
        config_path = self._init_repo_with_config(tmp_path, commit_config=True)
        src = tmp_path / "src" / "app.py"
        src.parent.mkdir()
        src.write_text("print('local work')\n")

        _require_committed_config_for_isolated_run({"_config_path": str(config_path)}, tmp_path)

    def test_isolated_cmd_run_refuses_untracked_config_before_state_creation(
        self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.chdir(tmp_path)
        config_path = self._init_repo_with_config(tmp_path, commit_config=False)
        task_file = tmp_path / "task.md"
        task_file.write_text("do something")
        cfg = _run_cfg(tmp_path)
        cfg["_config_path"] = str(config_path)

        with pytest.raises(SystemExit) as exc_info:
            cmd_run(_run_args(task_file=str(task_file)), cfg)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "requires Sikula config/context files to be committed" in out
        assert not (tmp_path / ".sikula" / "state").exists()

    def test_no_isolate_allows_untracked_config(self, tmp_path: Path):
        config_path = self._init_repo_with_config(tmp_path, commit_config=False)
        task_file = tmp_path / "task.md"
        task_file.write_text("do something")
        cfg = _run_cfg(tmp_path)
        cfg["_config_path"] = str(config_path)

        with (
            patch("sikula.build_orchestrator") as mock_orch,
            patch("sys.exit"),
        ):
            mock_orch.return_value.run.return_value = MagicMock(
                done=True,
                failed=False,
                task_id="tid",
                worktree_branch=None,
                files_changed=[],
                errors=[],
                history=[],
                build_iterations=0,
            )
            cmd_run(_run_args(task_file=str(task_file), no_isolate=True), cfg)

        assert mock_orch.called


# ---------------------------------------------------------------------------
# cmd_run: state_store handoff
# ---------------------------------------------------------------------------


def _run_args(**kwargs):
    defaults = dict(
        task_file=None,
        task_file_pos=None,
        task_id=None,
        no_isolate=False,
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
        require_contract_ready=False,
        min_contract_score=None,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _cleanup_args(**kwargs):
    defaults = dict(task_id="abc123", force=False, discard=False, delete_state=False)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _run_cfg(tmp_path: Path) -> dict:
    return {
        "project": {"root_path": str(tmp_path), "build_tool": "python"},
        "tasks": {"state_dir": ".sikula/state"},  # relative — re-resolution would break without fix
        "llm": {"provider": "codex", "model": "gpt-5.3-codex"},
        "sandbox": {"allowed_read_paths": ["."], "allowed_write_paths": ["src/"]},
        "agents": {},
    }


def _saved_state(tmp_path: Path, *, worktree: Path | None = None):
    from core.state import JsonStateStore, TaskState

    store = JsonStateStore(tmp_path / ".sikula" / "state")
    state = TaskState(task_id="abc123", task_description="cleanup me")
    if worktree:
        state.worktree_path = str(worktree)
        state.worktree_base = str(worktree)
        state.worktree_branch = "sikula/cleanup-me-abc123"
    store.save(state)
    return store, state


class TestCmdCleanup:
    def test_cleanup_defaults_to_dry_run(self, tmp_path: Path, capsys):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        store, _ = _saved_state(tmp_path, worktree=worktree)
        store.save_text_snapshot("abc123", "test_writer_audit_before", {"tests/test_main.py": "assert True\n"})

        with (
            patch("sikula._worktree_dirty", return_value=False),
            patch("sikula._remove_worktree") as remove_worktree,
        ):
            cmd_cleanup(_cleanup_args(force=False), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "CLEANUP (dry run)" in out
        assert "Would remove worktree" in out
        assert "No changes made" in out
        remove_worktree.assert_not_called()
        assert store.load("abc123").worktree_base == str(worktree)
        assert store.load_text_snapshot("abc123", "test_writer_audit_before") == {"tests/test_main.py": "assert True\n"}

    def test_cleanup_refuses_dirty_worktree_without_discard(self, tmp_path: Path, capsys):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _saved_state(tmp_path, worktree=worktree)

        with patch("sikula._worktree_dirty", return_value=True):
            with pytest.raises(SystemExit) as exc:
                cmd_cleanup(_cleanup_args(force=True, discard=False), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "Worktree has uncommitted changes" in out
        assert "--discard" in out

    def test_cleanup_dry_run_allows_dirty_worktree_without_discard(self, tmp_path: Path, capsys):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        _saved_state(tmp_path, worktree=worktree)

        with (
            patch("sikula._worktree_dirty", return_value=True),
            patch("sikula._remove_worktree") as remove_worktree,
        ):
            cmd_cleanup(_cleanup_args(force=False, discard=False), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "CLEANUP (dry run)" in out
        assert "requires --discard" in out
        remove_worktree.assert_not_called()

    def test_cleanup_force_removes_worktree_and_keeps_state(self, tmp_path: Path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        store, _ = _saved_state(tmp_path, worktree=worktree)
        store.save_text_snapshot("abc123", "test_writer_audit_before", {"tests/test_main.py": "assert True\n"})

        with (
            patch("sikula._worktree_dirty", return_value=False),
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._remove_worktree", return_value=True) as remove_worktree,
        ):
            cmd_cleanup(_cleanup_args(force=True), _run_cfg(tmp_path))

        remove_worktree.assert_called_once_with(worktree, tmp_path, force=False)
        state = store.load("abc123")
        assert state is not None
        assert state.worktree_base is None
        assert state.worktree_path is None
        assert any(h["action"] == "cleanup" for h in state.history)
        assert store.load_text_snapshot("abc123", "test_writer_audit_before") is None

    @pytest.mark.parametrize("delete_state", [False, True])
    def test_cleanup_force_refuses_to_remove_current_worktree(
        self, tmp_path: Path, monkeypatch, capsys, delete_state: bool
    ):
        worktree = tmp_path / "wt"
        current_dir = worktree / "src"
        current_dir.mkdir(parents=True)
        _saved_state(tmp_path, worktree=worktree)
        monkeypatch.chdir(current_dir)

        with pytest.raises(SystemExit) as exc:
            cmd_cleanup(_cleanup_args(force=True, delete_state=delete_state), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "Refusing to remove the current working tree" in out
        assert "Run this command from the original project or another directory" in out

    def test_cleanup_force_clears_missing_worktree_refs(self, tmp_path: Path):
        worktree = tmp_path / "missing-wt"
        store, _ = _saved_state(tmp_path, worktree=worktree)

        cmd_cleanup(_cleanup_args(force=True), _run_cfg(tmp_path))

        state = store.load("abc123")
        assert state is not None
        assert state.worktree_base is None
        assert state.worktree_path is None
        assert any(h["action"] == "cleanup" for h in state.history)

    def test_delete_force_removes_worktree_and_state(self, tmp_path: Path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        store, _ = _saved_state(tmp_path, worktree=worktree)
        store.save_text_snapshot("abc123", "test_writer_audit_before", {"tests/test_main.py": "assert True\n"})

        with (
            patch("sikula._worktree_dirty", return_value=False),
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._remove_worktree", return_value=True),
        ):
            cmd_cleanup(_cleanup_args(force=True, delete_state=True), _run_cfg(tmp_path))

        assert store.load("abc123") is None
        assert store.load_text_snapshot("abc123", "test_writer_audit_before") is None


class TestCmdRunStateStore:
    """Verify that build_orchestrator receives the store created before root_path is mutated."""

    def test_build_orchestrator_config_snapshot_includes_planner_config(self, tmp_path: Path):
        cfg = _run_cfg(tmp_path)
        cfg["planner"] = {"max_steps": 6}
        store = JsonStateStore(tmp_path / ".sikula" / "state")

        with patch("core.llm_client.create_llm_client", return_value=MagicMock()):
            orch = _sikula.build_orchestrator(cfg, state_store=store)

        assert orch._config_snapshot["planner"] == {"max_steps": 6}

    def test_task_file_with_isolate_passes_store_to_build_orchestrator(self, tmp_path: Path):
        task_file = tmp_path / "task.md"
        task_file.write_text(
            "# Greeting endpoint\n\n"
            "## Intent\nAdd a greeting endpoint.\n\n"
            "## Scope\nUpdate `src/app.py` only.\n\n"
            "## Acceptance Criteria\n- Calling `/hello` returns `Hello`.\n\n"
            "## Tests\n- Add pytest coverage for the endpoint.\n\n"
            "## Validation\n- Run `pytest`.\n\n"
            "## Out of Scope\n- Do not change persistence.\n\n"
            "## Security and Privacy\n- No auth or personal data changes.\n\n"
            "## Reviewer Focus\n- Verify the route contract.\n"
        )

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["state_store"] = state_store
            mock = MagicMock()
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            state.failed = False
            mock.run.return_value = state
            return mock

        def capture_sub(cmd, **kwargs):
            if cmd[0:2] == ["git", "rev-parse"]:
                return MagicMock(returncode=0, stdout=str(tmp_path) + "\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("sikula._create_worktree", return_value=(True, "")),
            patch("subprocess.run", side_effect=capture_sub),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sikula._finalize_worktree", return_value=(True, False, None)),
            patch("sys.exit"),
        ):
            cmd_run(_run_args(task_file=str(task_file)), _run_cfg(tmp_path))

        assert captured.get("state_store") is not None, "state_store was not passed to build_orchestrator"
        # The store must already contain the task saved before root_path was changed to worktree.
        tasks = captured["state_store"].list_tasks()
        assert len(tasks) == 1
        state = captured["state_store"].load(tasks[0])
        assert state.implementation_contract["schema_version"] == 1
        assert state.implementation_contract["source"]["path"] == "task.md"
        assert state.implementation_contract["source"]["format"] == "markdown"
        assert state.implementation_contract["source"]["sha256"].startswith("sha256:")
        assert isinstance(state.implementation_contract["readiness_score"], int)
        assert state.implementation_contract["status"] in {"ready", "warn", "weak", "not_ready"}
        assert isinstance(state.implementation_contract["clarifying_question_ids"], list)
        assert "validation" in state.implementation_contract
        assert not (tmp_path / ".sikula" / "contract-reports").exists()

    def test_task_file_no_isolate_records_contract_preflight(self, tmp_path: Path, capsys):
        task_file = tmp_path / "task.md"
        task_file.write_text("Goal: add endpoint\n\nAcceptance criteria:\n- It works\n\nValidation:\n- pytest\n")

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            mock.run.return_value = state
            return mock

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit") as exit_mock,
        ):
            cmd_run(_run_args(task_file=str(task_file), no_isolate=True), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "Implementation contract:" in out
        exit_mock.assert_called_with(0)

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        task_ids = store.list_tasks()
        assert len(task_ids) == 1
        state = store.load(task_ids[0])
        assert state.implementation_contract["source"]["path"] == "task.md"
        assert state.implementation_contract["source"]["sha256"].startswith("sha256:")
        assert not (tmp_path / ".sikula" / "contract-reports").exists()

    def test_task_file_no_isolate_records_implementation_asset_snapshot(self, tmp_path: Path):
        asset_path = tmp_path / ".sikula" / "task-assets" / "success-check.svg"
        asset_path.parent.mkdir(parents=True)
        asset_path.write_text("<svg viewBox='0 0 16 16'></svg>")
        task_file = tmp_path / "task.md"
        task_file.write_text(
            "# Add success icon\n\n"
            "## Scope\n"
            "- Add the success icon to the confirmation UI.\n\n"
            "## Assets\n\n"
            "### Delivery assets\n\n"
            "- Path: `.sikula/task-assets/success-check.svg`\n"
            "  - Usage: delivery asset.\n"
            "  - Target: `app/assets/success-check.svg`\n"
            "  - Source/license: provided by product team; MIT.\n\n"
            "## Acceptance criteria\n"
            "- The confirmation UI uses the provided success icon.\n\n"
            "## Validation\n"
            "- pytest\n"
        )

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            mock.run.return_value = state
            return mock

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit") as exit_mock,
        ):
            cmd_run(_run_args(task_file=str(task_file), no_isolate=True), _run_cfg(tmp_path))

        exit_mock.assert_called_with(0)
        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert len(state.implementation_asset_records) == 1
        asset_record = state.implementation_asset_records[0]
        assert asset_record["path"] == ".sikula/task-assets/success-check.svg"
        assert asset_record["project_path"] == ".sikula/task-assets/success-check.svg"
        assert asset_record["kind"] == "delivery"
        assert asset_record["status"] == "available"
        assert asset_record["requested_target"] == "app/assets/success-check.svg"
        assert asset_record["source_license"] == "provided by product team; MIT."
        assert asset_record["sha256"].startswith("sha256:")
        assert "_raw_paths" not in asset_record
        assert "excerpt" not in asset_record
        assert any(entry["action"] == "asset_snapshot" for entry in state.history)

    def test_task_file_contract_preflight_uses_cli_phase_overrides(self, tmp_path: Path):
        task_file = tmp_path / "task.md"
        task_file.write_text(
            "# Add endpoint\n\n"
            "## Scope\nAdd a small endpoint.\n\n"
            "## Acceptance criteria\n- It returns a successful response.\n\n"
            "## Validation\n- `pytest`\n- `python -m ruff format --check .`\n"
        )
        cfg = _run_cfg(tmp_path)
        cfg["run_build"] = True
        cfg["run_tests"] = True
        cfg["run_checks"] = True
        cfg["build"] = {"checks": [{"name": "format", "command": "python -m ruff format --check ."}]}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            mock.run.return_value = state
            return mock

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit"),
        ):
            cmd_run(
                _run_args(task_file=str(task_file), no_isolate=True, tests=False, checks=False),
                cfg,
            )

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.implementation_contract["validation"] == {
            "task_command_count": 2,
            "configured_command_count": 1,
            "covered_command_count": 0,
            "coverage_gap_count": 2,
        }

    def test_task_file_contract_preflight_error_is_warning_only(self, tmp_path: Path, capsys):
        task_file = tmp_path / "task.md"
        task_file.write_text("do something")

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            mock.run.return_value = state
            return mock

        with (
            patch("core.contract_check.check_contract_file", side_effect=RuntimeError("contract parser unavailable")),
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit") as exit_mock,
        ):
            cmd_run(_run_args(task_file=str(task_file), no_isolate=True), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "Implementation contract: unavailable (warning-only)" in out
        exit_mock.assert_called_with(0)

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.implementation_contract["status"] == "error"
        assert "contract parser unavailable" in state.implementation_contract["error"]

    def test_task_file_contract_ready_gate_aborts_before_orchestrator(self, tmp_path: Path, capsys):
        task_file = tmp_path / "task.md"
        task_file.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.\n")

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator") as build_orchestrator,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(
                _run_args(task_file=str(task_file), no_isolate=True, require_contract_ready=True),
                _run_cfg(tmp_path),
            )

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "Implementation contract gate failed:" in out
        assert "sikula contract prepare" in out
        build_orchestrator.assert_not_called()

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed
        assert not state.done
        assert state.contract_gate_blocked
        assert state.worktree_path is None
        assert state.implementation_contract["status"] == "not_ready"
        assert any(entry["action"] == "contract_gate_failed" for entry in state.history)

    def test_task_file_contract_ready_gate_aborts_before_isolated_worktree(self, tmp_path: Path, capsys):
        task_file = tmp_path / "task.md"
        task_file.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.\n")

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._require_committed_config_for_isolated_run"),
            patch("sikula._create_worktree") as create_worktree,
            patch("sikula.build_orchestrator") as build_orchestrator,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(
                _run_args(task_file=str(task_file), require_contract_ready=True),
                _run_cfg(tmp_path),
            )

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "Implementation contract gate failed:" in out
        create_worktree.assert_not_called()
        build_orchestrator.assert_not_called()

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed
        assert state.contract_gate_blocked
        assert state.worktree_path is None
        assert state.worktree_branch is None

    def test_task_file_contract_gate_saves_effective_config_snapshot(self, tmp_path: Path):
        task_file = tmp_path / "task.md"
        task_file.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.\n")
        cfg = _run_cfg(tmp_path)
        cfg["run_build"] = True
        cfg["run_tests"] = True
        cfg["run_checks"] = True
        cfg["build"] = {"checks": [{"name": "ruff", "command": "ruff check ."}]}

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator") as build_orchestrator,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(
                _run_args(
                    task_file=str(task_file),
                    no_isolate=True,
                    require_contract_ready=True,
                    tests=False,
                    checks=False,
                ),
                cfg,
            )

        assert exc.value.code == 1
        build_orchestrator.assert_not_called()

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed
        assert state.contract_gate_blocked
        assert state.config_snapshot["run_build"] is True
        assert state.config_snapshot["run_tests"] is False
        assert state.config_snapshot["run_checks"] is False
        assert state.config_snapshot["build"] == {"checks": [{"name": "ruff", "command": "ruff check ."}]}
        assert state.implementation_contract["validation"]["configured_command_count"] == 1

    def test_task_file_min_contract_score_gate_aborts_below_threshold(self, tmp_path: Path, capsys):
        task_file = tmp_path / "task.md"
        task_file.write_text(
            "# Add endpoint\n\n"
            "## Scope\nAdd a small endpoint.\n\n"
            "## Acceptance criteria\n- It returns a successful response.\n"
        )

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator") as build_orchestrator,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(
                _run_args(task_file=str(task_file), no_isolate=True, min_contract_score=90),
                _run_cfg(tmp_path),
            )

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "minimum required score is 90/100" in out
        build_orchestrator.assert_not_called()

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed
        assert state.contract_gate_blocked
        assert any(entry["action"] == "contract_gate_failed" for entry in state.history)

    def test_task_file_contract_gate_passes_ready_task(self, tmp_path: Path):
        task_file = tmp_path / "task.md"
        task_file.write_text(
            "# Team invites\n\n"
            "## Scope\n"
            "- Add invite creation endpoint.\n"
            "- Add invite acceptance endpoint.\n"
            "- Add pending invite model.\n\n"
            "## Acceptance criteria\n"
            "- Owner/admin can invite a user by email.\n"
            "- Non-admin users cannot invite users.\n"
            "- Duplicate pending invite returns a deterministic error.\n"
            "- Expired invite token cannot be accepted.\n"
            "- Accepted invite token cannot be reused.\n\n"
            "## Security and privacy\n"
            "- Invite tokens must be unguessable.\n"
            "- Invite tokens must not be logged.\n"
            "- Error messages must not reveal whether an email already has an account.\n\n"
            "## Out of scope\n"
            "- Billing seat enforcement.\n"
            "- Bulk invites.\n"
            "- Full team settings redesign.\n\n"
            "## Tests\n"
            "- Permission tests for allowed and denied inviter roles.\n"
            "- Token lifecycle tests for expired and reused tokens.\n"
            "- Duplicate invite test.\n\n"
            "## Validation\n"
            "- `pytest`\n"
            "- `ruff check .`\n\n"
            "## Reviewer focus\n"
            "- Authorization rules.\n"
            "- Token expiry and reuse.\n"
            "- Email enumeration behaviour.\n"
        )
        cfg = _run_cfg(tmp_path)
        cfg["run_build"] = True
        cfg["run_tests"] = True
        cfg["run_checks"] = True
        cfg["build"] = {"checks": [{"name": "ruff", "command": "ruff check ."}]}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            state_store.save(state)
            mock.run.return_value = state
            return mock

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator", side_effect=capture_orch) as build_orchestrator,
            patch("sys.exit") as exit_mock,
        ):
            cmd_run(
                _run_args(
                    task_file=str(task_file),
                    no_isolate=True,
                    require_contract_ready=True,
                    min_contract_score=80,
                ),
                cfg,
            )

        build_orchestrator.assert_called_once()
        exit_mock.assert_called_with(0)

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.done
        assert not state.failed
        assert state.implementation_contract["ready_for_autonomous_delivery"]
        assert not any(entry["action"] == "contract_gate_failed" for entry in state.history)

    def test_task_file_contract_preflight_error_blocks_strict_gate(self, tmp_path: Path, capsys):
        task_file = tmp_path / "task.md"
        task_file.write_text("do something")

        with (
            patch("core.contract_check.check_contract_file", side_effect=RuntimeError("contract parser unavailable")),
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator") as build_orchestrator,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(
                _run_args(task_file=str(task_file), no_isolate=True, require_contract_ready=True),
                _run_cfg(tmp_path),
            )

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "Implementation contract: unavailable (warning-only)" in out
        assert "strict readiness requires a valid contract check" in out
        build_orchestrator.assert_not_called()

        store = JsonStateStore(tmp_path / ".sikula" / "state")
        state = store.load(store.list_tasks()[0])
        assert state.failed
        assert state.contract_gate_blocked
        assert state.implementation_contract["status"] == "error"

    def test_task_file_run_refuses_current_task_worktree(self, tmp_path: Path, monkeypatch, capsys):
        current_dir = tmp_path / ".sikula" / "worktrees" / "oldtask" / "src"
        current_dir.mkdir(parents=True)
        task_file = current_dir / "task.md"
        task_file.write_text("do something")
        monkeypatch.chdir(current_dir)

        with pytest.raises(SystemExit) as exc:
            cmd_run(_run_args(task_file=str(task_file)), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "Refusing to start a new task from inside a Sikula task worktree" in out
        assert "sikula run --task-id <task-id>" in out

    def test_task_id_resume_passes_same_store(self, tmp_path: Path):
        from core.state import JsonStateStore, TaskState

        # Pre-create a task in the original state_dir.
        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        state = TaskState(task_id="abc123", task_description="resume me")
        state.worktree_path = str(tmp_path / "wt")
        state.worktree_base = str(tmp_path / "wt")
        state.worktree_branch = "sikula/task-abc123"
        store.save(state)
        (tmp_path / "wt").mkdir(parents=True)

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["state_store"] = state_store
            mock = MagicMock()
            mock.run.return_value = TaskState(
                done=True,
                failed=False,
                task_id="abc123",
                task_description="resume me",
                worktree_branch="sikula/task-abc123",
                files_changed=[],
                errors=[],
                history=[],
                build_iterations=0,
            )
            return mock

        with (
            patch("core.contract_check.check_contract_file") as check_contract_file,
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sikula._finalize_worktree", return_value=(True, False, None)),
            patch("sys.exit"),
        ):
            cmd_run(
                _run_args(task_id="abc123", require_contract_ready=True, min_contract_score=100),
                _run_cfg(tmp_path),
            )

        assert captured.get("state_store") is not None
        assert captured["state_store"].load("abc123") is not None
        check_contract_file.assert_not_called()

    def test_task_id_resume_from_current_worktree_chdirs_before_finalize(self, tmp_path: Path, monkeypatch):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        worktree = tmp_path / ".sikula" / "worktrees" / "abc123"
        current_dir = worktree / "src"
        current_dir.mkdir(parents=True)
        state = TaskState(task_id="abc123", task_description="resume me")
        state.worktree_path = str(worktree)
        state.worktree_base = str(worktree)
        state.worktree_branch = "sikula/task-abc123"
        store.save(state)
        monkeypatch.chdir(current_dir)

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            done_state = state_store.load("abc123")
            done_state.done = True
            mock.run.return_value = done_state
            return mock

        def capture_finalize(*args, **kwargs):
            captured["cwd_at_finalize"] = Path.cwd()
            return True, False, None

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._finalize_worktree", side_effect=capture_finalize),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        assert exc.value.code == 0
        assert captured["cwd_at_finalize"] == tmp_path

    def test_review_fix_resume_forces_review_mode_overrides(self, tmp_path: Path):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        state = TaskState(
            task_id="abc123",
            task_description="Review branch changes",
            implementation_prompt="Review branch changes",
            files_changed=["src/main.py"],
            review_diff="@@ -1 +1 @@\n+x",
            review_mode="review_fix",
            plan_decided=True,
            worktree_path=str(worktree),
            worktree_base=str(worktree),
            worktree_branch="feature/review-me",
            config_snapshot={"run_security_review": False},
        )
        store.save(state)

        cfg = _run_cfg(tmp_path)
        cfg["run_planner"] = True
        cfg["run_review"] = False
        cfg["run_security_review"] = True

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["overrides"] = overrides
            mock = MagicMock()
            mock.run.return_value = state_store.load("abc123")
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(_run_args(task_id="abc123"), cfg)

        assert exc.value.code == 1
        assert captured["overrides"]["run_planner"] is False
        assert captured["overrides"]["run_review"] is True
        assert captured["overrides"]["run_security_review"] is False

    def test_review_fix_resume_uses_review_fix_commit_message(self, tmp_path: Path):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        state = TaskState(
            task_id="abc123",
            task_description="Review branch changes",
            implementation_prompt="Review branch changes",
            files_changed=["src/main.py"],
            review_diff="@@ -1 +1 @@\n+x",
            review_mode="review_fix",
            plan_decided=True,
            worktree_path=str(worktree),
            worktree_base=str(worktree),
            worktree_branch="feature/review-me",
        )
        store.save(state)

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            done_state = state_store.load("abc123")
            done_state.done = True
            mock.run.return_value = done_state
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._finalize_worktree", return_value=(True, True, "commit-sha")) as finalize,
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        assert exc.value.code == 0
        assert finalize.call_args.kwargs["commit_msg"] == (
            "sikula: review fixes for feature/review-me\n\nTask ID: abc123"
        )

    def test_report_only_review_task_cannot_be_resumed(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        state = TaskState(
            task_id="abc123",
            task_description="Review branch changes",
            implementation_prompt="Review branch changes",
            files_changed=["src/main.py"],
            review_diff="@@ -1 +1 @@\n+x",
            review_mode="review_report",
            plan_decided=True,
            worktree_path=str(worktree),
            worktree_base=str(worktree),
            worktree_branch="feature/review-me",
        )
        store.save(state)

        with pytest.raises(SystemExit) as exc:
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "report-only review task and cannot be resumed" in out

    def test_review_diff_without_review_mode_does_not_trigger_report_review_guard(self, tmp_path: Path):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        worktree = tmp_path / "wt"
        worktree.mkdir(parents=True)
        state = TaskState(
            task_id="abc123",
            task_description="resume me",
            implementation_prompt="resume me",
            files_changed=["src/main.py"],
            review_diff="@@ -1 +1 @@\n+x",
            worktree_path=str(worktree),
            worktree_base=str(worktree),
            worktree_branch="feature/legacy-review-state",
        )
        store.save(state)

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["overrides"] = overrides
            mock = MagicMock()
            mock.run.return_value = state_store.load("abc123")
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        assert exc.value.code == 1
        assert captured["overrides"]["run_review"] is None

    def test_task_id_terminal_done_ignores_missing_worktree_path(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        state = TaskState(task_id="abc123", task_description="resume me")
        state.done = True
        state.build_iterations = 2
        state.record("test_writer", "test_write", "files changed", elapsed_s=193.2)
        state.worktree_path = str(tmp_path / "missing-wt")
        state.worktree_base = str(tmp_path / "missing-wt")
        state.worktree_branch = "sikula/task-abc123"
        store.save(state)

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["state_store"] = state_store
            mock = MagicMock()
            mock.run.return_value = state_store.load("abc123")
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit"),
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert captured.get("state_store") is not None
        assert "This task is already complete; no work was run." in out
        assert "Previous run:" in out
        assert "Longest phase:" in out
        assert "Build attempts:  2 total (max 10/loop)" in out
        assert "Total time:" not in out

    def test_terminal_done_prints_audit_warnings_without_failing(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        state = TaskState(task_id="abc123", task_description="resume me")
        state.done = True
        state.build_status = "success"
        state.test_status = "success"
        state.check_status = "success"
        state.record(
            "implementer",
            "write_path_warning",
            "files outside allowed_write_paths: ['README.md']; allowed: ['src/']",
        )
        store.save(state)

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            mock.run.return_value = state_store.load("abc123")
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit") as exit_mock,
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "Task abc123: ✓ DONE with warnings (1)" in out
        assert "Audit warnings:" in out
        assert "implementer: files outside allowed_write_paths" in out
        exit_mock.assert_called_with(0)

    def test_task_id_terminal_failed_prints_reset_failed_hint(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        state = TaskState(task_id="abc123", task_description="resume me")
        state.failed = True
        state.build_iterations = 10
        state.record("fixer", "fix", "still failing", elapsed_s=42.0)
        state.worktree_path = str(tmp_path / "missing-wt")
        state.worktree_base = str(tmp_path / "missing-wt")
        state.worktree_branch = "sikula/task-abc123"
        store.save(state)

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            mock.run.return_value = state_store.load("abc123")
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit"),
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "This task has failed; no work was run." in out
        assert "sikula run --task-id abc123 --reset-failed" in out
        assert "Previous run:" in out
        assert "Longest phase:" in out
        assert "Build attempts:  10 total (max 10/loop)" in out
        assert "Total time:" not in out

    def test_task_id_contract_gate_failed_prints_contract_check_hint(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        state = TaskState(task_id="abc123", task_description="resume me")
        state.failed = True
        state.contract_gate_blocked = True
        state.implementation_contract = {
            "source": {
                "path": ".sikula/tasks/invites.md",
            }
        }
        state.record("orchestrator", "contract_gate_failed", "contract is not ready")
        store.save(state)

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            mock = MagicMock()
            mock.run.return_value = state_store.load("abc123")
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit"),
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "This task has failed; no work was run." in out
        assert "contract readiness gate blocked delivery before a worktree was created" in out
        assert "sikula contract check .sikula/tasks/invites.md --write-report" in out
        assert "--reset-failed" not in out

    def test_task_id_resume_refuses_cleaned_isolated_task(self, tmp_path: Path, capsys):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        state = TaskState(task_id="abc123", task_description="resume me")
        state.worktree_branch = "sikula/task-abc123"
        state.record("sikula", "cleanup", "worktree removed")
        store.save(state)

        with pytest.raises(SystemExit) as exc:
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert exc.value.code == 1
        assert "cannot be resumed safely" in out
        assert "sikula show abc123" in out

    def test_task_id_resume_allows_no_isolate_task_without_worktree(self, tmp_path: Path):
        from core.state import JsonStateStore, TaskState

        state_dir = tmp_path / ".sikula" / "state"
        store = JsonStateStore(state_dir)
        state = TaskState(task_id="abc123", task_description="resume me")
        store.save(state)

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["state_store"] = state_store
            mock = MagicMock()
            mock.run.return_value = MagicMock(
                done=True,
                failed=False,
                task_id="abc123",
                worktree_branch=None,
                files_changed=[],
                errors=[],
                history=[],
                build_iterations=0,
            )
            return mock

        with (
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sys.exit"),
        ):
            cmd_run(_run_args(task_id="abc123"), _run_cfg(tmp_path))

        assert captured.get("state_store") is not None


class TestCmdRunNoIsolateWarnings:
    def test_no_isolate_without_git_exits(self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        task_file = tmp_path / "task.md"
        task_file.write_text("do something")

        with (
            patch("sikula._find_git_root", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_run(_run_args(task_file=str(task_file), no_isolate=True), _run_cfg(tmp_path))

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "not inside a git repository" in out
        assert "git init" in out

    def test_no_isolate_with_git_no_warning(self, tmp_path: Path, capsys):
        task_file = tmp_path / "task.md"
        task_file.write_text("do something")

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula.build_orchestrator") as mock_orch,
            patch("sikula._finalize_worktree", return_value=(True, False, None)),
            patch("sys.exit"),
        ):
            mock_orch.return_value.run.return_value = MagicMock(
                done=True,
                failed=False,
                task_id="tid",
                worktree_branch=None,
                files_changed=[],
                errors=[],
                history=[],
                build_iterations=0,
            )
            cmd_run(_run_args(task_file=str(task_file), no_isolate=True), _run_cfg(tmp_path))

        out = capsys.readouterr().out
        assert "not inside a git repository" not in out
