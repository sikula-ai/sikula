"""Guardrails for adding new platform build tools."""

from __future__ import annotations

from pathlib import Path

import sikula as sikula_module
from core.orchestrator import _build_tool
from sikula_cli.init import generate_config
from tools.base_tool import Sandbox
from tools.cargo_tool import CargoTool
from tools.gradle_android_tool import AndroidGradleTool
from tools.gradle_jvm_tool import JvmGradleTool
from tools.maven_tool import MavenTool
from tools.node_tool import NodeTool
from tools.python_tool import PythonTool
from tools.scanner import _SIGNATURES
from tools.xcode_tool import XcodeTool


BUILD_TOOL_CLASSES = {
    "cargo": CargoTool,
    "gradle-android": AndroidGradleTool,
    "gradle-jvm": JvmGradleTool,
    "maven": MavenTool,
    "node": NodeTool,
    "python": PythonTool,
    "xcodebuild": XcodeTool,
}

BUILD_TOOL_INIT_CONTEXT = {
    "cargo": {"language": "Rust", "platform": None},
    "gradle-android": {"language": "Kotlin", "platform": "Android"},
    "gradle-jvm": {"language": "Kotlin", "platform": None},
    "maven": {"language": "Java", "platform": None},
    "node": {"language": "TypeScript", "platform": None},
    "python": {"language": "Python", "platform": None},
    "xcodebuild": {"language": "Swift", "platform": "iOS", "xcode_scheme": "Example"},
}


def test_supported_build_tools_match_platform_onboarding_registry():
    assert sikula_module._SUPPORTED_BUILD_TOOLS == set(BUILD_TOOL_CLASSES)
    assert set(BUILD_TOOL_INIT_CONTEXT) == set(BUILD_TOOL_CLASSES)


def test_supported_build_tools_have_env_file_factory_classes():
    for build_tool, expected_class in BUILD_TOOL_CLASSES.items():
        assert sikula_module._build_tool_class({"project": {"build_tool": build_tool}}) is expected_class


def test_supported_build_tools_have_orchestrator_factory_branches(tmp_path: Path):
    sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])

    for build_tool, expected_class in BUILD_TOOL_CLASSES.items():
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": build_tool}, "build": {}})
        assert isinstance(tool, expected_class), build_tool


def test_supported_build_tools_have_generated_init_build_sections():
    generic_markers = (
        "# TODO: configure compile and test commands for your project.",
        'compile_command: "TODO"',
        'test_command: "TODO"',
    )

    for build_tool, context in BUILD_TOOL_INIT_CONTEXT.items():
        cfg = generate_config(
            build_tool=build_tool,
            language=context["language"],
            platform=context["platform"],
            guidelines_files=[],
            project_name="example",
            provider="codex",
            model="gpt-5.3-codex",
            xcode_scheme=context.get("xcode_scheme"),
        )
        assert f"build_tool: {build_tool}" in cfg
        for marker in generic_markers:
            assert marker not in cfg, build_tool


def test_scanner_detection_surface_matches_supported_build_tools():
    scanner_tools = {tool for _, tool, _, _ in _SIGNATURES}
    # Xcode projects are detected by directory suffix, and Gradle JVM is refined from
    # the Gradle signature when no Android manifest is present.
    scanner_tools.update({"gradle-jvm", "xcodebuild"})

    assert scanner_tools == sikula_module._SUPPORTED_BUILD_TOOLS


def test_platform_onboarding_docs_include_audit_and_buildtool_registries():
    documents = {
        "ARCHITECTURE.md": Path("ARCHITECTURE.md").read_text(encoding="utf-8"),
        "guidelines.md": Path("guidelines.md").read_text(encoding="utf-8"),
        "tools/base_tool.py": Path("tools/base_tool.py").read_text(encoding="utf-8"),
    }
    required_entries = [
        "tests/test_platform_onboarding.py",
        "is_sync_adoptable_file",
        "is_test_only_change",
        "requires_test_only_change_content",
        "_TEST_GATE_AUDIT_SOURCE_SUFFIXES",
        "core/test_execution_gate_audit.py",
        "tests/test_test_execution_gate_audit.py",
        "core/synthetic_test_harness_audit.py",
        "tests/test_synthetic_test_harness_audit.py",
    ]

    for entry in required_entries:
        for name, content in documents.items():
            assert entry in content, f"{entry} missing from {name}"


def test_readme_points_to_developer_architecture_docs():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "ARCHITECTURE.md" in readme
    assert "CONTRIBUTING.md" in readme
