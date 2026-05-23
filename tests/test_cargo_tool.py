"""Tests for tools/cargo_tool.py — CargoTool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base_tool import Sandbox
from tools.cargo_tool import CargoTool, _BUILD_CONFIG_FILES


def _make_tool(root: Path, compile_command: str = "cargo check", test_command: str = "cargo test") -> CargoTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return CargoTool(sandbox=sandbox, project_root=root, compile_command=compile_command, test_command=test_command)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestCargoToolRun:
    def test_success_returns_combined_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run(stdout="ok\n", stderr="warn\n")):
            result = tool.compile_check()
        assert result.success
        assert "ok" in result.output
        assert "warn" in result.output

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.cargo_tool.subprocess.run", return_value=_mock_run(returncode=1, stderr="error: type mismatch")
        ):
            result = tool.compile_check()
        assert not result.success
        assert "error: type mismatch" in result.error

    def test_stdout_only_failure_captured_in_error(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.cargo_tool.subprocess.run",
            return_value=_mock_run(returncode=101, stdout="test test_foo ... FAILED", stderr=""),
        ):
            result = tool.run_tests()
        assert not result.success
        assert "test test_foo ... FAILED" in result.error

    def test_long_test_output_keeps_failure_block_not_only_tail(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        output = (
            "Compiling workspace\n"
            + "".join(f"build line {i}\n" for i in range(300))
            + "thread 'test_rejects_wrong_result_type' panicked at assertion failed\n"
            + "failures:\n"
            + "    test_rejects_wrong_result_type\n"
            + "".join(f"Running unrelated test binary {i}\n" for i in range(500))
            + "error: test failed, to rerun pass `-p example_crate --test validation_tests`\n"
        )
        with patch(
            "tools.cargo_tool.subprocess.run",
            return_value=_mock_run(returncode=101, stdout=output, stderr=""),
        ):
            result = tool.run_tests()
        assert not result.success
        assert "test_rejects_wrong_result_type" in result.error
        assert "error: test failed" in result.error

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("cmd", 1)):
            result = tool.compile_check()
        assert not result.success
        assert "timed out" in result.error

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", side_effect=OSError("cargo not found")):
            result = tool.compile_check()
        assert not result.success
        assert "cargo not found" in result.error

    def test_runs_in_project_root(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["cwd"] == tmp_path.resolve()

    def test_default_timeout_used(self, tmp_path: Path):
        tool = CargoTool(
            sandbox=Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."]),
            project_root=tmp_path,
            timeout=999,
        )
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 999


class TestCargoToolCompileCheck:
    def test_uses_configured_compile_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="cargo check --workspace")
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert args[0] == "cargo check --workspace"


class TestCargoToolRunTests:
    def test_uses_configured_test_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, test_command="cargo test --workspace")
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert args[0] == "cargo test --workspace"


class TestCargoToolSync:
    def test_runs_cargo_fetch(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            result = tool.sync()
        assert result.success
        args, _ = mock.call_args
        assert args[0] == "cargo fetch"


class TestCargoToolRunCheck:
    def test_uses_command_from_task_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("clippy", {"command": "cargo clippy -- -D warnings"})
        args, _ = mock.call_args
        assert args[0] == "cargo clippy -- -D warnings"

    def test_falls_back_to_name_when_no_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("cargo clippy", {})
        args, _ = mock.call_args
        assert args[0] == "cargo clippy"

    def test_custom_timeout_from_task_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.cargo_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("fmt", {"command": "cargo fmt --check", "timeout": "30"})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 30


class TestCargoToolIsBuildConfigFile:
    @pytest.mark.parametrize("filename", _BUILD_CONFIG_FILES)
    def test_recognizes_build_config_files(self, filename: str):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file(filename) is True

    def test_recognizes_nested_cargo_toml(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("crates/core/Cargo.toml") is True

    def test_rejects_non_build_config_file(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("src/main.rs") is False

    def test_rejects_partial_match(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("not-Cargo.toml") is False
