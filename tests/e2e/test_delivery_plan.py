"""E2E smoke tests for delivery plan CLI primitives."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from sikula import main


def _write_delivery_unit(root: Path, name: str, body: str) -> str:
    path = root / ".sikula" / "delivery" / "demo" / "units" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path.relative_to(root).as_posix()


def _write_delivery_plan(root: Path, data: dict, *, name: str = "plan.yaml") -> Path:
    path = root / ".sikula" / "delivery" / "demo" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _write_project_config(root: Path) -> None:
    path = root / ".sikula" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("project:\n  root_path: .\n  build_tool: python\n", encoding="utf-8")


def test_delivery_check_cli_validates_plan_without_project_config(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_1 = _write_delivery_unit(git_project, "01-foundation.md", "# Unit 01\n\nAdd foundation.\n")
    unit_2 = _write_delivery_unit(git_project, "02-feature.md", "# Unit 02\n\nBuild on foundation.\n")
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-smoke",
            "title": "Delivery smoke",
            "planning_mode": "fixed_window",
            "final_branch": "sikula/delivery/delivery-smoke",
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
        },
    )
    monkeypatch.chdir(git_project)

    with patch("sys.argv", ["sikula", "delivery", "check", plan_path.relative_to(git_project).as_posix()]):
        main()

    out = capsys.readouterr().out
    assert "Status: valid" in out
    assert "Plan ID: delivery-smoke" in out
    assert "Units: 2" in out


def test_delivery_status_cli_reports_pending_units_without_project_config(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_1 = _write_delivery_unit(git_project, "01-foundation.md", "# Unit 01\n\nAdd foundation.\n")
    unit_2 = _write_delivery_unit(git_project, "02-feature.md", "# Unit 02\n\nBuild on foundation.\n")
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-status-smoke",
            "title": "Delivery status smoke",
            "final_branch": "sikula/delivery/delivery-status-smoke",
            "streams": [{"id": "app", "label": "App"}],
            "components": [{"id": "web", "path": "apps/web", "stream": "app"}],
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add foundation",
                    "stream": "app",
                    "component": "web",
                    "scope_paths": ["apps/web/src"],
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
        },
    )
    monkeypatch.chdir(git_project)

    with patch("sys.argv", ["sikula", "delivery", "status", plan_path.relative_to(git_project).as_posix(), "--json"]):
        main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["status"] == "pending"
    assert payload["progress_exists"] is False
    assert payload["units"][0]["eligible"] is True
    assert payload["units"][0]["component"] == "web"
    assert payload["units"][0]["scope_paths"] == ["apps/web/src"]
    assert payload["units"][1]["blocked_by"] == ["01-foundation"]
    assert payload["plan"]["components"] == [{"id": "web", "path": "apps/web", "stream": "app"}]


def test_delivery_run_next_dry_run_reports_selected_unit_with_project_config(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_1 = _write_delivery_unit(git_project, "01-foundation.md", "# Unit 01\n\nAdd foundation.\n")
    unit_2 = _write_delivery_unit(git_project, "02-feature.md", "# Unit 02\n\nBuild on foundation.\n")
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-run-next-smoke",
            "title": "Delivery run-next smoke",
            "final_branch": "sikula/delivery/delivery-run-next-smoke",
            "streams": [{"id": "app", "label": "App"}],
            "components": [{"id": "web", "path": "apps/web", "stream": "app"}],
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add foundation",
                    "stream": "app",
                    "component": "web",
                    "scope_paths": ["apps/web/src"],
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
        },
    )
    _write_project_config(git_project)
    monkeypatch.chdir(git_project)

    with patch(
        "sys.argv",
        ["sikula", "delivery", "run-next", plan_path.relative_to(git_project).as_posix(), "--dry-run", "--json"],
    ):
        main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["ready"] is True
    assert payload["dry_run"] is True
    assert payload["selected_unit"]["id"] == "01-foundation"
    assert payload["selected_unit"]["task_path"] == unit_1
    assert payload["selected_unit"]["component"] == "web"
    assert payload["selected_unit"]["scope_paths"] == ["apps/web/src"]
    assert payload["progress_exists"] is False
    assert not (git_project / ".sikula" / "state" / "delivery" / "delivery-run-next-smoke" / "progress.json").exists()


def test_delivery_commands_ignore_malformed_project_config(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_1 = _write_delivery_unit(git_project, "01-foundation.md", "# Unit 01\n\nAdd foundation.\n")
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-config-independent",
            "title": "Delivery config independent",
            "final_branch": "sikula/delivery/config-independent",
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add foundation",
                    "task_path": unit_1,
                    "depends_on": [],
                }
            ],
        },
    )
    (git_project / ".sikula" / "config.yaml").write_text("root_path: [unterminated\n", encoding="utf-8")
    monkeypatch.chdir(git_project)

    with patch("sys.argv", ["sikula", "delivery", "check", plan_path.relative_to(git_project).as_posix(), "--json"]):
        main()

    check_payload = json.loads(capsys.readouterr().out)
    assert check_payload["valid"] is True

    with patch("sys.argv", ["sikula", "delivery", "status", plan_path.relative_to(git_project).as_posix(), "--json"]):
        main()

    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["valid"] is True
    assert status_payload["status"] == "pending"


def test_delivery_check_cli_reports_invalid_plan_as_json(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_1 = _write_delivery_unit(git_project, "01-foundation.md", "# Unit 01\n\nAdd foundation.\n")
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-smoke-invalid",
            "title": "Delivery smoke invalid",
            "final_branch": "sikula/delivery/delivery-smoke-invalid",
            "units": [
                {
                    "id": "01-foundation",
                    "task_path": unit_1,
                    "depends_on": ["missing-unit"],
                }
            ],
        },
        name="invalid-plan.yaml",
    )
    monkeypatch.chdir(git_project)

    with patch("sys.argv", ["sikula", "delivery", "check", plan_path.relative_to(git_project).as_posix(), "--json"]):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["errors"] == [
        {
            "severity": "error",
            "code": "dependencies.unknown_unit",
            "message": "Unit depends on unknown unit id: missing-unit",
            "path": "units[0].depends_on[0]",
        }
    ]
