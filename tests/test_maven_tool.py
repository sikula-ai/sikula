"""Tests for tools/maven_tool.py — MavenTool."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.subprocess_utils import resolve_windows_batch_command
from tools.base_tool import BuildTool, Sandbox
from tools.maven_tool import MavenTool, _BUILD_CONFIG_DIRS, _BUILD_CONFIG_FILES

_SHELL_RUNNER = "tools.maven_tool.run_windows_shell_process" if os.name == "nt" else "tools.maven_tool.subprocess.run"


@pytest.fixture(autouse=True)
def _avoid_host_maven_batch_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tools.maven_tool.resolve_windows_batch_command",
        lambda command: (command, None, None),
    )


def _make_tool(root: Path, **kwargs) -> MavenTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return MavenTool(sandbox=sandbox, project_root=root, **kwargs)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestMavenToolInheritance:
    def test_is_build_tool(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert isinstance(tool, BuildTool)

    def test_env_files_is_empty(self, tmp_path: Path):
        assert MavenTool.env_files() == []


class TestMavenToolMvnwDetection:
    def test_uses_mvnw_when_wrapper_exists(self, tmp_path: Path):
        wrapper = tmp_path / ("mvnw.cmd" if os.name == "nt" else "mvnw")
        wrapper.write_text("")
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert args[0] == [str(wrapper), "compile"]
        assert mock.call_args.kwargs.get("shell") is None

    def test_falls_back_to_mvn_when_no_wrapper(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert args[0] == ["mvn", "compile"]

    def test_explicit_compile_command_overrides_detection(self, tmp_path: Path):
        (tmp_path / "mvnw").write_text("")
        tool = _make_tool(tmp_path, compile_command="mvn compile -P prod")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert args[0] == "mvn compile -P prod"
        if os.name == "nt":
            assert "shell" not in mock.call_args.kwargs
        else:
            assert mock.call_args.kwargs["shell"] is True

    def test_windows_uses_mvnw_cmd(self, tmp_path: Path):
        wrapper = tmp_path / "mvnw.cmd"
        wrapper.write_text("@echo off\r\n")
        resolved = str(wrapper.resolve())

        with (
            patch("tools.maven_tool.os.name", "nt"),
            patch("tools.maven_tool.resolve_windows_batch_command", side_effect=resolve_windows_batch_command),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("tools.maven_tool.run_windows_batch_process", return_value=_mock_run()) as run,
        ):
            tool = _make_tool(tmp_path)
            tool.compile_check()

        assert tool._mvn_bin.endswith("mvnw.cmd")
        assert run.call_args.args[0].endswith(r'/c "%_SIKULA_BATCH_COMMAND% %_SIKULA_BATCH_ARG_0%"')
        assert run.call_args.kwargs["executable"] == r"C:\Windows\System32\cmd.exe"
        assert run.call_args.kwargs["env"]["_SIKULA_BATCH_COMMAND"] == resolved
        assert run.call_args.kwargs["env"]["_SIKULA_BATCH_ARG_0"] == "compile"

    def test_windows_configured_command_uses_tree_aware_shell_runner(self, tmp_path: Path):
        with (
            patch("tools.maven_tool.os.name", "nt"),
            patch("tools.maven_tool.run_windows_shell_process", return_value=_mock_run()) as run,
        ):
            tool = _make_tool(tmp_path, compile_command="mvn compile -P prod")
            result = tool.compile_check()

        assert result.success
        run.assert_called_once_with(
            "mvn compile -P prod",
            capture_output=True,
            text=True,
            errors="replace",
            cwd=tmp_path.resolve(),
            timeout=600,
        )

    @pytest.mark.skipif(os.name != "nt", reason="requires the Windows command processor")
    def test_windows_configured_command_executes(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="echo CONFIGURED_MAVEN_COMMAND")

        result = tool.compile_check()

        assert result.success
        assert "CONFIGURED_MAVEN_COMMAND" in result.output


class TestMavenToolCommands:
    def test_replaces_undecodable_output_with_locale_encoding(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        assert mock.call_args.kwargs["errors"] == "replace"
        assert "encoding" not in mock.call_args.kwargs

    def test_compile_check_runs_compile(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "compile" in args[0]

    def test_run_tests_runs_test(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert "test" in args[0]

    def test_sync_runs_dependency_resolve(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert "dependency:resolve" in args[0]

    def test_generate_sources_runs_generate_sources(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.generate_sources()
        args, _ = mock.call_args
        assert "generate-sources" in args[0]

    def test_runs_in_project_root(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["cwd"] == tmp_path.resolve()


class TestMavenToolResults:
    def test_success_returns_combined_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run(stdout="BUILD SUCCESS\n")):
            result = tool.compile_check()
        assert result.success
        assert "BUILD SUCCESS" in result.output

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run(returncode=1, stderr="BUILD FAILURE")):
            result = tool.compile_check()
        assert not result.success
        assert "BUILD FAILURE" in result.error

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.maven_tool.subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("cmd", 1),
        ):
            result = tool.compile_check()
        assert not result.success
        assert "timed out" in result.error.lower()

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.maven_tool.subprocess.run", side_effect=OSError("mvn not found")):
            result = tool.compile_check()
        assert not result.success
        assert "mvn not found" in result.error


class TestMavenToolPresyncClean:
    def test_presync_clean_runs_clean_first(self, tmp_path: Path):
        tool = _make_tool(tmp_path, presync_clean=True)
        call_args_list = []
        with patch("tools.maven_tool.subprocess.run", return_value=_mock_run()) as mock:
            mock.side_effect = lambda *a, **kw: call_args_list.append(a[0] if a else kw.get("args", "")) or _mock_run()
            tool.generate_sources()
        assert any("clean" in str(c) for c in call_args_list)

    def test_presync_clean_aborts_when_clean_fails(self, tmp_path: Path):
        tool = _make_tool(tmp_path, presync_clean=True)
        call_count = {"n": 0}

        def side_effect(*args, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mock_run(returncode=1, stderr="clean failed")
            return _mock_run()

        with patch("tools.maven_tool.subprocess.run", side_effect=side_effect):
            result = tool.generate_sources()
        assert not result.success
        assert call_count["n"] == 1


class TestMavenToolRunCheck:
    def test_uses_command_from_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("checkstyle", {"command": "./mvnw checkstyle:check"})
        args, _ = mock.call_args
        assert args[0] == "./mvnw checkstyle:check"

    def test_falls_back_to_name_when_no_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("spotbugs", {})
        args, _ = mock.call_args
        assert args[0] == "spotbugs"

    def test_timeout_from_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("checkstyle", {"command": "./mvnw checkstyle:check", "timeout": "120"})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 120


class TestMavenToolIsBuildConfigFile:
    @pytest.mark.parametrize("filename", _BUILD_CONFIG_FILES)
    def test_recognizes_build_config_files(self, filename: str, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file(filename) is True

    @pytest.mark.parametrize("directory", _BUILD_CONFIG_DIRS)
    def test_recognizes_build_config_dirs(self, directory: str, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file(f"{directory}settings.xml") is True

    def test_rejects_regular_source_file(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file("src/main/java/CountryService.java") is False
