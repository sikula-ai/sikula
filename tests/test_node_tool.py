"""Tests for tools/node_tool.py - NodeTool."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base_tool import BuildTool, Sandbox
from tools.node_tool import (
    NodeTool,
    _BUILD_CONFIG_FILES,
    _PACKAGE_MANAGER_LOCKFILES,
    default_node_checks,
    default_node_compile_command,
    default_node_sync_command,
    default_node_test_command,
    detect_node_language,
    detect_node_package_manager,
    node_script_command,
    node_test_command,
    node_tsc_command,
    read_node_package_scripts,
)

_SHELL_RUNNER = "tools.node_tool.run_windows_shell_process" if os.name == "nt" else "tools.node_tool.subprocess.run"


def _make_tool(root: Path, **kwargs) -> NodeTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return NodeTool(sandbox=sandbox, project_root=root, **kwargs)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def _write_package_json(root: Path, body: str) -> None:
    (root / "package.json").write_text(body)


class TestNodeToolInheritance:
    def test_is_build_tool(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert isinstance(tool, BuildTool)

    def test_env_files_is_empty(self):
        assert NodeTool.env_files() == []


class TestNodePackageMetadata:
    def test_detects_pnpm_from_lockfile(self, tmp_path: Path):
        (tmp_path / "pnpm-lock.yaml").write_text("")
        assert detect_node_package_manager(tmp_path) == "pnpm"

    def test_detects_yarn_from_package_manager_field(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"packageManager": "yarn@4.0.0"}')
        assert detect_node_package_manager(tmp_path) == "yarn"

    def test_configured_package_manager_takes_precedence(self, tmp_path: Path):
        (tmp_path / "package-lock.json").write_text("")
        assert detect_node_package_manager(tmp_path, configured="pnpm") == "pnpm"

    def test_package_manager_field_takes_precedence_over_lockfile(self, tmp_path: Path):
        (tmp_path / "package-lock.json").write_text("")
        _write_package_json(tmp_path, '{"packageManager": "pnpm@9.0.0"}')
        assert detect_node_package_manager(tmp_path) == "pnpm"

    def test_reads_scripts_as_strings(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"scripts": {"typecheck": "tsc --noEmit", "lint": "eslint ."}}')
        assert read_node_package_scripts(tmp_path) == {"typecheck": "tsc --noEmit", "lint": "eslint ."}

    def test_invalid_package_json_has_no_scripts(self, tmp_path: Path):
        _write_package_json(tmp_path, "{")
        assert read_node_package_scripts(tmp_path) == {}

    def test_non_object_scripts_are_ignored(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"scripts": "npm test"}')
        assert read_node_package_scripts(tmp_path) == {}

    def test_detects_typescript_from_dependency(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"devDependencies": {"typescript": "^5.0.0"}}')
        assert detect_node_language(tmp_path) == "TypeScript"

    def test_detects_typescript_from_ts_file(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.ts").write_text("")
        assert detect_node_language(tmp_path) == "TypeScript"

    def test_defaults_to_javascript(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"scripts": {"build": "vite build"}}')
        assert detect_node_language(tmp_path) == "JavaScript"


class TestNodeDefaultCommands:
    def test_compile_prefers_typecheck_script(self, tmp_path: Path):
        scripts = {"typecheck": "tsc --noEmit", "build": "vite build"}
        assert default_node_compile_command(tmp_path, "npm", scripts) == "npm run typecheck"

    def test_compile_uses_pnpm_script_shorthand(self, tmp_path: Path):
        scripts = {"build": "vite build"}
        assert default_node_compile_command(tmp_path, "pnpm", scripts) == "pnpm build"

    def test_compile_uses_tsc_when_tsconfig_exists_without_scripts(self, tmp_path: Path):
        (tmp_path / "tsconfig.json").write_text("{}")
        assert default_node_compile_command(tmp_path, "npm", {}) == "npx tsc --noEmit"

    def test_command_helpers_cover_package_manager_variants(self):
        assert node_script_command("bun", "build") == "bun run build"
        assert node_script_command("custompm", "build") == "custompm run build"
        assert node_test_command("bun") == "bun run test"
        assert node_test_command("custompm") == "custompm test"
        assert node_tsc_command("pnpm") == "pnpm exec tsc --noEmit"
        assert node_tsc_command("yarn") == "yarn tsc --noEmit"
        assert node_tsc_command("bun") == "bunx tsc --noEmit"
        assert node_tsc_command("custompm") == "npx tsc --noEmit"
        assert default_node_test_command("bun", {"test": "bun test"}) == "bun run test"

    @pytest.mark.parametrize("lockfile,manager", _PACKAGE_MANAGER_LOCKFILES)
    def test_sync_can_regenerate_existing_lockfiles(self, tmp_path: Path, lockfile, manager):
        expected = "pnpm install --no-frozen-lockfile" if manager == "pnpm" else f"{manager} install"
        assert default_node_sync_command(tmp_path, manager) == expected
        (tmp_path / lockfile).write_text("")
        assert default_node_sync_command(tmp_path, manager) == expected

    def test_unknown_sync_command_falls_back_to_install(self, tmp_path: Path):
        assert default_node_sync_command(tmp_path, "custompm") == "custompm install"

    def test_checks_include_lint_and_format_autofix(self):
        checks = default_node_checks(
            "npm", {"lint": "eslint .", "format:check": "prettier --check .", "format": "prettier --write ."}
        )
        assert checks == [
            {"name": "lint", "command": "npm run lint", "timeout": 120},
            {
                "name": "format",
                "command": "npm run format:check",
                "fix_command": "npm run format",
                "timeout": 120,
            },
        ]


class TestNodeToolCommands:
    def test_sync_uses_npm_install_when_lockfile_exists(self, tmp_path: Path):
        (tmp_path / "package-lock.json").write_text("")
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert args[0] == "npm install"

    def test_sync_uses_pnpm_install_when_lockfile_exists(self, tmp_path: Path):
        (tmp_path / "pnpm-lock.yaml").write_text("")
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert args[0] == "pnpm install --no-frozen-lockfile"

    def test_sync_uses_non_frozen_install_when_package_manager_field_has_no_lockfile(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"packageManager": "pnpm@9.0.0"}')
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert args[0] == "pnpm install --no-frozen-lockfile"

    def test_sync_uses_non_frozen_yarn_install_without_lockfile(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"packageManager": "yarn@4.0.0"}')
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.sync()
        args, _ = mock.call_args
        assert args[0] == "yarn install"

    @pytest.mark.parametrize("custom", [False, True])
    def test_yarn_ci_environment_is_changed_only_for_default_sync(self, tmp_path, monkeypatch, custom):
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("YARN_ENABLE_IMMUTABLE_INSTALLS", "true")
        tool = _make_tool(tmp_path, package_manager="yarn", sync_command="yarn install --immutable" if custom else None)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as run:
            assert tool.sync().success
            sync_call = run.call_args
            tool.compile_check()
        if custom:
            assert sync_call.args[0] == "yarn install --immutable"
            assert "env" not in sync_call.kwargs
        else:
            assert sync_call.args[0] == "yarn install"
            assert sync_call.kwargs["env"]["YARN_ENABLE_IMMUTABLE_INSTALLS"] == "false"
            assert sync_call.kwargs["env"]["CI"] == "true"
        assert "env" not in run.call_args.kwargs
        assert os.environ["YARN_ENABLE_IMMUTABLE_INSTALLS"] == "true"

    @pytest.mark.parametrize(
        "manager,command",
        [("npm", "npm ci"), ("pnpm", "pnpm install --frozen-lockfile"), ("bun", "bun install --frozen-lockfile")],
    )
    def test_explicit_frozen_commands_are_not_rewritten_or_retried(self, tmp_path, manager, command):
        tool = _make_tool(tmp_path, package_manager=manager, sync_command=command)
        with patch(_SHELL_RUNNER, return_value=_mock_run(returncode=1, stderr="lockfile is stale")) as run:
            assert not tool.sync().success
        assert run.call_count == 1
        assert run.call_args.args[0] == command
        assert "env" not in run.call_args.kwargs

    def test_configured_commands_override_defaults(self, tmp_path: Path):
        tool = _make_tool(
            tmp_path,
            package_manager="pnpm",
            sync_command="pnpm install --offline",
            compile_command="pnpm typecheck",
            test_command="pnpm test -- --runInBand",
        )
        assert tool._sync_command == "pnpm install --offline"
        assert tool._compile_command == "pnpm typecheck"
        assert tool._test_command == "pnpm test -- --runInBand"

    def test_compile_check_runs_configured_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="npm run typecheck")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert args[0] == "npm run typecheck"

    def test_run_tests_runs_default_test_script(self, tmp_path: Path):
        _write_package_json(tmp_path, '{"scripts": {"test": "vitest run"}}')
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert args[0] == "npm test"

    def test_run_check_uses_command_from_task_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("lint", {"command": "npm run lint"})
        args, _ = mock.call_args
        assert args[0] == "npm run lint"

    def test_run_check_uses_custom_timeout(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("lint", {"command": "npm run lint", "timeout": "30"})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 30

    def test_runs_in_project_root(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="npm run build")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["cwd"] == tmp_path.resolve()

    def test_replaces_undecodable_output_with_locale_encoding(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="npm run build")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        assert mock.call_args.kwargs["errors"] == "replace"
        assert "encoding" not in mock.call_args.kwargs


class TestNodeToolResults:
    def test_success_returns_combined_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="npm run build")
        with patch(_SHELL_RUNNER, return_value=_mock_run(stdout="ok\n", stderr="warn\n")):
            result = tool.compile_check()
        assert result.success
        assert "ok" in result.output
        assert "warn" in result.output

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="npm run build")
        with patch(_SHELL_RUNNER, return_value=_mock_run(returncode=1, stderr="TS2322 type error")):
            result = tool.compile_check()
        assert not result.success
        assert "TS2322" in result.error

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="npm run build")
        with patch(
            _SHELL_RUNNER,
            side_effect=__import__("subprocess").TimeoutExpired("cmd", 1),
        ):
            result = tool.compile_check()
        assert not result.success
        assert "timed out" in result.error

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="npm run build")
        with patch(_SHELL_RUNNER, side_effect=OSError("npm not found")):
            result = tool.compile_check()
        assert not result.success
        assert "npm not found" in result.error


class TestNodeToolIsBuildConfigFile:
    @pytest.mark.parametrize("filename", sorted(_BUILD_CONFIG_FILES))
    def test_recognizes_build_config_files(self, filename: str):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file(filename) is True

    def test_recognizes_nested_package_json(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("packages/app/package.json") is True

    def test_recognizes_framework_config_files(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("apps/web/vite.config.ts") is True
        assert tool.is_build_config_file("next.config.mjs") is True
        assert tool.is_build_config_file("remix.config.js") is True
        assert tool.is_build_config_file("vue.config.js") is True

    def test_recognizes_workspace_config_files(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("pnpm-workspace.yaml") is True
        assert tool.is_build_config_file("lerna.json") is True
        assert tool.is_build_config_file("rush.json") is True

    def test_recognizes_yarn_and_patch_dirs(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file(".yarn/releases/yarn-4.0.0.cjs") is True
        assert tool.is_build_config_file("patches/example.patch") is True

    def test_recognizes_package_level_patch_dirs(self, tmp_path: Path):
        package = tmp_path / "packages" / "web"
        package.mkdir(parents=True)
        (package / "package.json").write_text("{}")
        tool = _make_tool(tmp_path)
        assert tool.is_build_config_file("packages/web/patches/example.patch") is True

    def test_rejects_source_file(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("src/App.tsx") is False

    def test_rejects_partial_match(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("not-package.json") is False
        assert tool.is_build_config_file("src/dispatches/applyPatch.ts") is False
        assert tool.is_build_config_file("src/patches/applyPatch.ts") is False
        assert tool.is_build_config_file("src/yarn.locked.ts") is False


class TestNodeToolEphemeralBuildPaths:
    @pytest.mark.parametrize(
        "path",
        [
            ".yarn/install-state.gz",
            ".yarn/cache/package-npm-1.0.0.zip",
            ".yarn/unplugged/package/node_modules/package/index.js",
            "packages/web/node_modules/package/index.js",
        ],
    )
    def test_recognizes_disposable_dependency_state(self, tmp_path: Path, path: str):
        tool = _make_tool(tmp_path)

        assert tool.is_ephemeral_build_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            ".pnp.cjs",
            ".yarn/patches/package.patch",
            ".yarn/plugins/plugin.cjs",
            ".yarn/releases/yarn.cjs",
            ".yarn/sdks/typescript/lib/tsserver.js",
            ".yarn/versions/version.yml",
            "yarn.lock",
        ],
    )
    def test_keeps_zero_install_and_configuration_paths_auditable(self, tmp_path: Path, path: str):
        tool = _make_tool(tmp_path)

        assert tool.is_ephemeral_build_path(path) is False


class TestNodeToolIsSyncAdoptableFile:
    @pytest.mark.parametrize("lockfile,package_manager", _PACKAGE_MANAGER_LOCKFILES)
    def test_recognizes_package_manager_lockfiles(self, lockfile: str, package_manager: str, tmp_path: Path):
        tool = _make_tool(tmp_path, package_manager=package_manager)
        assert tool.is_sync_adoptable_file(lockfile) is True
        assert tool.is_sync_adoptable_file(f"packages/app/{lockfile}") is True

    def test_rejects_package_manifest(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        assert tool.is_sync_adoptable_file("package.json") is False


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is needed for the offline lockfile regression")
def test_default_npm_sync_regenerates_stale_lockfile_offline(tmp_path, monkeypatch):
    # Isolate npm state and prevent registry access and lifecycle scripts.
    empty_config = tmp_path / "empty.npmrc"
    empty_config.write_text("")
    for key, value in {
        "cache": str(tmp_path / "npm-cache"),
        "userconfig": str(empty_config),
        "globalconfig": str(tmp_path / "global.npmrc"),
        "offline": "true",
        "ignore_scripts": "true",
        "audit": "false",
        "fund": "false",
        "package_lock": "true",
    }.items():
        monkeypatch.setenv(f"npm_config_{key}", value)
    root = tmp_path / "project"
    root.mkdir()
    manifest = root / "package.json"
    manifest.write_text(json.dumps({"name": "sync-regression", "version": "1.0.0", "private": True}))
    tool = _make_tool(root)
    initial = tool.sync()
    assert initial.success, initial.error
    lock = root / "package-lock.json"
    original_lock = lock.read_bytes()
    dependency = root / "local-dependency"
    dependency.mkdir()
    (dependency / "package.json").write_text(json.dumps({"name": "local-dependency", "version": "1.0.0"}))
    body = json.loads(manifest.read_text())
    body["dependencies"] = {"local-dependency": "file:./local-dependency"}
    manifest.write_text(json.dumps(body))
    expected_manifest = manifest.read_bytes()
    frozen = _make_tool(root, sync_command="npm ci").sync()
    assert not frozen.success
    assert lock.read_bytes() == original_lock
    repaired = tool.sync()
    assert repaired.success, repaired.error
    assert manifest.read_bytes() == expected_manifest
    assert json.loads(lock.read_text())["packages"][""]["dependencies"] == body["dependencies"]
    assert _make_tool(root, sync_command="npm ci").sync().success
