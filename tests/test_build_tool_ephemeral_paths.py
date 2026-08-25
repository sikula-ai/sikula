"""Platform ownership of disposable dependency and build-output paths."""

from __future__ import annotations

import os

import pytest

from tools.cargo_tool import CargoTool
from tools.gradle_tool import GradleBaseTool
from tools.maven_tool import MavenTool
from tools.node_tool import NodeTool
from tools.python_tool import PythonTool
from tools.xcode_tool import XcodeTool


@pytest.mark.parametrize(
    ("tool_type", "ephemeral_path", "persistent_path"),
    [
        (NodeTool, "packages/web/node_modules/pkg/index.js", "packages/web/package-lock.json"),
        (CargoTool, "crates/app/target/debug/app", "crates/app/Cargo.lock"),
        (GradleBaseTool, "app/build/generated/source.kt", "app/gradle.lockfile"),
        (MavenTool, "service/target/classes/App.class", "service/pom.xml"),
        (PythonTool, "src/__pycache__/module.pyc", "requirements.txt"),
        (XcodeTool, ".build/checkouts/package/file.swift", "Package.resolved"),
    ],
)
def test_platform_build_tool_classifies_only_disposable_paths(
    tool_type: type,
    ephemeral_path: str,
    persistent_path: str,
) -> None:
    tool = object.__new__(tool_type)

    assert tool.is_ephemeral_build_path(ephemeral_path) is True
    assert tool.is_ephemeral_build_path(persistent_path) is False


@pytest.mark.parametrize(
    ("tool_type", "ephemeral_component"),
    [
        (NodeTool, "node_modules"),
        (CargoTool, "target"),
        (GradleBaseTool, "build"),
        (MavenTool, "target"),
        (PythonTool, "__pycache__"),
        (XcodeTool, "DerivedData"),
    ],
)
def test_platform_build_tool_preserves_native_backslash_semantics(
    tool_type: type,
    ephemeral_component: str,
) -> None:
    tool = object.__new__(tool_type)
    path = rf"outside\{ephemeral_component}\payload"

    assert tool.is_ephemeral_build_path(path) is (os.name == "nt")
