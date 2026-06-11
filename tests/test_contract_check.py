from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core.contract_check import check_contract, check_contract_file, render_contract_check, write_contract_report
from sikula import main


def _python_project_config(tmp_path: Path) -> dict:
    return {
        "project": {"build_tool": "python", "root_path": str(tmp_path)},
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "build": {"checks": [{"name": "ruff", "command": "ruff check ."}]},
    }


def test_weak_security_sensitive_task_reports_blocking_gaps():
    result = check_contract("# Add team invites\n\nUsers should be able to invite teammates by email.")

    assert result.status == "not_ready"
    assert not result.ready_for_autonomous_delivery
    assert result.readiness_score < 40
    gap_ids = {gap.id for gap in result.gaps}
    assert "gap.acceptance.criteria" in gap_ids
    assert "gap.security_privacy.impact" in gap_ids
    assert "gap.validation.commands" in gap_ids
    question_ids = {question.id for question in result.clarifying_questions}
    assert "acceptance.criteria" in question_ids
    assert "token.lifecycle" in question_ids
    assert "privacy.data_handling" in question_ids


def test_strong_task_is_ready_when_validation_is_covered(tmp_path: Path):
    task = """# Team invites

## Scope
- Add invite creation endpoint.
- Add invite acceptance endpoint.
- Add pending invite model.

## Acceptance criteria
- Owner/admin can invite a user by email.
- Non-admin users cannot invite users.
- Duplicate pending invite returns a deterministic error.
- Expired invite token cannot be accepted.
- Accepted invite token cannot be reused.

## Security and privacy
- Invite tokens must be unguessable.
- Invite tokens must not be logged.
- Error messages must not reveal whether an email already has an account.

## Out of scope
- Billing seat enforcement.
- Bulk invites.
- Full team settings redesign.

## Tests
- Permission tests for allowed and denied inviter roles.
- Token lifecycle tests for expired and reused tokens.
- Duplicate invite test.

## Validation
- `pytest`
- `ruff check .`

## Reviewer focus
- Authorization rules.
- Token expiry and reuse.
- Email enumeration behaviour.
"""

    result = check_contract(task, source_path="task.md", project_config=_python_project_config(tmp_path))

    assert result.status == "ready"
    assert result.ready_for_autonomous_delivery
    assert result.readiness_score >= 85
    assert result.gaps == []
    assert result.validation["coverage_gaps"] == []
    assert result.sections_detected["acceptance_criteria"]
    assert result.sections_detected["security_privacy"]
    assert result.sections_detected["validation"]


def test_json_output_schema_is_stable(tmp_path: Path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "# Add search\n\n## Acceptance criteria\n- Search filters countries by name.\n", encoding="utf-8"
    )

    result = check_contract_file(task_path)
    data = result.to_dict()

    assert data["schema_version"] == 1
    assert data["source"]["path"] == str(task_path)
    assert data["source"]["format"] == "markdown"
    assert data["source"]["sha256"].startswith("sha256:")
    assert isinstance(data["readiness_score"], int)
    assert data["status"] in {"not_ready", "weak", "warn", "ready"}
    assert isinstance(data["sections_detected"], dict)
    assert isinstance(data["scores"], dict)
    assert isinstance(data["gaps"], list)
    assert isinstance(data["clarifying_questions"], list)
    assert isinstance(data["suggested_sections"], list)
    assert isinstance(data["validation"], dict)


def test_txt_task_uses_text_format_and_colon_section_headings(tmp_path: Path):
    task_path = tmp_path / "task.txt"
    task_path.write_text(
        """Add country search

Scope:
- Add search by country name.
- Keep existing region filtering.
- Keep sorting unchanged.

Acceptance criteria:
- Matching is case-insensitive.
- Clearing search shows the full list.
- No matching countries shows an empty state.

Out of scope:
- Do not add server-side search.
- Do not change sorting.
""",
        encoding="utf-8",
    )

    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))

    assert result.source["format"] == "text"
    assert result.sections_detected["scope"]
    assert result.sections_detected["acceptance_criteria"]
    assert result.sections_detected["out_of_scope"]
    assert all(gap.id != "gap.scope.boundaries" for gap in result.gaps)
    assert all(gap.id != "gap.acceptance.criteria" for gap in result.gaps)


def test_question_ids_are_stable_for_same_task():
    task = "# Add team invites\n\nUsers should be able to invite teammates by email."

    first = [question.id for question in check_contract(task).clarifying_questions]
    second = [question.id for question in check_contract(task).clarifying_questions]

    assert first == second
    assert first


def test_gap_and_question_ids_are_unique_within_result():
    task = """# Add team invites

Users should be able to invite teammates by email.
"""

    result = check_contract(task)
    gap_ids = [gap.id for gap in result.gaps]
    question_ids = [question.id for question in result.clarifying_questions]

    assert len(gap_ids) == len(set(gap_ids))
    assert len(question_ids) == len(set(question_ids))


def test_validation_coverage_gap_uses_configured_pipeline(tmp_path: Path):
    task = """# Update generator

## Scope
- Update generated fixture support.
- Keep existing parser behaviour.

## Acceptance criteria
- Existing fixture generation keeps working.
- Invalid fixture input is rejected.

## Validation
- `cargo test --workspace`
"""

    result = check_contract(task, project_config=_python_project_config(tmp_path))

    assert result.validation["task_commands"] == ["cargo test --workspace"]
    assert result.validation["coverage_gaps"] == ["cargo test --workspace"]
    assert any(gap.id == "gap.validation.coverage" for gap in result.gaps)
    assert not result.ready_for_autonomous_delivery


def test_missing_task_validation_is_not_gap_when_pipeline_is_configured(tmp_path: Path):
    task = """# Add country search

## Goal
Users should be able to filter countries by name.

## Desired behaviour
- Typing a search term filters countries by name.
- Matching is case-insensitive.
- Clearing the field shows the full list again.

## Out of scope
- Do not add server-side search.
- Do not change sorting.
"""

    result = check_contract(task, project_config=_python_project_config(tmp_path))

    assert result.validation["task_commands"] == []
    assert result.validation["configured_commands"]
    assert all(gap.id != "gap.validation.commands" for gap in result.gaps)
    assert "Configured Sikula validation pipeline is available." in result.strong_signals


def test_human_renderer_groups_gaps_and_questions():
    result = check_contract("# Add team invites\n\nUsers should be able to invite teammates by email.")

    rendered = render_contract_check(result)

    assert "Implementation Contract Readiness:" in rendered
    assert "Blocking gaps:" in rendered
    assert "Follow-up questions:" in rendered
    assert "[acceptance.criteria]" in rendered


def test_contract_check_cli_json_without_project_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "# Add search\n\n## Acceptance criteria\n- Search filters countries by name.\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    with patch("sys.argv", ["sikula", "contract", "check", str(task_path), "--json"]):
        main()

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert data["source"]["path"] == str(task_path)
    assert not (tmp_path / ".sikula" / "contracts").exists()


def test_write_report_creates_check_json_and_answers_template(tmp_path: Path):
    task_dir = tmp_path / ".sikula" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "team-invites.md"
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))

    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)

    assert written.report_path == tmp_path / ".sikula" / "contracts" / "team-invites.check.json"
    assert written.answers_path == tmp_path / ".sikula" / "contracts" / "team-invites.answers.yaml"
    report = json.loads(written.report_path.read_text(encoding="utf-8"))
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    assert report["generated_by"] == "sikula.contract_check"
    assert report["checked_at"].endswith("Z")
    assert report["source"]["path"] == ".sikula/tasks/team-invites.md"
    assert report["source"]["sha256"] == result.source["sha256"]
    assert answers["generated_by"] == "sikula.contract_check"
    assert answers["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert answers["task"]["sha256"] == result.source["sha256"]
    assert answers["check_report"] == ".sikula/contracts/team-invites.check.json"
    assert set(answers["answers"]) == {question.id for question in result.clarifying_questions}


def test_write_report_uses_empty_answers_mapping_when_no_questions(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "ready.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        """# Team invites

## Scope
- Add invite creation endpoint.
- Add invite acceptance endpoint.
- Add pending invite model.

## Acceptance criteria
- Owner/admin can invite a user by email.
- Non-admin users cannot invite users.
- Duplicate pending invite returns a deterministic error.
- Expired invite token cannot be accepted.
- Accepted invite token cannot be reused.

## Security and privacy
- Invite tokens must be unguessable.
- Invite tokens must not be logged.
- Error messages must not reveal whether an email already has an account.

## Out of scope
- Billing seat enforcement.
- Bulk invites.
- Full team settings redesign.

## Tests
- Permission tests for allowed and denied inviter roles.
- Token lifecycle tests for expired and reused tokens.
- Duplicate invite test.

## Validation
- `pytest`
- `ruff check .`

## Reviewer focus
- Authorization rules.
- Token expiry and reuse.
- Email enumeration behaviour.
""",
        encoding="utf-8",
    )
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    assert result.clarifying_questions == []

    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)

    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    assert answers["questions"] == []
    assert answers["answers"] == {}


def test_write_report_keeps_same_stem_tasks_distinct(tmp_path: Path):
    first = tmp_path / "a" / "task.md"
    second = tmp_path / "b" / "task.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("# Add search\n\n## Acceptance criteria\n- Search filters countries by name.\n", encoding="utf-8")
    second.write_text("# Add sort\n\n## Acceptance criteria\n- Sort countries by name.\n", encoding="utf-8")

    first_written = write_contract_report(check_contract_file(first), task_path=first, project_root=tmp_path)
    second_written = write_contract_report(check_contract_file(second), task_path=second, project_root=tmp_path)
    second_repeat = write_contract_report(check_contract_file(second), task_path=second, project_root=tmp_path)

    assert first_written.report_path.name == "task.check.json"
    assert second_written.report_path.name.startswith("task-")
    assert second_written.report_path.name.endswith(".check.json")
    assert second_written.report_path != first_written.report_path
    assert second_repeat.report_path == second_written.report_path


def test_write_report_does_not_overwrite_non_generated_files(tmp_path: Path):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "# Add search\n\n## Acceptance criteria\n- Search filters countries by name.\n", encoding="utf-8"
    )
    contracts = tmp_path / ".sikula" / "contracts"
    contracts.mkdir(parents=True)
    manual_report = contracts / "task.check.json"
    manual_report.write_text("manual report\n", encoding="utf-8")

    written = write_contract_report(check_contract_file(task_path), task_path=task_path, project_root=tmp_path)

    assert manual_report.read_text(encoding="utf-8") == "manual report\n"
    assert written.report_path != manual_report
    assert written.report_path.name.startswith("task-")


def test_write_report_preserves_existing_answers_for_same_task_hash(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "task.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["acceptance.criteria"]["answer"] = "Owners can send invites and members cannot."
    written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    repeated = write_contract_report(result, task_path=task_path, project_root=tmp_path)

    answers = yaml.safe_load(repeated.answers_path.read_text(encoding="utf-8"))
    assert repeated.answers_path == written.answers_path
    assert answers["task"]["sha256"] == result.source["sha256"]
    assert answers["answers"]["acceptance.criteria"]["answer"] == "Owners can send invites and members cannot."
    assert "previous_answers" not in answers


def test_write_report_archives_existing_answers_when_task_hash_changes(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "task.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    first_result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    first_written = write_contract_report(first_result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(first_written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["acceptance.criteria"]["answer"] = "Owners can send invites and members cannot."
    first_written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")
    task_path.write_text(
        "# Add team invites\n\nUsers should be able to invite teammates by email. Tokens expire after 24 hours.",
        encoding="utf-8",
    )
    second_result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    second_written = write_contract_report(second_result, task_path=task_path, project_root=tmp_path)

    report = json.loads(second_written.report_path.read_text(encoding="utf-8"))
    answers = yaml.safe_load(second_written.answers_path.read_text(encoding="utf-8"))
    assert second_written.report_path == first_written.report_path
    assert report["source"]["sha256"] == second_result.source["sha256"]
    assert report["source"]["sha256"] != first_result.source["sha256"]
    assert answers["task"]["sha256"] == second_result.source["sha256"]
    assert answers["answers"]["acceptance.criteria"]["answer"] == ""
    assert answers["previous_answers"][0]["task"]["sha256"] == first_result.source["sha256"]
    assert (
        answers["previous_answers"][0]["answers"]["acceptance.criteria"]["answer"]
        == "Owners can send invites and members cannot."
    )

    repeated = write_contract_report(second_result, task_path=task_path, project_root=tmp_path)
    repeated_answers = yaml.safe_load(repeated.answers_path.read_text(encoding="utf-8"))
    assert len(repeated_answers["previous_answers"]) == 1
    assert repeated_answers["answers"]["acceptance.criteria"]["answer"] == ""


def test_contract_check_cli_json_write_report_stays_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "# Add search\n\n## Acceptance criteria\n- Search filters countries by name.\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    with patch("sys.argv", ["sikula", "contract", "check", str(task_path), "--write-report", "--json"]):
        main()

    out = capsys.readouterr().out
    data = json.loads(out)
    assert "written_report" in data
    assert Path(data["written_report"]["check_report"]).exists()
    assert Path(data["written_report"]["answers_template"]).exists()


def test_contract_check_cli_write_report_prints_generated_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / "task.md"
    task_path.write_text(
        "# Add search\n\n## Acceptance criteria\n- Search filters countries by name.\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    with patch("sys.argv", ["sikula", "contract", "check", str(task_path), "--write-report"]):
        main()

    out = capsys.readouterr().out
    assert "Generated contract artifacts:" in out
    assert ".check.json" in out
    assert ".answers.yaml" in out


def test_contract_namespace_help_does_not_require_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.chdir(tmp_path)

    with patch("sys.argv", ["sikula", "contract"]):
        with pytest.raises(SystemExit) as exc:
            main()

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "usage:" in out
    assert "No config found" not in out
