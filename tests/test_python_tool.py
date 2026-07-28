"""Tests for tools/python_tool.py — PythonTool."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base_tool import Sandbox
from tools.python_tool import PythonTool, _BUILD_CONFIG_FILES

_SHELL_RUNNER = "tools.python_tool.run_windows_shell_process" if os.name == "nt" else "tools.python_tool.subprocess.run"


def _make_tool(root: Path, compile_command: str = "ruff check .", test_command: str = "pytest") -> PythonTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return PythonTool(sandbox=sandbox, project_root=root, compile_command=compile_command, test_command=test_command)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestPythonToolRun:
    def test_success_returns_combined_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run(stdout="ok\n", stderr="warn\n")):
            result = tool.compile_check()
        assert result.success
        assert "ok" in result.output
        assert "warn" in result.output

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run(returncode=1, stderr="error")):
            result = tool.compile_check()
        assert not result.success
        assert "error" in result.error

    def test_stdout_only_failure_captured_in_error(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            _SHELL_RUNNER,
            return_value=_mock_run(returncode=1, stdout="FAILED test_foo.py::test_bar", stderr=""),
        ):
            result = tool.run_tests()
        assert not result.success
        assert "FAILED test_foo.py::test_bar" in result.error

    def test_exit_code_5_treated_as_success(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run(returncode=5)):
            result = tool.run_tests()
        assert result.success

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, side_effect=__import__("subprocess").TimeoutExpired("cmd", 1)):
            result = tool.compile_check()
        assert not result.success
        assert "timed out" in result.error

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, side_effect=OSError("no such file")):
            result = tool.compile_check()
        assert not result.success
        assert "no such file" in result.error

    def test_custom_timeout_passed_to_subprocess(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("lint", {"command": "ruff check .", "timeout": 42})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 42

    def test_default_timeout_used_when_not_specified(self, tmp_path: Path):
        tool = PythonTool(
            sandbox=Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."]),
            project_root=tmp_path,
            timeout=999,
        )
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 999


class TestPythonToolCompileCheck:
    def test_replaces_undecodable_output_with_locale_encoding(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        assert mock.call_args.kwargs["errors"] == "replace"
        assert "encoding" not in mock.call_args.kwargs

    def test_uses_configured_compile_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="mypy .")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert args[0] == "mypy ."

    def test_runs_in_project_root(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["cwd"] == tmp_path.resolve()


class TestPythonToolRunTests:
    def test_uses_configured_test_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, test_command="python3 -m pytest tests/ -v")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert args[0] == "python3 -m pytest tests/ -v"


class TestPythonToolRunCheck:
    def test_uses_command_from_task_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("ruff-format", {"command": "ruff format --check ."})
        args, _ = mock.call_args
        assert args[0] == "ruff format --check ."

    def test_falls_back_to_name_when_no_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("detekt", {})
        args, _ = mock.call_args
        assert args[0] == "detekt"

    def test_timeout_from_task_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("lint", {"command": "ruff .", "timeout": "120"})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 120


class TestPythonToolSync:
    def test_skips_sync_when_no_requirements_txt(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        result = tool.sync()
        assert result.success
        assert "skipping" in result.output

    def test_runs_pip_install_when_requirements_txt_exists(self, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("pyyaml\n")
        tool = _make_tool(tmp_path)
        executable = r"C:\Program Files\Python\python.exe"
        with (
            patch("tools.python_tool.sys.executable", executable),
            patch("tools.python_tool.subprocess.run", return_value=_mock_run()) as mock,
        ):
            result = tool.sync()
        assert result.success
        args, _ = mock.call_args
        assert args[0] == [executable, "-m", "pip", "install", "-r", "requirements.txt"]
        assert mock.call_args.kwargs["shell"] is False


class TestPythonToolIsBuildConfigFile:
    @pytest.mark.parametrize("filename", _BUILD_CONFIG_FILES)
    def test_recognizes_build_config_files(self, filename: str):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file(filename) is True

    def test_recognizes_nested_build_config_file(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("subdir/requirements.txt") is True

    def test_rejects_non_build_config_file(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("agents/analyst_agent.py") is False

    def test_rejects_partial_match(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("not-requirements.txt") is False
