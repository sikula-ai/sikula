"""Validation command coverage helpers shared by orchestration and reviewer prompts."""

from __future__ import annotations

from collections.abc import Callable
import re
import shlex
from pathlib import Path

from core.markdown_headings import FENCED_BLOCK_RE, MarkdownHeadingScanner, is_fenced_block_closer
from core.state import TaskState

INTERNAL_PIPELINE_CONFIG_KEY = "__sikula_effective_pipeline"

_SHELL_FENCE_LANGS = {"", "bash", "sh", "shell", "zsh"}
_TRANSCRIPT_FENCE_LANGS = {"console", "terminal"}
_AMBIGUOUS_VALIDATION_SECTION_HEADINGS = {"test", "test plan", "tests"}
VALIDATION_SECTION_HEADINGS = frozenset(
    {
        "before merge",
        "check",
        "checks",
        "how to validate",
        "test",
        "test plan",
        "tests",
        "validation",
        "verification",
    }
)
_PACKAGE_SCRIPT_SHORTCUT_MANAGERS = {"npm", "pnpm", "yarn"}
_PACKAGE_SCRIPT_SHORTCUTS = {"test"}
_PACKAGE_RUN_SHORTHAND_MANAGERS = {"pnpm", "yarn"}
_PACKAGE_RUN_SHORTHAND_SCRIPTS = {
    "build",
    "check",
    "check-types",
    "format",
    "format-check",
    "format:check",
    "format:write",
    "lint",
    "prettier",
    "prettier:check",
    "prettier:write",
    "test",
    "type-check",
    "typecheck",
}
_PYTHON_MODULE_ALIASES = {"pytest", "ruff"}


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


def extract_validation_commands(text: str) -> list[str]:
    commands: list[str] = []

    def add(command: str) -> None:
        normalized = _normalize_command(command)
        if normalized and "\n" not in normalized and normalized not in commands:
            commands.append(normalized)

    lines = text.splitlines()
    scanner = MarkdownHeadingScanner(ignore_fenced_blocks=True)
    headings = [(idx, heading) for idx, line in enumerate(lines) if (heading := scanner.match(line)) is not None]
    for idx, (line_idx, heading) in enumerate(headings):
        if heading.normalized not in VALIDATION_SECTION_HEADINGS:
            continue
        end_idx = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        _extract_validation_section_commands(
            lines[line_idx + 1 : end_idx],
            add,
            allow_inline_list=heading.normalized not in _AMBIGUOUS_VALIDATION_SECTION_HEADINGS,
        )

    return commands


def _extract_validation_section_commands(
    lines: list[str],
    add: Callable[[str], None],
    *,
    allow_inline_list: bool,
) -> None:
    opening_fence = ""
    code_fence_kind = ""
    for line in lines:
        stripped = line.strip()
        fence_match = FENCED_BLOCK_RE.match(line)
        if opening_fence:
            if is_fenced_block_closer(line, opening_fence):
                opening_fence = ""
                code_fence_kind = ""
                continue
            if fence_match:
                continue
            if code_fence_kind == "shell" and not stripped.startswith("#"):
                add(stripped)
            elif code_fence_kind == "transcript" and stripped.startswith("$"):
                add(stripped)
            continue
        if fence_match:
            opening_fence = fence_match.group(1)
            lang = line[fence_match.end() :].strip().lower()
            if lang in _SHELL_FENCE_LANGS:
                code_fence_kind = "shell"
            elif lang in _TRANSCRIPT_FENCE_LANGS:
                code_fence_kind = "transcript"
            continue
        if not stripped:
            continue

        if stripped.startswith("$"):
            add(stripped)
            continue
        list_match = re.match(r"^(?:[-*+]|\d+[.)])\s+", stripped)
        if not list_match or not allow_inline_list:
            continue
        list_item = stripped[list_match.end() :]
        inline_match = re.match(r"`([^`\n]+)`", list_item)
        if inline_match:
            add(inline_match.group(1))


def _option_value(tokens: list[str], names: set[str]) -> str:
    for idx, token in enumerate(tokens):
        for name in names:
            if token == name and idx + 1 < len(tokens):
                return tokens[idx + 1]
            prefix = f"{name}="
            if token.startswith(prefix):
                return token[len(prefix) :]
    return ""


def _package_script_signature(manager: str, tokens: list[str]) -> tuple[str, ...]:
    subcommand = tokens[1] if len(tokens) >= 2 else ""
    if subcommand == "run":
        script = next((token for token in tokens[2:] if not token.startswith("-")), "")
        return (manager, "run", script)
    if manager in _PACKAGE_RUN_SHORTHAND_MANAGERS and subcommand in _PACKAGE_RUN_SHORTHAND_SCRIPTS:
        return (manager, "run", subcommand)
    if manager in _PACKAGE_SCRIPT_SHORTCUT_MANAGERS and subcommand in _PACKAGE_SCRIPT_SHORTCUTS:
        return (manager, "run", subcommand)
    return (manager, subcommand)


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

    if basename in {"npm", "pnpm", "bun", "yarn"}:
        return _package_script_signature(basename, tokens)

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
        if executable in {"python", "python3"} and len(tokens) >= 3 and tokens[1] == "-m":
            module = tokens[2]
            if module in _PYTHON_MODULE_ALIASES:
                tokens = [module, *tokens[3:]]
        if executable in {"gradlew", "gradle"}:
            tokens[0] = "gradle"
        elif executable in {"mvnw", "mvn"}:
            tokens[0] = "mvn"
        elif (
            executable in _PACKAGE_RUN_SHORTHAND_MANAGERS
            and len(tokens) >= 2
            and tokens[1] in _PACKAGE_RUN_SHORTHAND_SCRIPTS
        ):
            tokens[1:1] = ["run"]
        elif executable in _PACKAGE_SCRIPT_SHORTCUT_MANAGERS and len(tokens) >= 2:
            if tokens[1] in _PACKAGE_SCRIPT_SHORTCUTS:
                tokens[1:2] = ["run", tokens[1]]
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


def _project_root(project_config: dict) -> Path | None:
    root_path = project_config.get("project", {}).get("root_path")
    if not root_path:
        return None
    return Path(str(root_path))


def _node_package_context(project_config: dict) -> tuple[Path, str, dict[str, str]] | None:
    root = _project_root(project_config)
    if root is None:
        return None
    build = project_config.get("build", {})
    configured_package_manager = build.get("package_manager")
    package_manager = str(configured_package_manager) if configured_package_manager else None

    from tools.node_tool import detect_node_package_manager, read_node_package_scripts

    detected_package_manager = detect_node_package_manager(root, package_manager)
    scripts = read_node_package_scripts(root)
    return root, detected_package_manager, scripts


def _default_compile_command(project_config: dict) -> str | None:
    build_tool = project_config.get("project", {}).get("build_tool", "gradle-android")
    build = project_config.get("build", {})
    if build_tool == "cargo":
        return str(build.get("compile_command") or "cargo check")
    if build_tool == "python":
        return str(build.get("compile_command") or "ruff check .")
    if build_tool == "node":
        if build.get("compile_command"):
            return str(build["compile_command"])
        node_context = _node_package_context(project_config)
        if node_context:
            from tools.node_tool import default_node_compile_command

            root, package_manager, scripts = node_context
            return default_node_compile_command(root, package_manager, scripts)
        package_manager = str(build.get("package_manager") or "npm")
        if package_manager == "npm":
            default = "npm run build"
        elif package_manager == "bun":
            default = "bun run build"
        else:
            default = f"{package_manager} build"
        return default
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
    if build_tool == "node":
        if build.get("test_command"):
            return str(build["test_command"])
        node_context = _node_package_context(project_config)
        if node_context:
            from tools.node_tool import default_node_test_command

            _, package_manager, scripts = node_context
            return default_node_test_command(package_manager, scripts)
        package_manager = str(build.get("package_manager") or "npm")
        default = "bun run test" if package_manager == "bun" else f"{package_manager} test"
        return default
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
