from __future__ import annotations

from pathlib import Path

from tools.base_tool import BuildTool, Sandbox
from tools.cargo_tool import CargoTool
from tools.gradle_android_tool import AndroidGradleTool
from tools.node_tool import NodeTool
from tools.python_tool import PythonTool


def build_tool_class(project_config: dict) -> type[BuildTool]:
    """Return the configured platform class for static platform policy hooks."""
    platform = project_config.get("project", {}).get("build_tool", "gradle-android")
    if platform == "python":
        return PythonTool
    if platform == "cargo":
        return CargoTool
    if platform == "node":
        return NodeTool
    if platform == "xcodebuild":
        from tools.xcode_tool import XcodeTool

        return XcodeTool
    if platform == "gradle-jvm":
        from tools.gradle_jvm_tool import JvmGradleTool

        return JvmGradleTool
    if platform == "maven":
        from tools.maven_tool import MavenTool

        return MavenTool
    return AndroidGradleTool


def create_build_tool(sandbox: Sandbox, root: Path, project_config: dict) -> BuildTool:
    """Create the configured platform build tool for pipeline-owned validation."""
    platform = project_config.get("project", {}).get("build_tool", "gradle-android")
    build = project_config.get("build", {})
    if platform == "python":
        return PythonTool(
            sandbox,
            root,
            compile_command=build.get("compile_command", "ruff check ."),
            test_command=build.get("test_command", "pytest"),
            timeout=build.get("timeout", 300),
        )
    if platform == "cargo":
        return CargoTool(
            sandbox,
            root,
            sync_command=build.get("sync_command"),
            compile_command=build.get("compile_command", "cargo check"),
            test_command=build.get("test_command", "cargo test"),
            timeout=build.get("timeout", 600),
        )
    if platform == "node":
        timeout = build.get("timeout")
        return NodeTool(
            sandbox,
            root,
            package_manager=build.get("package_manager"),
            sync_command=build.get("sync_command"),
            compile_command=build.get("compile_command"),
            test_command=build.get("test_command"),
            sync_timeout=build.get("sync_timeout", timeout or 600),
            compile_timeout=build.get("compile_timeout", timeout or 600),
            test_timeout=build.get("test_timeout", timeout or 600),
        )
    if platform == "xcodebuild":
        from tools.xcode_tool import XcodeTool

        return XcodeTool(
            sandbox,
            root,
            scheme=build.get("scheme", "Countries"),
            destination=build.get("destination", "generic/platform=iOS Simulator"),
            test_destination=build.get("test_destination", "platform=iOS Simulator,OS=latest,name=iPhone 16"),
            compile_timeout=build.get("compile_timeout", 1800),
            test_timeout=build.get("test_timeout", 1800),
        )
    if platform == "gradle-jvm":
        from tools.gradle_jvm_tool import JvmGradleTool

        return JvmGradleTool(
            sandbox,
            root,
            compile_task=build.get("compile_task", "classes"),
            test_task=build.get("test_task", "test"),
            sync_task=build.get("sync_task", "classes"),
            presync_task=build.get("presync_task", "classes"),
            presync_clean=bool(build.get("presync_clean", False)),
            sync_timeout=build.get("sync_timeout", 600),
            compile_timeout=build.get("compile_timeout", 600),
            test_timeout=build.get("test_timeout", 600),
        )
    if platform == "maven":
        from tools.maven_tool import MavenTool

        return MavenTool(
            sandbox,
            root,
            compile_command=build.get("compile_command"),
            test_command=build.get("test_command"),
            sync_command=build.get("sync_command"),
            presync_command=build.get("presync_command"),
            presync_clean=bool(build.get("presync_clean", False)),
            sync_timeout=build.get("sync_timeout", 300),
            compile_timeout=build.get("compile_timeout", 600),
            test_timeout=build.get("test_timeout", 600),
        )
    return AndroidGradleTool(
        sandbox,
        root,
        compile_task=build.get("compile_task", "compileDebugKotlin"),
        test_task=build.get("test_task", "testDebugUnitTest"),
        presync_task=build.get("presync_task", "generateDebugSources"),
        presync_clean=bool(build.get("presync_clean", False)),
        sync_timeout=build.get("sync_timeout", 1800),
        compile_timeout=build.get("compile_timeout", 1800),
        test_timeout=build.get("test_timeout", 1800),
    )
