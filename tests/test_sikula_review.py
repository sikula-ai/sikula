"""Tests for sikula.py — review command helpers."""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.base_agent import AGENT_SECURITY_PREFIX
from agents.base_agent import AgentResult
from core.state import JsonStateStore
from core.state import TaskState
from tests.conftest import StubLLMClient

_sikula = importlib.import_module("sikula")
_worktree_error_message = _sikula._worktree_error_message
_ensure_gitignore = _sikula._ensure_gitignore
_ensure_project_gitignore_entry = _sikula._ensure_project_gitignore_entry
_enrich_prompt_with_referenced_files = _sikula._enrich_prompt_with_referenced_files
_run_review_agent_with_retry_history = _sikula._run_review_agent_with_retry_history
cmd_review = _sikula.cmd_review


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sub(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _subprocess_sequence(*results):
    pending = iter(results)

    def run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["git"]:
            return next(pending)
        return _sub()

    return run


def _args(**kwargs):
    defaults = dict(
        branch="feat/x",
        base_branch="main",
        fix=False,
        description="Test review",
        description_file=None,
        security_review=None,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _cfg(tmp_path: Path) -> dict:
    return {
        "project": {"root_path": str(tmp_path), "build_tool": "python"},
        "tasks": {"state_dir": str(tmp_path / "state")},
        "llm": {"provider": "codex", "model": "gpt-5.3-codex"},
        "sandbox": {"allowed_read_paths": ["."], "allowed_write_paths": ["src/"]},
        "run_security_review": True,
        "agents": {},
    }


def _run_review(
    tmp_path: Path, *, reviewer_approved: bool, security_approved: bool = True, run_security_review: bool = True
):
    cfg = _cfg(tmp_path)
    cfg["run_security_review"] = run_security_review

    subprocess_results = [
        _sub(stdout="abc1234\n"),  # git rev-parse (report-only: resolve branch to SHA)
        _sub(),  # git worktree add --detach
        _sub(stdout="@@ -1 +1 @@\n+x"),  # git diff
        _sub(stdout="src/main.py\n"),  # git diff --name-only
        _sub(stdout="@@ -1 +1 @@\n+x"),  # git diff --relative HEAD (reviewer fallback)
        _sub(),  # git worktree remove
    ]

    def reviewer_run(state):
        state.review_approved = reviewer_approved
        return MagicMock(success=reviewer_approved)

    def security_run(state):
        state.security_approved = security_approved
        return MagicMock(success=security_approved)

    mock_reviewer = MagicMock()
    mock_reviewer.run.side_effect = reviewer_run
    mock_security = MagicMock()
    mock_security.run.side_effect = security_run

    with (
        patch("sikula._find_git_root", return_value=tmp_path),
        patch("sikula._ensure_gitignore"),
        patch("subprocess.run", side_effect=_subprocess_sequence(*subprocess_results)),
        patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
        patch("agents.reviewer_agent.ReviewerAgent", return_value=mock_reviewer),
        patch("agents.security_reviewer_agent.SecurityReviewerAgent", return_value=mock_security),
        patch("sys.exit"),
    ):
        cmd_review(_args(), cfg)

    from core.state import JsonStateStore

    store = JsonStateStore(tmp_path / "state")
    tasks = store.list_tasks()
    assert tasks, "no task state was saved"
    return store.load(tasks[0])


# ---------------------------------------------------------------------------
# Tests — report-only state outcome
# ---------------------------------------------------------------------------


class TestCmdReviewReportOnlyState:
    def test_approved_sets_done(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=True, security_approved=True)
        assert state.done
        assert not state.failed
        assert state.review_mode == "review_report"
        assert state.review_base_branch == "main"
        assert state.test_status == "skipped"
        assert state.check_status == "skipped"

    def test_rejected_review_sets_failed(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=False)
        assert not state.done
        assert state.failed

    def test_security_rejected_sets_failed(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=True, security_approved=False)
        assert not state.done
        assert state.failed

    def test_approved_without_security_review_sets_done(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=True, run_security_review=False)
        assert state.done
        assert not state.failed

    def test_config_snapshot_contains_security_review_flag(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=True)
        assert "run_security_review" in state.config_snapshot
        assert state.config_snapshot["run_security_review"] is True

    def test_config_snapshot_contains_progress_heartbeat_config(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=True)
        assert state.config_snapshot["progress"] == {
            "heartbeat_interval_seconds": 60,
        }

    def test_heartbeat_interval_treats_zero_as_disabled(self):
        assert _sikula._heartbeat_interval_seconds({"progress": {"heartbeat_interval_seconds": 0}}) == 0

    def test_report_only_review_records_llm_retry_history(self, tmp_path: Path):
        class RetryReportingReviewAgent:
            def __init__(self) -> None:
                self.llm = StubLLMClient()

            def run(self, state: TaskState) -> AgentResult:
                self.llm._retry_observer(
                    {
                        "provider": "opencode",
                        "model": "openai/gpt-5.5",
                        "operation": "run_readonly_agent",
                        "attempt": 1,
                        "max_attempts": 4,
                        "delay_s": 30,
                        "error": "provider returned no text output",
                        "error_type": "RuntimeError",
                    }
                )
                state.review_approved = True
                state.record("reviewer", "review", "approved")
                return AgentResult(success=True, message="approved")

        state = TaskState(task_id="t1", task_description="review branch")
        store = JsonStateStore(tmp_path / "state")

        _run_review_agent_with_retry_history(RetryReportingReviewAgent(), "reviewer", state, store)

        retry = state.history[0]
        assert retry["agent"] == "reviewer"
        assert retry["action"] == "llm_retry"
        assert retry["result"] == "provider returned no text output"
        assert retry["provider"] == "opencode"
        assert retry["model"] == "openai/gpt-5.5"
        assert retry["operation"] == "run_readonly_agent"
        assert retry["attempt"] == 1
        assert retry["max_attempts"] == 4
        assert retry["delay_s"] == 30
        assert retry["error_type"] == "RuntimeError"
        assert state.history[1]["action"] == "review"

    def test_report_only_review_records_active_operation_while_agent_runs(self, tmp_path: Path):
        class ObservingReviewAgent:
            def run(self, state: TaskState) -> AgentResult:
                loaded = store.load(state.task_id)
                assert loaded is not None
                assert loaded.active_operation is not None
                assert loaded.active_operation["phase"] == "agent"
                assert loaded.active_operation["agent"] == "reviewer"
                state.review_approved = True
                state.record("reviewer", "review", "approved")
                return AgentResult(success=True, message="approved")

        state = TaskState(task_id="t1", task_description="review branch")
        store = JsonStateStore(tmp_path / "state")

        _run_review_agent_with_retry_history(ObservingReviewAgent(), "reviewer", state, store, 60)

        loaded = store.load(state.task_id)
        assert loaded is not None
        assert loaded.active_operation is None

    def test_config_snapshot_contains_sandbox_paths(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=True)
        sandbox = state.config_snapshot["sandbox"]
        assert sandbox["allowed_write_paths"] == ["src/"]
        assert sandbox["allowed_read_paths"] == ["."]

    def test_config_snapshot_contains_extra_rules_when_configured(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        cfg["reviewer"] = {"extra_rules": "sikula/reviewer_rules.md"}
        cfg["security_reviewer"] = {"extra_rules": "sikula/security_rules.md"}

        subprocess_results = [
            _sub(stdout="abc1234\n"),
            _sub(),
            _sub(stdout="@@ -1 +1 @@\n+x"),
            _sub(stdout="src/main.py\n"),
            _sub(),
        ]

        mock_reviewer = MagicMock()
        mock_reviewer.run.side_effect = lambda state: (
            setattr(state, "review_approved", True),
            MagicMock(success=True),
        )[1]
        mock_security = MagicMock()
        mock_security.run.side_effect = lambda state: (
            setattr(state, "security_approved", True),
            MagicMock(success=True),
        )[1]

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=subprocess_results),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
            patch("agents.reviewer_agent.ReviewerAgent", return_value=mock_reviewer),
            patch("agents.security_reviewer_agent.SecurityReviewerAgent", return_value=mock_security),
            patch("sys.exit"),
        ):
            cmd_review(_args(), cfg)

        from core.state import JsonStateStore

        store = JsonStateStore(tmp_path / "state")
        state = store.load(store.list_tasks()[0])
        agents_snap = state.config_snapshot["agents"]
        assert agents_snap["reviewer"]["extra_rules"] == "sikula/reviewer_rules.md"
        assert agents_snap["security_reviewer"]["extra_rules"] == "sikula/security_rules.md"

    def test_config_snapshot_omits_extra_rules_when_not_configured(self, tmp_path: Path):
        state = _run_review(tmp_path, reviewer_approved=True)
        agents_snap = state.config_snapshot["agents"]
        assert "extra_rules" not in agents_snap["reviewer"]
        assert "extra_rules" not in agents_snap["security_reviewer"]


class TestCmdReviewDescriptionValidation:
    def test_requires_description_or_description_file(self, tmp_path: Path, capsys):
        with (
            patch("sikula._find_git_root") as find_git_root,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_review(_args(description=None, description_file=None), _cfg(tmp_path))

        assert exc_info.value.code == 1
        assert not find_git_root.called
        assert "requires --description or --description-file" in capsys.readouterr().out

    def test_rejects_empty_description(self, tmp_path: Path, capsys):
        with (
            patch("sikula._find_git_root") as find_git_root,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_review(_args(description="   ", description_file=None), _cfg(tmp_path))

        assert exc_info.value.code == 1
        assert not find_git_root.called
        assert "review description is empty" in capsys.readouterr().out

    def test_rejects_description_and_description_file_together(self, tmp_path: Path, capsys):
        with (
            patch("sikula._find_git_root") as find_git_root,
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_review(_args(description="Review branch", description_file="review.md"), _cfg(tmp_path))

        assert exc_info.value.code == 1
        assert not find_git_root.called
        assert "either --description or --description-file" in capsys.readouterr().out


class TestEnsureGitignore:
    def test_writes_to_git_info_exclude_not_gitignore(self, tmp_path: Path):
        (tmp_path / ".git" / "info").mkdir(parents=True)
        _ensure_gitignore(tmp_path)
        assert (tmp_path / ".git" / "info" / "exclude").exists()
        assert not (tmp_path / ".gitignore").exists()

    def test_entry_added_to_exclude(self, tmp_path: Path):
        (tmp_path / ".git" / "info").mkdir(parents=True)
        _ensure_gitignore(tmp_path)
        content = (tmp_path / ".git" / "info" / "exclude").read_text()
        assert ".sikula/worktrees/" in content

    def test_idempotent(self, tmp_path: Path):
        (tmp_path / ".git" / "info").mkdir(parents=True)
        _ensure_gitignore(tmp_path)
        _ensure_gitignore(tmp_path)
        content = (tmp_path / ".git" / "info" / "exclude").read_text()
        assert content.count(".sikula/worktrees/") == 1

    def test_appends_to_existing_exclude(self, tmp_path: Path):
        info = tmp_path / ".git" / "info"
        info.mkdir(parents=True)
        (info / "exclude").write_text("# existing entry\n*.log\n")
        _ensure_gitignore(tmp_path)
        content = (info / "exclude").read_text()
        assert "# existing entry" in content
        assert ".sikula/worktrees/" in content

    def test_creates_info_dir_if_missing(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        _ensure_gitignore(tmp_path)
        assert (tmp_path / ".git" / "info" / "exclude").exists()


class TestEnsureProjectGitignoreEntry:
    def test_creates_gitignore_with_entry(self, tmp_path: Path):
        _ensure_project_gitignore_entry(tmp_path, ".env")
        assert (tmp_path / ".gitignore").read_text() == ".env\n"

    def test_appends_to_existing_gitignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("dist/\n")
        _ensure_project_gitignore_entry(tmp_path, ".env")
        assert (tmp_path / ".gitignore").read_text() == "dist/\n.env\n"

    def test_idempotent(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text(".env\n")
        _ensure_project_gitignore_entry(tmp_path, ".env")
        assert (tmp_path / ".gitignore").read_text().count(".env") == 1


class TestCmdReviewWorktreeSetup:
    def test_report_only_uses_detached_head(self, tmp_path: Path):
        """Report-only mode must use --detach so the caller can be on the reviewed branch."""
        calls = []

        def capture(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0:2] == ["git", "rev-parse"]:
                return _sub(stdout="abc1234\n")
            if cmd[0:3] == ["git", "diff", "--name-only"]:
                return _sub(stdout="src/main.py\n")
            if "diff" in cmd:
                return _sub(stdout="@@ -1 +1 @@\n+x")
            return _sub()

        mock_reviewer = MagicMock()
        mock_reviewer.run.side_effect = lambda state: setattr(state, "review_approved", True) or MagicMock(success=True)
        mock_security = MagicMock()
        mock_security.run.side_effect = lambda state: (
            setattr(state, "security_approved", True) or MagicMock(success=True)
        )

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=capture),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
            patch("agents.reviewer_agent.ReviewerAgent", return_value=mock_reviewer),
            patch("agents.security_reviewer_agent.SecurityReviewerAgent", return_value=mock_security),
            patch("sys.exit"),
        ):
            cmd_review(_args(fix=False), _cfg(tmp_path))

        worktree_call = next(c for c in calls if "worktree" in c and "add" in c)
        assert "--detach" in worktree_call
        assert not any(
            c[0:2] == ["git", "worktree"] and "add" in c and "--detach" not in c
            for c in calls
            if "worktree" in c and "add" in c
        )

    def test_fix_mode_uses_branch_checkout(self, tmp_path: Path):
        """Fix mode must use a real branch checkout so _finalize_worktree can commit."""
        calls = []

        def capture(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0:3] == ["git", "diff", "--name-only"]:
                return _sub(stdout="src/main.py\n")
            if "diff" in cmd:
                return _sub(stdout="@@ -1 +1 @@\n+x")
            return _sub()

        mock_state = TaskState(
            task_id="tid",
            task_description="Test review",
            done=True,
            failed=False,
            worktree_branch="feat/x",
        )
        mock_orch = MagicMock()
        mock_orch.run.return_value = mock_state

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=capture),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
            patch("sikula.build_orchestrator", return_value=mock_orch),
            patch("sikula._finalize_worktree", return_value=(True, False, None)),
            patch("sikula._print_review_summary"),
            patch("sys.exit"),
        ):
            cmd_review(_args(fix=True), _cfg(tmp_path))

        worktree_call = next(c for c in calls if "worktree" in c and "add" in c)
        assert "--detach" not in worktree_call
        assert "feat/x" in worktree_call

    def test_fix_mode_copies_env_files(self, tmp_path: Path):
        """Fix mode must copy gitignored build files (e.g. local.properties) into the worktree."""
        (tmp_path / "local.properties").write_text("sdk.dir=/opt/android-sdk\n")

        calls = []

        def capture(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0:3] == ["git", "diff", "--name-only"]:
                return _sub(stdout="src/main.py\n")
            if "diff" in cmd:
                return _sub(stdout="@@ -1 +1 @@\n+x")
            return _sub()

        mock_state = TaskState(
            task_id="tid",
            task_description="Test review",
            done=True,
            failed=False,
            worktree_branch="feat/x",
        )
        mock_orch = MagicMock()
        mock_orch.run.return_value = mock_state

        cfg = _cfg(tmp_path)
        cfg["project"]["build_tool"] = "gradle-android"
        copied = []

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=capture),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
            patch("sikula.build_orchestrator", return_value=mock_orch),
            patch("sikula._finalize_worktree", return_value=(True, False, None)),
            patch("sikula._print_review_summary"),
            patch("sikula.shutil.copy2", side_effect=lambda s, d: copied.append((str(s), str(d)))),
            patch("sys.exit"),
        ):
            cmd_review(_args(fix=True), cfg)

        assert any("local.properties" in src for src, _ in copied)

    def test_report_only_does_not_copy_env_files(self, tmp_path: Path):
        """Report-only mode must not copy any files — it is strictly read-only."""
        (tmp_path / "local.properties").write_text("sdk.dir=/opt/android-sdk\n")

        cfg = _cfg(tmp_path)
        cfg["project"]["build_tool"] = "gradle-android"
        copied = []

        mock_reviewer = MagicMock()
        mock_reviewer.run.side_effect = lambda state: setattr(state, "review_approved", True) or MagicMock(success=True)
        mock_security = MagicMock()
        mock_security.run.side_effect = lambda state: (
            setattr(state, "security_approved", True) or MagicMock(success=True)
        )

        subprocess_results = [
            _sub(stdout="abc1234\n"),
            _sub(),
            _sub(stdout="@@ -1 +1 @@\n+x"),
            _sub(stdout="src/main.py\n"),
            _sub(),
        ]

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=subprocess_results),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
            patch("agents.reviewer_agent.ReviewerAgent", return_value=mock_reviewer),
            patch("agents.security_reviewer_agent.SecurityReviewerAgent", return_value=mock_security),
            patch("sikula.shutil.copy2", side_effect=lambda s, d: copied.append((s, d))),
            patch("sys.exit"),
        ):
            cmd_review(_args(fix=False), cfg)

        assert not copied


class TestWorktreeErrorMessage:
    def test_already_checked_out_shows_helpful_message(self):
        msg = _worktree_error_message("feature/payment", "fatal: 'feature/payment' is already checked out at '/tmp/wt'")
        assert "already checked out" in msg
        assert "git checkout" in msg
        assert "git worktree list" in msg
        assert "git worktree remove" in msg

    def test_already_checked_out_includes_branch_name(self):
        msg = _worktree_error_message("feature/payment", "fatal: 'feature/payment' is already checked out at '/tmp/wt'")
        assert "feature/payment" in msg

    def test_already_used_by_worktree_shows_helpful_message(self):
        msg = _worktree_error_message(
            "feature/payment", "fatal: 'feature/payment' is already used by worktree at '/home/user/project'"
        )
        assert "feature/payment" in msg
        assert "git checkout" in msg
        assert "git worktree list" in msg
        assert "git worktree remove" in msg

    def test_generic_error_shows_stderr(self):
        msg = _worktree_error_message("feature/login", "fatal: not a git repository")
        assert "not a git repository" in msg
        assert "feature/login" in msg

    def test_generic_error_does_not_show_worktree_hint(self):
        msg = _worktree_error_message("feature/login", "fatal: not a git repository")
        assert "git worktree list" not in msg


# ---------------------------------------------------------------------------
# cmd_review --fix: state_store handoff
# ---------------------------------------------------------------------------


class TestCmdReviewFixStateStore:
    """Verify that build_orchestrator receives the store created before root_path is mutated."""

    def test_fix_mode_passes_store_to_build_orchestrator(self, tmp_path: Path):
        # Use relative state_dir — without the fix, root_path mutation would cause
        # build_orchestrator to re-derive a different (worktree) state_dir and lose the task.
        cfg = _cfg(tmp_path)
        cfg["tasks"] = {"state_dir": ".sikula/state"}

        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            captured["state_store"] = state_store
            mock = MagicMock()
            task_id = state_store.list_tasks()[0]
            state = state_store.load(task_id)
            state.done = True
            state.failed = False
            state.review_approved = True
            state.security_approved = True
            mock.run.return_value = state
            return mock

        def capture_sub(cmd, **kwargs):
            if cmd[0:3] == ["git", "diff", "--name-only"]:
                return _sub(stdout="src/main.py\n")
            if "diff" in cmd:
                return _sub(stdout="@@ -1 +1 @@\n+x")
            return _sub()

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=capture_sub),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sikula._finalize_worktree", return_value=(True, False, None)),
            patch("sikula._print_review_summary"),
            patch("sys.exit"),
        ):
            cmd_review(_args(fix=True), cfg)

        assert captured.get("state_store") is not None, "state_store was not passed to build_orchestrator"
        # The store must already contain the task saved before root_path was changed to worktree.
        tasks = captured["state_store"].list_tasks()
        assert len(tasks) == 1
        state = captured["state_store"].load(tasks[0])
        assert state.review_mode == "review_fix"
        assert state.review_base_branch == "main"


# ---------------------------------------------------------------------------
# _enrich_prompt_with_referenced_files — unit tests
# ---------------------------------------------------------------------------


class TestEnrichPromptWithReferencedFiles:
    def test_returns_llm_output_stripped(self):
        llm = MagicMock()
        llm.run_readonly_agent.return_value = "  file contents  "
        result = _enrich_prompt_with_referenced_files("task desc", llm, Path("/tmp"))
        assert result == "file contents"

    def test_returns_empty_string_on_exception(self):
        llm = MagicMock()
        llm.run_readonly_agent.side_effect = RuntimeError("no api key")
        result = _enrich_prompt_with_referenced_files("task desc", llm, Path("/tmp"))
        assert result == ""

    def test_returns_empty_string_when_llm_returns_empty(self):
        llm = MagicMock()
        llm.run_readonly_agent.return_value = "   "
        result = _enrich_prompt_with_referenced_files("task desc", llm, Path("/tmp"))
        assert result == ""

    def test_returns_empty_string_when_no_referenced_files_sentinel(self):
        llm = MagicMock()
        llm.run_readonly_agent.return_value = "  NO_REFERENCED_FILES  "
        result = _enrich_prompt_with_referenced_files("task desc", llm, Path("/tmp"))
        assert result == ""

    def test_passes_task_description_in_prompt(self):
        llm = MagicMock()
        llm.run_readonly_agent.return_value = "NO_REFERENCED_FILES"
        _enrich_prompt_with_referenced_files("See Drawer design.png for layout", llm, Path("/tmp"))
        prompt_arg = llm.run_readonly_agent.call_args[0][0]
        assert "Drawer design.png" in prompt_arg
        assert "NO_REFERENCED_FILES" in prompt_arg

    def test_security_prefix_prepended_to_prompt(self):
        llm = MagicMock()
        llm.run_readonly_agent.return_value = "NO_REFERENCED_FILES"
        _enrich_prompt_with_referenced_files("task", llm, Path("/tmp"))
        prompt_arg = llm.run_readonly_agent.call_args[0][0]
        assert prompt_arg.startswith(AGENT_SECURITY_PREFIX)

    def test_passes_project_root_as_cwd(self):
        llm = MagicMock()
        llm.run_readonly_agent.return_value = "NO_REFERENCED_FILES"
        _enrich_prompt_with_referenced_files("task", llm, Path("/some/project"))
        cwd_arg = llm.run_readonly_agent.call_args.kwargs["cwd"]
        assert cwd_arg == Path("/some/project")


# ---------------------------------------------------------------------------
# cmd_review — design file enrichment integration
# ---------------------------------------------------------------------------


def _fix_sub(cmd, **kwargs):
    if cmd[0:3] == ["git", "diff", "--name-only"]:
        return _sub(stdout="src/main.py\n")
    if "diff" in cmd:
        return _sub(stdout="@@ -1 +1 @@\n+x")
    return _sub()


def _mock_orchestrator():
    mock = MagicMock()
    mock.run.return_value = TaskState(
        done=True,
        failed=False,
        task_id="tid",
        task_description="Test review",
        review_approved=True,
        security_approved=True,
        worktree_branch="feat/x",
    )
    return mock


class TestCmdReviewDesignFileEnrichment:
    def test_report_only_enriches_prompt_when_files_found(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        subprocess_results = [
            _sub(stdout="abc1234\n"),
            _sub(),
            _sub(stdout="@@ -1 +1 @@\n+x"),
            _sub(stdout="src/main.py\n"),
            _sub(),
        ]

        def reviewer_run(state):
            state.review_approved = True
            return MagicMock(success=True)

        def security_run(state):
            state.security_approved = True
            return MagicMock(success=True)

        mock_reviewer = MagicMock()
        mock_reviewer.run.side_effect = reviewer_run
        mock_security = MagicMock()
        mock_security.run.side_effect = security_run

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=subprocess_results),
            patch("sikula._enrich_prompt_with_referenced_files", return_value="## Drawer\n[image content]"),
            patch("agents.reviewer_agent.ReviewerAgent", return_value=mock_reviewer),
            patch("agents.security_reviewer_agent.SecurityReviewerAgent", return_value=mock_security),
            patch("sys.exit"),
        ):
            cmd_review(_args(description="See Drawer design.png"), cfg)

        from core.state import JsonStateStore

        store = JsonStateStore(tmp_path / "state")
        state = store.load(store.list_tasks()[0])
        assert "Files referenced in the task" in state.implementation_prompt
        assert "## Drawer" in state.implementation_prompt

    def test_report_only_leaves_prompt_unchanged_when_no_files_found(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        subprocess_results = [
            _sub(stdout="abc1234\n"),
            _sub(),
            _sub(stdout="@@ -1 +1 @@\n+x"),
            _sub(stdout="src/main.py\n"),
            _sub(),
        ]

        def reviewer_run(state):
            state.review_approved = True
            return MagicMock(success=True)

        def security_run(state):
            state.security_approved = True
            return MagicMock(success=True)

        mock_reviewer = MagicMock()
        mock_reviewer.run.side_effect = reviewer_run
        mock_security = MagicMock()
        mock_security.run.side_effect = security_run

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=subprocess_results),
            patch("sikula._enrich_prompt_with_referenced_files", return_value=""),
            patch("agents.reviewer_agent.ReviewerAgent", return_value=mock_reviewer),
            patch("agents.security_reviewer_agent.SecurityReviewerAgent", return_value=mock_security),
            patch("sys.exit"),
        ):
            cmd_review(_args(description="Simple review"), cfg)

        from core.state import JsonStateStore

        store = JsonStateStore(tmp_path / "state")
        state = store.load(store.list_tasks()[0])
        assert state.implementation_prompt == "Simple review"

    def test_fix_mode_enriches_prompt_before_orchestrator(self, tmp_path: Path):
        cfg = _cfg(tmp_path)
        captured: dict = {}

        def capture_orch(cfg_arg, overrides=None, state_store=None):
            tasks = state_store.list_tasks()
            if tasks:
                captured["prompt"] = state_store.load(tasks[0]).implementation_prompt
            return _mock_orchestrator()

        with (
            patch("sikula._find_git_root", return_value=tmp_path),
            patch("sikula._ensure_gitignore"),
            patch("subprocess.run", side_effect=_fix_sub),
            patch("sikula._enrich_prompt_with_referenced_files", return_value="## Drawer\n[image]"),
            patch("sikula.build_orchestrator", side_effect=capture_orch),
            patch("sikula._finalize_worktree", return_value=(True, False, None)),
            patch("sikula._print_review_summary"),
            patch("sys.exit"),
        ):
            cmd_review(_args(fix=True, description="See Drawer design.png"), cfg)

        assert "Files referenced in the task" in captured.get("prompt", "")
        assert "## Drawer" in captured.get("prompt", "")
