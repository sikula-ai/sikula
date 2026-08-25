from pathlib import Path

import pytest

import core.delivery_asset_assignment as delivery_asset_assignment_module
from core.delivery_asset_assignment import (
    DeliveryAssetAssignmentError,
    DeliveryAssetAssignmentUnit,
    render_delivery_asset_assignments,
)
from core.markdown_document import parse_markdown_document


def _unit(
    unit_id: str, asset_paths: list[str] | None = None, *, markdown: str = "# Unit\n"
) -> DeliveryAssetAssignmentUnit:
    return DeliveryAssetAssignmentUnit(unit_id, markdown, asset_paths or [])


def _render(source: str, units: list[DeliveryAssetAssignmentUnit], root: Path) -> dict[str, str]:
    return render_delivery_asset_assignments(
        source,
        units,
        source_task_path=None,
        project_root=root,
        project_config=None,
    )


def test_assigns_only_relevant_assets_and_preserves_exact_declarations(tmp_path: Path) -> None:
    source = """# Task

## Assets

### Reference assets

- Reference asset: `assets/reference image.png`
  - Usage: reference only.
  - Notes: preserve this constraint exactly.
  - Do not copy this file into production assets.

### Delivery assets

- Delivery asset: `assets/icon.svg`
  - Target: `app/assets/icon.svg`.
  - Source/license: provided by product.
  - SHA-256: `sha256:abc123`
"""

    rendered = _render(
        source,
        [_unit("reference", ["assets/reference image.png"]), _unit("icon", ["assets/icon.svg"])],
        tmp_path,
    )

    assert "### Reference assets" in rendered["reference"]
    assert "- Reference asset: `assets/reference image.png`" in rendered["reference"]
    assert "  - Notes: preserve this constraint exactly." in rendered["reference"]
    assert "assets/icon.svg" not in rendered["reference"]
    assert "### Delivery assets" in rendered["icon"]
    assert "  - Target: `app/assets/icon.svg`." in rendered["icon"]
    assert "  - Source/license: provided by product." in rendered["icon"]
    assert "  - SHA-256: `sha256:abc123`" in rendered["icon"]


def test_preserves_heading_context_across_subsection_and_root_assets(tmp_path: Path) -> None:
    source = """## Assets

### Reference assets

- Reference asset: `assets/reference.png`

## Assets

- Delivery asset: `assets/root-icon.svg`

## Assets

### Delivery assets

- Delivery asset: `assets/grouped-icon.svg`
"""

    rendered = _render(
        source,
        [
            _unit(
                "one",
                ["assets/reference.png", "assets/root-icon.svg", "assets/grouped-icon.svg"],
            )
        ],
        tmp_path,
    )["one"]
    document = parse_markdown_document(rendered)

    assert [(heading.level, heading.raw) for _line, heading in document.headings if not heading.is_document_title] == [
        (2, "Assets"),
        (3, "Reference assets"),
        (2, "Assets"),
        (3, "Delivery assets"),
    ]
    assert rendered.index("assets/reference.png") < rendered.index("assets/root-icon.svg")
    assert rendered.index("assets/root-icon.svg") < rendered.index("assets/grouped-icon.svg")


def test_rejects_rendered_assets_with_changed_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """## Assets

### Reference assets

- Path: `assets/reference.png`
  - Usage: reference only.
"""
    append_assets = delivery_asset_assignment_module._append_assigned_assets

    def append_with_changed_heading(task_markdown, assets):
        rendered, heading_line = append_assets(task_markdown, assets)
        return rendered.replace("### Reference assets", "### Delivery assets"), heading_line

    monkeypatch.setattr(delivery_asset_assignment_module, "_append_assigned_assets", append_with_changed_heading)

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert exc_info.value.code == "unit_asset_render_invalid"


def test_allows_one_source_asset_in_multiple_units(tmp_path: Path) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"

    rendered = _render(
        source,
        [_unit("one", ["assets/reference.png"]), _unit("two", ["assets/reference.png"])],
        tmp_path,
    )

    assert "assets/reference.png" in rendered["one"]
    assert "assets/reference.png" in rendered["two"]


def test_ignores_sibling_requirements_under_assets(tmp_path: Path) -> None:
    source = """## Assets

- Reference asset: `assets/reference.png`
- Keep existing icons unchanged.
  - This remains a source-task requirement.
"""

    rendered = _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert "- Reference asset: `assets/reference.png`" in rendered["one"]
    assert "Keep existing icons unchanged" not in rendered["one"]


def test_rejects_asset_declarations_nested_below_sibling_requirements(tmp_path: Path) -> None:
    source = """## Assets

- References:
  - Mobile:
    - Reference asset: `assets/reference.png`
"""

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one")], tmp_path)

    assert exc_info.value.code == "source_asset_noncanonical"


def test_preserves_wrapped_child_metadata_lines(tmp_path: Path) -> None:
    source = """## Assets

- Delivery asset: `assets/icon.svg`
  - Source/license: provided by the product team for this project and
    approved for redistribution with the application.
  - Target: `app/assets/icon.svg`
"""

    rendered = _render(source, [_unit("one", ["assets/icon.svg"])], tmp_path)

    assert (
        "  - Source/license: provided by the product team for this project and\n"
        "    approved for redistribution with the application."
    ) in rendered["one"]
    assert "  - Target: `app/assets/icon.svg`" in rendered["one"]


def test_preserves_visible_asset_declarations_with_inline_comments(tmp_path: Path) -> None:
    source = """## Assets

- Reference asset: `assets/reference.png` <!-- explanatory note -->
"""

    rendered = _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert "- Reference asset: `assets/reference.png` <!-- explanatory note -->" in rendered["one"]


@pytest.mark.parametrize(
    ("units", "code"),
    [
        ([_unit("one")], "source_asset_unassigned"),
        ([_unit("one", ["assets/other.png"])], "asset_assignment_unknown"),
        (
            [_unit("one", ["assets/reference.png", "./assets/reference.png"])],
            "asset_assignment_duplicate",
        ),
    ],
)
def test_rejects_invalid_assignments(
    tmp_path: Path,
    units: list[DeliveryAssetAssignmentUnit],
    code: str,
) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, units, tmp_path)

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "source",
    [
        "## [Assets](https://example.test)\n",
        "## [Assets](https://example.test)\n\n- Reference asset: `assets/reference.png`\n",
        "## Assets {#assets}\n\n- Path: `assets/reference.png`\n",
        "Assets\n------\n",
        "Assets\n------\n\n- Reference asset: `assets/reference.png`\n",
        "Assets:\nReference assets:\n- Path: `assets/reference.png`\n",
        "## Assets\n\n  - Reference asset: `assets/reference.png`\n",
        "## Assets\n\n* Reference asset: `assets/reference.png`\n",
        "## Assets\n\n- Reference asset: [reference](assets/reference.png)\n",
        "## Assets\n\n- Reference asset: `assets/reference.png`\n    - Notes: nested metadata\n",
        "## Assets\n\n```markdown\n- Reference asset: `assets/reference.png`\n```\n",
        ("## Assets\n\n### [Reference assets](https://example.test)\n\n- Reference asset: `assets/reference.png`\n"),
        "## Assets\n\n### Group\n\n- Reference asset: `assets/reference.png`\n",
        "## Assets\n\n#### Group\n\n- Reference asset: `assets/reference.png`\n",
        (
            "## Assets\n\n- Reference asset: `assets/reference.png`\n"
            "<!-- hidden separator -->\n  - Notes: preserve me.\n"
        ),
    ],
)
def test_rejects_noncanonical_source_asset_declarations(tmp_path: Path, source: str) -> None:
    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert exc_info.value.code == "source_asset_noncanonical"


def test_rejects_duplicate_declarations_for_one_project_asset(tmp_path: Path) -> None:
    source = """## Assets

- Reference asset: `assets/reference.png`
- Reference asset: `./assets/reference.png`
"""

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert exc_info.value.code == "source_asset_conflict"


@pytest.mark.parametrize(
    "markdown",
    [
        "# Unit\n\n## Assets\n",
        "# Unit\n\n## Task assets\n",
        "# Unit\n\n## [Assets](https://example.test)\n",
        "# Unit\n\n## [Assets](https://example.test)\n\n- Asset: `assets/reference.png`\n",
        "# Unit\n\n## Assets {#assets}\n",
        "# Unit\n\n## Assets {.generated}\n\n- Path: `assets/reference.png`\n",
        "# Unit\n\nAssets\n------\n",
        "# Unit\n\nAssets\n------\n\n- Asset: `assets/reference.png`\n",
        "# Unit\n\n- Path: assets/reference.png\n",
        "# Unit\n\n- Delivery asset: `assets/model.glb`\n",
    ],
)
def test_rejects_unit_authored_asset_content(tmp_path: Path, markdown: str) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", ["assets/reference.png"], markdown=markdown)], tmp_path)

    assert exc_info.value.code == "unit_asset_section_forbidden"


def test_rejects_unsupported_source_asset_formats(tmp_path: Path) -> None:
    source = "## Assets\n\n- Reference asset: `.sikula/task-assets/model.glb`\n"

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", [".sikula/task-assets/model.glb"])], tmp_path)

    assert exc_info.value.code == "source_asset_path_invalid"


def test_allows_non_asset_path_labels_outside_asset_sections(tmp_path: Path) -> None:
    source = "# Source\n\n- Path: docs/spec.md\n"
    unit = _unit("one", markdown="# Unit\n\n- Path: docs/unit-spec.md\n")

    rendered = _render(source, [unit], tmp_path)

    assert rendered["one"] == "# Unit\n\n- Path: docs/unit-spec.md\n"


def test_allows_domain_asset_labels_without_paths(tmp_path: Path) -> None:
    source = """# Source

- Asset: records remain editable

## Assets

- Reference asset: `assets/reference.png`
"""
    unit = _unit(
        "one",
        ["assets/reference.png"],
        markdown="# Unit\n\n- Asset: records remain editable\n",
    )

    rendered = _render(source, [unit], tmp_path)

    assert "- Asset: records remain editable" in rendered["one"]
    assert "- Reference asset: `assets/reference.png`" in rendered["one"]


def test_does_not_treat_list_item_before_thematic_break_as_setext_heading(tmp_path: Path) -> None:
    markdown = "# Unit\n\n- Assets\n---\n"

    rendered = _render("# Source\n", [_unit("one", markdown=markdown)], tmp_path)

    assert rendered["one"] == markdown


@pytest.mark.parametrize("title", ["# Assets", "# Asset manifest", "Assets\n======"])
def test_allows_asset_names_as_document_titles(tmp_path: Path, title: str) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"
    unit = _unit("one", ["assets/reference.png"], markdown=f"{title}\n")

    rendered = _render(source, [unit], tmp_path)

    assert rendered["one"].startswith(title)
    assert rendered["one"].count("## Assets") == 1


def test_preserves_prepared_asset_manifest_when_explicitly_allowed(tmp_path: Path) -> None:
    source = """# Prepared contract

## Asset manifest

### Reference assets

- Path: `assets/reference.png`
  - SHA-256: `sha256:abc123`
  - Usage: reference only; do not copy this asset into production files.
"""

    rendered = render_delivery_asset_assignments(
        source,
        [_unit("one", ["assets/reference.png"])],
        source_task_path=None,
        project_root=tmp_path,
        project_config=None,
        allow_source_asset_manifest=True,
    )

    assert "## Asset manifest" in rendered["one"]
    assert "## Assets" not in rendered["one"]
    assert "  - SHA-256: `sha256:abc123`" in rendered["one"]


def test_rejects_prepared_asset_manifest_for_delivery_prepare_source(tmp_path: Path) -> None:
    source = "## Asset manifest\n\n- Path: `assets/reference.png`\n"

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert exc_info.value.code == "source_asset_manifest_reserved"


def test_prefers_prepared_manifest_when_contract_retains_source_assets(tmp_path: Path) -> None:
    source = """## Assets

- Reference asset: `assets/reference.png`
  - Usage: reference only.
  - Notes: preserve this source-only requirement.

## Asset manifest

- Path: `assets/reference.png`
  - SHA-256: `sha256:abc123`
  - Usage: reference only; do not copy this asset into production files.
"""

    rendered = render_delivery_asset_assignments(
        source,
        [_unit("one", ["assets/reference.png"])],
        source_task_path=None,
        project_root=tmp_path,
        project_config=None,
        allow_source_asset_manifest=True,
    )

    assert rendered["one"].count("## Asset manifest") == 1
    assert rendered["one"].count("- Path: `assets/reference.png`") == 1
    assert "  - SHA-256: `sha256:abc123`" in rendered["one"]
    assert "  - Notes: preserve this source-only requirement." in rendered["one"]


def test_preserves_source_only_classification_when_merging_a_manifest(tmp_path: Path) -> None:
    source = """## Assets

- Reference asset: `assets/reference.png`
  - Notes: source-only constraint.

## Asset manifest

- Path: `assets/reference.png`
  - SHA-256: `sha256:abc123`
"""

    rendered = render_delivery_asset_assignments(
        source,
        [_unit("one", ["assets/reference.png"])],
        source_task_path=None,
        project_root=tmp_path,
        project_config=None,
        allow_source_asset_manifest=True,
    )["one"]

    assert "## Asset manifest" in rendered
    assert "- Reference asset: `assets/reference.png`" in rendered
    assert "  - SHA-256: `sha256:abc123`" in rendered
    assert "  - Notes: source-only constraint." in rendered


def test_rejects_conflicting_source_and_manifest_asset_constraints(tmp_path: Path) -> None:
    source = """## Assets

- Reference asset: `assets/reference.png`

## Asset manifest

- Delivery asset: `assets/reference.png`
"""

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        render_delivery_asset_assignments(
            source,
            [_unit("one", ["assets/reference.png"])],
            source_task_path=None,
            project_root=tmp_path,
            project_config=None,
            allow_source_asset_manifest=True,
        )

    assert exc_info.value.code == "source_asset_conflict"


def test_rejects_repeated_source_declarations_around_a_manifest(tmp_path: Path) -> None:
    source = """## Assets

- Reference asset: `assets/reference.png`

## Asset manifest

- Path: `assets/reference.png`
  - Usage: reference only.

## Assets

- Reference asset: `./assets/reference.png`
"""

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        render_delivery_asset_assignments(
            source,
            [_unit("one", ["assets/reference.png"])],
            source_task_path=None,
            project_root=tmp_path,
            project_config=None,
            allow_source_asset_manifest=True,
        )

    assert exc_info.value.code == "source_asset_conflict"


@pytest.mark.parametrize(
    "suffix",
    [
        "\n```text\n",
        "\n<!-- hidden\n",
        "\n<pre>\n",
        "\n<script>\n",
        "\n<style>\n",
        "\n<textarea>\n",
        "\n<?instruction\n",
        "\n<!DOCTYPE\n",
        "\n<![CDATA[\n",
    ],
)
def test_rejects_unterminated_unit_blocks_that_would_hide_assets(tmp_path: Path, suffix: str) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", ["assets/reference.png"], markdown="# Unit\n" + suffix)], tmp_path)

    assert exc_info.value.code == "unit_asset_render_invalid"


def test_leaves_unterminated_unit_without_assignments_to_readiness_checks(tmp_path: Path) -> None:
    rendered = _render("# Source\n", [_unit("one", markdown="# Unit\n\n<!-- unfinished\n")], tmp_path)

    assert rendered["one"] == "# Unit\n\n<!-- unfinished\n"


def test_ignores_asset_examples_in_complete_hidden_blocks(tmp_path: Path) -> None:
    source = """# Task

```markdown
## Assets
- Reference asset: `assets/fenced.png`
```

<!--
## Assets
- Reference asset: `assets/commented.png`
-->

<pre>
## Assets
- Reference asset: `assets/html.png`
</pre>

<?instruction?>

<!DOCTYPE html>

<![CDATA[
## Assets
- Reference asset: `assets/cdata.png`
]]>

<div>
## Assets
- Reference asset: `assets/block-html.png`
</div>
"""

    rendered = _render(source, [_unit("one")], tmp_path)

    assert rendered == {"one": "# Unit\n"}


def test_standard_html_block_asset_syntax_is_not_active(tmp_path: Path) -> None:
    markdown = """# Unit

<div>
## Assets
- Asset: `assets/hidden.png`
</div>
"""

    rendered = _render(markdown, [_unit("one", markdown=markdown)], tmp_path)

    assert rendered == {"one": markdown}


def test_quoted_asset_section_is_not_a_task_asset_section(tmp_path: Path) -> None:
    markdown = """# Unit

> ## Assets
>
> - Asset: `assets/quoted.png`
"""

    rendered = _render(markdown, [_unit("one", markdown=markdown)], tmp_path)

    assert rendered == {"one": markdown}


def test_appends_assets_after_blank_terminated_html_block(tmp_path: Path) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"
    unit = _unit(
        "one",
        ["assets/reference.png"],
        markdown="# Unit\n\n<div>\n## Assets\n- Asset: `assets/hidden.png`\n</div>\n",
    )

    rendered = _render(source, [unit], tmp_path)

    assert rendered["one"].endswith("## Assets\n\n- Reference asset: `assets/reference.png`\n")


@pytest.mark.parametrize("fence", ["```markdown", "~~~markdown"])
def test_comment_fences_do_not_hide_later_source_assets(tmp_path: Path, fence: str) -> None:
    source = f"""# Task

<!--
{fence}
## Assets
-->

## Assets

- Reference asset: `assets/reference.png`
"""

    rendered = _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert "- Reference asset: `assets/reference.png`" in rendered["one"]


@pytest.mark.parametrize("following_heading", ["Scope:", "Scope\n-----"])
def test_supported_sibling_headings_end_source_asset_sections(
    tmp_path: Path,
    following_heading: str,
) -> None:
    source = f"""## Assets

- Reference asset: `assets/reference.png`

{following_heading}

- Keep this behavior unchanged.
"""

    rendered = _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert "- Reference asset: `assets/reference.png`" in rendered["one"]
    assert "Keep this behavior unchanged" not in rendered["one"]


@pytest.mark.parametrize("tag", ["pre", "script", "style", "textarea"])
def test_appends_assets_after_complete_raw_html_blocks(tmp_path: Path, tag: str) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"
    unit = _unit(
        "one",
        ["assets/reference.png"],
        markdown=f"# Unit\n\n<{tag}>\n## Assets\n</{tag.upper()}>\n",
    )

    rendered = _render(source, [unit], tmp_path)

    assert rendered["one"].endswith("## Assets\n\n- Reference asset: `assets/reference.png`\n")


def test_indented_comment_example_does_not_hide_later_source_assets(tmp_path: Path) -> None:
    source = """# Task

    <!-- example only

## Assets

- Reference asset: `assets/reference.png`
"""

    rendered = _render(source, [_unit("one", ["assets/reference.png"])], tmp_path)

    assert "- Reference asset: `assets/reference.png`" in rendered["one"]


def test_indented_comment_example_does_not_hide_appended_assets(tmp_path: Path) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference.png`\n"
    unit = _unit(
        "one",
        ["assets/reference.png"],
        markdown="# Unit\n\n    <!-- example only\n",
    )

    rendered = _render(source, [unit], tmp_path)

    assert "## Assets" in rendered["one"]


@pytest.mark.parametrize(
    "assignment",
    [
        "assets/nested/../reference image.png",
        "assets\\reference image.png",
        "assets/reference%20image.png",
    ],
)
def test_matches_declared_path_aliases(tmp_path: Path, assignment: str) -> None:
    source = "## Assets\n\n- Reference asset: `assets/reference%20image.png`\n"

    rendered = _render(source, [_unit("one", [assignment])], tmp_path)

    assert "- Reference asset: `assets/reference%20image.png`" in rendered["one"]


def test_matches_absolute_source_alias_inside_project(tmp_path: Path) -> None:
    source_path = tmp_path / "assets" / "reference.png"
    source = f"## Assets\n\n- Reference asset: `{source_path}`\n"

    rendered = _render(source, [_unit("one", [str(source_path)])], tmp_path)

    assert f"- Reference asset: `{source_path}`" in rendered["one"]


@pytest.mark.parametrize(
    "asset_url",
    [
        "https://example.test/mock.png",
        "http://example.test/mock.png",
        "file:///tmp/mock.png",
        "data:image/png;base64,mock.png",
    ],
)
def test_rejects_url_source_assets(tmp_path: Path, asset_url: str) -> None:
    source = f"## Assets\n\n- Reference asset: `{asset_url}`\n"

    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render(source, [_unit("one", [asset_url])], tmp_path)

    assert exc_info.value.code == "source_asset_path_invalid"


def test_rejects_reserved_source_manifest(tmp_path: Path) -> None:
    with pytest.raises(DeliveryAssetAssignmentError) as exc_info:
        _render("## Asset manifest\n", [_unit("one")], tmp_path)

    assert exc_info.value.code == "source_asset_manifest_reserved"
