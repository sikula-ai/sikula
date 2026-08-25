"""Tests for tools/cargo_tool.py — CargoTool."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.base_tool import Sandbox
from tools.cargo_tool import CargoTool, _BUILD_CONFIG_FILES

_SHELL_RUNNER = "tools.cargo_tool.run_windows_shell_process" if os.name == "nt" else "tools.cargo_tool.subprocess.run"


def _make_tool(
    root: Path,
    sync_command: str | None = None,
    compile_command: str = "cargo check",
    test_command: str = "cargo test",
) -> CargoTool:
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    return CargoTool(
        sandbox=sandbox,
        project_root=root,
        sync_command=sync_command,
        compile_command=compile_command,
        test_command=test_command,
    )


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestCargoToolRun:
    def test_success_returns_combined_output(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run(stdout="ok\n", stderr="warn\n")):
            result = tool.compile_check()
        assert result.success
        assert "ok" in result.output
        assert "warn" in result.output

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run(returncode=1, stderr="error: type mismatch")):
            result = tool.compile_check()
        assert not result.success
        assert "error: type mismatch" in result.error

    def test_stdout_only_failure_captured_in_error(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(
            _SHELL_RUNNER,
            return_value=_mock_run(returncode=101, stdout="test test_foo ... FAILED", stderr=""),
        ):
            result = tool.run_tests()
        assert not result.success
        assert "test test_foo ... FAILED" in result.error

    def test_long_test_output_keeps_failure_block_not_only_tail(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        output = (
            "Compiling workspace\n"
            + "".join(f"build line {i}\n" for i in range(300))
            + "thread 'test_rejects_wrong_result_type' panicked at assertion failed\n"
            + "failures:\n"
            + "    test_rejects_wrong_result_type\n"
            + "".join(f"Running unrelated test binary {i}\n" for i in range(500))
            + "error: test failed, to rerun pass `-p example_crate --test validation_tests`\n"
        )
        with patch(
            _SHELL_RUNNER,
            return_value=_mock_run(returncode=101, stdout=output, stderr=""),
        ):
            result = tool.run_tests()
        assert not result.success
        assert "test_rejects_wrong_result_type" in result.error
        assert "error: test failed" in result.error

    def test_noisy_workspace_test_output_keeps_cargo_failure_block(self, tmp_path: Path):
        tool = _make_tool(tmp_path, test_command="cargo test --workspace --all-features")
        output = (
            "Compiling workspace\n"
            + "".join(
                "running 0 tests\n\n"
                "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n\n"
                f"     Running unrelated_test_binary_{i}\n"
                for i in range(180)
            )
            + "running 47 tests\n"
            + "test parses_config_with_default_values ... FAILED\n"
            + "test rejects_config_with_missing_required_field ... FAILED\n"
            + "failures:\n\n"
            + "---- parses_config_with_default_values stdout ----\n\n"
            + "thread 'parses_config_with_default_values' panicked at "
            + "crates/config_parser/tests/config_validation.rs:849:10:\n"
            + 'configuration parsing should succeed: MissingField("timeout_ms")\n\n'
            + "---- rejects_config_with_missing_required_field stdout ----\n\n"
            + "thread 'rejects_config_with_missing_required_field' panicked at "
            + "crates/config_parser/tests/config_validation.rs:813:10:\n"
            + 'configuration validation should report the expected field: MissingField("timeout_ms")\n\n'
            + "failures:\n"
            + "    parses_config_with_default_values\n"
            + "    rejects_config_with_missing_required_field\n\n"
            + "test result: FAILED. 45 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out\n\n"
            + "error: test failed, to rerun pass `-p config_parser --test config_validation`\n"
            + "".join(f"     Running post_failure_noise_{i}\n" for i in range(180))
        )
        with patch(
            _SHELL_RUNNER,
            return_value=_mock_run(returncode=101, stdout=output, stderr=""),
        ):
            result = tool.run_tests()

        assert not result.success
        assert "parses_config_with_default_values" in result.error
        assert "rejects_config_with_missing_required_field" in result.error
        assert 'MissingField("timeout_ms")' in result.error
        assert "error: test failed, to rerun pass `-p config_parser --test config_validation`" in result.error

    def test_timeout_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, side_effect=__import__("subprocess").TimeoutExpired("cmd", 1)):
            result = tool.compile_check()
        assert not result.success
        assert "timed out" in result.error

    def test_unexpected_exception_returns_failure(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, side_effect=OSError("cargo not found")):
            result = tool.compile_check()
        assert not result.success
        assert "cargo not found" in result.error

    def test_runs_in_project_root(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["cwd"] == tmp_path.resolve()

    def test_replaces_undecodable_output_with_locale_encoding(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        assert mock.call_args.kwargs["errors"] == "replace"
        assert "encoding" not in mock.call_args.kwargs

    def test_default_timeout_used(self, tmp_path: Path):
        tool = CargoTool(
            sandbox=Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."]),
            project_root=tmp_path,
            timeout=999,
        )
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 999


class TestCargoToolCompileCheck:
    def test_uses_configured_compile_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, compile_command="cargo check --workspace")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.compile_check()
        args, _ = mock.call_args
        assert args[0] == "cargo check --workspace"


class TestCargoToolRunTests:
    def test_uses_configured_test_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, test_command="cargo test --workspace")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_tests()
        args, _ = mock.call_args
        assert args[0] == "cargo test --workspace"


class TestCargoToolSync:
    def test_runs_locked_sync_when_lockfile_exists(self, tmp_path: Path):
        (tmp_path / "Cargo.lock").write_text("# lock\n")
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            result = tool.sync()
        assert result.success
        args, _ = mock.call_args
        assert args[0] == "cargo fetch --locked"

    def test_runs_locked_sync_when_workspace_lockfile_exists(self, tmp_path: Path):
        workspace = tmp_path
        member = workspace / "crates" / "app"
        member.mkdir(parents=True)
        (workspace / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/app"]\n')
        (workspace / "Cargo.lock").write_text("# workspace lock\n")
        (member / "Cargo.toml").write_text('[package]\nname = "app"\n')
        tool = _make_tool(member)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            result = tool.sync()
        assert result.success
        args, _ = mock.call_args
        assert args[0] == "cargo fetch --locked"

    def test_locked_default_retries_unlocked_when_lockfile_needs_update(self, tmp_path: Path):
        (tmp_path / "Cargo.lock").write_text("# lock\n")
        tool = _make_tool(tmp_path)
        locked_failure = _mock_run(
            returncode=101,
            stderr="error: the lock file /tmp/Cargo.lock needs to be updated but --locked was passed",
        )
        with patch(_SHELL_RUNNER, side_effect=[locked_failure, _mock_run()]) as mock:
            result = tool.sync()
        assert result.success
        assert [call.args[0] for call in mock.call_args_list] == ["cargo fetch --locked", "cargo fetch"]
        assert result.metadata["sync_retry"]["reason"] == "cargo_lockfile_needs_update"
        assert result.metadata["sync_retry"]["initial_command"] == "cargo fetch --locked"
        assert result.metadata["sync_retry"]["initial_status"] == "failed"
        assert "needs to be updated" in result.metadata["sync_retry"]["initial_error_excerpt"]
        assert result.metadata["sync_retry"]["retry_command"] == "cargo fetch"
        assert result.metadata["sync_retry"]["retry_status"] == "success"

    def test_locked_default_preserves_retry_metadata_when_unlocked_retry_fails(self, tmp_path: Path):
        (tmp_path / "Cargo.lock").write_text("# lock\n")
        tool = _make_tool(tmp_path)
        locked_failure = _mock_run(
            returncode=101,
            stderr="error: the lock file /tmp/Cargo.lock needs to be updated but --locked was passed",
        )
        retry_failure = _mock_run(returncode=101, stderr="error: failed to download package")

        with patch(_SHELL_RUNNER, side_effect=[locked_failure, retry_failure]):
            result = tool.sync()

        assert not result.success
        assert result.metadata["sync_retry"]["reason"] == "cargo_lockfile_needs_update"
        assert result.metadata["sync_retry"]["retry_status"] == "failed"

    def test_locked_default_does_not_retry_unrelated_failures(self, tmp_path: Path):
        (tmp_path / "Cargo.lock").write_text("# lock\n")
        tool = _make_tool(tmp_path)
        with patch(
            _SHELL_RUNNER,
            return_value=_mock_run(returncode=101, stderr="error: failed to download package"),
        ) as mock:
            result = tool.sync()
        assert not result.success
        assert mock.call_count == 1
        args, _ = mock.call_args
        assert args[0] == "cargo fetch --locked"

    def test_runs_unlocked_sync_when_lockfile_is_missing(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            result = tool.sync()
        assert result.success
        args, _ = mock.call_args
        assert args[0] == "cargo fetch"

    def test_runs_unlocked_sync_when_workspace_lockfile_is_missing(self, tmp_path: Path):
        workspace = tmp_path
        member = workspace / "crates" / "app"
        member.mkdir(parents=True)
        (workspace / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/app"]\n')
        (member / "Cargo.toml").write_text('[package]\nname = "app"\n')
        tool = _make_tool(member)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            result = tool.sync()
        assert result.success
        args, _ = mock.call_args
        assert args[0] == "cargo fetch"

    def test_uses_configured_sync_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path, sync_command="cargo generate-lockfile --offline")
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            result = tool.sync()
        assert result.success
        args, _ = mock.call_args
        assert args[0] == "cargo generate-lockfile --offline"

    def test_configured_locked_sync_command_does_not_retry(self, tmp_path: Path):
        tool = _make_tool(tmp_path, sync_command="cargo fetch --locked")
        with patch(
            _SHELL_RUNNER,
            return_value=_mock_run(
                returncode=101,
                stderr="error: the lock file /tmp/Cargo.lock needs to be updated but --locked was passed",
            ),
        ) as mock:
            result = tool.sync()
        assert not result.success
        assert mock.call_count == 1
        args, _ = mock.call_args
        assert args[0] == "cargo fetch --locked"


class TestCargoToolRunCheck:
    def test_uses_command_from_task_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("clippy", {"command": "cargo clippy -- -D warnings"})
        args, _ = mock.call_args
        assert args[0] == "cargo clippy -- -D warnings"

    def test_falls_back_to_name_when_no_command(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("cargo clippy", {})
        args, _ = mock.call_args
        assert args[0] == "cargo clippy"

    def test_custom_timeout_from_task_config(self, tmp_path: Path):
        tool = _make_tool(tmp_path)
        with patch(_SHELL_RUNNER, return_value=_mock_run()) as mock:
            tool.run_check("fmt", {"command": "cargo fmt --check", "timeout": "30"})
        _, kwargs = mock.call_args
        assert kwargs["timeout"] == 30


class TestCargoToolIsBuildConfigFile:
    @pytest.mark.parametrize("filename", _BUILD_CONFIG_FILES)
    def test_recognizes_build_config_files(self, filename: str):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file(filename) is True

    def test_recognizes_nested_cargo_toml(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("crates/core/Cargo.toml") is True

    def test_rejects_non_build_config_file(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("src/main.rs") is False

    def test_rejects_partial_match(self):
        tool = _make_tool(Path("."))
        assert tool.is_build_config_file("not-Cargo.toml") is False


class TestCargoToolIsSyncAdoptableFile:
    def test_recognizes_cargo_lock(self):
        tool = _make_tool(Path("."))
        assert tool.is_sync_adoptable_file("Cargo.lock") is True
        assert tool.is_sync_adoptable_file("crates/app/Cargo.lock") is True

    def test_rejects_manifest(self):
        tool = _make_tool(Path("."))
        assert tool.is_sync_adoptable_file("Cargo.toml") is False


class TestCargoToolIsTestOnlyChange:
    def test_requests_bounded_content_only_for_rust_sources(self):
        tool = _make_tool(Path("."))

        assert tool.requires_test_only_change_content("src/lib.rs") is True
        assert tool.requires_test_only_change_content("target/debug/generated.rs") is False
        assert tool.requires_test_only_change_content("target/debug/lib.rmeta") is False

    def test_allows_changes_inside_existing_cfg_test_module(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

pub fn borrowed<'a>(value: &'a str) -> &'a str {
    value
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds_one() {
        assert_eq!(add_one(1), 2);
    }
}
"""
        after = before.replace("assert_eq!(add_one(1), 2);", "assert_eq!(add_one(2), 3);")

        assert tool.is_test_only_change("src/lib.rs", before, after) is True

    def test_allows_insertions_inside_existing_cfg_test_module(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

#[cfg(test)]
mod tests {
    use super::*;
}
"""
        after = before.replace(
            "    use super::*;\n",
            "    use super::*;\n\n    #[test]\n    fn smoke() {\n        assert_eq!(add_one(1), 2);\n    }\n",
        )

        assert tool.is_test_only_change("src/lib.rs", before, after) is True

    def test_rejects_commented_cfg_test_marker_before_production_module(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

// #[cfg(test)]
mod tests {
    pub fn helper() -> i32 {
        add_one(1)
    }
}
"""
        after = before.replace("add_one(1)", "add_one(2)")

        assert tool.is_test_only_change("src/lib.rs", before, after) is False

    def test_rejects_string_cfg_test_marker_before_production_module(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

const MARKER: &str = "#[cfg(test)]";
mod tests {
    pub fn helper() -> i32 {
        add_one(1)
    }
}
"""
        after = before.replace("add_one(1)", "add_one(2)")

        assert tool.is_test_only_change("src/lib.rs", before, after) is False

    def test_rejects_block_commented_cfg_test_marker_before_production_module(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

/* #[cfg(test)] */
mod tests {
    pub fn helper() -> i32 {
        add_one(1)
    }
}
"""
        after = before.replace("add_one(1)", "add_one(2)")

        assert tool.is_test_only_change("src/lib.rs", before, after) is False

    def test_rejects_raw_string_cfg_test_marker_before_production_module(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

const MARKER: &str = r#"#[cfg(test)]"#;
mod tests {
    pub fn helper() -> i32 {
        add_one(1)
    }
}
"""
        after = before.replace("add_one(1)", "add_one(2)")

        assert tool.is_test_only_change("src/lib.rs", before, after) is False

    def test_rejects_production_changes_in_rust_source(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

#[cfg(test)]
mod tests {
    #[test]
    fn smoke() {
        assert!(true);
    }
}
"""
        after = before.replace("value + 1", "value + 2")

        assert tool.is_test_only_change("src/lib.rs", before, after) is False

    def test_rejects_mixed_production_and_test_changes(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}

#[cfg(test)]
mod tests {
    #[test]
    fn smoke() {
        assert_eq!(1, 1);
    }
}
"""
        after = before.replace("value + 1", "value + 2").replace("assert_eq!(1, 1);", "assert_eq!(2, 2);")

        assert tool.is_test_only_change("src/lib.rs", before, after) is False

    def test_rejects_new_inline_test_module(self):
        tool = _make_tool(Path("."))
        before = """\
pub fn add_one(value: i32) -> i32 {
    value + 1
}
"""
        after = (
            before
            + """\

#[cfg(test)]
mod tests {
    #[test]
    fn smoke() {
        assert_eq!(1, 1);
    }
}
"""
        )

        assert tool.is_test_only_change("src/lib.rs", before, after) is False

    def test_rejects_non_rust_paths(self):
        tool = _make_tool(Path("."))

        assert tool.is_test_only_change("src/lib.py", "assert 1 == 1\n", "assert 2 == 2\n") is False
