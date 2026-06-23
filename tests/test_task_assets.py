from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from core.task_assets import (
    asset_manifest_h1_has_structured_body,
    detect_asset_references,
    detect_undeclared_asset_paths,
)


def _project_config(project_root: Path, *, task_asset_dir: str = ".sikula/task-assets") -> dict:
    return {
        "project": {"root_path": str(project_root)},
        "tasks": {"task_asset_dir": task_asset_dir},
    }


def _references_by_path(markdown: str, tmp_path: Path, *, task_asset_dir: str = ".sikula/task-assets") -> dict:
    references = detect_asset_references(
        markdown,
        source_path=tmp_path / ".sikula" / "tasks" / "task.md",
        project_config=_project_config(tmp_path, task_asset_dir=task_asset_dir),
    )
    return {reference["project_path"]: reference for reference in references}


def _undeclared_paths(markdown: str, tmp_path: Path, *, task_asset_dir: str = ".sikula/task-assets") -> list[dict]:
    references = detect_asset_references(
        markdown,
        source_path=tmp_path / ".sikula" / "tasks" / "task.md",
        project_config=_project_config(tmp_path, task_asset_dir=task_asset_dir),
    )
    return detect_undeclared_asset_paths(
        markdown,
        project_config=_project_config(tmp_path, task_asset_dir=task_asset_dir),
        asset_references=references,
    )


def _asset_manifest_title_has_structured_body(markdown: str, *, ignore_fenced_blocks: bool = False) -> bool:
    lines = markdown.splitlines()
    return asset_manifest_h1_has_structured_body(lines, 1, ignore_fenced_blocks=ignore_fenced_blocks)


def test_asset_manifest_title_body_classification_matrix():
    cases = [
        (
            "title with product scope path field",
            """# Asset manifest

## Scope

- Path: `.sikula/task-assets/login-spacing.png` should be shown in the UI.
""",
            False,
        ),
        (
            "title with product asset declarations",
            """# Asset manifest

## Assets

### Reference assets

- Path: `.sikula/task-assets/login-spacing.png`
""",
            False,
        ),
        (
            "title with direct manifest field",
            """# Asset manifest

- Path: `.sikula/task-assets/login-spacing.png`
""",
            True,
        ),
        (
            "title with direct manifest subsection",
            """# Asset manifest

### Reference assets

- Path: `.sikula/task-assets/login-spacing.png`
""",
            True,
        ),
        (
            "title with neutral intro before manifest subsection",
            """# Asset manifest

## Summary

Generated asset entries.

### Reference assets

- Path: `.sikula/task-assets/login-spacing.png`
""",
            True,
        ),
        (
            "title stops before next h1",
            """# Asset manifest

# Follow-up task

### Reference assets

- Path: `.sikula/task-assets/login-spacing.png`
""",
            False,
        ),
    ]

    for _name, markdown, expected in cases:
        assert _asset_manifest_title_has_structured_body(markdown) is expected


def test_asset_manifest_title_body_ignores_fenced_manifest_example():
    markdown = """# Asset manifest

## Scope

```md
### Reference assets

- Path: `.sikula/task-assets/login-spacing.png`
```
"""

    assert _asset_manifest_title_has_structured_body(markdown, ignore_fenced_blocks=True) is False


def test_detect_asset_references_reads_structured_reference_and_delivery_assets(tmp_path: Path):
    reference_path = tmp_path / ".sikula" / "task-assets" / "login-spacing-bug.png"
    delivery_path = tmp_path / ".sikula" / "task-assets" / "success-check.svg"
    reference_path.parent.mkdir(parents=True)
    reference_path.write_bytes(b"fake-png")
    delivery_path.write_text("<svg />", encoding="utf-8")
    markdown = """# Add success icon

## Assets

### Reference assets

- Path: `.sikula/task-assets/login-spacing-bug.png`
  - Usage: reference only.
  - Notes: Shows expected spacing.

### Delivery assets

- Path: `.sikula/task-assets/success-check.svg`
  - Usage: delivery asset.
  - Purpose: new success state icon.
  - Target: app/assets/success-check.svg
  - Source/license: provided by product team for this project.
"""

    references = _references_by_path(markdown, tmp_path)

    reference = references[".sikula/task-assets/login-spacing-bug.png"]
    assert reference["kind"] == "reference"
    assert reference["status"] == "available"
    assert reference["sha256"] == "sha256:" + sha256(b"fake-png").hexdigest()

    delivery = references[".sikula/task-assets/success-check.svg"]
    assert delivery["kind"] == "delivery"
    assert delivery["requested_target"] == "app/assets/success-check.svg"
    assert delivery["source_license"] == "provided by product team for this project."


def test_detect_asset_references_accepts_structured_path_label_outside_task_asset_dir(tmp_path: Path):
    asset_path = tmp_path / "designs" / "login-spacing.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Fix login spacing

## Assets

### Reference assets

- Path: `designs/login-spacing.png`
  - Usage: reference only.
"""

    references = _references_by_path(markdown, tmp_path)

    assert sorted(references) == ["designs/login-spacing.png"]
    assert references["designs/login-spacing.png"]["kind"] == "reference"
    assert references["designs/login-spacing.png"]["status"] == "available"


def test_detect_asset_references_treats_first_h1_asset_manifest_as_document_title(tmp_path: Path):
    markdown = """# Asset manifest

## Scope

- Path: `.sikula/task-assets/login-spacing.png` is product copy, not a structured asset declaration.
"""

    assert _references_by_path(markdown, tmp_path) == {}


def test_detect_asset_references_reads_first_h1_asset_manifest_with_manifest_body(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Asset manifest

### Reference assets

- Path: `.sikula/task-assets/login-spacing.png`
  - Usage: reference only.
"""

    references = _references_by_path(markdown, tmp_path)

    assert sorted(references) == [".sikula/task-assets/login-spacing.png"]


def test_detect_asset_references_reads_first_h1_asset_manifest_after_neutral_subheading(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Asset manifest

## Summary

These assets document the expected spacing.

### Reference assets

- Path: `.sikula/task-assets/login-spacing.png`
  - Usage: reference only.
"""

    references = _references_by_path(markdown, tmp_path)

    assert sorted(references) == [".sikula/task-assets/login-spacing.png"]


def test_detect_asset_references_reads_non_title_asset_manifest_sections(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Fix login spacing

## Asset manifest

- Path: `.sikula/task-assets/login-spacing.png`
  - Usage: reference only.
"""

    references = _references_by_path(markdown, tmp_path)

    assert sorted(references) == [".sikula/task-assets/login-spacing.png"]


def test_detect_asset_references_still_reads_first_h1_assets_section(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Assets

- Path: `.sikula/task-assets/login-spacing.png`
  - Usage: reference only.
"""

    references = _references_by_path(markdown, tmp_path)

    assert sorted(references) == [".sikula/task-assets/login-spacing.png"]


def test_detect_asset_references_ignores_bare_asset_path_in_asset_section(tmp_path: Path):
    asset_path = tmp_path / "designs" / "login-spacing.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Fix login spacing

## Assets

### Mockups

- `designs/login-spacing.png`
"""

    assert _references_by_path(markdown, tmp_path) == {}


def test_detect_asset_references_ignores_unstructured_project_path_in_asset_section(tmp_path: Path):
    markdown = """# Add success icon

## Assets

- The implementation target is `app/assets/success.svg`.
"""

    assert _references_by_path(markdown, tmp_path) == {}


def test_detect_asset_references_ignores_ordinary_output_paths(tmp_path: Path):
    markdown = """# Update asset docs

## Scope

- Update `docs/assets.md` with new asset naming guidance.

## Assets

- Create `app/assets/success.svg` as the generated success icon.
"""

    assert _references_by_path(markdown, tmp_path) == {}


def test_detect_asset_references_ignores_prose_copy_source_and_destination(tmp_path: Path):
    source_path = tmp_path / "designs" / "success.svg"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("<svg />", encoding="utf-8")
    markdown = """# Add success icon

## Assets

### Delivery assets

- Copy `designs/success.svg` into `app/assets/success.svg`.
  - Source/license: provided by product team for this project.
"""

    assert _references_by_path(markdown, tmp_path) == {}


def test_detect_asset_references_requires_structured_declaration_for_custom_task_asset_dir(tmp_path: Path):
    asset_path = tmp_path / "spec-assets" / "login-reference.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Fix login spacing

## Scope

- Match the login form spacing shown in `spec-assets/login-reference.png` as reference only.
"""

    assert _references_by_path(markdown, tmp_path, task_asset_dir="spec-assets") == {}


def test_detect_undeclared_asset_paths_reports_prose_task_asset_paths(tmp_path: Path):
    markdown = """# Fix login spacing

## Scope

- Match the login form spacing shown in `.sikula/task-assets/login-reference.png`.
"""

    assert _undeclared_paths(markdown, tmp_path) == [{"path": ".sikula/task-assets/login-reference.png", "line": 5}]


def test_detect_undeclared_asset_paths_reports_bare_paths_inside_assets_section(tmp_path: Path):
    markdown = """# Fix login spacing

## Assets

- `.sikula/task-assets/login-reference.png`
"""

    assert _undeclared_paths(markdown, tmp_path) == [{"path": ".sikula/task-assets/login-reference.png", "line": 5}]


def test_detect_undeclared_asset_paths_reports_prose_task_asset_paths_inside_assets_section(tmp_path: Path):
    markdown = """# Fix login spacing

## Assets

- Use `.sikula/task-assets/login-reference.png` as the reference mockup.
"""

    assert _undeclared_paths(markdown, tmp_path) == [{"path": ".sikula/task-assets/login-reference.png", "line": 5}]


def test_detect_undeclared_asset_paths_ignores_declared_asset_paths_reused_in_text(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-reference.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    markdown = """# Fix login spacing

## Scope

- Match the spacing shown in `.sikula/task-assets/login-reference.png`.

## Assets

### Reference assets

- Path: `.sikula/task-assets/login-reference.png`
  - Usage: reference only.
"""

    assert _undeclared_paths(markdown, tmp_path) == []
