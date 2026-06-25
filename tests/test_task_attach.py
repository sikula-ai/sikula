from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest

from core.task_attach import append_task_asset_snippet, attach_task_asset
from sikula import cmd_task_attach


def _cfg(project_root: Path, *, task_asset_dir: str = ".sikula/task-assets") -> dict:
    return {
        "project": {"root_path": str(project_root)},
        "tasks": {"task_asset_dir": task_asset_dir},
    }


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "task_file": "",
        "asset_file": "",
        "reference": False,
        "delivery": False,
        "note": None,
        "purpose": None,
        "target": None,
        "source": None,
        "write": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_attach_reference_asset_copies_file_and_prints_snippet_without_writing_task(tmp_path: Path):
    task_file = tmp_path / ".sikula" / "tasks" / "team invite.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("# Add team invite\n\n## Scope\n\n- Fix spacing.\n", encoding="utf-8")
    source = tmp_path / "Desktop" / "Login Spacing.png"
    source.parent.mkdir()
    source.write_bytes(b"fake-png")

    result = attach_task_asset(
        task_file=task_file,
        source_file=source,
        project_root=tmp_path,
        task_asset_dir=tmp_path / ".sikula" / "task-assets",
        kind="reference",
        note="Shows expected login spacing.",
    )

    assert result.project_path == ".sikula/task-assets/team-invite/Login-Spacing.png"
    assert result.asset_path.read_bytes() == b"fake-png"
    assert result.sha256 == "sha256:" + sha256(b"fake-png").hexdigest()
    assert result.wrote_task_file is False
    assert "Reference asset: `.sikula/task-assets/team-invite/Login-Spacing.png`" in result.snippet
    assert "Do not copy this file into production assets." in result.snippet
    assert "## Assets" not in task_file.read_text(encoding="utf-8")


def test_attach_delivery_asset_writes_assets_section(tmp_path: Path):
    task_file = tmp_path / ".sikula" / "tasks" / "success.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("# Add success state\n\n## Scope\n\n- Show a success icon.\n", encoding="utf-8")
    source = tmp_path / "success.svg"
    source.write_text("<svg />", encoding="utf-8")

    result = attach_task_asset(
        task_file=task_file,
        source_file=source,
        project_root=tmp_path,
        task_asset_dir=tmp_path / ".sikula" / "task-assets",
        kind="delivery",
        purpose="Success state icon.",
        target="app/assets/success.svg",
        source_license="provided by product team for this project.",
        write=True,
    )

    task_text = task_file.read_text(encoding="utf-8")
    assert result.wrote_task_file is True
    assert "## Assets" in task_text
    assert "### Delivery assets" in task_text
    assert "- Delivery asset: `.sikula/task-assets/success/success.svg`" in task_text
    assert "  - Purpose: Success state icon." in task_text
    assert "  - Target: `app/assets/success.svg`" in task_text
    assert "  - Source/license: provided by product team for this project." in task_text


def test_append_task_asset_snippet_uses_existing_matching_subsection():
    markdown = """# Task

## Assets

### Reference assets

- Reference asset: `.sikula/task-assets/task/old.png`
  - Usage: reference only.

### Delivery assets

- Delivery asset: `.sikula/task-assets/task/icon.svg`
  - Usage: delivery asset.
"""
    snippet = "- Reference asset: `.sikula/task-assets/task/new.png`\n  - Usage: reference only."

    updated = append_task_asset_snippet(markdown, snippet, kind="reference")

    assert updated.index("new.png") < updated.index("### Delivery assets")
    assert updated.count("### Reference assets") == 1


def test_attach_delivery_requires_purpose_and_source(tmp_path: Path):
    task_file = tmp_path / "task.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    source = tmp_path / "icon.svg"
    source.write_text("<svg />", encoding="utf-8")

    with pytest.raises(ValueError, match="--purpose is required"):
        attach_task_asset(
            task_file=task_file,
            source_file=source,
            project_root=tmp_path,
            task_asset_dir=tmp_path / ".sikula" / "task-assets",
            kind="delivery",
            source_license="provided by product team.",
        )

    with pytest.raises(ValueError, match="--source is required"):
        attach_task_asset(
            task_file=task_file,
            source_file=source,
            project_root=tmp_path,
            task_asset_dir=tmp_path / ".sikula" / "task-assets",
            kind="delivery",
            purpose="Success icon.",
        )


def test_attach_rejects_unsupported_extensions_and_outside_asset_dir(tmp_path: Path):
    task_file = tmp_path / "task.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    source = tmp_path / "asset.bin"
    source.write_bytes(b"binary")

    with pytest.raises(ValueError, match="unsupported asset extension"):
        attach_task_asset(
            task_file=task_file,
            source_file=source,
            project_root=tmp_path,
            task_asset_dir=tmp_path / ".sikula" / "task-assets",
            kind="reference",
        )

    supported = tmp_path / "asset.png"
    supported.write_bytes(b"png")
    with pytest.raises(ValueError, match="task asset directory must be inside the project root"):
        attach_task_asset(
            task_file=task_file,
            source_file=supported,
            project_root=tmp_path,
            task_asset_dir=tmp_path.parent / "assets",
            kind="reference",
        )


def test_attach_does_not_write_through_existing_asset_symlink(tmp_path: Path):
    task_file = tmp_path / "task.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    source = tmp_path / "asset.png"
    source.write_bytes(b"png")
    destination_dir = tmp_path / ".sikula" / "task-assets" / "task"
    destination_dir.mkdir(parents=True)
    outside = tmp_path.parent / "outside-asset.png"
    outside.write_bytes(b"outside")
    symlink = destination_dir / "asset.png"
    try:
        symlink.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")

    result = attach_task_asset(
        task_file=task_file,
        source_file=source,
        project_root=tmp_path,
        task_asset_dir=tmp_path / ".sikula" / "task-assets",
        kind="reference",
    )

    assert result.project_path == ".sikula/task-assets/task/asset-2.png"
    assert (destination_dir / "asset-2.png").read_bytes() == b"png"
    assert outside.read_bytes() == b"outside"


def test_attach_rejects_delivery_target_outside_project(tmp_path: Path):
    task_file = tmp_path / "task.md"
    task_file.write_text("# Task\n", encoding="utf-8")
    source = tmp_path / "icon.svg"
    source.write_text("<svg />", encoding="utf-8")

    with pytest.raises(ValueError, match="path must be inside the project root"):
        attach_task_asset(
            task_file=task_file,
            source_file=source,
            project_root=tmp_path,
            task_asset_dir=tmp_path / ".sikula" / "task-assets",
            kind="delivery",
            purpose="Success icon.",
            target="../outside/icon.svg",
            source_license="provided by product team.",
        )


def test_cmd_task_attach_respects_configured_asset_dir_and_write_flag(tmp_path: Path, capsys):
    task_file = tmp_path / ".sikula" / "tasks" / "task.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("# Task\n", encoding="utf-8")
    source = tmp_path / "mockup.png"
    source.write_bytes(b"png")
    cfg = _cfg(tmp_path, task_asset_dir="design-inputs")

    with patch("sys.exit") as exit_mock:
        cmd_task_attach(
            _args(
                task_file=str(task_file),
                asset_file=str(source),
                reference=True,
                note="Expected state.",
                write=True,
            ),
            cfg,
        )

    exit_mock.assert_not_called()
    out = capsys.readouterr().out
    assert "Attached task asset: design-inputs/task/mockup.png" in out
    assert "Task file updated: yes" in out
    assert (tmp_path / "design-inputs" / "task" / "mockup.png").exists()
    assert "Reference asset: `design-inputs/task/mockup.png`" in task_file.read_text(encoding="utf-8")
