"""Init command helpers for the Sikula CLI."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys

import yaml

from core import worktree as core_worktree
from sikula_cli.config import _load_project_env

_SIKULA_GITIGNORE_ENTRIES = ("state/", "worktrees/", "contract-reports/")


def register_parser(subparsers) -> argparse.ArgumentParser:
    init_p = subparsers.add_parser("init", help="Initialize a new .sikula project config")
    init_p.add_argument("--force", action="store_true", default=False, help="Overwrite existing config")
    init_p.add_argument(
        "--guidelines",
        action="store_true",
        default=False,
        help="Use LLM to generate .sikula/guidelines.md from codebase analysis",
    )
    init_p.add_argument(
        "--provider",
        default=None,
        help="LLM provider for --guidelines (codex/claude/gemini/opencode/antigravity); falls back to config when present",
    )
    init_p.add_argument("--model", default=None, help="LLM model for --guidelines; falls back to config when present")
    return init_p


def generate_config(  # noqa: PLR0912
    build_tool: str | None,
    language: str | None,
    platform: str | None,
    guidelines_files: list[str],
    project_name: str,
    provider: str | None,
    model: str | None,
    write_paths: list[str] | None = None,
    test_write_paths: list[str] | None = None,
    xcode_scheme: str | None = None,
    node_package_manager: str | None = None,
    node_sync_command: str | None = None,
    node_compile_command: str | None = None,
    node_test_command: str | None = None,
    node_checks: list[dict[str, str | int]] | None = None,
) -> str:
    if guidelines_files:
        guidelines_block = "  context_files:\n" + "\n".join(f"    - {f}" for f in guidelines_files)
    else:
        guidelines_block = "  context_files: []"

    wp = write_paths or ["src/"]
    twp = test_write_paths or ["tests/"]
    wp_list = "\n".join(f"    - {p}" for p in wp)
    twp_list = "\n".join(f"    - {p}" for p in twp)
    write_paths_comment = "" if write_paths else "  # TODO: restrict to dirs agents may write production code to.\n"
    test_paths_comment = "" if test_write_paths else "  # TODO: restrict to dirs the test writer may write to.\n"

    provider_comment = (
        "" if provider else "  # TODO: change to your provider (codex/claude/gemini/opencode/antigravity)"
    )
    model_comment = "" if model else "  # TODO: change to your model"
    agent_model_comment = "" if model else "  # TODO: consider a stronger model"

    build_section = ""
    if build_tool == "cargo":
        build_section = """\
build:
  compile_command: "cargo check"
  test_command: "cargo test"
  timeout: 600
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked Cargo.lock.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/"
  #   - "schema/generated/**/*.json"
  checks:
    - name: clippy
      command: "cargo clippy -- -D warnings"
      timeout: 120
    - name: fmt
      command: "cargo fmt --check"
      fix_command: "cargo fmt"
      timeout: 60
"""
    elif build_tool == "python":
        build_section = """\
build:
  compile_command: "python3 -m compileall -q ."
  test_command: "python3 -m pytest"
  timeout: 300
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # PythonTool has no built-in lockfile default, so use this for intentional generated sync outputs.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/"
  #   - "schema/generated/**/*.json"
  checks:
    - name: ruff-check
      command: "python3 -m ruff check ."
      timeout: 60
    - name: ruff-format
      command: "python3 -m ruff format --check ."
      fix_command: "python3 -m ruff format ."
      timeout: 60
"""
    elif build_tool == "node":
        package_manager = node_package_manager or "npm"
        sync_command = node_sync_command or (
            "bun install --frozen-lockfile" if package_manager == "bun" else f"{package_manager} install"
        )
        compile_command = node_compile_command or (
            "bun run build" if package_manager == "bun" else f"{package_manager} run build"
        )
        test_command = node_test_command or ("bun run test" if package_manager == "bun" else f"{package_manager} test")
        checks = node_checks or []
        if checks:
            check_lines: list[str] = ["  checks:"]
            for check in checks:
                check_lines.append(f"    - name: {check['name']}")
                check_lines.append(f'      command: "{check["command"]}"')
                if "fix_command" in check:
                    check_lines.append(f'      fix_command: "{check["fix_command"]}"')
                check_lines.append(f"      timeout: {check.get('timeout', 120)}")
            checks_block = "\n".join(check_lines)
        else:
            checks_block = "  checks: []"
        build_section = f"""\
build:
  package_manager: {package_manager}
  sync_command: "{sync_command}"
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked package-manager lockfiles.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/api/"
  #   - "schema/generated/**/*.json"
  compile_command: "{compile_command}"
  test_command: "{test_command}"
  sync_timeout: 600
  compile_timeout: 600
  test_timeout: 600
{checks_block}
"""
    elif build_tool == "gradle-android":
        build_section = """\
build:
  # Gradle task run by generate_sources() in the presync phase (run_presync: true).
  # Use openApiGenerateAll if generateDebugSources fails due to pre-existing compile errors.
  presync_task: generateDebugSources
  presync_clean: true
  # assembleDebug catches Kotlin errors + resource errors (R class, strings.xml, layouts).
  # Switch to compileDebugKotlin for faster builds on pure Kotlin tasks.
  # TODO: verify these tasks exist in your project (run: ./gradlew tasks).
  compile_task: assembleDebug
  test_task: testDebugUnitTest
  sync_timeout: 1800
  compile_timeout: 1800
  test_timeout: 1800
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked Gradle lock/verification metadata.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "app/src/main/generated/api/"
  #   - "schema/generated/**/*.json"
"""
    elif build_tool == "gradle-jvm":
        build_section = """\
build:
  # 'classes' compiles all sources and triggers annotation processors (Lombok, MapStruct, etc.).
  # Switch to compileKotlin or compileJava for faster builds if your project has no codegen.
  compile_task: classes
  test_task: test
  sync_task: classes
  presync_task: classes
  compile_timeout: 600
  test_timeout: 600
  sync_timeout: 600
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked Gradle lock/verification metadata.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/main/generated/api/"
  #   - "schema/generated/**/*.json"
"""
    elif build_tool == "maven":
        build_section = """\
build:
  # Uses ./mvnw if present, falls back to mvn on PATH.
  # Override compile_command / test_command to customize (e.g. add -DskipTests=true).
  compile_timeout: 600
  test_timeout: 600
  sync_timeout: 300
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Maven has no built-in lockfile default, so use this for intentional generated sync outputs.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/main/generated/api/"
  #   - "schema/generated/**/*.json"
"""
    elif build_tool == "xcodebuild":
        scheme_val = xcode_scheme if xcode_scheme else "TODO"
        scheme_comment = "" if xcode_scheme else "  # TODO: set scheme to match your Xcode project.\n"
        build_section = f"""\
build:
{scheme_comment}  scheme: "{scheme_val}"
  destination: "generic/platform=iOS Simulator"
  # TODO: set name to a simulator available on your machine (run: xcrun simctl list devices).
  test_destination: "platform=iOS Simulator,OS=latest,name=iPhone 16"
  compile_timeout: 1800
  test_timeout: 1800
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Platform defaults already cover existing tracked SwiftPM Package.resolved.
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "Sources/Generated/"
  #   - "schema/generated/**/*.json"
"""
    else:
        build_section = """\
build:
  # TODO: configure compile and test commands for your project.
  compile_command: "TODO"
  test_command: "TODO"
  timeout: 600
  # Optional: adopt project-specific source-controlled files generated or updated by sync().
  # Use this only for generated files that belong in the final diff; keep caches/build outputs gitignored.
  # sync_adopt_paths:
  #   - "src/generated/"
  #   - "schema/generated/**/*.json"
"""

    platform_line = f"  platform: {platform}\n" if platform else ""
    language_line = f"  language: {language}\n" if language else "  language: TODO\n"
    if build_tool == "gradle-android":
        ui_line = (
            "  # TODO: set ui to your UI framework — e.g. 'Jetpack Compose (Material 3)' "
            "or 'XML layouts'\n  # ui: Jetpack Compose (Material 3)\n"
        )
    elif build_tool == "xcodebuild":
        ui_line = "  # TODO: set ui to your UI framework — e.g. 'SwiftUI' or 'UIKit'\n  # ui: SwiftUI\n"
    else:
        ui_line = ""
    presync_line = (
        "run_presync: true" if build_tool in ("gradle-android", "gradle-jvm", "maven") else "run_presync: false"
    )

    return f"""\
# Sikula project configuration — generated by `sikula init`.
# Review any TODO comments before running your first task.

project:
  name: {project_name}
  root_path: .
  build_tool: {build_tool or "TODO"}
{language_line}{platform_line}{ui_line}
sandbox:
{write_paths_comment}  allowed_write_paths:
{wp_list}
{test_paths_comment}  allowed_test_write_paths:
{twp_list}
  allowed_read_paths:
    - .
  max_iterations: 10
  max_review_iterations: 3
  max_security_review_iterations: 3

tasks:
  task_description_dir: .sikula/tasks/
  contract_dir: .sikula/contracts/
  task_asset_dir: .sikula/task-assets/
  contract_report_dir: .sikula/contract-reports/
  refined_suffix: .refined.md
  contract_suffix: .contract.md
  state_dir: .sikula/state/

progress:
  heartbeat_interval_seconds: 60

llm:
  provider: {provider or "codex"}{provider_comment}
  model: {model or "gpt-5.3-codex"}{model_comment}
  agent_timeout: 1800

agents:
  analyst:
    llm:
      model: {model or "gpt-5.5"}{agent_model_comment}
  reviewer:
    llm:
      model: {model or "gpt-5.5"}{agent_model_comment}
  security_reviewer:
    llm:
      model: {model or "gpt-5.5"}{agent_model_comment}

run_planner: true
{presync_line}
run_build_per_step: false
run_review: true
run_security_review: true
run_build: true
run_test_writing: true
run_tests: true
run_checks: true

{build_section}
security:
  # Optional: describe what this application does, what data it handles, and who the users
  # are. The security reviewer uses this to focus on relevant threat categories.
  # Example: "Mobile app. Handles user auth tokens stored in EncryptedSharedPreferences.
  #   Network calls go to our own backend — responses are semi-trusted. No PII beyond email."
  context: ""

guidelines:
{guidelines_block}
  max_file_chars: 30000

planner:
  max_steps: 6
  # Optional project-specific prompt overlay for the planner.
  # If enabled for planner runs, commit the file before isolated worktrees use it.
  # extra_rules: .sikula/planner_rules.md

# reviewer:
#   # Optional project-specific prompt overlay for correctness, architecture, and invariants.
#   # If enabled for review runs, commit the file before isolated worktrees use it.
#   extra_rules: .sikula/reviewer_rules.md

# security_reviewer:
#   # Optional project-specific prompt overlay for threat model and data handling.
#   # If enabled for security review runs, commit the file before isolated worktrees use it.
#   extra_rules: .sikula/security_rules.md

test_writer:
  # Minimum branch+line coverage target within the configured test surface (percentage).
  coverage_target: 90
  # Test surface policy:
  # existing_infrastructure = stay within existing project test infra; missing heavy
  # UI/browser/device/runtime harnesses are not gaps by themselves.
  # complete = opt in to TESTABILITY GAP reports when important behaviour needs missing
  # test infra outside the existing surface.
  test_surface_policy: existing_infrastructure
  # What to do when safe tests require missing project seams/infrastructure:
  # warn = record a visible audit warning; fail = fail the task.
  testability_gap_policy: warn
  # Optional project-specific prompt overlay for testing conventions and required doubles.
  # If enabled for test-writing runs, commit the file before isolated worktrees use it.
  # extra_rules: .sikula/test_writer_rules.md
"""


def load_init_config(path: Path, *, strict: bool = False) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        if strict:
            print(f"Could not read config: {path}")
            print(f"  {e}")
            sys.exit(1)
        return {}
    except yaml.YAMLError as e:
        if strict:
            print(f"Invalid config YAML: {path}")
            print(f"  {e}")
            sys.exit(1)
        return {}
    return data if isinstance(data, dict) else {}


def init_llm_value(args: argparse.Namespace, existing_cfg: dict, key: str) -> str | None:
    value = getattr(args, key, None)
    if value:
        return value
    llm_cfg = existing_cfg.get("llm", {})
    return llm_cfg.get(key) if isinstance(llm_cfg, dict) else None


def init_tech_stack(existing_cfg: dict) -> str:
    project_cfg = existing_cfg.get("project", {})
    if not isinstance(project_cfg, dict):
        return "software"
    parts = [
        project_cfg.get("language"),
        project_cfg.get("platform"),
        project_cfg.get("build_tool"),
    ]
    return "/".join(str(p) for p in parts if p) or "software"


def config_references_guidelines(config_path: Path, guidelines_ref: str) -> bool:
    cfg = load_init_config(config_path)
    guidelines_cfg = cfg.get("guidelines", {})
    if not isinstance(guidelines_cfg, dict):
        return False
    context_files = guidelines_cfg.get("context_files", [])
    return isinstance(context_files, list) and guidelines_ref in context_files


def insert_guidelines_reference(config_path: Path, guidelines_ref: str) -> bool:
    """Add guidelines_ref to guidelines.context_files with a minimal text edit."""
    if config_references_guidelines(config_path, guidelines_ref):
        return False

    text = config_path.read_text()
    lines = text.splitlines(keepends=True)
    newline = "\n"

    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    guidelines_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.strip().split("#", 1)[0].strip() == "guidelines:":
            guidelines_idx = idx
            break

    if guidelines_idx is None:
        suffix = "" if text.endswith(("\n", "\r\n")) or not text else newline
        block = f"{suffix}guidelines:{newline}  context_files:{newline}    - {guidelines_ref}{newline}"
        config_path.write_text(text + block)
        return True

    guidelines_indent = _indent(lines[guidelines_idx])
    block_end = len(lines)
    for idx in range(guidelines_idx + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped and not stripped.startswith("#") and _indent(lines[idx]) <= guidelines_indent:
            block_end = idx
            break

    context_idx: int | None = None
    for idx in range(guidelines_idx + 1, block_end):
        if lines[idx].lstrip().startswith("context_files:"):
            context_idx = idx
            break

    if context_idx is None:
        item = f"{' ' * (guidelines_indent + 2)}context_files:{newline}"
        item += f"{' ' * (guidelines_indent + 4)}- {guidelines_ref}{newline}"
        lines.insert(guidelines_idx + 1, item)
        config_path.write_text("".join(lines))
        return True

    prefix, _, suffix = lines[context_idx].partition("context_files:")
    inline_value = suffix.split("#", 1)[0].strip()
    item_indent = _indent(lines[context_idx]) + 2
    if inline_value:
        cfg = load_init_config(config_path)
        existing = cfg.get("guidelines", {}).get("context_files", []) if isinstance(cfg.get("guidelines"), dict) else []
        if not isinstance(existing, list):
            existing = []
        refs = [guidelines_ref] + [str(ref) for ref in existing if ref != guidelines_ref]
        replacement = f"{prefix}context_files:{newline}"
        replacement += "".join(f"{' ' * item_indent}- {ref}{newline}" for ref in refs)
        lines[context_idx] = replacement
        config_path.write_text("".join(lines))
        return True

    lines.insert(context_idx + 1, f"{' ' * item_indent}- {guidelines_ref}{newline}")
    config_path.write_text("".join(lines))
    return True


def generate_guidelines_for_init(project_root: Path, tech: str, provider: str, model: str) -> str:
    from agents.init_agent import InitAgent
    from core.llm_client import LLMConfig, create_llm_client

    llm_cfg = LLMConfig(provider=provider, model=model)
    llm = create_llm_client(llm_cfg)
    agent = InitAgent(llm, tech)
    return agent.generate_guidelines(project_root)


def ensure_sikula_gitignore(sikula_dir: Path) -> None:
    gitignore = sikula_dir / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    lines = existing.splitlines()
    missing = [entry for entry in _SIKULA_GITIGNORE_ENTRIES if entry not in {line.strip() for line in lines}]
    if not missing:
        return
    content = existing
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(f"{entry}\n" for entry in missing)
    gitignore.write_text(content)


def ensure_provider_gitignore_entry(project_root: Path, provider: str | None) -> None:
    entries = {
        "claude": ".claude/",
        "gemini": ".gemini/",
    }
    entry = entries.get((provider or "").lower())
    if entry:
        core_worktree.ensure_project_gitignore_entry(project_root, entry)


@dataclass(frozen=True)
class InitContext:
    load_project_env: Callable[[Path], None] = _load_project_env
    load_init_config: Callable[..., dict] = load_init_config
    init_llm_value: Callable[[argparse.Namespace, dict, str], str | None] = init_llm_value
    init_tech_stack: Callable[[dict], str] = init_tech_stack
    generate_guidelines_for_init: Callable[[Path, str, str, str], str] = generate_guidelines_for_init
    insert_guidelines_reference: Callable[[Path, str], bool] = insert_guidelines_reference
    find_git_root: Callable[[Path], Path | None] = core_worktree.find_git_root
    ensure_project_gitignore_entry: Callable[[Path, str], None] = core_worktree.ensure_project_gitignore_entry
    ensure_provider_gitignore_entry: Callable[[Path, str | None], None] = ensure_provider_gitignore_entry
    ensure_sikula_gitignore: Callable[[Path], None] = ensure_sikula_gitignore
    generate_config: Callable[..., str] = generate_config


def _init_context(context: InitContext | None = None) -> InitContext:
    return context or InitContext()


def cmd_init_guidelines_only(
    args: argparse.Namespace,
    project_root: Path,
    config_path: Path,
    context: InitContext | None = None,
) -> None:
    context = _init_context(context)
    existing_cfg = context.load_init_config(config_path, strict=True)
    provider = context.init_llm_value(args, existing_cfg, "provider")
    model = context.init_llm_value(args, existing_cfg, "model")
    if not provider or not model:
        print("--provider and --model are required when using --guidelines unless llm.provider/model exist in config")
        print("  e.g. sikula init --guidelines --provider codex --model gpt-5.5")
        sys.exit(1)

    sikula_dir = project_root / ".sikula"
    sikula_dir.mkdir(exist_ok=True)
    print(f"Config already exists: {config_path}")
    print("Generating guidelines without rewriting the existing config ...")
    context.ensure_provider_gitignore_entry(project_root, provider)
    try:
        guidelines_content = context.generate_guidelines_for_init(
            project_root, context.init_tech_stack(existing_cfg), provider, model
        )
    except RuntimeError as e:
        print(f"Warning: guidelines generation failed: {e}")
        return

    gl_path = sikula_dir / "guidelines.md"
    gl_path.write_text(guidelines_content)
    guidelines_ref = ".sikula/guidelines.md"
    updated = context.insert_guidelines_reference(config_path, guidelines_ref)
    print("  Generated  : .sikula/guidelines.md")
    if updated:
        print("  Updated    : .sikula/config.yaml (guidelines.context_files)")
    else:
        print("  Config     : already references .sikula/guidelines.md")


def cmd_init(args: argparse.Namespace, context: InitContext | None = None) -> None:
    from tools.scanner import scan

    context = _init_context(context)
    project_root = Path.cwd()
    context.load_project_env(project_root)
    sikula_dir = project_root / ".sikula"
    config_path = sikula_dir / "config.yaml"
    existing_cfg = context.load_init_config(config_path) if config_path.exists() else {}

    if config_path.exists() and args.guidelines and not args.force:
        cmd_init_guidelines_only(args, project_root, config_path, context)
        return

    if config_path.exists() and not args.force:
        print(f"Config already exists: {config_path}")
        print("Use --force to overwrite.")
        sys.exit(1)

    provider = context.init_llm_value(args, existing_cfg, "provider")
    model = context.init_llm_value(args, existing_cfg, "model")
    if args.guidelines and (not provider or not model):
        print("--provider and --model are both required when using --guidelines")
        print("  e.g. sikula init --guidelines --provider codex --model gpt-5.5")
        sys.exit(1)

    print(f"Scanning {project_root} ...")
    result = scan(project_root)

    if result.ambiguous_tools:
        print(f"Multiple build tools detected: {', '.join(result.ambiguous_tools)}")
        print(f"Defaulting to: {result.build_tool} — edit .sikula/config.yaml to change.")

    if result.build_tool:
        tech = f"{result.language}/{result.build_tool}" if result.language else result.build_tool
        print(f"  build_tool : {result.build_tool}")
        print(f"  language   : {result.language}")
        if result.platform:
            print(f"  platform   : {result.platform}")
        if result.build_tool == "node" and result.package_manager:
            print(f"  package manager: {result.package_manager}")
    else:
        tech = "software"
        print("  No build tool detected — config will need manual setup.")

    if result.guidelines_files:
        print(f"  guidelines : {', '.join(result.guidelines_files)}")

    guidelines_content: str | None = None
    if args.guidelines:
        print("Generating guidelines (this may take a moment) ...")
        try:
            guidelines_content = context.generate_guidelines_for_init(project_root, tech, provider, model)
        except RuntimeError as e:
            print(f"Warning: guidelines generation failed: {e}")
            print("Continuing without generated guidelines.")

    if context.find_git_root(project_root) is None:
        print("Warning: not inside a git repository — git is required to run tasks.")
        print("  Run 'git init && git add -A && git commit -m init' before running tasks.")
    context.ensure_project_gitignore_entry(project_root, ".env")
    if args.guidelines:
        context.ensure_provider_gitignore_entry(project_root, provider)

    sikula_dir.mkdir(exist_ok=True)
    (sikula_dir / "tasks").mkdir(exist_ok=True)
    (sikula_dir / "contracts").mkdir(exist_ok=True)
    context.ensure_sikula_gitignore(sikula_dir)

    guidelines_files = list(result.guidelines_files)
    # If a previously generated guidelines file exists, keep it in the config even when
    # --guidelines is not passed (e.g. sikula init --force without --guidelines).
    existing_gl = ".sikula/guidelines.md"
    if (sikula_dir / "guidelines.md").exists() and existing_gl not in guidelines_files:
        guidelines_files = [existing_gl] + guidelines_files
    if guidelines_content:
        gl_path = sikula_dir / "guidelines.md"
        gl_path.write_text(guidelines_content)
        guidelines_files = [existing_gl] + [f for f in guidelines_files if f != existing_gl and f != "guidelines.md"]
        print("  Generated  : .sikula/guidelines.md")

    if result.xcode_scheme:
        print(f"  scheme     : {result.xcode_scheme}")
    if result.write_paths:
        print(f"  write_paths: {', '.join(result.write_paths)}")
    if result.test_write_paths:
        print(f"  test_paths : {', '.join(result.test_write_paths)}")

    config = context.generate_config(
        build_tool=result.build_tool,
        language=result.language,
        platform=result.platform,
        guidelines_files=guidelines_files,
        project_name=project_root.name,
        provider=provider,
        model=model,
        write_paths=result.write_paths or None,
        test_write_paths=result.test_write_paths or None,
        xcode_scheme=result.xcode_scheme,
        node_package_manager=result.package_manager,
        node_sync_command=result.node_sync_command,
        node_compile_command=result.node_compile_command,
        node_test_command=result.node_test_command,
        node_checks=result.node_checks,
    )
    config_path.write_text(config)

    todos: list[str] = []
    if not result.build_tool:
        todos.append(
            "project.build_tool — set to: cargo / gradle-android / gradle-jvm / maven / node / xcodebuild / python"
        )
    if not result.language:
        todos.append("project.language — set to your project's primary language")
    if result.build_tool in ("gradle-android", "xcodebuild"):
        ui_examples = (
            "Jetpack Compose (Material 3)' or 'XML layouts"
            if result.build_tool == "gradle-android"
            else "SwiftUI' or 'UIKit"
        )
        todos.append(f"project.ui — set to your UI framework (e.g. '{ui_examples}')")
    if result.build_tool == "gradle-android":
        todos.append(
            "build.compile_task / build.test_task — verify the Gradle tasks match your project (run: ./gradlew tasks)"
        )
    if result.build_tool == "gradle-jvm":
        todos.append(
            "build.compile_task / build.test_task — verify the Gradle tasks (default: classes / test); "
            "run ./gradlew tasks to list available tasks"
        )
    if result.build_tool == "node":
        todos.append(
            "build.sync_command / compile_command / test_command — verify the package-manager commands match "
            "your project scripts"
        )
    if result.build_tool == "xcodebuild" and not result.xcode_scheme:
        todos.append("build.scheme — set to your Xcode scheme name (run: xcodebuild -list)")
    if result.build_tool == "xcodebuild":
        todos.append("build.test_destination — set name to an available simulator (run: xcrun simctl list devices)")
    if not result.write_paths:
        todos.append("sandbox.allowed_write_paths — set to dirs where agents may write production code")
    if not result.test_write_paths:
        todos.append("sandbox.allowed_test_write_paths — set to dirs where the test writer may write")
    if not provider:
        todos.append("llm.provider / llm.model — set to your LLM provider and model")
    if not guidelines_content:
        meaningful_docs = [f for f in guidelines_files if f != "README.md"]
        if not meaningful_docs:
            todos.append(
                "guidelines.context_files — no coding-convention docs found; add architecture/guidelines files "
                "or auto-generate with: sikula init --guidelines --provider <provider> --model <model>"
            )
        else:
            todos.append(
                "guidelines.context_files — verify these files describe architecture and coding conventions "
                "(agents rely on them critically; or auto-generate with: sikula init --guidelines)"
            )

    print(f"\nCreated: {config_path}")
    if todos:
        print("\nTODOs to fill in before first run:")
        for item in todos:
            print(f"  • {item}")
    else:
        print("Config is ready — run: sikula run <task.md>")
    print("\nBefore the first isolated run, commit the Sikula config:")
    print("  git add .sikula/config.yaml .sikula/.gitignore")
    if guidelines_content:
        print("  git add .sikula/guidelines.md")
    print("  git commit -m 'Add Sikula config'")
