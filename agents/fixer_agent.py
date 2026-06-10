"""Fixer agent — runs the configured LLM as an autonomous agent to fix build or test errors.

The agent receives error output and task context, then navigates the codebase using its file
tools to locate and fix the affected files. Changed files are detected via git diff. Logical
and completeness issues are handled upstream by ReviewerAgent; the fixer's scope is strictly
limited to errors the build system or test runner reports.
"""

from __future__ import annotations

import logging
import posixpath
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agents.base_agent import (
    AgentResult,
    BaseAgent,
    AGENT_SECURITY_PREFIX,
    guidelines_files as _guidelines_files,
    paths_outside_allowed as _paths_outside_allowed,
    record_write_path_warnings as _record_write_path_warnings,
    tech_stack as _tech_stack,
)
from agents.build_guidance import write_agent_constraints as _write_agent_constraints
from core.state import TaskState
from core.synthetic_test_harness_audit import prompt_context_for_records as _synthetic_harness_prompt_context
from core.validation_artifacts import (
    detect_validation_artifacts,
    restore_validation_artifacts,
    snapshot_validation_dirty_files,
)

log = logging.getLogger(__name__)

_AGENT_PROMPT = """\
You are fixing errors in a {tech_stack} codebase.
The working directory is the project root.

BEFORE YOU START — read these project guidelines:
{guidelines_files}
They define the architecture conventions, patterns, and rules you must follow.

CONSTRAINTS — follow strictly:
- You may only read files under these paths: {allowed_read_paths}
- You may only write to these directories: {allowed_write_paths}
- To delete a file, use Bash: `git rm <path>` — only for files within {allowed_write_paths}
- Bash is restricted to read-only commands and `git rm` only: `grep`, `find`, `ls`, `git rm`
  Do not run any other shell commands (`rm`, `mv`, `cp`, `curl`, `wget`, etc.)
- Fix ONLY what the errors describe — nothing more
- Do not refactor unrelated code
- Do not change behaviour beyond what the errors require
{build_tool_constraints}- Do not add comments or documentation unless required by the task or project guidelines.
  When modifying a function, class, or property that already has a doc comment, update it
  to stay accurate (e.g. add or remove @param entries) — do not delete it.
{test_constraint}

ORIGINAL TASK (for context):
{task_description}

IMPLEMENTATION THAT WAS APPLIED:
{implementation_prompt}

{errors_section}
"""

_BUILD_TEST_CONSTRAINT = """\
- NEVER create or modify unit tests — no files under any test source directory
  (e.g. `test/`, `__tests__/`, `spec/`), regardless of what the errors say"""

_TEST_ORIGIN_VALIDATION_CONSTRAINT = """\
- This appears to be a test-origin validation failure: build/check diagnostics reference
  test files or test targets
- This is a test-only triage/fix pass
- You MAY create and modify test files
- Do NOT modify production source files, build configuration, runtime configuration,
  dependency declarations, or pipeline settings in this pass
- First decide whether the failure is caused by production behaviour or by an incorrect/stale test
- Start your final response with:
  TEST FAILURE TRIAGE:
  classification: production_defect | stale_test | malformed_test | unclear
  contract_affected: <task/guideline/structured contract, or none>
  chosen_fix: production_code | test_code
- If the failure exposes a production defect, choose `production_code`, explain the defect,
  and leave files unchanged; Sikula will run a separate production-enabled fixer pass
- If a test encodes the original task, implementation prompt, project guidelines, or a structured
  input/output contract, do not weaken the test
- Modify tests only when the test is malformed, stale, or inconsistent with the accepted contract
- If you choose test_code, explain which accepted contract the test conflicts with
- If the failure is caused by a malformed generated test or test harness assumption, fix the
  test or test helper. Do not change production source, build configuration, runtime
  configuration, dependency declarations, or pipeline settings merely to satisfy a malformed test.
- Do not keep expanding a synthetic runtime/framework harness just to satisfy generated tests.
  Test-local fakes that reimplement render trees, selector engines, event dispatch,
  lifecycle schedulers, navigation/history stacks, dependency containers, device/emulator APIs,
  filesystems, servers, command runners, or other runtime infrastructure are brittle harnesses.
  Prefer replacing the generated test with coverage through existing project-standard seams, or
  leave the test unchanged and explain the TESTABILITY GAP if no meaningful seam exists.
- Treat generated tests/helpers that combine multiple fake runtime subsystems as brittle even
  if each fake looks small in isolation. Coupled fake DOM/window, event dispatch,
  navigation/history/router, network/fetch, filesystem, server, scheduler, command-runner,
  or dependency-container models recreate missing infrastructure. Do not keep fixing one fake
  subsystem at a time; simplify to narrower pure-seam/public-contract coverage or report the
  TESTABILITY GAP for behaviour that cannot be executed meaningfully.
- Do not fix a generated test failure by adding a new skipped, disabled, ignored,
  expected-failure, assumption-gated, or environment-gated test for the changed behaviour. A
  test that is expected to be skipped, or expected to fail, in Sikula's configured validation
  pipeline is not coverage. If missing runtime infrastructure prevents execution, preserve
  stable-seam coverage where possible and output a structured TESTABILITY GAP block.
- When outputting a TESTABILITY GAP, use:
  TESTABILITY GAP:
  target: <behaviour or contract that remains untested>
  reason: <missing seam, missing test harness, unavailable helper, etc.>
  covered_by: <existing-surface tests/seams added or preserved, or none>
  recommended_action: <project-level test infrastructure or seam needed>
  risk: low | medium | high
- If the failing file is listed as a test generated or updated by Sikula earlier in this
  task, you may replace or delete that generated test only when it is malformed/stale,
  depends on unavailable or brittle framework/test harness internals, and does not encode
  the original task, project guidelines, or a structured contract. Preserve real coverage
  by replacing it with a stable-seam test when possible; if no meaningful existing seam
  exists, explain the testability gap instead. Never delete or weaken pre-existing tests
  merely to make validation pass.
- For framework/container wiring such as dependency injection modules, provider trees,
  route registries, plugin registries, or service containers, do not hand-copy production
  registration logic into a local test-only container. Prefer exercising the real
  production configuration through existing helpers; otherwise replace the brittle
  generated test with stable-seam coverage or explain the gap.
- If validation reports generated repository artifacts from tests, fix the test harness so it uses
  OS temp storage or project-ignored temp/cache paths and cleans up after itself. Do not add
  production shims or build configuration just to tolerate generated test artifacts.
- Treat build files, dependency manifests, project/workspace files, generated-source config,
  and runtime configuration as production code for this triage. Changing them for a test-origin
  validation failure requires `classification: production_defect` and `chosen_fix: production_code`.
- Do not delete, relax, or rewrite assertions just to make the run green"""

_TEST_TEST_CONSTRAINT = """\
- This is a test-only triage/fix pass
- You MAY create and modify test files
- Do NOT modify production source files, build configuration, runtime configuration,
  dependency declarations, or pipeline settings in this pass
- First decide whether the failure is caused by production behaviour or by an incorrect/stale test
- Start your final response with:
  TEST FAILURE TRIAGE:
  classification: production_defect | stale_test | malformed_test | unclear
  contract_affected: <task/guideline/structured contract, or none>
  chosen_fix: production_code | test_code
- If the failing test exposes a production defect, choose `production_code`, explain the
  defect, and leave files unchanged; Sikula will run a separate production-enabled fixer pass
- If a test encodes the original task, implementation prompt, project guidelines, or a structured
  input/output contract, do not weaken the test
- Modify tests only when the test is malformed, stale, or inconsistent with the accepted contract
- If you choose test_code, explain which accepted contract the test conflicts with
- If the failure is caused by a malformed generated test or test harness assumption
  (for example current-working-directory assumptions, brittle source-file inspection,
  invalid test doubles, or unavailable test APIs), fix the test or test helper. Do not
  change production source, build configuration, runtime configuration, dependency
  declarations, or pipeline settings merely to satisfy a malformed test.
- Do not keep expanding a synthetic runtime/framework harness just to satisfy generated tests.
  Test-local fakes that reimplement render trees, selector engines, event dispatch,
  lifecycle schedulers, navigation/history stacks, dependency containers, device/emulator APIs,
  filesystems, servers, command runners, or other runtime infrastructure are brittle harnesses.
  Prefer replacing the generated test with coverage through existing project-standard seams, or
  leave the test unchanged and explain the TESTABILITY GAP if no meaningful seam exists.
- Treat generated tests/helpers that combine multiple fake runtime subsystems as brittle even
  if each fake looks small in isolation. Coupled fake DOM/window, event dispatch,
  navigation/history/router, network/fetch, filesystem, server, scheduler, command-runner,
  or dependency-container models recreate missing infrastructure. Do not keep fixing one fake
  subsystem at a time; simplify to narrower pure-seam/public-contract coverage or report the
  TESTABILITY GAP for behaviour that cannot be executed meaningfully.
- Do not fix a generated test failure by adding a new skipped, disabled, ignored,
  expected-failure, assumption-gated, or environment-gated test for the changed behaviour. A
  test that is expected to be skipped, or expected to fail, in Sikula's configured validation
  pipeline is not coverage. If missing runtime infrastructure prevents execution, preserve
  stable-seam coverage where possible and output a structured TESTABILITY GAP block.
- When outputting a TESTABILITY GAP, use:
  TESTABILITY GAP:
  target: <behaviour or contract that remains untested>
  reason: <missing seam, missing test harness, unavailable helper, etc.>
  covered_by: <existing-surface tests/seams added or preserved, or none>
  recommended_action: <project-level test infrastructure or seam needed>
  risk: low | medium | high
- If the failing file is listed as a test generated or updated by Sikula earlier in this
  task, you may replace or delete that generated test only when it is malformed/stale,
  depends on unavailable or brittle framework/test harness internals, and does not encode
  the original task, project guidelines, or a structured contract. Preserve real coverage
  by replacing it with a stable-seam test when possible; if no meaningful existing seam
  exists, explain the testability gap instead. Never delete or weaken pre-existing tests
  merely to make validation pass.
- For framework/container wiring such as dependency injection modules, provider trees,
  route registries, plugin registries, or service containers, do not hand-copy production
  registration logic into a local test-only container. Prefer exercising the real
  production configuration through existing helpers; otherwise replace the brittle
  generated test with stable-seam coverage or explain the gap.
- If validation reports generated repository artifacts from tests, fix the test harness so it uses
  OS temp storage or project-ignored temp/cache paths and cleans up after itself. Do not add
  production shims or build configuration just to tolerate generated test artifacts.
- Treat build files, dependency manifests, project/workspace files, generated-source config,
  and runtime configuration as production code for this triage. Changing them for a test
  failure requires `classification: production_defect` and `chosen_fix: production_code`.
- Do not delete, relax, or rewrite assertions just to make the run green"""

_CONFIRMED_PRODUCTION_TEST_FIX_CONSTRAINT = """\
- A previous test-only triage in this fixer run classified this failure as a production defect
  and selected a production-code fix
- You MAY modify production source files to fix that confirmed defect
- You MAY modify tests only if needed to preserve the accepted task/guideline/structured contract
- Start your final response with:
  TEST FAILURE TRIAGE:
  classification: production_defect
  contract_affected: <task/guideline/structured contract>
  chosen_fix: production_code
- Fix ONLY the confirmed production defect that caused the failing test or test-origin validation
- Do not change build configuration, runtime configuration, dependency declarations, or pipeline
  settings unless they are the confirmed production defect
- Do not delete, relax, or rewrite assertions just to make the run green"""

_TEST_ONLY_SCOPE_RECOVERY_INSTRUCTION = """\
TEST-ONLY SCOPE RECOVERY:
- A previous test-only triage pass violated its write scope: {reason}
- Sikula restored all file writes from that pass before this retry
- Retry from the current working tree and keep this pass strictly test-only
- If the failure requires production code, choose `production_code`, explain the production
  defect, and leave files unchanged so Sikula can run the production-confirmed pass"""

_GENERATED_TEST_RETRIAGE_RECOVERY_INSTRUCTION = """\
GENERATED TEST RE-TRIAGE RECOVERY:
- A previous repeated-generated-test pass changed generated test files but omitted the
  required GENERATED TEST RE-TRIAGE block: {reason}
- Sikula restored all file writes from that pass before this retry
- Retry from the current working tree and do not keep patching the generated test harness
  one assertion or fake subsystem at a time
- If the root cause is the same mechanical error repeated within the failing generated
  test file(s) (wrong API, wrong import, wrong syntax form), fix every occurrence of
  that same pattern needed for the current validation failure in one pass. Do not rewrite
  unrelated assertions, broaden or narrow coverage, or clean up unrelated code just
  because it is in the same file.
- If you change generated tests again, include:
  GENERATED TEST RE-TRIAGE:
  strategy: replace_with_narrower_seam_test | remove_malformed_generated_test | report_testability_gap | production_defect
  target: <failing generated test or behaviour>
  reason: <why repeated fixes indicate wrong test surface, malformed test, or production defect>
  covered_by: <existing-surface tests/seams preserved or added, or none>"""

_CHECK_TEST_CONSTRAINT = """\
- You MAY modify production or test files if the check errors explicitly reference them
- Fix ONLY the files named in the check errors — nothing else"""

_DEFAULT_CONTEXT_FILES = ["README.md"]
_TESTABILITY_GAP_MARKER = "TESTABILITY GAP:"
_TESTABILITY_GAP_FIELDS = {"target", "reason", "covered_by", "recommended_action", "risk"}
_GENERATED_TEST_RETRIAGE_MARKER = "GENERATED TEST RE-TRIAGE:"
_GENERATED_TEST_RETRIAGE_FIELDS = {"strategy", "target", "reason", "covered_by"}
_GENERATED_TEST_RETRIAGE_STRATEGIES = {
    "replace_with_narrower_seam_test",
    "remove_malformed_generated_test",
    "report_testability_gap",
    "production_defect",
}
_TEST_PATH_MARKERS = {
    "__tests__",
    "acceptancetest",
    "acceptancetests",
    "androidtest",
    "commontest",
    "commontests",
    "e2etest",
    "e2etests",
    "functionaltest",
    "functionaltests",
    "integrationtest",
    "integrationtests",
    "spec",
    "specs",
    "sharedtest",
    "sharedtests",
    "test",
    "testfixtures",
    "tests",
    "unittest",
    "unittests",
    "uitest",
    "uitests",
}
_TEST_FILE_SUFFIXES = (
    ".spec.js",
    ".spec.jsx",
    ".spec.ts",
    ".spec.tsx",
    ".test.js",
    ".test.jsx",
    ".test.ts",
    ".test.tsx",
    "_spec.py",
    "_test.py",
    "_tests.py",
)
_TEST_FILE_PREFIXES = ("test_",)
_VALIDATION_PATH_EXTENSIONS = (
    "c",
    "cc",
    "cpp",
    "cs",
    "cts",
    "go",
    "gradle",
    "h",
    "hpp",
    "java",
    "js",
    "json",
    "jsx",
    "kt",
    "kts",
    "m",
    "mm",
    "mts",
    "php",
    "py",
    "rb",
    "rs",
    "scala",
    "swift",
    "toml",
    "ts",
    "tsx",
    "xml",
    "yaml",
    "yml",
)
_VALIDATION_PATH_RE = re.compile(
    # Path-like diagnostics with a directory separator are intentionally extension-neutral:
    # production files such as schemas/*.proto must not be ignored in mixed test/prod errors.
    r"(?<![\w./:-])(?P<path>(?:file://)?(?:[A-Za-z]:)?(?:[^:\s'\"`<>()\[\]{}]+[\\/])+"
    r"[^:\s'\"`<>()\[\]{}\\/]+\.[A-Za-z0-9][A-Za-z0-9_.-]{0,31})"
    r"|(?<![\w./:-])(?P<file>\b[^:\s'\"`<>()\[\]{}\\/]+\.(?:"
    + "|".join(re.escape(ext) for ext in _VALIDATION_PATH_EXTENSIONS)
    + r")\b)"
    r"|(?<![\w./:-])(?P<diag_file>\b[^:\s'\"`<>()\[\]{}\\/]+\.[A-Za-z0-9][A-Za-z0-9_.-]{0,31})"
    r"(?=(?::\d+(?![/?#\w.-])|\(\d|,\s*line\b))",
    re.IGNORECASE,
)
_BAZEL_TARGET_RE = re.compile(r"(?<![\w/.\-:])(?P<target>(?:@[A-Za-z0-9_.-]+)?//[A-Za-z0-9_./+-]*:[A-Za-z0-9_.+-]+)")
_GRADLE_TARGET_RE = re.compile(r"(?<![\w/.\-:])(?P<target>:(?:[A-Za-z][A-Za-z0-9_.-]*:)*[A-Za-z][A-Za-z0-9_.-]*)")
_TEST_TARGET_MARKERS = (
    "acceptancetest",
    "acceptancetests",
    "androidtest",
    "androidtests",
    "e2etest",
    "e2etests",
    "functionaltest",
    "functionaltests",
    "integrationtest",
    "integrationtests",
    "test",
    "tests",
    "unittest",
    "unittests",
    "uitest",
    "uitests",
)


def _scope(state: TaskState) -> str:
    if state.active_scope:
        return state.active_scope
    return "step" if state.plan else "task"


def _test_constraint(
    state: TaskState,
    *,
    test_origin_validation: bool = False,
    production_fix_confirmed: bool = False,
) -> str:
    if production_fix_confirmed:
        return _CONFIRMED_PRODUCTION_TEST_FIX_CONSTRAINT
    if test_origin_validation:
        return _TEST_ORIGIN_VALIDATION_CONSTRAINT
    if state.errors:
        return _BUILD_TEST_CONSTRAINT
    if state.test_errors and not state.check_errors:
        return _TEST_TEST_CONSTRAINT
    # check_errors (detekt/lint) can reference test or production files — allow both
    return _CHECK_TEST_CONSTRAINT


def _combined_write_paths(sandbox: dict) -> list[str]:
    paths: list[str] = []
    for key in ("allowed_write_paths", "allowed_test_write_paths"):
        for path in sandbox.get(key, []):
            if path not in paths:
                paths.append(path)
    return paths


def _test_write_paths(sandbox: dict) -> list[str]:
    paths: list[str] = []
    for path in sandbox.get("allowed_test_write_paths", []):
        if path not in paths:
            paths.append(path)
    return paths


def _write_paths_for_state(
    state: TaskState,
    sandbox: dict,
    *,
    test_origin_validation: bool = False,
    production_fix_confirmed: bool = False,
) -> list[str]:
    if production_fix_confirmed:
        return _combined_write_paths(sandbox)
    if test_origin_validation or (state.test_errors and not state.errors and not state.check_errors):
        return _test_write_paths(sandbox)
    if state.errors:
        return sandbox.get("allowed_write_paths", [])
    return _combined_write_paths(sandbox)


def _normalize_project_path(path: str) -> str:
    return posixpath.normpath(str(path).replace("\\", "/")).lower()


def _path_parts(path: str) -> list[str]:
    normalized = _normalize_project_path(path)
    return [part for part in normalized.split("/") if part and part != "."]


def _is_test_path_marker(part: str) -> bool:
    return part in _TEST_PATH_MARKERS or part.endswith(("tests", "_test", "_tests", "-test", "-tests"))


def _root_is_test_specific(root: str) -> bool:
    return any(_is_test_path_marker(part) for part in _path_parts(root))


def _path_is_under_root(path: str, root: str) -> bool:
    normalized_path = _normalize_project_path(path)
    normalized_root = _normalize_project_path(root).rstrip("/")
    if normalized_root in ("", "."):
        return True
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def _is_under_specific_test_root(path: str, roots: list[str]) -> bool:
    return any(_root_is_test_specific(root) and _path_is_under_root(path, root) for root in roots)


def _looks_like_test_artifact(path: str) -> bool:
    parts = _path_parts(path)
    if any(_is_test_path_marker(part) for part in parts[:-1]):
        return True
    if not parts:
        return False
    filename = parts[-1]
    return filename.startswith(_TEST_FILE_PREFIXES) or filename.endswith(_TEST_FILE_SUFFIXES)


def _project_relative_error_path(path: str, project_root: Path | None = None) -> str:
    cleaned = unquote(path.strip().strip("\"'`<>").rstrip(".,;"))
    if cleaned.lower().startswith("file://"):
        cleaned = cleaned[7:]
    cleaned = cleaned.replace("\\", "/")
    if project_root:
        try:
            candidate = Path(cleaned)
            if candidate.is_absolute():
                cleaned = candidate.resolve().relative_to(project_root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return posixpath.normpath(cleaned)


def _validation_error_paths(errors: list[str], project_root: Path | None = None) -> list[str]:
    paths: list[str] = []
    for error in errors:
        for match in _VALIDATION_PATH_RE.finditer(error):
            raw_path = match.group("path") or match.group("file") or match.group("diag_file")
            if not raw_path:
                continue
            path = _project_relative_error_path(raw_path, project_root)
            if path not in paths:
                paths.append(path)
    return paths


def _validation_error_targets(errors: list[str]) -> list[str]:
    targets: list[str] = []
    for error in errors:
        for regex in (_BAZEL_TARGET_RE, _GRADLE_TARGET_RE):
            for match in regex.finditer(error):
                target = match.group("target")
                if target and target not in targets:
                    targets.append(target)
    return targets


def _is_test_origin_path(path: str, sandbox: dict) -> bool:
    if posixpath.isabs(path) or re.match(r"^[A-Za-z]:/", path):
        return False
    return _is_under_specific_test_root(path, sandbox.get("allowed_test_write_paths", [])) or _looks_like_test_artifact(
        path
    )


def _target_words(target: str) -> list[str]:
    words: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", target):
        if not part:
            continue
        lower_part = part.lower()
        words.append(lower_part)
        camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", part)
        camel_split = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", camel_split)
        words.extend(
            token.lower() for token in re.split(r"[^A-Za-z0-9]+", camel_split) if token and token.lower() != lower_part
        )
    return words


def _is_test_origin_target(target: str) -> bool:
    words = _target_words(target)
    if not words:
        return False
    return any(word in _TEST_TARGET_MARKERS for word in words)


def _is_test_origin_validation_failure(state: TaskState, sandbox: dict, project_root: Path | None = None) -> bool:
    if state.test_errors:
        return False
    if not state.errors and not state.check_errors:
        return False
    validation_errors = [*state.errors, *state.check_errors]
    paths = _validation_error_paths(validation_errors, project_root)
    targets = _validation_error_targets(validation_errors)
    if not paths and not targets:
        return False
    return all(_is_test_origin_path(path, sandbox) for path in paths) and all(
        _is_test_origin_target(target) for target in targets
    )


def _uses_test_failure_triage(state: TaskState, sandbox: dict, project_root: Path | None = None) -> bool:
    return (
        bool(state.test_errors) and not state.errors and not state.check_errors
    ) or _is_test_origin_validation_failure(state, sandbox, project_root)


def _is_build_config_file(build_tool: Any, path: str) -> bool:
    if not build_tool or not hasattr(build_tool, "is_build_config_file"):
        return False
    try:
        return bool(build_tool.is_build_config_file(path))
    except Exception:
        return False


def _is_platform_test_only_change(
    build_tool: Any,
    path: str,
    before_contents: dict[str, str | None] | None,
    after_contents: dict[str, str | None] | None,
) -> bool:
    if not build_tool or not hasattr(build_tool, "is_test_only_change"):
        return False
    try:
        return bool(
            build_tool.is_test_only_change(
                path,
                (before_contents or {}).get(path),
                (after_contents or {}).get(path),
            )
        )
    except Exception:
        return False


def _test_failure_production_writes(
    changed: list[str],
    sandbox: dict,
    build_tool: Any = None,
    before_contents: dict[str, str | None] | None = None,
    after_contents: dict[str, str | None] | None = None,
) -> list[str]:
    test_paths = sandbox.get("allowed_test_write_paths", [])
    if not test_paths:
        return list(changed)
    outside_test_paths = set(_paths_outside_allowed(changed, test_paths))
    production: list[str] = []
    for path in changed:
        if _is_under_specific_test_root(path, test_paths):
            continue
        if _looks_like_test_artifact(path):
            continue
        if _is_build_config_file(build_tool, path):
            production.append(path)
            continue
        if _is_platform_test_only_change(build_tool, path, before_contents, after_contents):
            continue
        if path in outside_test_paths:
            production.append(path)
            continue
        production.append(path)
    return production


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
        return None


def _git_dirty_paths(cwd: Path) -> list[str]:
    modified = subprocess.run(
        ["git", "diff", "--name-only", "--relative", "HEAD"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if modified.returncode != 0 and untracked.returncode != 0:
        return []
    paths: list[str] = []
    for line in (modified.stdout + "\n" + untracked.stdout).splitlines():
        path = line.strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _git_dirty_text_snapshot(cwd: Path) -> dict[str, str | None]:
    return {path: _read_text(cwd / path) for path in _git_dirty_paths(cwd)}


def _git_head_text(cwd: Path, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _changed_text_contents_before(
    cwd: Path,
    changed: list[str],
    dirty_before: dict[str, str | None],
) -> dict[str, str | None]:
    contents: dict[str, str | None] = {}
    for path in changed:
        contents[path] = dirty_before[path] if path in dirty_before else _git_head_text(cwd, path)
    return contents


def _changed_text_contents_after(cwd: Path, changed: list[str]) -> dict[str, str | None]:
    return {path: _read_text(cwd / path) for path in changed}


def _triage_field(output: str, field: str) -> str:
    prefix = f"{field.lower()}:"
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix):
            return stripped.split(":", 1)[1].strip().lower()
    return ""


def _has_valid_production_test_failure_triage(output: str) -> bool:
    if "test failure triage:" not in output.lower():
        return False
    return (
        _triage_field(output, "classification") == "production_defect"
        and _triage_field(output, "chosen_fix") == "production_code"
    )


def _errors_section(state: TaskState) -> str:
    sections = []
    if state.errors:
        sections.append("BUILD ERRORS:\n" + "\n\n".join(state.errors[-3:]))
    if state.test_errors:
        sections.append("TEST FAILURES:\n" + "\n\n".join(state.test_errors[-3:]))
    if state.check_errors:
        sections.append("CHECK ERRORS:\n" + "\n\n".join(state.check_errors[-3:]))
    return "\n\n".join(sections)


def _parse_testability_gaps(output: str | None) -> list[dict]:
    if not output or _TESTABILITY_GAP_MARKER.lower() not in output.lower():
        return []

    gaps: list[dict] = []
    current: list[str] = []
    for line in output.splitlines():
        if line.strip().lower().startswith(_TESTABILITY_GAP_MARKER.lower()):
            if current:
                gaps.append(_gap_from_lines(current))
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        gaps.append(_gap_from_lines(current))
    return gaps


def _gap_from_lines(lines: list[str]) -> dict:
    message = "\n".join(lines).strip()
    gap = {"message": message}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        normalized_key = key.strip().lower().replace(" ", "_")
        if normalized_key in _TESTABILITY_GAP_FIELDS:
            gap[normalized_key] = value.strip()
    return gap


def _parse_generated_test_retriage(output: str | None) -> dict | None:
    if not output or _GENERATED_TEST_RETRIAGE_MARKER.lower() not in output.lower():
        return None

    lines = output.splitlines()
    block: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith(_GENERATED_TEST_RETRIAGE_MARKER.lower()):
            collecting = True
            block = [line]
            continue
        if not collecting:
            continue
        if stripped.endswith(":") and block and ":" not in stripped[:-1]:
            break
        block.append(line)

    if not block:
        return None

    retriage: dict = {"message": "\n".join(block).strip()}
    for line in block[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        normalized_key = key.strip().lower().replace(" ", "_")
        if normalized_key in _GENERATED_TEST_RETRIAGE_FIELDS:
            retriage[normalized_key] = value.strip()
    if any(not str(retriage.get(field, "")).strip() for field in _GENERATED_TEST_RETRIAGE_FIELDS):
        return None
    if retriage["strategy"] not in _GENERATED_TEST_RETRIAGE_STRATEGIES:
        return None
    return retriage


def _generated_test_context(state: TaskState) -> str:
    if not state.test_files_written:
        return ""
    files = "\n".join(f"  - {path}" for path in state.test_files_written[:50])
    if len(state.test_files_written) > 50:
        files += f"\n  ... ({len(state.test_files_written) - 50} more)"
    return (
        "TEST FILES GENERATED OR UPDATED BY SIKULA EARLIER IN THIS TASK:\n"
        f"{files}\n\n"
        "These files are generated test output from the current task, not pre-existing "
        "project tests. If the current failure is in one of these files, preserve any "
        "real task/guideline/structured-contract coverage, but do not keep repairing a "
        "malformed or brittle generated harness merely because it exists."
    )


def _repeated_generated_test_fix_context(state: TaskState) -> str:
    if not state.test_files_written:
        return ""
    generated = set(state.test_files_written)
    repeated_files = [
        (path, _generated_test_fix_count(state, path))
        for path in state.test_files_written
        if path in generated and _generated_test_fix_count(state, path) >= 2
    ]
    if not repeated_files:
        return ""

    file_list = "\n".join(f"  - {path} ({count} generated-test fix attempts)" for path, count in repeated_files[:20])
    if len(repeated_files) > 20:
        file_list += f"\n  ... ({len(repeated_files) - 20} more)"
    total_attempts = sum(count for _, count in repeated_files)
    return (
        "REPEATED GENERATED TEST FIX CONTEXT:\n"
        f"Sikula has already made {total_attempts} test-only fixer pass(es) against "
        "test files generated or updated in this task.\n"
        f"{file_list}\n\n"
        "Treat another failure in this area as a signal that the generated test harness may "
        "be the problem. Do not keep patching small pieces of a brittle synthetic runtime "
        "harness, especially when it combines fake runtime subsystems such as event "
        "dispatch, navigation/history/router, network/fetch, filesystem, server, scheduler, "
        "command-runner, or dependency-container models. Re-evaluate whether the test should "
        "be replaced with coverage through existing project-standard seams, narrowed to a "
        "stable public contract, or left as a TESTABILITY GAP when no meaningful existing "
        "seam exists.\n\n"
        "Before changing generated tests again, explicitly choose the strategy in your final "
        "response:\n"
        "GENERATED TEST RE-TRIAGE:\n"
        "strategy: replace_with_narrower_seam_test | remove_malformed_generated_test | "
        "report_testability_gap | production_defect\n"
        "target: <failing generated test or behaviour>\n"
        "reason: <why repeated fixes indicate wrong test surface, malformed test, or production defect>\n"
        "covered_by: <existing-surface tests/seams preserved or added, or none>\n\n"
        "Treat coverage targets as applying only to runnable behaviour inside the configured "
        "test surface; behaviour outside that surface belongs in a TESTABILITY GAP instead "
        "of a synthetic runtime harness."
    )


def _repeated_generated_test_retriage_required(state: TaskState) -> bool:
    return bool(_repeated_generated_test_fix_context(state))


def _generated_test_writes(state: TaskState, changed: list[str]) -> list[str]:
    generated = set(state.test_files_written)
    return [path for path in changed if path in generated]


def _generated_test_fix_count(state: TaskState, path: str) -> int:
    try:
        return max(0, int(state.generated_test_fix_counts.get(path, 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def _record_generated_test_fix_attempt(state: TaskState, paths: list[str]) -> None:
    if not isinstance(state.generated_test_fix_counts, dict):
        state.generated_test_fix_counts = {}
    for path in sorted({str(candidate) for candidate in paths if str(candidate).strip()}):
        state.generated_test_fix_counts[path] = _generated_test_fix_count(state, path) + 1


class FixerAgent(BaseAgent):
    name = "fixer"

    def run(self, state: TaskState) -> AgentResult:
        if not state.errors and not state.test_errors and not state.check_errors:
            return AgentResult(success=False, message="No errors in state to fix")

        file_tool = self.tools.get("file")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        sandbox = self.project_config.get("sandbox", {})
        agent_cwd = Path(file_tool._root)
        test_origin_validation = _is_test_origin_validation_failure(state, sandbox, agent_cwd)
        uses_test_failure_triage = _uses_test_failure_triage(state, sandbox, agent_cwd)
        triage_scope = None
        if test_origin_validation:
            triage_scope = "test_origin_validation"
        elif uses_test_failure_triage:
            triage_scope = "test_failure"
        allowed_read_paths = sandbox.get("allowed_read_paths", ["."])
        allowed_read_str = ", ".join(allowed_read_paths)

        errors_snapshot = {
            "build": list(state.errors),
            "test": list(state.test_errors),
            "check": list(state.check_errors),
        }

        def _prompt(
            *,
            allowed_write_paths: list[str],
            test_constraint: str,
            previous_triage: str | None = None,
            scope_recovery: str | None = None,
        ) -> str:
            allowed_str = ", ".join(allowed_write_paths) if allowed_write_paths else "(not configured)"
            errors_section = _errors_section(state)
            generated_test_context = _generated_test_context(state) if uses_test_failure_triage else ""
            if generated_test_context:
                errors_section += "\n\n" + generated_test_context
            repeated_generated_test_context = (
                _repeated_generated_test_fix_context(state) if uses_test_failure_triage else ""
            )
            if repeated_generated_test_context:
                errors_section += "\n\n" + repeated_generated_test_context
            synthetic_harness_context = (
                _synthetic_harness_prompt_context(state.synthetic_test_harness_records)
                if uses_test_failure_triage
                else ""
            )
            if synthetic_harness_context:
                errors_section += "\n\n" + synthetic_harness_context
            if previous_triage:
                errors_section += "\n\nCONFIRMED TEST FAILURE TRIAGE:\n" + previous_triage.strip()
            if scope_recovery:
                errors_section += "\n\n" + scope_recovery.strip()
            return AGENT_SECURITY_PREFIX + _AGENT_PROMPT.format(
                tech_stack=_tech_stack(self.project_config),
                guidelines_files=_guidelines_files(self.project_config),
                allowed_read_paths=allowed_read_str,
                allowed_write_paths=allowed_str,
                build_tool_constraints=_write_agent_constraints(self.project_config),
                test_constraint=test_constraint,
                task_description=state.task_description,
                implementation_prompt=state.implementation_prompt or "(not available)",
                errors_section=errors_section,
            )

        def _run_once(
            *,
            allowed_write_paths: list[str],
            test_constraint: str,
            triage_pass: str | None = None,
            previous_triage: str | None = None,
            scope_recovery: str | None = None,
        ) -> tuple[AgentResult | None, list[str], str, dict[str, str | None], dict[str, Any], list[str]]:
            prompt = _prompt(
                allowed_write_paths=allowed_write_paths,
                test_constraint=test_constraint,
                previous_triage=previous_triage,
                scope_recovery=scope_recovery,
            )
            dirty_before = _git_dirty_text_snapshot(agent_cwd) if uses_test_failure_triage else {}
            artifact_before = snapshot_validation_dirty_files(agent_cwd) if uses_test_failure_triage else {}
            try:
                changed, fixer_output = self.llm.run_agent(prompt, cwd=agent_cwd)
            except RuntimeError as e:
                msg = str(e)
                state.record(self.name, "fix_failed", msg[:500])
                record = {
                    "build_iteration": state.build_iterations,
                    "step": state.current_step,
                    "scope": _scope(state),
                    "errors_before": errors_snapshot,
                    "fixer_prompt": prompt,
                    "fixer_output": None,
                    "files_written": [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if triage_scope:
                    record["triage_scope"] = triage_scope
                if triage_pass:
                    record["triage_pass"] = triage_pass
                if previous_triage:
                    record["confirmed_test_failure_triage"] = previous_triage
                if scope_recovery:
                    record["scope_recovery"] = scope_recovery
                state.fix_cycle_records.append(record)
                return (
                    AgentResult(success=False, message=msg[:200]),
                    [],
                    "",
                    dirty_before,
                    artifact_before,
                    allowed_write_paths,
                )

            record = {
                "build_iteration": state.build_iterations,
                "step": state.current_step,
                "scope": _scope(state),
                "errors_before": errors_snapshot,
                "fixer_prompt": prompt,
                "fixer_output": fixer_output,
                "files_written": changed,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if triage_scope:
                record["triage_scope"] = triage_scope
            if triage_pass:
                record["triage_pass"] = triage_pass
            if previous_triage:
                record["confirmed_test_failure_triage"] = previous_triage
            if scope_recovery:
                record["scope_recovery"] = scope_recovery
            generated_test_retriage = _parse_generated_test_retriage(fixer_output)
            if generated_test_retriage:
                record["generated_test_retriage"] = generated_test_retriage
            state.fix_cycle_records.append(record)
            for gap in _parse_testability_gaps(fixer_output):
                state.record_testability_gap(
                    self.name,
                    gap["message"],
                    target=gap.get("target"),
                    reason=gap.get("reason"),
                    covered_by=gap.get("covered_by"),
                    recommended_action=gap.get("recommended_action"),
                    risk=gap.get("risk"),
                )
            return None, changed, fixer_output, dirty_before, artifact_before, allowed_write_paths

        def _fail_after_changes(changed: list[str], msg: str) -> AgentResult:
            for p in changed:
                if p not in state.files_changed:
                    state.files_changed.append(p)
            state.record(self.name, "fix_failed", msg)
            state.failed = True
            return AgentResult(
                success=False,
                message=msg[:200],
                data={"files_written": changed},
            )

        def _production_writes(changed: list[str], dirty_before: dict[str, str | None]) -> list[str]:
            before_contents = _changed_text_contents_before(agent_cwd, changed, dirty_before)
            after_contents = _changed_text_contents_after(agent_cwd, changed)
            return _test_failure_production_writes(
                changed,
                sandbox,
                self.tools.get("build"),
                before_contents,
                after_contents,
            )

        def _restore_attempt_writes(changed: list[str], artifact_before: dict[str, Any]) -> tuple[list[str], list[str]]:
            changed_set = set(changed)
            artifact_after = snapshot_validation_dirty_files(agent_cwd)
            artifacts = [
                artifact
                for artifact in detect_validation_artifacts(artifact_before, artifact_after)
                if artifact.path in changed_set
            ]
            errors = restore_validation_artifacts(agent_cwd, artifact_before, artifacts)
            return [artifact.path for artifact in artifacts], errors

        def _record_test_only_scope_violation(
            *,
            kind: str,
            reason: str,
            changed: list[str],
            violation_files: list[str],
            restored_files: list[str],
            restore_errors: list[str],
            will_retry: bool,
        ) -> None:
            if state.fix_cycle_records:
                state.fix_cycle_records[-1]["test_only_scope_violation"] = {
                    "kind": kind,
                    "reason": reason,
                    "files_written": list(changed),
                    "violation_files": list(violation_files),
                    "restored_files": list(restored_files),
                    "restore_errors": list(restore_errors),
                    "retry": will_retry,
                }
            action = "test_only_scope_violation_reverted"
            if restore_errors:
                action = "test_only_scope_violation_restore_failed"
            state.record(self.name, action, reason[:500])

        def _record_generated_test_retriage_violation(
            *,
            reason: str,
            changed: list[str],
            generated_test_files: list[str],
            restored_files: list[str],
            restore_errors: list[str],
            will_retry: bool,
        ) -> None:
            if state.fix_cycle_records:
                state.fix_cycle_records[-1]["generated_test_retriage_violation"] = {
                    "reason": reason,
                    "files_written": list(changed),
                    "generated_test_files": list(generated_test_files),
                    "restored_files": list(restored_files),
                    "restore_errors": list(restore_errors),
                    "retry": will_retry,
                }
            action = "generated_test_retriage_violation_reverted"
            if restore_errors:
                action = "generated_test_retriage_violation_restore_failed"
            state.record(self.name, action, reason[:500])

        violation_continue_restored = False
        if uses_test_failure_triage:
            allowed_write_paths = _write_paths_for_state(state, sandbox, test_origin_validation=test_origin_validation)
            test_constraint = _test_constraint(state, test_origin_validation=test_origin_validation)
            scope_recovery: str | None = None
            changed: list[str] = []
            fixer_output = ""
            final_allowed_write_paths = allowed_write_paths
            max_test_only_attempts = 2
            for attempt_index in range(max_test_only_attempts):
                triage_pass = "test_only" if attempt_index == 0 else "test_only_retry"
                generated_test_retriage_required = _repeated_generated_test_retriage_required(state)
                if scope_recovery:
                    log.info("Retrying fixer agent with %s recovery prompt", triage_pass)
                result, changed, fixer_output, dirty_before, artifact_before, final_allowed_write_paths = _run_once(
                    allowed_write_paths=allowed_write_paths,
                    test_constraint=test_constraint,
                    triage_pass=triage_pass,
                    scope_recovery=scope_recovery,
                )
                if result is not None:
                    return result

                production_writes = _production_writes(changed, dirty_before)
                production_request_with_writes = _has_valid_production_test_failure_triage(fixer_output) and bool(
                    changed
                )
                if not production_writes and not production_request_with_writes:
                    generated_test_files = _generated_test_writes(state, changed)
                    if generated_test_files:
                        _record_generated_test_fix_attempt(state, generated_test_files)
                    missing_generated_test_retriage = (
                        generated_test_retriage_required
                        and bool(generated_test_files)
                        and not _parse_generated_test_retriage(fixer_output)
                    )
                    if not missing_generated_test_retriage:
                        break

                    reason = (
                        "Repeated generated-test fixer changed generated test files without "
                        f"GENERATED TEST RE-TRIAGE during the {triage_pass} pass: {generated_test_files}"
                    )
                    will_retry = attempt_index + 1 < max_test_only_attempts
                    restored_files: list[str] = []
                    restore_errors: list[str] = []
                    if will_retry:
                        restored_files, restore_errors = _restore_attempt_writes(changed, artifact_before)
                        will_retry = not restore_errors
                    _record_generated_test_retriage_violation(
                        reason=reason,
                        changed=changed,
                        generated_test_files=generated_test_files,
                        restored_files=restored_files,
                        restore_errors=restore_errors,
                        will_retry=will_retry,
                    )
                    if restore_errors:
                        log.error(
                            "Generated test re-triage violation restore failed for %s: %s",
                            ", ".join(generated_test_files),
                            "; ".join(restore_errors),
                        )
                        msg = f"{reason}; failed to restore the violating fixer writes: {restore_errors}"
                        return _fail_after_changes(changed, msg)
                    if not will_retry:
                        log.warning(
                            "Generated test re-triage still missing after retry for %s - restoring writes and continuing",
                            ", ".join(generated_test_files),
                        )
                        restored_files, restore_errors = _restore_attempt_writes(changed, artifact_before)
                        if state.fix_cycle_records:
                            viol = state.fix_cycle_records[-1].get("generated_test_retriage_violation", {})
                            viol["restored_files"] = restored_files
                            viol["restore_errors"] = restore_errors
                        state.record(self.name, "generated_test_retriage_violation_continue", reason[:500])
                        if restore_errors:
                            log.error(
                                "Generated test re-triage violation restore failed for %s: %s",
                                ", ".join(generated_test_files),
                                "; ".join(restore_errors),
                            )
                            return _fail_after_changes(changed, f"{reason}; failed to restore: {restore_errors}")
                        # Files restored: no net changes on disk. Signal to the success path below
                        # to skip error-clearing so the next iteration gets correct error context.
                        violation_continue_restored = True
                        changed = []
                        break
                    log.warning(
                        "Generated test re-triage missing for %s - restored %s and retrying fixer",
                        ", ".join(generated_test_files),
                        ", ".join(restored_files) or "(no files)",
                    )
                    scope_recovery = _GENERATED_TEST_RETRIAGE_RECOVERY_INSTRUCTION.format(reason=reason)
                    continue

                failure_kind = "test-origin validation" if test_origin_validation else "test-failure"
                if production_writes:
                    kind = "production_writes"
                    violation_files = production_writes
                    reason = (
                        f"{failure_kind.capitalize()} fixer changed production files during the "
                        f"{triage_pass} pass: {production_writes}"
                    )
                else:
                    kind = "production_request_changed_files"
                    violation_files = changed
                    reason = (
                        f"{failure_kind.capitalize()} fixer requested a production-code fix but "
                        f"changed files during the {triage_pass} pass: {changed}"
                    )

                restored_files, restore_errors = _restore_attempt_writes(changed, artifact_before)
                will_retry = not restore_errors and attempt_index + 1 < max_test_only_attempts
                _record_test_only_scope_violation(
                    kind=kind,
                    reason=reason,
                    changed=changed,
                    violation_files=violation_files,
                    restored_files=restored_files,
                    restore_errors=restore_errors,
                    will_retry=will_retry,
                )
                if restore_errors:
                    log.error(
                        "Test-only fixer scope violation restore failed for %s: %s",
                        ", ".join(violation_files),
                        "; ".join(restore_errors),
                    )
                    msg = f"{reason}; failed to restore the violating fixer writes: {restore_errors}"
                    return _fail_after_changes(changed, msg)
                if not will_retry:
                    log.warning(
                        "Test-only fixer scope violation for %s - restored writes and no retry attempts remain",
                        ", ".join(violation_files),
                    )
                    msg = f"{reason}; restored violating writes and no retry attempts remain"
                    state.record(self.name, "fix_failed", msg)
                    state.failed = True
                    return AgentResult(
                        success=False,
                        message=msg[:200],
                        data={"files_written": changed, "restored_files": restored_files},
                    )
                log.warning(
                    "Test-only fixer scope violation for %s - restored %s and retrying fixer",
                    ", ".join(violation_files),
                    ", ".join(restored_files) or "(no files)",
                )
                scope_recovery = _TEST_ONLY_SCOPE_RECOVERY_INSTRUCTION.format(reason=reason)
            if _has_valid_production_test_failure_triage(fixer_output):
                failure_kind = "test-origin validation" if test_origin_validation else "test-failure"
                allowed_write_paths = _write_paths_for_state(
                    state,
                    sandbox,
                    test_origin_validation=test_origin_validation,
                    production_fix_confirmed=True,
                )
                log.info("Running fixer agent production-confirmed pass after %s triage", failure_kind)
                result, changed, fixer_output, dirty_before, _, final_allowed_write_paths = _run_once(
                    allowed_write_paths=allowed_write_paths,
                    test_constraint=_test_constraint(
                        state,
                        test_origin_validation=test_origin_validation,
                        production_fix_confirmed=True,
                    ),
                    triage_pass="production_confirmed",
                    previous_triage=fixer_output,
                )
                if result is not None:
                    return result
                production_writes = _production_writes(changed, dirty_before)
                if changed and not production_writes:
                    msg = (
                        f"{failure_kind.capitalize()} production-confirmed fixer changed no production files "
                        f"after production_defect triage: {changed}"
                    )
                    return _fail_after_changes(changed, msg)
        else:
            allowed_write_paths = _write_paths_for_state(state, sandbox, test_origin_validation=test_origin_validation)
            result, changed, fixer_output, _, _, final_allowed_write_paths = _run_once(
                allowed_write_paths=allowed_write_paths,
                test_constraint=_test_constraint(state, test_origin_validation=test_origin_validation),
            )
            if result is not None:
                return result

        if not changed:
            if violation_continue_restored:
                # Writes were restored after a GENERATED TEST RE-TRIAGE protocol violation.
                # Clear errors as normal — the next test run will repopulate them fresh.
                state.errors.clear()
                state.test_errors.clear()
                state.check_errors.clear()
                state.record(self.name, "fix_no_net_change", "violation_continue writes restored")
                return AgentResult(success=True, message="Restored violating writes; no net changes")
            msg = "Agent made no file changes"
            state.record(self.name, "fix_failed", msg)
            return AgentResult(success=False, message=msg)

        for p in changed:
            if p not in state.files_changed:
                state.files_changed.append(p)

        state.errors.clear()
        state.test_errors.clear()
        state.check_errors.clear()
        state.record(self.name, "fix", f"files changed: {changed}")
        _record_write_path_warnings(state, self.name, changed, final_allowed_write_paths, "active write paths")
        return AgentResult(
            success=True,
            message=f"Fix applied to {len(changed)} file(s): {changed}",
            data={"files_written": changed},
        )
