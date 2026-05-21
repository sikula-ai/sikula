"""Tests for tools/gradle_jvm_tool.py — JvmGradleTool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.base_tool import Sandbox
from tools.gradle_jvm_tool import JvmGradleTool
from tools.gradle_tool import GradleBaseTool


def _make_tool(root: Path, **kwargs) -> JvmGradleTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return JvmGradleTool(sandbox=sandbox, project_root=root, **kwargs)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestJvmGradleToolInheritance:
    def test_is_gradle_base_tool(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert isinstance(tool, GradleBaseTool)

    def test_env_files_is_empty(self, tmp_path: Path):
        assert JvmGradleTool.env_files() == []


class TestJvmGradleToolDefaults:
    def test_compile_check_uses_classes_by_default(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "classes" in args[0]

    def test_run_tests_uses_test_by_default(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert "test" in args[0]

    def test_sync_uses_classes_by_default(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert "classes" in args[0]

    def test_sync_passes_parallel_flag(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert "--parallel" in args[0]

    def test_generate_sources_uses_classes_by_default(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.generate_sources()
        args, _ = mock.call_args
        assert "classes" in args[0]


class TestJvmGradleToolConfigurable:
    def test_compile_check_uses_configured_task(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_task="compileKotlin")
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "compileKotlin" in args[0]

    def test_run_tests_uses_configured_task(self, tmp_path: Path):
        tool = _make_tool(tmp_path, test_task="testIntegration")
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert "testIntegration" in args[0]

    def test_sync_uses_configured_sync_task(self, tmp_path: Path):
        tool = _make_tool(tmp_path, sync_task="dependencies")
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert "dependencies" in args[0]

    def test_generate_sources_uses_configured_presync_task(self, tmp_path: Path):
        tool = _make_tool(tmp_path, presync_task="openApiGenerate")
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.generate_sources()
        args, _ = mock.call_args
        assert "openApiGenerate" in args[0]


class TestJvmGradleToolPresyncClean:
    def test_presync_clean_runs_clean_first(self, tmp_path: Path):
        tool = _make_tool(tmp_path, presync_clean=True)
        call_args_list = []
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            mock.side_effect = lambda args, **kw: call_args_list.append(args) or _mock_run()
            tool.generate_sources()
        assert any("clean" in a for a in call_args_list[0])

    def test_presync_clean_aborts_when_clean_fails(self, tmp_path: Path):
        tool = _make_tool(tmp_path, presync_clean=True)
        call_count = {"n": 0}

        def side_effect(args, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mock_run(returncode=1, stderr="clean failed")
            return _mock_run()

        with patch("tools.gradle_tool.subprocess.run", side_effect=side_effect):
            result = tool.generate_sources()
        assert not result.success
        assert call_count["n"] == 1


class TestJvmGradleToolRunCheck:
    def test_uses_command_from_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("detekt", {"command": "./gradlew detekt"})
        args, _ = mock.call_args
        assert args[0] == "./gradlew detekt"

    def test_falls_back_to_name_when_no_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("checkstyle", {})
        args, _ = mock.call_args
        assert args[0] == "checkstyle"
