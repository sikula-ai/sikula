"""E2E smoke tests for delivery plan CLI primitives."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from core.delivery_amendment import capture_delivery_amendment_failure_evidence, inspect_delivery_amendment_target
from core.delivery_handoff import delivery_unit_handoff_path, read_delivery_unit_handoff
from core.state import JsonStateStore
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


def _write_project_config(root: Path, *, allowed_write_paths: list[str] | None = None) -> None:
    path = root / ".sikula" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    config: dict[str, object] = {
        "project": {
            "root_path": ".",
            "build_tool": "python",
        }
    }
    if allowed_write_paths is not None:
        config["sandbox"] = {"allowed_write_paths": allowed_write_paths}
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _write_handoff_smoke_config(root: Path) -> None:
    path = root / ".sikula" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "delivery-handoff-smoke",
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
                "tasks": {"state_dir": ".sikula/state/"},
                "guidelines": {"context_files": [], "max_file_chars": 3000},
                "build": {"test_command": "python3 -m pytest tests_proj/", "timeout": 30},
                "planner": {"max_steps": 2},
                "run_planner": False,
                "run_build": False,
                "run_review": False,
                "run_security_review": False,
                "run_test_writing": False,
                "run_tests": False,
                "run_checks": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        ".sikula/state/\n.sikula/worktrees/\n.sikula/contract-reports/\n",
        encoding="utf-8",
    )


def _write_delivery_stop_config(root: Path) -> None:
    path = root / ".sikula" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "delivery-stop-smoke",
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
                "build": {
                    "compile_command": "python3 -m compileall -q src/",
                    "test_command": "python3 -m pytest tests_proj/",
                    "timeout": 30,
                },
                "planner": {"max_steps": 2},
                "run_planner": False,
                "run_build": True,
                "run_review": True,
                "run_security_review": True,
                "run_test_writing": True,
                "run_tests": True,
                "run_checks": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        ".sikula/state/\n.sikula/worktrees/\n.sikula/contract-reports/\n",
        encoding="utf-8",
    )


def _delivery_stop_unit_markdown(title: str) -> str:
    return f"""# {title}

## Goal

Deliver one bounded calculator behavior without crossing its ownership or write-scope boundary.

## Current behavior

The requested behavior has not been implemented in the delivery child.

## Desired behavior

- Keep production changes inside the declared unit scope.
- Stop when the required change belongs to an external repository.
- Preserve inspectable local evidence without continuing into later phases.

## Repo context

The unit runs through `delivery run-next` in an isolated Git worktree and reports its terminal result to parent delivery progress.

## Acceptance criteria

- In-scope calculator work remains inspectable.
- Boundary failures use a stable structured classification.
- A boundary failure cannot reach validation, review, commit, handoff, or assembly.
- An unchanged reset cannot bypass a terminal delivery stop.

## Security and privacy

Do not expose prompts, source contents, state blobs, credentials, or absolute paths in public output.

## Reviewer focus

Verify fail-closed phase ordering, state correlation, sanitized projections, and recovery guidance.

## Out of scope

Do not authorize changes in an external repository or broaden the configured repository sandbox.

## Validation

- `python3 -m compileall -q src/`
- `python3 -m pytest tests_proj/`
"""


def _write_delivery_stop_fixture(
    root: Path,
    *,
    plan_id: str,
    constraint_kind: str,
    constraint_summary: str,
) -> tuple[Path, str]:
    unit_id = "boundary-unit"
    source_path = root / ".sikula" / "tasks" / f"{plan_id}.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_text = f"# {plan_id}\n\nPRIVATE SOURCE TASK BODY. Keep externally owned protocol changes read-only.\n"
    source_path.write_text(source_text, encoding="utf-8")
    unit_path = _write_delivery_unit(
        root,
        f"{unit_id}.md",
        _delivery_stop_unit_markdown("Bounded delivery behavior"),
    )
    plan_path = _write_delivery_plan(
        root,
        {
            "schema_version": 1,
            "plan_id": plan_id,
            "title": "Delivery boundary stop smoke",
            "final_branch": f"sikula/delivery/{plan_id}",
            "source_task": {
                "path": source_path.relative_to(root).as_posix(),
                "sha256": f"sha256:{sha256(source_text.encode('utf-8')).hexdigest()}",
            },
            "constraints": [
                {
                    "id": "ownership-boundary",
                    "kind": constraint_kind,
                    "summary": constraint_summary,
                    "unit_ids": [unit_id],
                    "disposition": "preserved",
                }
            ],
            "units": [
                {
                    "id": unit_id,
                    "title": "Bounded delivery behavior",
                    "task_path": unit_path,
                    "depends_on": [],
                    "scope_paths": ["src/allowed"],
                }
            ],
        },
    )
    _write_delivery_stop_config(root)
    _git_commit_all(root, f"add {plan_id} fixture")
    return plan_path, unit_id


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
            "constraints": [],
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


def _delivery_assessment_output() -> str:
    return json.dumps(
        {
            "recommended_mode": "delivery_plan",
            "reason_codes": ["multiple_platforms", "dependency_order_required"],
            "units": [
                {
                    "id": "shared",
                    "title": "Shared behavior",
                    "depends_on": [],
                    "component": "shared",
                    "platform": "shared",
                },
                {
                    "id": "platform-a",
                    "title": "Platform A",
                    "depends_on": ["shared"],
                    "component": "client-a",
                    "platform": "platform-a",
                },
                {
                    "id": "platform-b",
                    "title": "Platform B",
                    "depends_on": ["shared"],
                    "component": "client-b",
                    "platform": "platform-b",
                },
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


def test_delivery_assess_cli_recommends_mixed_platform_plan_without_source_or_state_writes(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = git_project / ".sikula" / "tasks" / "cross-platform-feature.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("# Cross-platform feature\n\nPRIVATE TASK BODY\n", encoding="utf-8")
    _write_project_config(git_project)
    fake = fake_llm(generate_response=_delivery_assessment_output())
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch(
            "sys.argv",
            ["sikula", "delivery", "assess", ".sikula/tasks/cross-platform-feature.md", "--json"],
        ):
            main()

    output = capsys.readouterr().out
    payload = json.loads(output)
    audit_path = git_project / ".sikula" / "contract-reports" / "cross-platform-feature.delivery-assess.auto-llm.jsonl"

    assert payload["status"] == "ready"
    assert payload["recommended_mode"] == "delivery_plan"
    assert payload["reason_codes"] == ["multiple_platforms", "dependency_order_required"]
    assert payload["unit_count"] == 3
    assert payload["next_command"] == ("sikula delivery prepare .sikula/tasks/cross-platform-feature.md")
    assert payload["audit_path"] == (".sikula/contract-reports/cross-platform-feature.delivery-assess.auto-llm.jsonl")
    assert "PRIVATE TASK BODY" not in output
    assert str(git_project) not in output
    assert audit_path.is_file()
    assert not (git_project / ".sikula" / "delivery").exists()
    assert not (git_project / ".sikula" / "state").exists()
    assert not (git_project / ".sikula" / "worktrees").exists()


def test_delivery_prepare_cli_authors_artifacts_then_check_succeeds(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = git_project / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("# Team invites\n\nPRIVATE TASK BODY\n", encoding="utf-8")
    _write_project_config(git_project)
    fake = seq_fake_llm(
        generate_responses=[
            _delivery_prepare_authoring_output(),
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
        ]
    )
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
    assert check_payload["plan"]["source_task"]["path"] == ".sikula/tasks/team-invites.md"
    assert len(check_payload["plan"]["units"]) == 1


def test_delivery_prepare_cli_preserves_constraints_and_assets_from_one_source_snapshot(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asset_path = ".sikula/task-assets/invite-reference.png"
    asset_file = git_project / asset_path
    asset_file.parent.mkdir(parents=True, exist_ok=True)
    asset_file.write_bytes(b"invite reference")
    task_path = git_project / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        "# Team invites\n\n"
        "Consume the existing GET /api/v1/resource contract without modifying it.\n\n"
        "## Assets\n\n"
        f"- Reference asset: `{asset_path}`\n"
        "  - Usage: reference only.\n",
        encoding="utf-8",
    )
    _write_project_config(git_project)
    constraint = {
        "id": "protocol-authority",
        "kind": "authoritative_read_only_dependency",
        "summary": "GET /api/v1/resource remains an authoritative read-only dependency.",
        "unit_ids": ["prepare-artifacts"],
        "disposition": "preserved",
    }
    authored = json.loads(_delivery_prepare_authoring_output())
    authored["constraints"] = [constraint]
    authored["units"][0]["asset_paths"] = [asset_path]
    fake = seq_fake_llm(
        generate_responses=[
            json.dumps(authored),
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [constraint],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
        ]
    )
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch(
            "sys.argv",
            ["sikula", "delivery", "prepare", ".sikula/tasks/team-invites.md", "--json"],
        ):
            main()

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    plan_path = git_project / ".sikula" / "delivery" / "team-invites" / "plan.yaml"
    unit_path = git_project / ".sikula" / "delivery" / "team-invites" / "units" / "prepare-artifacts.md"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    unit_markdown = unit_path.read_text(encoding="utf-8")

    assert payload["status"] == "ready"
    assert plan["constraints"] == [constraint]
    assert f"- Reference asset: `{asset_path}`" in unit_markdown
    assert "  - Usage: reference only." in unit_markdown
    assert asset_path not in payload_text
    assert not (git_project / ".sikula" / "state" / "delivery" / "team-invites").exists()


def test_delivery_prepare_cli_adds_missing_source_literals_to_unit_contract(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_literal = '- <resource.title> — "Resource"'
    second_literal = '- <resource.submit> — "Save"'
    task_path = git_project / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        f"# Team invites\n\n## Context\n\nLocalization keys:\n\n{first_literal}\n{second_literal}\n",
        encoding="utf-8",
    )
    _write_project_config(git_project)
    authored = json.loads(_delivery_prepare_authoring_output())
    authored["units"][0]["task_markdown"] = authored["units"][0]["task_markdown"].replace(
        "Create the reviewable unit task source artifact for the delivery plan.",
        "Create the reviewable unit task source artifact using the provided localization keys.",
    )
    gap = {
        "unit_id": "prepare-artifacts",
        "source_literals": [first_literal, second_literal],
    }
    fake = seq_fake_llm(
        generate_responses=[
            json.dumps(authored),
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [],
                    "constraint_gaps": [],
                    "unit_context_complete": False,
                    "unit_context_gaps": [gap],
                }
            ),
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [],
                    "constraint_gaps": [],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
        ]
    )
    monkeypatch.chdir(git_project)

    with (
        patch("core.llm_client.create_llm_client", return_value=fake),
        patch(
            "sys.argv",
            ["sikula", "delivery", "prepare", ".sikula/tasks/team-invites.md", "--json"],
        ),
    ):
        main()

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    unit_path = git_project / ".sikula" / "delivery" / "team-invites" / "units" / "prepare-artifacts.md"
    unit_markdown = unit_path.read_text(encoding="utf-8")
    assert payload["status"] == "ready"
    assert first_literal in unit_markdown
    assert second_literal in unit_markdown
    assert "## Authoritative source values" in unit_markdown
    assert "resource.title" not in payload_text


def test_delivery_prepare_cli_blocks_when_source_literals_remain_missing(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_literal = '- <resource.title> — "Resource"'
    second_literal = '- <resource.submit> — "Save"'
    task_path = git_project / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        f"# Team invites\n\n## Context\n\n{first_literal}\n{second_literal}\n",
        encoding="utf-8",
    )
    _write_project_config(git_project)
    first_gap = {"unit_id": "prepare-artifacts", "source_literals": [first_literal]}
    remaining_gap = {"unit_id": "prepare-artifacts", "source_literals": [second_literal]}
    fake = seq_fake_llm(
        generate_responses=[
            _delivery_prepare_authoring_output(),
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [],
                    "constraint_gaps": [],
                    "unit_context_complete": False,
                    "unit_context_gaps": [first_gap],
                }
            ),
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [],
                    "constraint_gaps": [],
                    "unit_context_complete": False,
                    "unit_context_gaps": [remaining_gap],
                }
            ),
        ]
    )
    monkeypatch.chdir(git_project)

    with (
        patch("core.llm_client.create_llm_client", return_value=fake),
        patch(
            "sys.argv",
            ["sikula", "delivery", "prepare", ".sikula/tasks/team-invites.md", "--json"],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exc_info.value.code == 1
    assert payload["status"] == "blocked"
    assert payload["errors"][0]["code"] == "delivery_prepare.unit_context_incomplete"
    assert any(error["code"] == "delivery_prepare.unit_context_gap" for error in payload["errors"])
    assert "resource.submit" not in payload_text
    assert not (git_project / ".sikula" / "delivery" / "team-invites").exists()


def test_delivery_prepare_cli_repairs_an_omitted_constraint_before_writing(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = git_project / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        "# Team invites\n\nOnly the protocol repository may change protocol files.\n",
        encoding="utf-8",
    )
    _write_project_config(git_project)
    gap = {
        "reason": "omitted",
        "kind": "repository_ownership",
        "summary": "Protocol file changes remain under external repository ownership.",
        "affected_unit_ids": ["prepare-artifacts"],
    }
    constraint = {
        "id": "protocol-repository-ownership",
        "kind": "repository_ownership",
        "summary": gap["summary"],
        "unit_ids": ["prepare-artifacts"],
        "disposition": "preserved",
    }
    fake = seq_fake_llm(
        generate_responses=[
            _delivery_prepare_authoring_output(),
            json.dumps(
                {
                    "constraints_complete": False,
                    "constraints": [],
                    "constraint_gaps": [gap],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
            json.dumps({"constraints": [constraint]}),
            json.dumps(
                {
                    "constraints_complete": True,
                    "constraints": [constraint],
                    "constraint_gaps": [],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
        ]
    )
    monkeypatch.chdir(git_project)

    with (
        patch("core.llm_client.create_llm_client", return_value=fake),
        patch(
            "sys.argv",
            ["sikula", "delivery", "prepare", ".sikula/tasks/team-invites.md", "--json"],
        ),
    ):
        main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    plan_path = git_project / ".sikula" / "delivery" / "team-invites" / "plan.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    assert plan["constraints"] == [constraint]
    audit_path = git_project / ".sikula" / "contract-reports" / "team-invites.delivery-prepare.auto-llm.jsonl"
    audit_records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [record["record"]["phase"] for record in audit_records] == [
        "delivery_prepare_authoring",
        "delivery_prepare_constraint_verification",
        "delivery_prepare_constraint_repair",
        "delivery_prepare_constraint_verification",
    ]
    assert audit_records[-1]["record"]["round_index"] == 2


def test_delivery_prepare_cli_blocks_with_gaps_when_constraint_repair_remains_incomplete(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = git_project / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        "# Team invites\n\nOnly the protocol repository may change protocol files.\n",
        encoding="utf-8",
    )
    _write_project_config(git_project)
    first_gap = {
        "reason": "omitted",
        "kind": "repository_ownership",
        "summary": "Protocol file changes remain under external repository ownership.",
        "affected_unit_ids": ["prepare-artifacts"],
    }
    repaired_constraint = {
        "id": "protocol-repository-ownership",
        "kind": "repository_ownership",
        "summary": first_gap["summary"],
        "unit_ids": ["prepare-artifacts"],
        "disposition": "preserved",
    }
    remaining_gap = {
        "reason": "omitted",
        "kind": "authoritative_read_only_dependency",
        "summary": "The existing protocol contract remains authoritative.",
        "affected_unit_ids": ["prepare-artifacts"],
    }
    fake = seq_fake_llm(
        generate_responses=[
            _delivery_prepare_authoring_output(),
            json.dumps(
                {
                    "constraints_complete": False,
                    "constraints": [],
                    "constraint_gaps": [first_gap],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
            json.dumps({"constraints": [repaired_constraint]}),
            json.dumps(
                {
                    "constraints_complete": False,
                    "constraints": [repaired_constraint],
                    "constraint_gaps": [remaining_gap],
                    "unit_context_complete": True,
                    "unit_context_gaps": [],
                }
            ),
        ]
    )
    monkeypatch.chdir(git_project)

    with (
        patch("core.llm_client.create_llm_client", return_value=fake),
        patch(
            "sys.argv",
            ["sikula", "delivery", "prepare", ".sikula/tasks/team-invites.md", "--json"],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["status"] == "blocked"
    assert payload["errors"][0]["code"] == "delivery_prepare.constraint_review_required"
    assert any(error["code"] == "delivery_prepare.constraint_verification_incomplete" for error in payload["errors"])
    gap_error = next(error for error in payload["errors"] if error["code"] == "delivery_prepare.constraint_gap")
    assert "authoritative_read_only_dependency" in gap_error["message"]
    assert "prepare-artifacts" in gap_error["message"]
    assert not (git_project / ".sikula" / "delivery" / "team-invites").exists()
    audit_path = git_project / ".sikula" / "contract-reports" / "team-invites.delivery-prepare.auto-llm.jsonl"
    audit_records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert audit_records[-1]["record"]["phase"] == "delivery_prepare_constraint_verification"
    assert audit_records[-1]["record"]["round_index"] == 2
    assert audit_records[-1]["record"]["parsed"]["constraints_complete"] is False
    assert audit_records[-1]["record"]["parsed"]["constraint_gaps"] == [remaining_gap]


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
    _write_project_config(git_project, allowed_write_paths=["apps/web/src"])
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


def test_delivery_run_next_handoff_flows_between_dependent_children(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_1 = _write_delivery_unit(
        git_project,
        "01-foundation.md",
        "# Foundation\n\nAdd subtraction support to the calculator.\n",
    )
    unit_2 = _write_delivery_unit(
        git_project,
        "02-feature.md",
        "# Feature\n\nAdd multiplication support while preserving subtraction.\n",
    )
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-handoff-smoke",
            "title": "Delivery handoff smoke",
            "final_branch": "sikula/delivery/delivery-handoff-smoke",
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add subtraction",
                    "task_path": unit_1,
                    "depends_on": [],
                },
                {
                    "id": "02-feature",
                    "title": "Add multiplication",
                    "task_path": unit_2,
                    "depends_on": ["01-foundation"],
                },
            ],
        },
    )
    _write_handoff_smoke_config(git_project)
    _git_commit_all(git_project, "add delivery handoff smoke fixture")
    fake = fake_llm(
        agent_responses=[
            {"src/calculator.py": ("def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n")},
            {
                "src/calculator.py": (
                    "def add(a, b):\n"
                    "    return a + b\n\n"
                    "def subtract(a, b):\n"
                    "    return a - b\n\n"
                    "def multiply(a, b):\n"
                    "    return a * b\n"
                )
            },
        ]
    )
    relative_plan = plan_path.relative_to(git_project).as_posix()
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            main()

        first_payload = json.loads(capsys.readouterr().out)
        first_handoff = read_delivery_unit_handoff(
            delivery_unit_handoff_path(git_project, "delivery-handoff-smoke", "01-foundation")
        )
        assert first_payload["succeeded"] is True
        assert first_payload["selected_unit"]["status"] == "done"
        assert first_handoff.result_commit
        assert "task_description" not in first_handoff.to_dict()

        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            main()

    second_payload = json.loads(capsys.readouterr().out)
    second_child = JsonStateStore(git_project / ".sikula" / "state").load(second_payload["child_task_id"])
    progress = json.loads(
        (git_project / ".sikula" / "state" / "delivery" / "delivery-handoff-smoke" / "progress.json").read_text(
            encoding="utf-8"
        )
    )

    assert second_payload["succeeded"] is True
    assert second_payload["selected_unit"]["id"] == "02-feature"
    assert second_child is not None
    assert second_child.delivery_dependency_handoffs == [first_handoff.to_dict()]
    assert "Prior delivery dependency handoffs:" in second_child.analyst_prompt
    assert first_handoff.fingerprint in second_child.analyst_prompt
    assert progress["units"][0]["handoff_fingerprint"] == first_handoff.fingerprint
    assert progress["units"][1]["handoff_schema_version"] == 1
    assert progress["assembly_status"] == "ready"
    assert progress["assembled_commit"] == second_child.result_commit
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", first_handoff.result_commit, progress["assembled_commit"]],
            cwd=git_project,
            capture_output=True,
        ).returncode
        == 0
    )
    operator_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert operator_head != progress["assembled_commit"]


def test_delivery_child_cannot_publish_an_escaping_scope_symlink(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alias = git_project / "src" / "alias"
    alias.mkdir()
    (alias / ".keep").write_text("ordinary directory in operator checkout\n", encoding="utf-8")
    external_root = git_project.parent / "external-target"
    external_root.mkdir()
    (external_root / "sentinel.txt").write_text("MUST_NOT_CHANGE\n", encoding="utf-8")
    unit_1 = _write_delivery_unit(
        git_project,
        "01-symlink.md",
        "# Replace scoped path in dependency\n\nReplace the bounded path for the dependent unit.\n",
    )
    unit_2 = _write_delivery_unit(
        git_project,
        "02-consumer.md",
        "# Consume assembled scoped path\n\nUpdate the bounded path from the assembled dependency tree.\n",
    )
    plan_id = "delivery-assembled-scope-smoke"
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": plan_id,
            "title": "Assembled scope smoke",
            "final_branch": f"sikula/delivery/{plan_id}",
            "units": [
                {
                    "id": "01-symlink",
                    "title": "Replace scoped path",
                    "task_path": unit_1,
                    "depends_on": [],
                    "scope_paths": ["src/alias"],
                },
                {
                    "id": "02-consumer",
                    "title": "Consume scoped path",
                    "task_path": unit_2,
                    "depends_on": ["01-symlink"],
                    "scope_paths": ["src/alias"],
                },
            ],
        },
    )
    _write_handoff_smoke_config(git_project)
    _git_commit_all(git_project, "add assembled scope fixture")
    fake = fake_llm()
    implementer_calls: list[Path] = []
    readonly_calls: list[Path] = []
    generate_calls: list[str] = []
    original_readonly = fake.run_readonly_agent
    original_generate = fake.generate

    def replace_scope_with_symlink(_prompt: str, cwd: Path) -> tuple[list[str], str]:
        implementer_calls.append(cwd)
        worktree_alias = cwd / "src" / "alias"
        (worktree_alias / ".keep").unlink()
        worktree_alias.rmdir()
        worktree_alias.symlink_to(external_root, target_is_directory=True)
        return ["src/alias"], ""

    def track_readonly(prompt: str, cwd: Path) -> str:
        readonly_calls.append(cwd)
        return original_readonly(prompt, cwd)

    def track_generate(system: str, user: str) -> str:
        generate_calls.append(user)
        return original_generate(system, user)

    fake.run_agent = replace_scope_with_symlink
    fake.run_readonly_agent = track_readonly
    fake.generate = track_generate
    relative_plan = plan_path.relative_to(git_project).as_posix()
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exc_info.value.code == 1
    assert payload["succeeded"] is False
    assert payload["selected_unit"]["id"] == "01-symlink"
    assert payload["selected_unit"]["failure_code"] == "unit_scope_violation"
    assert len(implementer_calls) == len(readonly_calls) == len(generate_calls) == 1
    assert str(git_project) not in output
    child_state = JsonStateStore(git_project / ".sikula" / "state").load(payload["child_task_id"])
    assert child_state is not None
    assert child_state.failed is True
    assert child_state.delivery_stop_code == "unit_scope_violation"
    assert child_state.review_cycle_records == []
    scope_audit = next(
        record for record in child_state.validation_cycle_records if record.get("phase") == "delivery_scope_audit"
    )
    assert scope_audit["status"] == "failed"
    assert scope_audit["metadata"]["code"] == "delivery_scope_audit_unavailable"
    child_worktree = Path(child_state.worktree_path or "")
    assert (child_worktree / "src" / "alias").is_symlink()
    assert (external_root / "sentinel.txt").read_text(encoding="utf-8") == "MUST_NOT_CHANGE\n"
    assert not delivery_unit_handoff_path(git_project, plan_id, "01-symlink").exists()
    assert not delivery_unit_handoff_path(git_project, plan_id, "02-consumer").exists()


def test_delivery_child_rejects_in_project_scope_alias_created_by_dependency(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    alias = git_project / "src" / "alias"
    alias.mkdir()
    (alias / ".keep").write_text("ordinary directory in operator checkout\n", encoding="utf-8")
    apps = git_project / "src" / "apps"
    apps.mkdir()
    (apps / "sentinel.txt").write_text("MUST_NOT_CHANGE\n", encoding="utf-8")
    unit_1 = _write_delivery_unit(
        git_project,
        "01-internal-alias.md",
        "# Replace path\n\nReplace the consumer path with an internal alias.\n",
    )
    unit_2 = _write_delivery_unit(
        git_project,
        "02-internal-consumer.md",
        "# Consume path\n\nUpdate only the declared consumer path.\n",
    )
    plan_id = "delivery-internal-scope-alias-smoke"
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": plan_id,
            "title": "Internal scope alias smoke",
            "final_branch": f"sikula/delivery/{plan_id}",
            "units": [
                {
                    "id": "01-alias",
                    "title": "Create internal alias",
                    "task_path": unit_1,
                    "depends_on": [],
                    "scope_paths": ["src"],
                },
                {
                    "id": "02-consumer",
                    "title": "Consume bounded path",
                    "task_path": unit_2,
                    "depends_on": ["01-alias"],
                    "scope_paths": ["src/alias"],
                },
            ],
        },
    )
    _write_handoff_smoke_config(git_project)
    _git_commit_all(git_project, "add internal scope alias fixture")
    fake = fake_llm()
    implementer_calls: list[Path] = []

    def replace_scope_with_internal_alias(_prompt: str, cwd: Path) -> tuple[list[str], str]:
        implementer_calls.append(cwd)
        worktree_alias = cwd / "src" / "alias"
        (worktree_alias / ".keep").unlink()
        worktree_alias.rmdir()
        worktree_alias.symlink_to("apps", target_is_directory=True)
        return ["src/alias"], ""

    fake.run_agent = replace_scope_with_internal_alias
    relative_plan = plan_path.relative_to(git_project).as_posix()
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            main()
        first_payload = json.loads(capsys.readouterr().out)
        assert first_payload["succeeded"] is True

        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert len(implementer_calls) == 1
    assert payload["selected_unit"]["id"] == "02-consumer"
    assert payload["selected_unit"]["failure_code"] == "unit_scope_violation"
    child_state = JsonStateStore(git_project / ".sikula" / "state").load(payload["child_task_id"])
    assert child_state is not None
    assert child_state.delivery_runtime_write_scope_binding == {
        "schema_version": 1,
        "status": "denied",
        "roots": [],
    }
    assert child_state.analyst_prompt is None
    assert (apps / "sentinel.txt").read_text(encoding="utf-8") == "MUST_NOT_CHANGE\n"


def test_delivery_run_next_scope_violation_stops_before_later_phases_and_blocks_reset(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_id = "delivery-scope-stop-smoke"
    plan_path, unit_id = _write_delivery_stop_fixture(
        git_project,
        plan_id=plan_id,
        constraint_kind="repository_ownership",
        constraint_summary="Production changes remain inside the declared unit scope.",
    )
    fake = fake_llm(
        agent_responses=[
            {
                "src/allowed/kept.py": "KEPT_IN_SCOPE = True\n",
                "src/escaped.py": "PRIVATE_OUT_OF_SCOPE_CONTENT = True\n",
            }
        ]
    )
    relative_plan = plan_path.relative_to(git_project).as_posix()
    progress_path = git_project / ".sikula" / "state" / "delivery" / plan_id / "progress.json"
    events_path = git_project / ".sikula" / "state" / "delivery" / plan_id / "events.jsonl"
    final_ref = f"refs/heads/sikula/delivery/{plan_id}"
    operator_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        first_output = capsys.readouterr().out
        first_payload = json.loads(first_output)
        assert exc_info.value.code == 1
        assert first_payload["ran"] is True
        assert first_payload["succeeded"] is False
        assert first_payload["unit_status"] == "failed"
        assert first_payload["errors"][0]["code"] == "delivery.unit_scope_violation"
        assert first_payload["selected_unit"]["id"] == unit_id
        assert first_payload["selected_unit"]["failure_code"] == "unit_scope_violation"
        assert first_payload["selected_unit"]["run_next_blocked_reason"] == "unit_scope_violation"
        assert first_payload["selected_unit"]["run_next_available"] is False
        assert "run_next_action" not in first_payload["selected_unit"]
        assert str(git_project) not in first_output
        assert "PRIVATE_OUT_OF_SCOPE_CONTENT" not in first_output

        child_state = JsonStateStore(git_project / ".sikula" / "state").load(first_payload["child_task_id"])
        assert child_state is not None
        assert child_state.delivery_stop_code == "unit_scope_violation"
        assert child_state.failed is True
        assert child_state.done is False
        assert child_state.result_commit is None
        assert child_state.delivery_dependency_handoffs == []
        assert child_state.review_cycle_records == []
        assert child_state.security_review_cycle_records == []
        assert child_state.test_write_records == []
        assert not {record.get("phase") for record in child_state.validation_cycle_records} & {"build", "test", "check"}
        scope_audit = next(
            record for record in child_state.validation_cycle_records if record.get("phase") == "delivery_scope_audit"
        )
        assert scope_audit["status"] == "failed"
        assert scope_audit["metadata"]["violation_paths"] == ["src/escaped.py"]
        assert scope_audit["metadata"]["effective_paths"] == ["src/allowed"]

        worktree = Path(child_state.worktree_path or "")
        assert worktree.is_dir()
        assert (worktree / "src" / "allowed" / "kept.py").is_file()
        assert (worktree / "src" / "escaped.py").is_file()
        assert not delivery_unit_handoff_path(git_project, plan_id, unit_id).exists()
        assert (
            subprocess.run(
                ["git", "rev-parse", final_ref],
                cwd=git_project,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == operator_head
        )
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", f"{final_ref}:src/escaped.py"],
                cwd=git_project,
                check=False,
            ).returncode
            != 0
        )

        with patch("sys.argv", ["sikula", "delivery", "status", relative_plan, "--json"]):
            main()

        status_payload = json.loads(capsys.readouterr().out)
        assert status_payload["status"] == "failed"
        assert status_payload["next_action"] == (
            "prepare a delivery amendment with delivery amend prepare before continuing"
        )
        assert status_payload["units"][0]["failure_code"] == "unit_scope_violation"

        with patch("sys.argv", ["sikula", "run", "--task-id", child_state.task_id]):
            with pytest.raises(SystemExit) as direct_run_exc:
                main()

        direct_run_output = capsys.readouterr().out
        assert direct_run_exc.value.code == 1
        assert "non-retryable terminal stop: unit_scope_violation" in direct_run_output
        assert "prepare a delivery amendment with delivery amend prepare before continuing" in direct_run_output
        assert "--reset-failed" not in direct_run_output

        with patch("sys.argv", ["sikula", "status", "--json"]):
            main()

        task_status = next(row for row in json.loads(capsys.readouterr().out) if row["id"] == child_state.task_id)
        assert task_status["next_action"] == (
            "prepare a delivery amendment with delivery amend prepare before continuing"
        )

        progress_before_reset = progress_path.read_bytes()
        events_before_reset = events_path.read_bytes()
        child_before_reset = (git_project / ".sikula" / "state" / f"{child_state.task_id}.json").read_bytes()
        with patch(
            "sys.argv",
            ["sikula", "delivery", "run-next", relative_plan, "--reset-failed", "--json"],
        ):
            with pytest.raises(SystemExit) as reset_exc:
                main()

    reset_payload = json.loads(capsys.readouterr().out)
    assert reset_exc.value.code == 1
    assert reset_payload["ran"] is False
    assert reset_payload["errors"][0]["code"] == "delivery.unit_scope_violation"
    assert progress_path.read_bytes() == progress_before_reset
    assert events_path.read_bytes() == events_before_reset
    assert (git_project / ".sikula" / "state" / f"{child_state.task_id}.json").read_bytes() == child_before_reset


def test_delivery_scope_audit_rejects_provider_git_ref_change(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_id = "delivery-provider-commit-scope-smoke"
    plan_path, unit_id = _write_delivery_stop_fixture(
        git_project,
        plan_id=plan_id,
        constraint_kind="repository_ownership",
        constraint_summary="Production changes remain inside the declared unit scope.",
    )
    fake = fake_llm()

    def commit_outside_scope(_prompt: str, cwd: Path) -> tuple[list[str], str]:
        escaped = cwd / "docs" / "provider-committed.md"
        escaped.parent.mkdir()
        escaped.write_text("committed outside scope\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "docs/provider-committed.md"],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "provider commit"],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        allowed = cwd / "src" / "allowed" / "dirty.py"
        allowed.parent.mkdir(parents=True, exist_ok=True)
        allowed.write_text("dirty in scope\n", encoding="utf-8")
        return ["src/allowed/dirty.py"], ""

    fake.run_agent = commit_outside_scope
    relative_plan = plan_path.relative_to(git_project).as_posix()
    final_ref = f"refs/heads/sikula/delivery/{plan_id}"
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["selected_unit"]["failure_code"] == "unit_scope_violation"
    child_state = JsonStateStore(git_project / ".sikula" / "state").load(payload["child_task_id"])
    assert child_state is not None
    assert child_state.result_commit is None
    assert not delivery_unit_handoff_path(git_project, plan_id, unit_id).exists()
    audit = next(
        record for record in child_state.validation_cycle_records if record.get("phase") == "delivery_scope_audit"
    )
    assert audit["metadata"] == {
        "code": "delivery_scope_audit_unavailable",
        "agent": "implementer",
    }
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{final_ref}:docs/provider-committed.md"],
            cwd=git_project,
            check=False,
        ).returncode
        != 0
    )


def test_delivery_scope_amendment_preserves_gitignored_paths_from_passed_audit(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_id = "delivery-passed-audit-evidence-smoke"
    plan_path, unit_id = _write_delivery_stop_fixture(
        git_project,
        plan_id=plan_id,
        constraint_kind="repository_ownership",
        constraint_summary="Production changes remain inside the declared unit scope.",
    )
    gitignore = git_project / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "src/allowed/ignored.env\n",
        encoding="utf-8",
    )
    _git_commit_all(git_project, "ignore in-scope runtime evidence")
    reviewer_stop = (
        "## Issues\n\n### Boundary finding\nFile: src/allowed/tracked.py\n"
        "Problem: The unit needs a broader declared scope.\n"
        "Fix: Prepare a delivery amendment.\n\n"
        + json.dumps(
            {
                "sikula_disposition_schema_version": 1,
                "disposition": "requires_scope_amendment",
                "summary": "The implementation needs a broader declared scope.",
            }
        )
    )
    fake = seq_fake_llm(readonly_responses=["APPROVED", reviewer_stop])

    def write_tracked_and_ignored(_prompt: str, cwd: Path) -> tuple[list[str], str]:
        tracked = cwd / "src" / "allowed" / "tracked.py"
        ignored = cwd / "src" / "allowed" / "ignored.env"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("TRACKED = True\n", encoding="utf-8")
        ignored.write_text("IGNORED_RUNTIME_VALUE=True\n", encoding="utf-8")
        return ["src/allowed/tracked.py"], ""

    fake.run_agent = write_tracked_and_ignored
    relative_plan = plan_path.relative_to(git_project).as_posix()
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["selected_unit"]["failure_code"] == "scope_amendment_required"
    child_state = JsonStateStore(git_project / ".sikula" / "state").load(payload["child_task_id"])
    assert child_state is not None
    scope_audit = next(
        record for record in child_state.validation_cycle_records if record.get("phase") == "delivery_scope_audit"
    )
    assert scope_audit["status"] == "passed"
    assert scope_audit["metadata"]["changed_paths"] == [
        "src/allowed/ignored.env",
        "src/allowed/tracked.py",
    ]
    assert child_state.files_changed == ["src/allowed/tracked.py"]

    target = inspect_delivery_amendment_target(plan_path, unit_id, project_root=git_project)
    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(git_project / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.changed_count == 2
    assert evidence.changed_paths == ("src/allowed/ignored.env", "src/allowed/tracked.py")
    assert evidence.omitted_changed_paths_count == 0


def test_nested_project_scope_amendment_preserves_sibling_violation_paths(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = git_project / "apps" / "service"
    project_root.mkdir(parents=True)
    for name in ("src", "tests_proj", "pyproject.toml"):
        (git_project / name).rename(project_root / name)
    plan_id = "delivery-nested-sibling-evidence-smoke"
    plan_path, unit_id = _write_delivery_stop_fixture(
        project_root,
        plan_id=plan_id,
        constraint_kind="repository_ownership",
        constraint_summary="Production changes remain inside the nested project boundary.",
    )
    fake = fake_llm()

    def write_inside_and_sibling(_prompt: str, cwd: Path) -> tuple[list[str], str]:
        kept = cwd / "src" / "allowed" / "kept.py"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text("KEPT_IN_PROJECT = True\n", encoding="utf-8")
        sibling = cwd.parents[1] / "shared" / "escaped.py"
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text("OUTSIDE_NESTED_PROJECT = True\n", encoding="utf-8")
        return ["src/allowed/kept.py"], ""

    fake.run_agent = write_inside_and_sibling
    relative_plan = plan_path.relative_to(project_root).as_posix()
    monkeypatch.chdir(project_root)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["selected_unit"]["failure_code"] == "unit_scope_violation"
    child_state = JsonStateStore(project_root / ".sikula" / "state").load(payload["child_task_id"])
    assert child_state is not None
    scope_audit = next(
        record for record in child_state.validation_cycle_records if record.get("phase") == "delivery_scope_audit"
    )
    assert scope_audit["metadata"]["outside_project_count"] == 1
    assert scope_audit["metadata"]["outside_project_paths"] == ["shared/escaped.py"]

    target = inspect_delivery_amendment_target(plan_path, unit_id, project_root=project_root)
    evidence = capture_delivery_amendment_failure_evidence(
        target,
        state_store=JsonStateStore(project_root / ".sikula" / "state"),
    )

    assert evidence is not None
    assert evidence.violation_count == 1
    assert evidence.violation_paths == ()
    assert evidence.outside_project_count == 1
    assert evidence.outside_project_paths == ("shared/escaped.py",)
    assert evidence.to_dict()["scope_violations"]["outside_project"]["paths"] == ["shared/escaped.py"]


def test_delivery_invalid_implementer_disposition_is_terminal_after_partial_write(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_id = "delivery-invalid-disposition-smoke"
    plan_path, unit_id = _write_delivery_stop_fixture(
        git_project,
        plan_id=plan_id,
        constraint_kind="repository_ownership",
        constraint_summary="Production changes remain inside the declared unit scope.",
    )
    fake = fake_llm()
    implementer_calls: list[Path] = []

    def malformed_implementer(_prompt: str, cwd: Path) -> tuple[list[str], str]:
        implementer_calls.append(cwd)
        partial = cwd / "src" / "allowed" / "partial.py"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text("PARTIAL_PRIVATE_CONTENT = True\n", encoding="utf-8")
        return ["src/allowed/partial.py"], json.dumps(
            {
                "sikula_disposition_schema_version": 1,
                "disposition": "external_dependency_gap",
            }
        )

    fake.run_agent = malformed_implementer
    relative_plan = plan_path.relative_to(git_project).as_posix()
    progress_path = git_project / ".sikula" / "state" / "delivery" / plan_id / "progress.json"
    events_path = git_project / ".sikula" / "state" / "delivery" / plan_id / "events.jsonl"
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        output = capsys.readouterr().out
        payload = json.loads(output)
        assert exc_info.value.code == 1
        assert payload["errors"][0]["code"] == "delivery.implementer_disposition_invalid"
        assert payload["selected_unit"]["id"] == unit_id
        assert payload["selected_unit"]["failure_code"] == "implementer_disposition_invalid"
        assert payload["selected_unit"]["run_next_available"] is False
        assert len(implementer_calls) == 1
        assert str(git_project) not in output
        assert "PARTIAL_PRIVATE_CONTENT" not in output

        child_state = JsonStateStore(git_project / ".sikula" / "state").load(payload["child_task_id"])
        assert child_state is not None
        assert child_state.failed is True
        assert child_state.done is False
        assert child_state.delivery_stop_code == "implementer_disposition_invalid"
        assert child_state.delivery_disposition_parse_error is not None
        assert child_state.delivery_disposition_parse_error["error_code"] == "delivery_disposition.keys_invalid"
        assert child_state.files_changed == ["src/allowed/partial.py"]
        assert child_state.result_commit is None
        assert child_state.review_cycle_records == []
        assert child_state.security_review_cycle_records == []
        assert child_state.test_write_records == []
        assert not {record.get("phase") for record in child_state.validation_cycle_records} & {
            "build",
            "test",
            "check",
        }
        assert not delivery_unit_handoff_path(git_project, plan_id, unit_id).exists()

        progress_before_reset = progress_path.read_bytes()
        events_before_reset = events_path.read_bytes()
        child_path = git_project / ".sikula" / "state" / f"{child_state.task_id}.json"
        child_before_reset = child_path.read_bytes()
        with patch(
            "sys.argv",
            ["sikula", "delivery", "run-next", relative_plan, "--reset-failed", "--json"],
        ):
            with pytest.raises(SystemExit) as reset_exc:
                main()

    reset_payload = json.loads(capsys.readouterr().out)
    assert reset_exc.value.code == 1
    assert reset_payload["ran"] is False
    assert reset_payload["errors"][0]["code"] == "delivery.implementer_disposition_invalid"
    assert len(implementer_calls) == 1
    assert progress_path.read_bytes() == progress_before_reset
    assert events_path.read_bytes() == events_before_reset
    assert child_path.read_bytes() == child_before_reset


def test_delivery_external_dependency_stop_projects_follow_up_without_proposal(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_id = "delivery-external-stop-smoke"
    plan_path, unit_id = _write_delivery_stop_fixture(
        git_project,
        plan_id=plan_id,
        constraint_kind="authoritative_read_only_dependency",
        constraint_summary="Protocol changes remain owned by the external protocol repository.",
    )
    fake = fake_llm(
        readonly_response=json.dumps(
            {
                "sikula_disposition_schema_version": 1,
                "disposition": "external_dependency_gap",
                "summary": "The required protocol change belongs to the external protocol repository.",
            }
        )
    )
    relative_plan = plan_path.relative_to(git_project).as_posix()
    progress_path = git_project / ".sikula" / "state" / "delivery" / plan_id / "progress.json"
    events_path = git_project / ".sikula" / "state" / "delivery" / plan_id / "events.jsonl"
    proposal_root = git_project / ".sikula" / "contract-reports" / "delivery-amendments" / plan_id
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", relative_plan, "--json"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        first_output = capsys.readouterr().out
        first_payload = json.loads(first_output)
        assert exc_info.value.code == 1
        assert first_payload["errors"][0]["code"] == "delivery.external_dependency_gap"
        assert first_payload["selected_unit"]["failure_code"] == "external_dependency_gap"
        assert first_payload["selected_unit"]["run_next_blocked_reason"] == "external_dependency_gap"
        assert first_payload["selected_unit"]["run_next_available"] is False
        assert str(git_project) not in first_output
        assert "PRIVATE SOURCE TASK BODY" not in first_output

        child_state = JsonStateStore(git_project / ".sikula" / "state").load(first_payload["child_task_id"])
        assert child_state is not None
        assert child_state.delivery_stop_code == "external_dependency_gap"
        assert child_state.delivery_stop_disposition is not None
        assert child_state.delivery_stop_disposition["source"] == "analyst"
        assert child_state.implementation_prompt is None
        assert child_state.implement_cycle_records == []
        assert child_state.review_cycle_records == []
        assert child_state.security_review_cycle_records == []
        assert child_state.test_write_records == []
        assert not {record.get("phase") for record in child_state.validation_cycle_records} & {"build", "test", "check"}
        assert not delivery_unit_handoff_path(git_project, plan_id, unit_id).exists()

        progress_before_reset = progress_path.read_bytes()
        events_before_reset = events_path.read_bytes()
        with patch(
            "sys.argv",
            ["sikula", "delivery", "run-next", relative_plan, "--reset-failed", "--json"],
        ):
            with pytest.raises(SystemExit) as reset_exc:
                main()

        reset_payload = json.loads(capsys.readouterr().out)
        assert reset_exc.value.code == 1
        assert reset_payload["ran"] is False
        assert reset_payload["errors"][0]["code"] == "delivery.external_dependency_gap"
        assert progress_path.read_bytes() == progress_before_reset
        assert events_path.read_bytes() == events_before_reset

        with patch(
            "sys.argv",
            [
                "sikula",
                "delivery",
                "amend",
                "prepare",
                relative_plan,
                "--split-unit",
                unit_id,
                "--json",
            ],
        ):
            with pytest.raises(SystemExit) as amend_exc:
                main()

    amend_output = capsys.readouterr().out
    amend_payload = json.loads(amend_output)
    assert amend_exc.value.code == 1
    assert amend_payload["prepared"] is False
    assert amend_payload["proposal_id"] is None
    assert amend_payload["proposal_path"] is None
    assert amend_payload["recommended_action"] == "external_dependency_follow_up"
    assert amend_payload["errors"][0]["code"] == "delivery_amend.external_dependency_follow_up_required"
    assert str(git_project) not in amend_output
    assert "PRIVATE SOURCE TASK BODY" not in amend_output
    assert not list(proposal_root.glob("*.json"))


def test_delivery_run_executes_and_finalizes_two_unit_plan(
    git_project: Path,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unit_1 = _write_delivery_unit(
        git_project,
        "01-run-foundation.md",
        "# Foundation\n\nAdd subtraction support to the calculator.\n",
    )
    unit_2 = _write_delivery_unit(
        git_project,
        "02-run-feature.md",
        "# Feature\n\nAdd multiplication support while preserving subtraction.\n",
    )
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-run-smoke",
            "title": "Delivery run smoke",
            "final_branch": "sikula/delivery/delivery-run-smoke",
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add subtraction",
                    "task_path": unit_1,
                    "depends_on": [],
                },
                {
                    "id": "02-feature",
                    "title": "Add multiplication",
                    "task_path": unit_2,
                    "depends_on": ["01-foundation"],
                },
            ],
        },
    )
    _write_handoff_smoke_config(git_project)
    _git_commit_all(git_project, "add delivery run smoke fixture")
    operator_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fake = fake_llm(
        agent_responses=[
            {"src/calculator.py": ("def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n")},
            {
                "src/calculator.py": (
                    "def add(a, b):\n"
                    "    return a + b\n\n"
                    "def subtract(a, b):\n"
                    "    return a - b\n\n"
                    "def multiply(a, b):\n"
                    "    return a * b\n"
                )
            },
        ]
    )
    relative_plan = plan_path.relative_to(git_project).as_posix()
    progress_path = git_project / ".sikula" / "state" / "delivery" / "delivery-run-smoke" / "progress.json"
    final_ref = "refs/heads/sikula/delivery/delivery-run-smoke"
    monkeypatch.chdir(git_project)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run", relative_plan, "--dry-run", "--json"]):
            main()

        preview = json.loads(capsys.readouterr().out)
        assert preview["ready"] is True
        assert preview["started"] is False
        assert preview["units_attempted"] == 0
        assert not progress_path.exists()
        assert (
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", final_ref],
                cwd=git_project,
                check=False,
            ).returncode
            != 0
        )

        with patch("sys.argv", ["sikula", "delivery", "run", relative_plan, "--json"]):
            main()

        payload = json.loads(capsys.readouterr().out)
        events_path = git_project / ".sikula" / "state" / "delivery" / "delivery-run-smoke" / "events.jsonl"
        first_events = events_path.read_text(encoding="utf-8").splitlines()

        with patch("sys.argv", ["sikula", "delivery", "run", relative_plan, "--json"]):
            main()

    repeated = json.loads(capsys.readouterr().out)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    final_commit = subprocess.run(
        ["git", "rev-parse", final_ref],
        cwd=git_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert payload["succeeded"] is True
    assert payload["completed"] is True
    assert payload["finalized"] is True
    assert payload["units_attempted"] == 2
    assert payload["units_succeeded"] == 2
    assert payload["stop_code"] == "delivery.run.completed"
    assert progress["final_commit"] == final_commit
    assert all(unit["status"] == "done" for unit in progress["units"])
    assert repeated["succeeded"] is True
    assert repeated["completed"] is True
    assert repeated["units_attempted"] == 0
    assert events_path.read_text(encoding="utf-8").splitlines() == first_events
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == operator_head
    )


def test_delivery_budget_split_applies_to_assembly_and_runs_replacement(
    git_project: Path,
    seq_fake_llm,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = git_project / "apps" / "service"
    project_root.mkdir(parents=True)
    for name in ("src", "tests_proj", "pyproject.toml"):
        (git_project / name).rename(project_root / name)
    unit_path = _write_delivery_unit(
        project_root,
        "01-foundation.md",
        "# Foundation\n\nImplement the requested foundation behavior.\n",
    )
    plan_path = _write_delivery_plan(
        project_root,
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
    config_path = project_root / ".sikula" / "config.yaml"
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
                "run_build": True,
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
    (project_root / ".gitignore").write_text(
        ".sikula/state/\n.sikula/worktrees/\n.sikula/contract-reports/\n",
        encoding="utf-8",
    )
    _git_commit_all(git_project, "add budget split smoke fixture")
    operator_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    authoring_fake = seq_fake_llm(
        generate_responses=[
            "1. Add the first independent behavior.\n"
            "2. Add the second independent behavior.\n"
            "3. Wire the final independent behavior.",
            "1. Add the first independent behavior.\n"
            "2. Add the second independent behavior.\n"
            "3. Wire the final independent behavior.",
            _budget_split_authoring_output(),
        ]
    )
    execution_fake = seq_fake_llm(
        agent_responses=[
            {"src/calculator.py": ("def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n")}
        ]
    )
    monkeypatch.chdir(project_root)

    with patch("core.llm_client.create_llm_client", return_value=authoring_fake):
        with patch(
            "sys.argv",
            [
                "sikula",
                "delivery",
                "run-next",
                plan_path.relative_to(project_root).as_posix(),
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
    assert (project_root / preparation["proposal_path"]).is_file()
    assert (project_root / preparation["audit_path"]).is_file()
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

    with patch("core.llm_client.create_llm_client", return_value=execution_fake):
        with patch(
            "sys.argv",
            [
                "sikula",
                "delivery",
                "amend",
                "apply",
                plan_path.relative_to(project_root).as_posix(),
                "--proposal",
                preparation["proposal_id"],
                "--json",
            ],
        ):
            main()

        apply_payload = json.loads(capsys.readouterr().out)
        assert apply_payload["applied"] is True
        assert apply_payload["replacement_ids"] == ["foundation-a", "foundation-b"]
        amendment_commit = subprocess.run(
            ["git", "rev-parse", "refs/heads/sikula/delivery/delivery-budget-split-smoke"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert amendment_commit != operator_head
        assert (
            subprocess.run(
                [
                    "git",
                    "cat-file",
                    "-e",
                    f"{amendment_commit}:apps/service/{preparation['proposal_path']}",
                ],
                cwd=git_project,
                capture_output=True,
            ).returncode
            != 0
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "cat-file",
                    "-e",
                    f"{amendment_commit}:apps/service/.sikula/delivery/demo/units/foundation-a.md",
                ],
                cwd=git_project,
                capture_output=True,
            ).returncode
            == 0
        )
        committed_plan = yaml.safe_load(
            subprocess.run(
                ["git", "show", f"{amendment_commit}:apps/service/.sikula/delivery/demo/plan.yaml"],
                cwd=git_project,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        assert committed_plan["units"][0]["superseded_by"] == ["foundation-a", "foundation-b"]

        with patch(
            "sys.argv",
            ["sikula", "delivery", "run-next", plan_path.relative_to(project_root).as_posix(), "--json"],
        ):
            main()

    replacement_payload = json.loads(capsys.readouterr().out)
    replacement_state = JsonStateStore(project_root / ".sikula" / "state").load(replacement_payload["child_task_id"])
    assert replacement_payload["succeeded"] is True
    assert replacement_payload["selected_unit"]["id"] == "foundation-a"
    assert replacement_state is not None
    assert replacement_state.result_commit
    assert (
        subprocess.run(
            ["git", "rev-parse", f"{replacement_state.result_commit}^"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == amendment_commit
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == operator_head
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


def test_delivery_check_json_does_not_project_verbatim_source_constraint(
    git_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_rule = "Only the protocol repository may change protocol files."
    source_text = f"# Source task\n\n- {source_rule}\n"
    source_path = git_project / ".sikula" / "tasks" / "private-source.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source_text, encoding="utf-8")
    unit = _write_delivery_unit(git_project, "01-foundation.md", "# Unit 01\n\nAdd foundation.\n")
    plan_path = _write_delivery_plan(
        git_project,
        {
            "schema_version": 1,
            "plan_id": "delivery-private-constraint",
            "title": "Delivery private constraint",
            "final_branch": "sikula/delivery/private-constraint",
            "source_task": {
                "path": source_path.relative_to(git_project).as_posix(),
                "sha256": "sha256:" + sha256(source_text.encode("utf-8")).hexdigest(),
            },
            "constraints": [
                {
                    "id": "protocol-authority",
                    "kind": "repository_ownership",
                    "summary": source_rule,
                    "unit_ids": ["01-foundation"],
                    "disposition": "preserved",
                }
            ],
            "units": [
                {
                    "id": "01-foundation",
                    "title": "Add foundation",
                    "task_path": unit,
                    "depends_on": [],
                }
            ],
        },
        name="private-constraint-plan.yaml",
    )
    monkeypatch.chdir(git_project)

    with patch(
        "sys.argv",
        ["sikula", "delivery", "check", plan_path.relative_to(git_project).as_posix(), "--json"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            main()

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exc_info.value.code == 1
    assert payload["valid"] is False
    assert any(issue["code"] == "constraints.summary_source_excerpt" for issue in payload["errors"])
    assert source_rule not in payload_text
