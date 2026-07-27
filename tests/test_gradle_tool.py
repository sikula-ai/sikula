"""Tests for tools/gradle_tool.py — GradleBaseTool and AndroidGradleTool."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base_tool import Sandbox
from tools.gradle_android_tool import AndroidGradleTool
from tools.gradle_tool import GradleBaseTool, _BUILD_CONFIG_DIRS, _BUILD_CONFIG_SUFFIXES


def _make_tool(root: Path, **kwargs) -> AndroidGradleTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return AndroidGradleTool(sandbox=sandbox, project_root=root, **kwargs)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestGradleBaseToolRun:
    def test_success_returns_combined_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run(stdout="BUILD SUCCESS\n")):
            result = tool.compile_check()
        assert result.success
        assert "BUILD SUCCESS" in result.output

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run(returncode=1, stderr="BUILD FAILED")):
            result = tool.compile_check()
        assert not result.success
        assert "BUILD FAILED" in result.error

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.gradle_tool.subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("cmd", 1),
        ):
            result = tool.compile_check()
        assert not result.success
        assert "timed out" in result.error.lower()

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", side_effect=OSError("gradlew not found")):
            result = tool.compile_check()
        assert not result.success
        assert "gradlew not found" in result.error

    def test_runs_in_project_root(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["cwd"] == tmp_path.resolve()

    def test_replaces_undecodable_output_with_locale_encoding(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        assert mock.call_args.kwargs["errors"] == "replace"
        assert "encoding" not in mock.call_args.kwargs

    def test_windows_invokes_gradlew_bat(self, tmp_path: Path):
        resolved = str(tmp_path.resolve() / "gradlew.bat")
        with (
            patch("tools.gradle_tool.os.name", "nt"),
            patch("core.subprocess_utils.shutil.which", return_value=resolved),
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}),
            patch("tools.gradle_tool.run_windows_batch_process", return_value=_mock_run()) as run,
        ):
            tool = _make_tool(tmp_path)
            tool.compile_check()

        assert str(tool._gradlew).endswith("gradlew.bat")
        assert run.call_args.args[0].endswith(r'/c "%_SIKULA_BATCH_COMMAND% %_SIKULA_BATCH_ARG_0%"')
        assert run.call_args.kwargs["executable"] == r"C:\Windows\System32\cmd.exe"
        assert run.call_args.kwargs["env"]["_SIKULA_BATCH_COMMAND"] == resolved
        assert run.call_args.kwargs["env"]["_SIKULA_BATCH_ARG_0"] == "compileDebugKotlin"
        assert run.call_args.kwargs["timeout"] == 1800

    def test_posix_invokes_extensionless_gradlew(self, tmp_path: Path):
        with (
            patch("tools.gradle_tool.os.name", "posix"),
            patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock,
        ):
            tool = _make_tool(tmp_path)
            tool.compile_check()

        assert str(tool._gradlew).endswith("gradlew")
        assert not str(tool._gradlew).endswith(".bat")
        assert mock.call_args.args[0][0].endswith("gradlew")
        assert "executable" not in mock.call_args.kwargs

    @pytest.mark.skipif(os.name != "nt", reason="requires the Windows command processor")
    def test_windows_gradle_batch_wrapper_executes(self, tmp_path: Path):
        wrapper = tmp_path / "gradlew.bat"
        wrapper.write_text("@echo off\r\necho TASK:%1\r\n", encoding="utf-8")
        tool = _make_tool(tmp_path)

        result = tool._run("classes")

        assert result.success
        assert "TASK:classes" in result.output

    @pytest.mark.skipif(os.name != "nt", reason="requires Windows process groups")
    def test_windows_gradle_timeout_terminates_descendant(self, tmp_path: Path):
        started = tmp_path / "child-started"
        survived = tmp_path / "child-survived"
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('started')\n"
            "time.sleep(3)\n"
            "pathlib.Path(sys.argv[2]).write_text('survived')\n",
            encoding="utf-8",
        )
        parent_script = tmp_path / "parent.py"
        parent_script.write_text(
            "import pathlib, subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(child_script)!r}, {str(started)!r}, {str(survived)!r}])\n"
            f"started = pathlib.Path({str(started)!r})\n"
            "deadline = time.monotonic() + 2\n"
            "while not started.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        wrapper = tmp_path / "gradlew.bat"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{parent_script}"\r\n',
            encoding="utf-8",
        )
        tool = _make_tool(tmp_path)

        result = tool._run("classes", timeout=1)

        assert not result.success
        assert started.exists()
        time.sleep(2.5)
        assert not survived.exists()

    @pytest.mark.skipif(os.name != "nt", reason="requires Windows job objects")
    def test_windows_gradle_completion_terminates_descendant_after_wrapper_exits(self, tmp_path: Path):
        started = tmp_path / "child-started"
        survived = tmp_path / "child-survived"
        child_script = tmp_path / "child.py"
        child_script.write_text(
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('started')\n"
            "time.sleep(3)\n"
            "pathlib.Path(sys.argv[2]).write_text('survived')\n",
            encoding="utf-8",
        )
        parent_script = tmp_path / "parent.py"
        parent_script.write_text(
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen(\n"
            "    [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],\n"
            "    stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            ")\n"
            "started = pathlib.Path(sys.argv[2])\n"
            "deadline = time.monotonic() + 2\n"
            "while not started.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n",
            encoding="utf-8",
        )
        wrapper = tmp_path / "gradlew.bat"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{parent_script}" "{child_script}" "{started}" "{survived}"\r\n',
            encoding="utf-8",
        )
        tool = _make_tool(tmp_path)

        result = tool._run("classes", timeout=5)

        assert result.success
        assert started.exists()
        time.sleep(2.5)
        assert not survived.exists()


class TestAndroidGradleToolTasks:
    def test_compile_check_uses_configured_task(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_task="compileReleaseKotlin")
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert "compileReleaseKotlin" in args[0]

    def test_run_tests_uses_configured_task(self, tmp_path: Path):
        tool = _make_tool(tmp_path, test_task="testReleaseUnitTest")
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert "testReleaseUnitTest" in args[0]

    def test_sync_runs_generate_debug_sources(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert "generateDebugSources" in args[0]

    def test_sync_passes_parallel_flag(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert "--parallel" in args[0]


class TestGradleBaseToolRunCheck:
    def test_uses_command_from_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("detekt", {"command": "./gradlew detektMain"})
        args, _ = mock.call_args
        assert args[0] == "./gradlew detektMain"

    def test_falls_back_to_name_when_no_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("lint", {})
        args, _ = mock.call_args
        assert args[0] == "lint"

    def test_timeout_from_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("detekt", {"command": "./gradlew detekt", "timeout": "300"})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 300


class TestAndroidGradleToolGenerateSources:
    def test_runs_configured_presync_task(self, tmp_path: Path):
        tool = _make_tool(tmp_path, presync_task="openApiGenerateAll")
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.generate_sources()
        args, _ = mock.call_args
        assert "openApiGenerateAll" in args[0]

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


class TestGradleBaseToolIsBuildConfigFile:
    @pytest.mark.parametrize("suffix", _BUILD_CONFIG_SUFFIXES)
    def test_recognizes_build_config_suffixes(self, suffix: str, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file(f"app/build{suffix}") is True

    @pytest.mark.parametrize("directory", _BUILD_CONFIG_DIRS)
    def test_recognizes_build_config_dirs(self, directory: str, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file(f"{directory}settings.kt") is True

    def test_rejects_regular_source_file(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file("app/src/main/Login.kt") is False

    def test_handles_windows_path_separators(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file("gradle\\wrapper\\gradle-wrapper.properties") is True


class TestGradleBaseToolIsSyncAdoptableFile:
    @pytest.mark.parametrize(
        "path",
        [
            "gradle.lockfile",
            "app/gradle.lockfile",
            "gradle/dependency-locks/compileClasspath.lockfile",
            "gradle/verification-metadata.xml",
            "gradle/verification-keyring.keys",
        ],
    )
    def test_recognizes_gradle_sync_outputs(self, path: str, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_sync_adoptable_file(path) is True

    def test_rejects_regular_build_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_sync_adoptable_file("build.gradle.kts") is False
        assert tool.is_sync_adoptable_file("src/generated.lockfile") is False


class TestAndroidGradleToolEnvFiles:
    def test_returns_local_properties(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert "local.properties" in tool.env_files()


class TestGradleBaseToolRunShell:
    def test_success_returns_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run(stdout="ok")):
            result = tool.run_check("lint", {"command": "./gradlew lintDebug"})
        assert result.success
        assert "ok" in result.output

    def test_replaces_undecodable_output_with_locale_encoding(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.run_check("lint", {"command": "./gradlew lintDebug"})
        assert mock.call_args.kwargs["errors"] == "replace"
        assert "encoding" not in mock.call_args.kwargs

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run(returncode=1, stderr="FAILED")):
            result = tool.run_check("lint", {"command": "./gradlew lintDebug"})
        assert not result.success
        assert "FAILED" in result.error

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            "tools.gradle_tool.subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("cmd", 1),
        ):
            result = tool.run_check("lint", {"command": "./gradlew lintDebug"})
        assert not result.success
        assert "timed out" in result.error.lower()

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", side_effect=OSError("not found")):
            result = tool.run_check("lint", {"command": "./gradlew lintDebug"})
        assert not result.success
        assert "not found" in result.error


class TestAndroidGradleToolExtras:
    def test_assemble_runs_assemble_debug(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.assemble()
        args, _ = mock.call_args
        assert "assembleDebug" in args[0]

    def test_lint_runs_lint_debug(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch("tools.gradle_tool.subprocess.run", return_value=_mock_run()) as mock:
            tool.lint()
        args, _ = mock.call_args
        assert "lintDebug" in args[0]


class TestGradleBaseToolInheritance:
    def test_android_gradle_tool_is_gradle_base_tool(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert isinstance(tool, GradleBaseTool)
