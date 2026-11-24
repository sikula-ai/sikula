"""Custom Gradle sync commands preserve shell, timeout and default task semantics."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.base_tool import Sandbox, ToolResult
from tools.build_factory import create_build_tool
from tools.gradle_android_tool import AndroidGradleTool
from tools.gradle_jvm_tool import JvmGradleTool


@pytest.mark.parametrize(
    "platform,kind,default",
    [
        ("gradle-android", AndroidGradleTool, "generateDebugSources"),
        ("gradle-jvm", JvmGradleTool, "classes"),
    ],
)
def test_gradle_sync_factory_defaults_and_override(tmp_path, platform, kind, default):
    sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
    config = {"project": {"build_tool": platform}, "build": {"sync_timeout": 17}}
    tool = create_build_tool(sandbox, tmp_path, config)
    assert isinstance(tool, kind)
    with patch.object(tool, "_run", return_value=ToolResult(True, "")) as run:
        assert tool.sync().success
    run.assert_called_once_with(default, "--parallel", timeout=17)
    config["build"].update(sync_command="custom-wrapper lockTask generateTask --flag", sync_task="ignored")
    tool = create_build_tool(sandbox, tmp_path, config)
    with (
        patch.object(tool, "_run_shell", return_value=ToolResult(True, "")) as shell,
        patch.object(tool, "_run") as run,
    ):
        assert tool.sync().success
    shell.assert_called_once_with(config["build"]["sync_command"], timeout=17)
    run.assert_not_called()


@pytest.mark.parametrize("kind", [AndroidGradleTool, JvmGradleTool])
@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_custom_gradle_sync_uses_existing_platform_shell_runner(tmp_path, kind, windows, outcome):
    sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
    command = 'custom-wrapper "two tasks" --update-locks selected:module'
    tool = kind(sandbox, tmp_path, sync_command=command, sync_timeout=19)
    runner = "run_windows_shell_process" if windows else "subprocess.run"
    completed = SimpleNamespace(returncode=1 if outcome == "failure" else 0, stdout="resolved\n", stderr="")
    with (
        patch("tools.gradle_tool.os", SimpleNamespace(name="nt" if windows else "posix")),
        patch(
            f"tools.gradle_tool.{runner}",
            return_value=completed,
            side_effect=subprocess.TimeoutExpired(command, 19) if outcome == "timeout" else None,
        ) as execute,
    ):
        result = tool.sync()
    assert result.success == (outcome == "success")
    assert execute.call_args.args == (command,)
    assert execute.call_args.kwargs["timeout"] == 19
    assert execute.call_args.kwargs["cwd"] == tmp_path
    assert execute.call_args.kwargs.get("shell", False) == (not windows)
    if outcome == "timeout":
        assert "timed out" in result.error


@pytest.mark.parametrize("kind", [AndroidGradleTool, JvmGradleTool])
@pytest.mark.parametrize("command", ["", "  ", False, ["task"]])
def test_invalid_custom_sync_cannot_silently_skip_resolution(tmp_path, kind, command):
    sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
    with pytest.raises(ValueError, match="non-empty string"):
        kind(sandbox, tmp_path, sync_command=command)
