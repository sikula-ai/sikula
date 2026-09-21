from __future__ import annotations

from pathlib import Path
import os

import pytest

from tools.delivery_context_tool import read_delivery_context


def test_context_reader_respects_read_scope_and_private_boundaries(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "contract.py").write_text("def existing_contract(): pass\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "credentials.txt").write_text("PRIVATE VALUE")
    (tmp_path / ".env").write_text("PRIVATE VALUE")
    result = read_delivery_context(
        tmp_path,
        ["src/contract.py", "private/credentials.txt", ".env", "../outside"],
        {"sandbox": {"allowed_read_paths": ["src"]}},
    )
    assert result["files"][0]["status"] == "read"
    assert result["files"][0]["sha256"].startswith("sha256:")
    assert all(item["status"] != "read" for item in result["files"][1:])
    assert "PRIVATE VALUE" not in str(result)
    assert str(tmp_path) not in str(result)


def test_context_reader_rejects_symlinks_binary_and_oversized_files(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("x" * 20_000)
    (tmp_path / "binary.py").write_bytes(b"\x00binary")
    (tmp_path / "source.py").write_text("PRIVATE SOURCE")
    try:
        (tmp_path / "alias.py").symlink_to(tmp_path / "source.py")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    result = read_delivery_context(tmp_path, ["alias.py", "large.py", "binary.py"], {})
    assert all(item["status"] != "read" for item in result["files"])
    assert "PRIVATE SOURCE" not in str(result)


def test_context_reader_excludes_configured_runtime_roots_and_platform_environment(tmp_path: Path) -> None:
    for directory in ("custom-state", "custom-reports"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "audit.json").write_text("PRIVATE AUDIT")
    (tmp_path / "local.properties").write_text("PRIVATE ENVIRONMENT")
    result = read_delivery_context(
        tmp_path,
        ["custom-state/audit.json", "custom-reports/audit.json", "local.properties"],
        {
            "tasks": {"state_dir": "custom-state", "contract_report_dir": "custom-reports"},
            "build": {"tool": "gradle-android"},
        },
    )
    assert all(item["status"] == "denied" for item in result["files"])
    assert "PRIVATE" not in str(result)


def test_context_reader_rejects_replaced_directory_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "contract.py").write_text("original contract")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "contract.py").write_text("PRIVATE SOURCE")
    original_open = os.open

    def replace_before_open(path: Path, flags: int) -> int:
        (tmp_path / "src").rename(tmp_path / "original-src")
        try:
            (tmp_path / "src").symlink_to(tmp_path / "private", target_is_directory=True)
        except OSError:
            pytest.skip("Symlink creation is unavailable")
        return original_open(path, flags)

    monkeypatch.setattr("tools.delivery_context_tool.os.open", replace_before_open)
    result = read_delivery_context(tmp_path, ["src/contract.py"], {"sandbox": {"allowed_read_paths": ["src"]}})
    assert result["files"][0]["status"] == "denied"
    assert "PRIVATE SOURCE" not in str(result)


def test_context_reader_enforces_aggregate_and_file_count_bounds(tmp_path: Path) -> None:
    names = [f"context-{index}.py" for index in range(9)]
    for name in names:
        (tmp_path / name).write_text("x" * 16_000)
    result = read_delivery_context(tmp_path, names, {})
    assert len(result["files"]) == 8
    assert [item["status"] for item in result["files"]] == ["read"] * 4 + ["too_large"] * 4


def test_context_reader_cannot_reauthorize_a_link_as_an_unallowed_regular_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.delivery_context_tool as context_tool

    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed/contract.py").write_text("authorized context")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(tmp_path / "allowed", target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    original_read = context_tool._read_regular_file

    def replace_after_scope_check(root: Path, path: Path, limit: int) -> bytes:
        alias.unlink()
        alias.mkdir()
        (alias / "contract.py").write_text("PRIVATE OUTSIDE READ SCOPE")
        return original_read(root, path, limit)

    monkeypatch.setattr(context_tool, "_read_regular_file", replace_after_scope_check)
    result = read_delivery_context(tmp_path, ["alias/contract.py"], {"sandbox": {"allowed_read_paths": ["allowed"]}})
    assert result["files"][0]["status"] == "denied"
    assert "PRIVATE OUTSIDE READ SCOPE" not in str(result)
