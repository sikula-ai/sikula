from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from core.delivery_progress import delivery_progress_path
from core.delivery_run_next import preview_delivery_run_next, render_delivery_run_next_preview
from sikula_cli.delivery import cmd_delivery_run_next


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _write_unit(root: Path, name: str, body: str) -> str:
    path = root / ".sikula" / "delivery" / "demo" / "units" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path.relative_to(root).as_posix()


def _write_plan(root: Path) -> Path:
    unit_1 = _write_unit(root, "01-foundation.md", "# Unit 01\n\nPrivate task body.\n")
    unit_2 = _write_unit(root, "02-feature.md", "# Unit 02\n\nPrivate follow-up body.\n")
    plan = {
        "schema_version": 1,
        "plan_id": "delivery-run-next-demo",
        "title": "Delivery run-next demo",
        "planning_mode": "fixed_window",
        "final_branch": "sikula/delivery/run-next-demo",
        "streams": [{"id": "app", "label": "App"}],
        "units": [
            {
                "id": "01-foundation",
                "title": "Add foundation",
                "stream": "app",
                "platform": "shared",
                "task_path": unit_1,
                "depends_on": [],
            },
            {
                "id": "02-feature",
                "title": "Add feature",
                "stream": "app",
                "platform": "shared",
                "task_path": unit_2,
                "depends_on": ["01-foundation"],
            },
        ],
    }
    path = root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    return path


def _write_progress(root: Path, units: list[dict]) -> None:
    path = delivery_progress_path(root, "delivery-run-next-demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": "delivery-run-next-demo",
                "units": units,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_preview_delivery_run_next_selects_first_eligible_unit(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    result = preview_delivery_run_next(plan_path)

    assert result.valid is True
    assert result.ready is True
    assert result.dry_run is True
    assert result.selected_unit is not None
    assert result.selected_unit.id == "01-foundation"
    assert result.progress_exists is False
    assert "Private task body" not in json.dumps(result.to_dict())


def test_preview_delivery_run_next_respects_completed_dependencies(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "done"}])

    result = preview_delivery_run_next(plan_path)

    assert result.ready is True
    assert result.selected_unit is not None
    assert result.selected_unit.id == "02-feature"
    assert result.progress_exists is True


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("running", "delivery.running"),
        ("failed", "delivery.failed"),
        ("waiting", "delivery.waiting"),
        ("canceled", "delivery.canceled"),
        ("done", "delivery.complete"),
    ],
)
def test_preview_delivery_run_next_blocks_non_runnable_statuses(tmp_path: Path, status: str, code: str) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    _write_progress(
        tmp_path,
        [
            {"unit_id": "01-foundation", "status": "done"},
            {"unit_id": "02-feature", "status": status},
        ],
    )

    result = preview_delivery_run_next(plan_path)

    assert result.ready is False
    assert result.selected_unit is None
    assert [issue.code for issue in result.errors] == [code]


def test_render_delivery_run_next_preview_is_safe_and_actionable(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    output = render_delivery_run_next_preview(preview_delivery_run_next(plan_path))

    assert "Status: ready" in output
    assert "Selected unit: 01-foundation - Add foundation" in output
    assert "Dry run: yes" in output
    assert "Private task body" not in output


def test_cmd_delivery_run_next_requires_dry_run_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(argparse.Namespace(plan_file=str(plan_path), dry_run=False, json=False), {})

    assert exc.value.code == 2
    assert "--dry-run" in capsys.readouterr().out


def test_cmd_delivery_run_next_uses_configured_project_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    _git_init(project)
    _git_init(other)
    plan_path = _write_plan(project)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            argparse.Namespace(plan_file=str(plan_path), dry_run=True, json=True),
            {"project": {"root_path": str(other)}},
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["errors"][0]["code"] == "plan.path_outside_project"


def test_cmd_delivery_run_next_rejects_configured_root_without_git(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_path = _write_plan(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_run_next(
            argparse.Namespace(plan_file=str(plan_path), dry_run=True, json=True),
            {"project": {"root_path": str(tmp_path)}},
        )

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["errors"][0]["code"] == "project.git_root_missing"
