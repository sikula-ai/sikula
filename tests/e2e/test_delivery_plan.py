"""E2E smoke tests for delivery plan CLI primitives."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
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


def _delivery_prepare_authoring_output() -> str:
    unit_markdown = """# Prepare delivery artifacts

## Goal

Create the reviewable unit task source artifact for the delivery plan.

## Current behavior

The high-level task has not yet been split into tracked delivery units.

## Desired behavior

The delivery plan contains a focused unit that can be validated before execution.

## Acceptance criteria

- The generated plan references the unit task source artifact.
- Delivery prepare does not create runtime delivery progress.

## Security/privacy

- Do not expose raw prompts, provider output, source excerpts, secrets, or task state.

## Reviewer focus

- Check that artifact paths are project-relative and deterministic.

## Out of scope

- Do not run the generated unit.

## Verification

- `pytest`
"""
    return json.dumps(
        {
            "plan_id": "team-invites",
            "title": "Team invites delivery",
            "planning_mode": "fixed_window",
            "warnings": [],
            "units": [
                {
                    "id": "prepare-artifacts",
                    "title": "Prepare delivery artifacts",
                    "depends_on": [],
                    "task_markdown": unit_markdown,
                    "stream": "docs",
                    "platform": "shared",
                    "scope_paths": ["docs"],
                }
            ],
        }
    )


def _git_commit_file(root: Path, name: str, body: str) -> str:
    path = root / name
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sikula Test",
            "-c",
            "user.email=sikula@example.test",
            "commit",
            "-m",
            f"add {name}",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)


def _budget_split_unit_markdown(title: str) -> str:
    return f"""# {title}

## Goal

Deliver one focused replacement behavior.

## Current behavior

The behavior belongs to an oversized unit.

## Desired behavior

The behavior can be implemented independently.

## Acceptance criteria

- The focused behavior is implemented and validated.

## Security and privacy

- Do not expose child task audit content.

## Reviewer focus

- Verify the replacement boundary.

## Out of scope

- Do not implement the other replacement behavior.

## Validation

- `python3 -m pytest tests_proj/`
"""


def _budget_split_authoring_output() -> str:
    return json.dumps(
        {
            "plan_id": "delivery-budget-split-smoke",
            "target_unit_id": "01-foundation",
            "amend_reason": "unit_budget_exceeded",
            "budget_exceeded": {"name": "max_planner_steps", "limit": 1, "actual": 3},
            "warnings": [],
            "replacement_units": [
                {
                    "id": "foundation-a",
                    "title": "Foundation A",
                    "depends_on": [],
                    "budget": {"max_planner_steps": 1},
                    "task_markdown": _budget_split_unit_markdown("Foundation A"),
                },
                {
                    "id": "foundation-b",
                    "title": "Foundation B",
                    "depends_on": ["foundation-a"],
                    "budget": {"max_planner_steps": 1},
                    "task_markdown": _budget_split_unit_markdown("Foundation B"),
                },
            ],
        }
    )


def test_delivery_prepare_cli_authors_artifacts_then_check_succeeds(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = git_project / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("# Team invites\n\nPRIVATE TASK BODY\n", encoding="utf-8")
    _write_project_config(git_project)
    fake = fake_llm(generate_response=_delivery_prepare_authoring_output())
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch(
            "sys.argv",
            ["sikula", "delivery", "prepare", ".sikula/tasks/team-invites.md", "--json"],
        ):
            main()

    prepare_payload = json.loads(capsys.readouterr().out)
    plan_path = git_project / ".sikula" / "delivery" / "team-invites" / "plan.yaml"
    unit_path = git_project / ".sikula" / "delivery" / "team-invites" / "units" / "prepare-artifacts.md"
    audit_path = git_project / ".sikula" / "contract-reports" / "team-invites.delivery-prepare.auto-llm.jsonl"

    assert prepare_payload["status"] == "ready"
    assert prepare_payload["ready"] is True
    assert prepare_payload["prepared"] is True
    assert prepare_payload["selected_plan_id"] == "team-invites"
    assert prepare_payload["paths"] == {
        "task_file": ".sikula/tasks/team-invites.md",
        "output_dir": ".sikula/delivery/team-invites",
        "plan_file": ".sikula/delivery/team-invites/plan.yaml",
        "units_dir": ".sikula/delivery/team-invites/units",
    }
    assert prepare_payload["unit_task_paths"] == {
        "prepare-artifacts": ".sikula/delivery/team-invites/units/prepare-artifacts.md"
    }
    assert prepare_payload["authoring"]["audit_path"] == (
        ".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl"
    )
    assert "PRIVATE TASK BODY" not in json.dumps(prepare_payload)
    assert plan_path.is_file()
    assert unit_path.is_file()
    assert audit_path.is_file()
    assert not (git_project / ".sikula" / "state" / "delivery" / "team-invites").exists()

    with patch("sys.argv", ["sikula", "delivery", "check", ".sikula/delivery/team-invites/plan.yaml", "--json"]):
        main()

    check_payload = json.loads(capsys.readouterr().out)
    assert check_payload["valid"] is True
    assert check_payload["plan"]["plan_id"] == "team-invites"
    assert len(check_payload["plan"]["units"]) == 1


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


def test_delivery_run_next_prepares_budget_split_with_fake_llm(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_path = _write_delivery_unit(
        git_project,
        "01-foundation.md",
        "# Foundation\n\nImplement the requested foundation behavior.\n",
    )
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-budget-split-smoke",
            "title": "Delivery budget split smoke",
            "final_branch": "sikula/delivery/delivery-budget-split-smoke",
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add foundation",
                    "task_path": unit_path,
                    "depends_on": [],
                    "budget": {"max_planner_steps": 1},
                }
            ],
        },
    )
    config_path = git_project / ".sikula" / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "budget-split-smoke",
                    "root_path": ".",
                    "build_tool": "python",
                    "language": "Python",
                },
                "sandbox": {
                    "allowed_write_paths": ["src/"],
                    "allowed_test_write_paths": ["tests_proj/"],
                    "allowed_read_paths": ["."],
                    "max_iterations": 1,
                    "max_review_iterations": 1,
                    "max_security_review_iterations": 1,
                },
                "tasks": {
                    "state_dir": ".sikula/state/",
                    "contract_report_dir": ".sikula/contract-reports/",
                },
                "guidelines": {"context_files": [], "max_file_chars": 3000},
                "build": {"test_command": "python3 -m pytest tests_proj/", "timeout": 30},
                "planner": {"max_steps": 6},
                "run_planner": False,
                "run_build": False,
                "run_review": False,
                "run_security_review": False,
                "run_test_writing": False,
                "run_tests": True,
                "run_checks": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (git_project / ".gitignore").write_text(
        ".sikula/state/\n.sikula/worktrees/\n.sikula/contract-reports/\n",
        encoding="utf-8",
    )
    _git_commit_all(git_project, "add budget split smoke fixture")
    fake = seq_fake_llm(
        generate_responses=[
            "1. Add the first independent behavior.\n"
            "2. Add the second independent behavior.\n"
            "3. Wire the final independent behavior.",
            _budget_split_authoring_output(),
        ]
    )
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch(
            "sys.argv",
            [
                "sikula",
                "delivery",
                "run-next",
                plan_path.relative_to(git_project).as_posix(),
                "--prepare-budget-split",
                "--json",
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()

    payload = json.loads(capsys.readouterr().out)
    preparation = payload["budget_split_preparation"]
    assert exc_info.value.code == 1
    assert payload["unit_status"] == "failed"
    assert preparation["prepared"] is True
    assert preparation["budget_exceeded"] == {"name": "max_planner_steps", "limit": 1, "actual": 3}
    assert preparation["next_action"] == "delivery_amend_apply"
    assert (git_project / preparation["proposal_path"]).is_file()
    assert (git_project / preparation["audit_path"]).is_file()
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )


def test_delivery_finalize_dry_run_reports_final_branch_with_project_config(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = _git_commit_file(git_project, "feature.txt", "feature\n")
    unit_1 = _write_delivery_unit(git_project, "01-foundation.md", "# Unit 01\n\nAdd foundation.\n")
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-finalize-smoke",
            "title": "Delivery finalize smoke",
            "final_branch": "sikula/delivery/delivery-finalize-smoke",
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
    progress_path = git_project / ".sikula" / "state" / "delivery" / "delivery-finalize-smoke" / "progress.json"
    progress_path.parent.mkdir(parents=True)
    progress_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": "delivery-finalize-smoke",
                "units": [{"unit_id": "01-foundation", "status": "done", "commit": commit}],
            }
        ),
        encoding="utf-8",
    )
    _write_project_config(git_project)
    monkeypatch.chdir(git_project)

    with patch(
        "sys.argv",
        ["sikula", "delivery", "finalize", plan_path.relative_to(git_project).as_posix(), "--dry-run", "--json"],
    ):
        main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["dry_run"] is True
    assert payload["finalized"] is False
    assert payload["final_branch"] == "sikula/delivery/delivery-finalize-smoke"
    assert payload["final_commit"] == commit
    assert not (git_project / ".git" / "refs" / "heads" / "sikula" / "delivery" / "delivery-finalize-smoke").exists()


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
