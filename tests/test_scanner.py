"""Tests for tools/scanner.py."""

from __future__ import annotations

from pathlib import Path

from tools.scanner import ScanResult, scan


class TestScanBuildTool:
    def test_detects_cargo(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "cargo"
        assert result.language == "Rust"
        assert result.platform is None

    def test_detects_maven_from_pom_xml(self, tmp_path: Path):
        (tmp_path / "pom.xml").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "maven"
        assert result.language == "Java"
        assert result.platform is None

    def test_detects_gradle_android_from_build_gradle_with_manifest(self, tmp_path: Path):
        (tmp_path / "build.gradle").write_text("")
        manifest = tmp_path / "app" / "src" / "main"
        manifest.mkdir(parents=True)
        (manifest / "AndroidManifest.xml").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "gradle-android"
        assert result.language == "Kotlin"
        assert result.platform == "Android"

    def test_detects_gradle_jvm_from_build_gradle_without_manifest(self, tmp_path: Path):
        (tmp_path / "build.gradle").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "gradle-jvm"
        assert result.platform is None

    def test_detects_gradle_jvm_from_build_gradle_kts_without_manifest(self, tmp_path: Path):
        (tmp_path / "build.gradle.kts").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "gradle-jvm"

    def test_detects_gradle_android_from_settings_gradle_kts_with_manifest(self, tmp_path: Path):
        (tmp_path / "settings.gradle.kts").write_text("")
        manifest = tmp_path / "app" / "src" / "main"
        manifest.mkdir(parents=True)
        (manifest / "AndroidManifest.xml").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "gradle-android"

    def test_maven_takes_priority_over_gradle_when_both_present(self, tmp_path: Path):
        (tmp_path / "pom.xml").write_text("")
        (tmp_path / "build.gradle").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "maven"

    def test_detects_python_from_pyproject(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "python"
        assert result.language == "Python"
        assert result.platform is None

    def test_detects_python_from_requirements(self, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "python"

    def test_detects_xcode_from_xcodeproj(self, tmp_path: Path):
        (tmp_path / "MyApp.xcodeproj").mkdir()
        result = scan(tmp_path)
        assert result.build_tool == "xcodebuild"
        assert result.language == "Swift"
        assert result.platform == "iOS"

    def test_detects_xcode_from_xcworkspace(self, tmp_path: Path):
        (tmp_path / "MyApp.xcworkspace").mkdir()
        result = scan(tmp_path)
        assert result.build_tool == "xcodebuild"

    def test_no_build_tool_detected(self, tmp_path: Path):
        result = scan(tmp_path)
        assert result.build_tool is None
        assert result.language is None
        assert result.platform is None

    def test_ambiguous_tools_populated_when_multiple_detected(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        result = scan(tmp_path)
        assert len(result.ambiguous_tools) == 2
        assert "cargo" in result.ambiguous_tools
        assert "python" in result.ambiguous_tools

    def test_first_signature_wins_on_ambiguity(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        result = scan(tmp_path)
        assert result.build_tool == "cargo"

    def test_no_ambiguity_for_single_tool(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        result = scan(tmp_path)
        assert result.ambiguous_tools == []


class TestScanGuidelineFiles:
    def test_detects_agents_md(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("")
        result = scan(tmp_path)
        assert "AGENTS.md" in result.guidelines_files

    def test_detects_lowercase_agents_md(self, tmp_path: Path):
        (tmp_path / "agents.md").write_text("")
        result = scan(tmp_path)
        assert "agents.md" in result.guidelines_files

    def test_detects_copilot_instructions(self, tmp_path: Path):
        instructions = tmp_path / ".github" / "copilot-instructions.md"
        instructions.parent.mkdir()
        instructions.write_text("")
        result = scan(tmp_path)
        assert ".github/copilot-instructions.md" in result.guidelines_files

    def test_detects_architecture_docs(self, tmp_path: Path):
        (tmp_path / "ARCHITECTURE.md").write_text("")
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "architecture.md").write_text("")
        result = scan(tmp_path)
        assert "ARCHITECTURE.md" in result.guidelines_files
        assert "docs/architecture.md" in result.guidelines_files

    def test_detects_readme(self, tmp_path: Path):
        (tmp_path / "README.md").write_text("")
        result = scan(tmp_path)
        assert "README.md" in result.guidelines_files

    def test_detects_contributing(self, tmp_path: Path):
        (tmp_path / "CONTRIBUTING.md").write_text("")
        result = scan(tmp_path)
        assert "CONTRIBUTING.md" in result.guidelines_files

    def test_detects_guidelines_md(self, tmp_path: Path):
        (tmp_path / "guidelines.md").write_text("")
        result = scan(tmp_path)
        assert "guidelines.md" in result.guidelines_files

    def test_detects_nested_guidelines(self, tmp_path: Path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "guidelines.md").write_text("")
        result = scan(tmp_path)
        assert "docs/guidelines.md" in result.guidelines_files

    def test_no_guidelines_when_none_present(self, tmp_path: Path):
        result = scan(tmp_path)
        assert result.guidelines_files == []

    def test_detects_multiple_guideline_files(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("")
        (tmp_path / "README.md").write_text("")
        (tmp_path / "CONTRIBUTING.md").write_text("")
        result = scan(tmp_path)
        assert result.guidelines_files.index("AGENTS.md") < result.guidelines_files.index("README.md")
        assert "README.md" in result.guidelines_files
        assert "CONTRIBUTING.md" in result.guidelines_files


class TestScanReturnType:
    def test_returns_scan_result(self, tmp_path: Path):
        assert isinstance(scan(tmp_path), ScanResult)


class TestScanPermissionError:
    def test_permission_error_on_iterdir_is_swallowed(self, tmp_path: Path):
        from unittest.mock import patch

        with patch.object(Path, "iterdir", side_effect=PermissionError("no access")):
            result = scan(tmp_path)
        assert result.build_tool is None


class TestScanXcodeScheme:
    def test_detects_scheme_from_xcodeproj(self, tmp_path: Path):
        schemes = tmp_path / "MyApp.xcodeproj" / "xcshareddata" / "xcschemes"
        schemes.mkdir(parents=True)
        (schemes / "MyApp.xcscheme").write_text("")
        result = scan(tmp_path)
        assert result.xcode_scheme == "MyApp"

    def test_no_scheme_when_xcworkspace_only(self, tmp_path: Path):
        (tmp_path / "MyApp.xcworkspace").mkdir()
        result = scan(tmp_path)
        assert result.xcode_scheme is None

    def test_no_scheme_when_schemes_dir_missing(self, tmp_path: Path):
        (tmp_path / "MyApp.xcodeproj").mkdir()
        result = scan(tmp_path)
        assert result.xcode_scheme is None

    def test_scheme_stem_only_no_extension(self, tmp_path: Path):
        schemes = tmp_path / "Countries.xcodeproj" / "xcshareddata" / "xcschemes"
        schemes.mkdir(parents=True)
        (schemes / "Countries.xcscheme").write_text("")
        result = scan(tmp_path)
        assert result.xcode_scheme == "Countries"
        assert ".xcscheme" not in result.xcode_scheme


class TestScanWritePaths:
    def test_cargo_write_paths(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        result = scan(tmp_path)
        assert result.write_paths == ["src/"]

    def test_cargo_test_paths_includes_tests_dir_when_present(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        (tmp_path / "tests").mkdir()
        result = scan(tmp_path)
        assert "tests/" in result.test_write_paths

    def test_cargo_test_paths_no_tests_dir(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        result = scan(tmp_path)
        assert result.test_write_paths == ["src/"]

    def test_gradle_android_detects_app_dir(self, tmp_path: Path):
        (tmp_path / "build.gradle").write_text("")
        manifest = tmp_path / "app" / "src" / "main"
        manifest.mkdir(parents=True)
        (manifest / "AndroidManifest.xml").write_text("")
        result = scan(tmp_path)
        assert "app/" in result.write_paths

    def test_gradle_android_detects_feature_and_library_dirs(self, tmp_path: Path):
        (tmp_path / "build.gradle").write_text("")
        manifest = tmp_path / "app" / "src" / "main"
        manifest.mkdir(parents=True)
        (manifest / "AndroidManifest.xml").write_text("")
        (tmp_path / "feature").mkdir()
        (tmp_path / "library").mkdir()
        result = scan(tmp_path)
        assert "feature/" in result.write_paths
        assert "library/" in result.write_paths

    def test_gradle_android_fallback_to_app_when_no_known_dirs(self, tmp_path: Path):
        (tmp_path / "build.gradle").write_text("")
        manifest = tmp_path / "app" / "src" / "main"
        manifest.mkdir(parents=True)
        (manifest / "AndroidManifest.xml").write_text("")
        result = scan(tmp_path)
        assert result.write_paths == ["app/"]

    def test_gradle_jvm_detects_kotlin_source_dir(self, tmp_path: Path):
        (tmp_path / "build.gradle.kts").write_text("")
        (tmp_path / "src" / "main" / "kotlin").mkdir(parents=True)
        (tmp_path / "src" / "test" / "kotlin").mkdir(parents=True)
        result = scan(tmp_path)
        assert result.build_tool == "gradle-jvm"
        assert "src/main/kotlin/" in result.write_paths
        assert "src/test/kotlin/" in result.test_write_paths

    def test_gradle_jvm_detects_java_source_dir(self, tmp_path: Path):
        (tmp_path / "build.gradle.kts").write_text("")
        (tmp_path / "src" / "main" / "java").mkdir(parents=True)
        result = scan(tmp_path)
        assert "src/main/java/" in result.write_paths

    def test_gradle_jvm_fallback_when_no_source_dirs(self, tmp_path: Path):
        (tmp_path / "build.gradle.kts").write_text("")
        result = scan(tmp_path)
        assert result.write_paths == ["src/main/java/"]
        assert result.test_write_paths == ["src/test/java/"]

    def test_maven_detects_kotlin_source_dir(self, tmp_path: Path):
        (tmp_path / "pom.xml").write_text("")
        (tmp_path / "src" / "main" / "kotlin").mkdir(parents=True)
        (tmp_path / "src" / "test" / "kotlin").mkdir(parents=True)
        result = scan(tmp_path)
        assert "src/main/kotlin/" in result.write_paths
        assert "src/test/kotlin/" in result.test_write_paths

    def test_maven_fallback_when_no_source_dirs(self, tmp_path: Path):
        (tmp_path / "pom.xml").write_text("")
        result = scan(tmp_path)
        assert result.write_paths == ["src/main/java/"]
        assert result.test_write_paths == ["src/test/java/"]

    def test_python_detects_src_dir(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "src").mkdir()
        result = scan(tmp_path)
        assert result.write_paths == ["src/"]

    def test_python_detects_tests_dir(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "tests").mkdir()
        result = scan(tmp_path)
        assert result.test_write_paths == ["tests/"]

    def test_python_fallback_when_no_dirs(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("")
        result = scan(tmp_path)
        assert result.write_paths == ["src/"]
        assert result.test_write_paths == ["tests/"]

    def test_xcode_detects_source_dir_matching_project_name(self, tmp_path: Path):
        (tmp_path / "Countries.xcodeproj").mkdir()
        (tmp_path / "Countries").mkdir()
        result = scan(tmp_path)
        assert result.write_paths == ["Countries/"]

    def test_xcode_detects_tests_dir(self, tmp_path: Path):
        (tmp_path / "Countries.xcodeproj").mkdir()
        (tmp_path / "Countries").mkdir()
        (tmp_path / "CountriesTests").mkdir()
        result = scan(tmp_path)
        assert "CountriesTests/" in result.test_write_paths

    def test_xcode_detects_ui_tests_dir(self, tmp_path: Path):
        (tmp_path / "Countries.xcodeproj").mkdir()
        (tmp_path / "Countries").mkdir()
        (tmp_path / "CountriesUITests").mkdir()
        result = scan(tmp_path)
        assert "CountriesUITests/" in result.test_write_paths

    def test_xcode_fallback_source_when_no_matching_dir(self, tmp_path: Path):
        (tmp_path / "Countries.xcodeproj").mkdir()
        result = scan(tmp_path)
        assert result.write_paths == ["Sources/"]

    def test_no_write_paths_when_no_build_tool(self, tmp_path: Path):
        result = scan(tmp_path)
        assert result.write_paths == []
        assert result.test_write_paths == []
