"""Validation command coverage helpers shared by orchestration and reviewer prompts."""

from __future__ import annotations

import re
import shlex

from core.state import TaskState

INTERNAL_PIPELINE_CONFIG_KEY = "__sikula_effective_pipeline"

_VALIDATION_COMMAND_RE = re.compile(
    r"^(?:"
    r"\./gradlew|gradlew|gradle|"
    r"\./mvnw|mvnw|mvn|"
    r"cargo|"
    r"python|python3|pytest|ruff|"
    r"xcodebuild|swift|swiftlint|"
    r"npm|npx|yarn|pnpm|bun|"
    r"go|dotnet|make"
    r")(?:\s|$)"
)
_VALIDATION_CONTEXT_RE = re.compile(
    r"\b(run|verify|validate|validation|check|test|format|lint|acceptance|before merge)\b",
    re.IGNORECASE,
)
_SHELL_FENCE_LANGS = {"", "bash", "sh", "shell", "zsh", "console", "terminal"}


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _normalize_command(command: str) -> str:
    command = command.strip()
    inline_match = re.fullmatch(r"`([^`\n]+)`[;,.:]?", command)
    if inline_match:
        command = inline_match.group(1)
    command = re.sub(r"^\$\s*", "", command)
    command = command.strip("` \t\r\n")
    command = re.sub(r"\s+", " ", command)
    return command.rstrip(",;:")


def _looks_like_validation_command(command: str) -> bool:
    normalized = _normalize_command(command)
    if not normalized or "\n" in normalized:
        return False
    if not _VALIDATION_COMMAND_RE.match(normalized):
        return False
    tokens = _shell_tokens(normalized)
    if len(tokens) < 2:
        return True
    first = tokens[0].removeprefix("./").rsplit("/", 1)[-1]
    second = tokens[1].lower()
    if first == "make" and second in {"sure", "certain"}:
        return False
    if first == "go" and second not in {"build", "test", "vet", "fmt", "run", "mod", "generate", "tool"}:
        return False
    if first == "swift" and second not in {"build", "test", "run", "package"}:
        return False
    return True


def extract_validation_commands(text: str) -> list[str]:
    commands: list[str] = []

    def add(command: str) -> None:
        normalized = _normalize_command(command)
        if normalized and _looks_like_validation_command(normalized) and normalized not in commands:
            commands.append(normalized)

    in_code_fence = False
    code_fence_is_shell = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_fence:
                in_code_fence = False
                code_fence_is_shell = False
            else:
                lang = stripped[3:].strip().lower()
                in_code_fence = True
                code_fence_is_shell = lang in _SHELL_FENCE_LANGS
            continue
        if in_code_fence:
            if code_fence_is_shell and not stripped.startswith("#"):
                add(stripped)
            continue

        stripped = re.sub(r"^[-*]\s+", "", stripped)
        stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
        has_inline_command = bool(re.search(r"`[^`\n]+`", stripped))
        starts_with_inline_command = bool(re.match(r"`[^`\n]+`", stripped))
        context_text = re.sub(r"`[^`\n]+`", "", stripped)
        has_validation_context = bool(_VALIDATION_CONTEXT_RE.search(context_text))
        if has_validation_context or starts_with_inline_command:
            for match in re.finditer(r"`([^`\n]+)`", stripped):
                add(match.group(1))
        if ":" in stripped:
            prefix, _, rest = stripped.partition(":")
            if _VALIDATION_CONTEXT_RE.search(prefix) and "`" not in rest:
                add(rest)
        if not has_inline_command:
            add(stripped)

    return commands


def _option_value(tokens: list[str], names: set[str]) -> str:
    for idx, token in enumerate(tokens):
        for name in names:
            if token == name and idx + 1 < len(tokens):
                return tokens[idx + 1]
            prefix = f"{name}="
            if token.startswith(prefix):
                return token[len(prefix) :]
    return ""


def _command_signature(command: str) -> tuple[str, ...]:
    tokens = _shell_tokens(_normalize_command(command))
    if not tokens:
        return ()

    first = tokens[0].removeprefix("./")
    basename = first.rsplit("/", 1)[-1]
    if basename in {"python", "python3"} and len(tokens) >= 3 and tokens[1] == "-m":
        module = tokens[2]
        if module == "ruff" and len(tokens) >= 4:
            return ("ruff", tokens[3])
        if module == "pytest":
            return ("pytest",)
        return ("python-module", module)
    if basename in {"python", "python3"}:
        script = tokens[1] if len(tokens) >= 2 else ""
        subcommand = next((token for token in tokens[2:] if not token.startswith("-")), "")
        return ("python", script, subcommand)

    if basename == "cargo" and len(tokens) >= 2:
        subcommand = tokens[1]
        if subcommand in {"fmt", "clippy", "test", "check"}:
            return ("cargo", subcommand)
        if subcommand == "run":
            package = ""
            for idx, token in enumerate(tokens):
                if token in {"-p", "--package"} and idx + 1 < len(tokens):
                    package = tokens[idx + 1]
                    break
            return ("cargo", "run", package, _normalize_command(command))
        return ("cargo", subcommand)

    if basename in {"gradlew", "gradle"}:
        task = next((token for token in tokens[1:] if not token.startswith("-")), "")
        return ("gradle", task)

    if basename in {"mvn", "mvnw"}:
        goals = tuple(token for token in tokens[1:] if not token.startswith("-"))
        return ("maven", *goals)

    if basename == "ruff" and len(tokens) >= 2:
        return ("ruff", tokens[1])

    if basename == "pytest":
        return ("pytest",)

    if basename == "swiftlint":
        subcommand = next((token for token in tokens[1:] if not token.startswith("-")), "")
        return ("swiftlint", subcommand)

    if basename == "xcodebuild":
        action = next((token for token in tokens[1:] if token in {"build", "test"}), "")
        scheme = _option_value(tokens, {"-scheme"})
        project = _option_value(tokens, {"-project"})
        workspace = _option_value(tokens, {"-workspace"})
        return ("xcodebuild", action, scheme, project, workspace)

    if basename == "swift" and len(tokens) >= 2:
        return ("swift", tokens[1])

    if basename in {"npm", "pnpm", "bun"}:
        subcommand = tokens[1] if len(tokens) >= 2 else ""
        if subcommand == "run":
            script = next((token for token in tokens[2:] if not token.startswith("-")), "")
            return (basename, "run", script)
        return (basename, subcommand)

    if basename == "yarn":
        subcommand = tokens[1] if len(tokens) >= 2 else ""
        if subcommand == "run":
            script = next((token for token in tokens[2:] if not token.startswith("-")), "")
            return ("yarn", "run", script)
        return ("yarn", subcommand)

    if basename in {"npx", "go", "dotnet", "make"}:
        subcommand = next((token for token in tokens[1:] if not token.startswith("-")), "")
        return (basename, subcommand)

    return (basename,)


def _exact_command_key(command: str) -> tuple[str, ...]:
    tokens = _shell_tokens(_normalize_command(command))
    if not tokens:
        return ()

    executable = tokens[0].removeprefix("./")
    if "/" not in executable:
        if executable in {"gradlew", "gradle"}:
            tokens[0] = "gradle"
        elif executable in {"mvnw", "mvn"}:
            tokens[0] = "mvn"
    return tuple(tokens)


def validation_commands_equivalent(task_command: str, pipeline_command: str) -> tuple[bool, str]:
    task_normalized = _normalize_command(task_command)
    pipeline_normalized = _normalize_command(pipeline_command)
    if task_normalized == pipeline_normalized or _exact_command_key(task_normalized) == _exact_command_key(
        pipeline_normalized
    ):
        return True, "exact"
    task_signature = _command_signature(task_normalized)
    pipeline_signature = _command_signature(pipeline_normalized)
    if task_signature and task_signature == pipeline_signature:
        return True, "same command family"
    return False, ""


def validation_command_coverage(
    task_command: str,
    configured_commands: list[dict[str, str]],
) -> tuple[bool, str, dict[str, str] | None]:
    nearest: tuple[str, dict[str, str] | None] = ("", None)
    for configured_command in configured_commands:
        if configured_command.get("phase") == "check_autofix":
            continue
        covered, match_kind = validation_commands_equivalent(task_command, configured_command["command"])
        if not covered:
            continue
        if match_kind == "exact":
            return True, "exact", configured_command
        if nearest[1] is None:
            nearest = (match_kind, configured_command)
    return False, nearest[0], nearest[1]


def pipeline_flags(project_config: dict, state: TaskState) -> dict[str, bool]:
    configured = project_config.get(INTERNAL_PIPELINE_CONFIG_KEY)
    if isinstance(configured, dict):
        return {
            "run_build": bool(configured.get("run_build", True)),
            "run_tests": bool(configured.get("run_tests", True)),
            "run_checks": bool(configured.get("run_checks", True)),
        }
    if state.review_mode == "review_report":
        return {"run_build": False, "run_tests": False, "run_checks": False}
    return {
        "run_build": bool(project_config.get("run_build", True)),
        "run_tests": bool(project_config.get("run_tests", True)),
        "run_checks": bool(project_config.get("run_checks", True)),
    }


def _default_compile_command(project_config: dict) -> str | None:
    build_tool = project_config.get("project", {}).get("build_tool", "gradle-android")
    build = project_config.get("build", {})
    if build_tool == "cargo":
        return str(build.get("compile_command") or "cargo check")
    if build_tool == "python":
        return str(build.get("compile_command") or "ruff check .")
    if build_tool == "maven":
        return str(build.get("compile_command") or "mvn compile")
    if build_tool == "gradle-jvm":
        return f"./gradlew {build.get('compile_task') or 'classes'}"
    if build_tool == "xcodebuild":
        return f"xcodebuild build -scheme {build.get('scheme') or 'Countries'}"
    return f"./gradlew {build.get('compile_task') or 'compileDebugKotlin'}"


def _default_test_command(project_config: dict) -> str | None:
    build_tool = project_config.get("project", {}).get("build_tool", "gradle-android")
    build = project_config.get("build", {})
    if build_tool == "cargo":
        return str(build.get("test_command") or "cargo test")
    if build_tool == "python":
        return str(build.get("test_command") or "pytest")
    if build_tool == "maven":
        return str(build.get("test_command") or "mvn test")
    if build_tool == "gradle-jvm":
        return f"./gradlew {build.get('test_task') or 'test'}"
    if build_tool == "xcodebuild":
        return f"xcodebuild test -scheme {build.get('scheme') or 'Countries'}"
    return f"./gradlew {build.get('test_task') or 'testDebugUnitTest'}"


def configured_validation_commands(project_config: dict, state: TaskState) -> list[dict[str, str]]:
    flags = pipeline_flags(project_config, state)
    if not flags["run_build"]:
        return []

    build = project_config.get("build", {})
    commands: list[dict[str, str]] = []

    compile_command = _default_compile_command(project_config)
    if compile_command:
        commands.append({"phase": "build", "name": "compile", "command": _normalize_command(str(compile_command))})

    test_command = _default_test_command(project_config)
    if flags["run_tests"] and test_command:
        commands.append({"phase": "test", "name": "tests", "command": _normalize_command(str(test_command))})

    if flags["run_checks"]:
        for idx, check in enumerate(build.get("checks") or [], start=1):
            if not isinstance(check, dict):
                continue
            command = check.get("command")
            name = str(check.get("name") or f"check-{idx}")
            if command:
                commands.append({"phase": "check", "name": name, "command": _normalize_command(str(command))})
            fix_command = check.get("fix_command")
            if fix_command:
                commands.append(
                    {
                        "phase": "check_autofix",
                        "name": f"{name} autofix",
                        "command": _normalize_command(str(fix_command)),
                    }
                )

    return commands


def validation_coverage_gaps(project_config: dict, state: TaskState) -> list[str]:
    configured_commands = configured_validation_commands(project_config, state)
    gaps: list[str] = []
    for task_command in extract_validation_commands(state.task_description or ""):
        covered, _, _ = validation_command_coverage(task_command, configured_commands)
        if not covered:
            gaps.append(task_command)
    return gaps
