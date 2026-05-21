"""Static project scanner for sikula init.

Detects build tool, language, platform, and guideline files by inspecting
the filesystem — no LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# (trigger_files, build_tool, language, platform)
# Priority order: first match wins when resolving ambiguity.
# When adding a new platform: add an entry here, add path detection helpers below,
# and extend _generate_config() in sikula.py and _build_tool() in core/orchestrator.py.
# Note: Gradle entries use "gradle-android" as a placeholder; scan() refines them to
# "gradle-android" or "gradle-jvm" based on AndroidManifest.xml presence.
_SIGNATURES: list[tuple[list[str], str, str, str | None]] = [
    (["Cargo.toml"], "cargo", "Rust", None),
    # pom.xml before Gradle — Spring Boot projects that have both pom.xml and build files
    # should be treated as Maven projects.
    (["pom.xml"], "maven", "Java", None),
    (
        ["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"],
        "gradle-android",
        "Kotlin",
        "Android",
    ),
    (["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "tox.ini"], "python", "Python", None),
]

_GUIDELINE_CANDIDATES = [
    "AGENTS.md",
    "agents.md",
    ".github/copilot-instructions.md",
    "guidelines.md",
    "ARCHITECTURE.md",
    "docs/architecture.md",
    "docs/guidelines.md",
    "docs/coding-standards.md",
    "docs/development.md",
    "README.md",
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
]

_GRADLE_SOURCE_CANDIDATES = [
    "app",
    "feature",
    "features",
    "library",
    "libraries",
    "core",
    "data",
    "domain",
    "presentation",
    "common",
    "shared",
    "section",
    "module",
    "modules",
    "ui",
    "network",
    "database",
    "base",
    "api",
    "auth",
    "navigation",
]


@dataclass
class ScanResult:
    build_tool: str | None = None
    language: str | None = None
    platform: str | None = None
    guidelines_files: list[str] = field(default_factory=list)
    ambiguous_tools: list[str] = field(default_factory=list)
    xcode_scheme: str | None = None
    write_paths: list[str] = field(default_factory=list)
    test_write_paths: list[str] = field(default_factory=list)


def _detect_xcode_scheme(root: Path, xcodeproj_name: str) -> str | None:
    schemes_dir = root / xcodeproj_name / "xcshareddata" / "xcschemes"
    if schemes_dir.is_dir():
        schemes = sorted(schemes_dir.glob("*.xcscheme"))
        if schemes:
            return schemes[0].stem
    return None


def _detect_xcode_paths(root: Path, project_name: str) -> tuple[list[str], list[str]]:
    source_dirs = [f"{project_name}/"] if (root / project_name).is_dir() else ["Sources/"]
    test_dirs = [
        f"{project_name}{suffix}/" for suffix in ("Tests", "UITests") if (root / f"{project_name}{suffix}").is_dir()
    ] or [f"{project_name}Tests/"]
    return source_dirs, test_dirs


def _is_android_gradle(root: Path) -> bool:
    """Return True when the Gradle project is an Android project.

    Checks known AndroidManifest.xml locations rather than reading file contents.
    """
    for candidate in (
        root / "app" / "src" / "main" / "AndroidManifest.xml",
        root / "src" / "main" / "AndroidManifest.xml",
    ):
        if candidate.exists():
            return True
    return False


def _detect_gradle_paths(root: Path) -> tuple[list[str], list[str]]:
    found = [f"{d}/" for d in _GRADLE_SOURCE_CANDIDATES if (root / d).is_dir()]
    # Tests live inside the same modules as source in Android projects.
    return (found or ["app/"]), (found or ["app/"])


def _detect_jvm_paths(root: Path) -> tuple[list[str], list[str]]:
    """Detect source and test paths for JVM projects using the standard Maven layout."""
    write_dirs = [f"{d}/" for d in ("src/main/kotlin", "src/main/java") if (root / d).is_dir()]
    test_dirs = [f"{d}/" for d in ("src/test/kotlin", "src/test/java") if (root / d).is_dir()]
    return (write_dirs or ["src/main/java/"]), (test_dirs or ["src/test/java/"])


def _detect_python_paths(root: Path) -> tuple[list[str], list[str]]:
    src = next((d for d in ("src", "lib") if (root / d).is_dir()), None)
    tests = next((d for d in ("tests", "test", "spec") if (root / d).is_dir()), None)
    return ([f"{src}/"] if src else ["src/"]), ([f"{tests}/"] if tests else ["tests/"])


def scan(root: Path) -> ScanResult:
    result = ScanResult()
    detected: list[tuple[str, str, str | None]] = []
    xcodeproj_name: str | None = None

    for triggers, tool, lang, platform in _SIGNATURES:
        if any((root / f).exists() for f in triggers):
            detected.append((tool, lang, platform))

    try:
        for entry in root.iterdir():
            if entry.suffix == ".xcodeproj":
                detected.append(("xcodebuild", "Swift", "iOS"))
                xcodeproj_name = entry.name
                break
            elif entry.suffix == ".xcworkspace":
                detected.append(("xcodebuild", "Swift", "iOS"))
                break
    except PermissionError:
        pass

    if len(detected) == 1:
        result.build_tool, result.language, result.platform = detected[0]
    elif len(detected) > 1:
        result.ambiguous_tools = [t for t, _, _ in detected]
        result.build_tool, result.language, result.platform = detected[0]

    result.guidelines_files = [f for f in _GUIDELINE_CANDIDATES if (root / f).exists()]

    # Refine Gradle detection: Android projects have AndroidManifest.xml; everything
    # else is treated as a JVM backend project (Spring Boot, Quarkus, Micronaut, …).
    if result.build_tool == "gradle-android" and not _is_android_gradle(root):
        result.build_tool = "gradle-jvm"
        result.platform = None

    if result.build_tool == "xcodebuild":
        if xcodeproj_name:
            result.xcode_scheme = _detect_xcode_scheme(root, xcodeproj_name)
            project_name = Path(xcodeproj_name).stem
            result.write_paths, result.test_write_paths = _detect_xcode_paths(root, project_name)
        else:
            result.write_paths = ["Sources/"]
            result.test_write_paths = ["Tests/"]
    elif result.build_tool == "gradle-android":
        result.write_paths, result.test_write_paths = _detect_gradle_paths(root)
    elif result.build_tool in ("gradle-jvm", "maven"):
        result.write_paths, result.test_write_paths = _detect_jvm_paths(root)
    elif result.build_tool == "cargo":
        result.write_paths = ["src/"]
        result.test_write_paths = ["src/", "tests/"] if (root / "tests").is_dir() else ["src/"]
    elif result.build_tool == "python":
        result.write_paths, result.test_write_paths = _detect_python_paths(root)

    return result
