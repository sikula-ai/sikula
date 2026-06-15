from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core.contract_check import (
    check_contract,
    check_contract_file,
    improve_contract_text,
    improve_contract_from_answers,
    prepare_contract,
    render_contract_check,
    write_contract_report,
)
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


def test_check_contract_ignores_generated_open_questions_for_readiness():
    task = "# Add team invites\n\nUsers should be able to invite teammates by email.\n"
    generated_open_questions = (
        task
        + "\n## Open questions\n\n"
        + "<!-- sikula:generated-open-questions -->\n\n"
        + "- What observable behaviours must be true when this task is complete?\n"
        + "  - Why it matters: Acceptance criteria are the contract used by implementer, reviewer, and test writer.\n"
        + "  - Blocks delivery: yes\n"
    )

    base = check_contract(task)
    generated = check_contract(generated_open_questions)

    assert generated.source["sha256"] == "sha256:" + sha256(generated_open_questions.encode("utf-8")).hexdigest()
    assert generated.readiness_score == base.readiness_score
    assert [gap.id for gap in generated.gaps] == [gap.id for gap in base.gaps]
    assert [question.id for question in generated.clarifying_questions] == [
        question.id for question in base.clarifying_questions
    ]


def test_prepare_contract_returns_questions_without_file_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)

    result = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
    )

    assert result.stage == "needs_user_input"
    assert result.needs_user_input
    assert not result.ready_to_save
    assert not result.ready_to_run
    assert result.required_next_step == "answer_questions"
    assert "acceptance.criteria" in result.answers_template
    assert "token.lifecycle" in result.answers_template
    assert result.safe_task_path == ".sikula/tasks/team-invites.md"
    assert result.required_user_action == "answer_contract_questions"
    assert result.primary_user_action == "answer_contract_questions"
    assert result.user_questions
    assert result.open_question_ids == [question.id for question in result.questions_for_user]
    assert result.resume_arguments["contract_markdown"].startswith("# Add team invites")
    assert result.resume_arguments["status_applies_to_sha256"] == result.status_applies_to_sha256
    assert "sikula run" not in "\n".join(result.suggested_next_steps)
    assert "ready_to_run" in result.assistant_response_markdown
    assert result.recheck_result is None
    assert result.status_applies_to_sha256 == result.check_result.source["sha256"]
    assert not (tmp_path / ".sikula").exists()


def test_prepare_contract_checks_normalized_output_without_answers():
    task = "# Add team invites\n\nUsers should be able to invite teammates by email."

    result = prepare_contract(task, contract_name="team-invites.md")

    expected_markdown = task + "\n"
    expected_sha = "sha256:" + sha256(expected_markdown.encode("utf-8")).hexdigest()
    assert result.prepared_contract_markdown == expected_markdown
    assert result.authoritative_output_markdown == expected_markdown
    assert result.resume_arguments["contract_markdown"] == expected_markdown
    assert result.status_applies_to_sha256 == expected_sha
    assert result.check_result.source["sha256"] == expected_sha
    assert result.resume_arguments["status_applies_to_sha256"] == expected_sha


def test_prepare_contract_uses_project_context_validation_commands():
    task = """# Add search

## Scope
- Add search by country name.
- Keep existing filtering.
- Keep sorting unchanged.

## Acceptance criteria
- Search is case-insensitive.
- Clearing search shows all countries.
- No results shows an empty state.

## Out of scope
- Do not add server-side search.

## Validation
- `npm test`
"""

    result = prepare_contract(
        task,
        contract_name="search.md",
        project_context={"validation_commands": ["npm test"]},
    )

    assert result.check_result.validation["coverage_gaps"] == []
    assert result.check_result.validation["configured_commands"] == [
        {"phase": "project_context", "name": "validation-1", "command": "npm test"}
    ]
    assert all(gap.id != "gap.validation.coverage" for gap in result.unresolved_gaps)


def test_prepare_contract_applies_answers_and_rechecks():
    result = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={
            "scope.boundaries": "Add team invite creation and acceptance endpoints.",
            "acceptance.criteria": "Owners can invite teammates by email.\nMembers cannot invite teammates.",
            "scope.out_of_scope": "Billing and bulk invites are out of scope.",
        },
        project_context={"validation_commands": ["pytest"]},
    )

    assert result.recheck_result is not None
    assert "scope.boundaries" in result.answered_question_ids
    assert "acceptance.criteria" in result.answered_question_ids
    assert "## Scope" in result.prepared_contract_markdown
    assert "- Add team invite creation and acceptance endpoints." in result.prepared_contract_markdown
    assert result.recheck_result.readiness_score > result.check_result.readiness_score
    assert result.status_applies_to_sha256 == result.recheck_result.source["sha256"]
    assert result.required_next_step in {
        "answer_questions",
        "save_contract",
        "save_and_run_contract",
        "revise_contract",
    }


def test_prepare_contract_reconciles_open_questions_after_recheck():
    result = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={
            "scope.boundaries": "Add invite creation and acceptance endpoints.",
            "acceptance.criteria": (
                "Owners can invite teammates by email. Duplicate invites return a deterministic error. "
                "Expired invite tokens cannot be accepted. Reused invite tokens cannot be accepted."
            ),
            "scope.out_of_scope": "Billing and bulk invites are out of scope.",
            "token.lifecycle": "Invite tokens expire after 24 hours and cannot be reused.",
            "privacy.data_handling": "Do not log invite tokens or reveal whether an email already has an account.",
            "reviewer.focus": "Authorization, duplicate invite handling, and token lifecycle.",
            "context.domain_rules": "Follow existing team membership service patterns.",
        },
        project_context={"validation_commands": ["pytest"]},
    )

    assert not result.needs_user_input
    assert result.ready_to_save
    assert result.open_question_ids == []
    assert result.questions_for_user == []
    assert "## Open questions" not in result.prepared_contract_markdown
    assert result.status_applies_to_sha256 == result.recheck_result.source["sha256"]


def test_prepare_contract_keeps_current_open_questions_after_final_recheck():
    result = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={
            "scope.boundaries": "Add invite creation and acceptance endpoints.",
            "scope.out_of_scope": "Billing changes are out of scope.",
        },
        project_context={"validation_commands": ["pytest"]},
    )

    assert result.needs_user_input
    assert "acceptance.criteria" in result.open_question_ids
    assert "## Open questions" in result.prepared_contract_markdown
    assert "<!-- sikula:generated-open-questions -->" in result.prepared_contract_markdown
    assert "What observable behaviours must be true when this task is complete?" in result.prepared_contract_markdown
    assert result.status_applies_to_sha256 == result.recheck_result.source["sha256"]


def test_prepare_contract_marks_repeated_questions_as_revised_answer_needed():
    result = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"acceptance.negative_cases": "ok"},
        project_context={"validation_commands": ["pytest"]},
    )

    assert result.needs_user_input
    assert "acceptance.negative_cases" in result.answered_question_ids
    assert {question.id for question in result.questions_for_user}.issubset(set(result.open_question_ids))
    assert "acceptance.negative_cases" in result.revised_answer_question_ids
    question = next(question for question in result.user_questions if question["id"] == "acceptance.negative_cases")
    assert question["requires_revised_answer"] is True
    assert "more specific answer" in question["reason"]
    assert result.answers_template["acceptance.negative_cases"]["requires_revised_answer"] is True
    assert result.required_user_action == "answer_contract_questions"


def test_prepare_contract_strips_stale_open_questions_between_answer_rounds():
    first = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"scope.boundaries": "Add invite creation and acceptance endpoints."},
        project_context={"validation_commands": ["pytest"]},
    )
    stale_question = next(
        question.question for question in first.questions_for_user if question.id == "acceptance.negative_cases"
    )
    assert "## Open questions" in first.prepared_contract_markdown
    assert stale_question in first.prepared_contract_markdown

    second = prepare_contract(
        first.resume_arguments["contract_markdown"],
        contract_name="team-invites.md",
        answers={"acceptance.negative_cases": "Duplicate invites return a deterministic error."},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "- Duplicate invites return a deterministic error." in second.prepared_contract_markdown
    assert stale_question not in second.prepared_contract_markdown


def test_prepare_contract_replaces_revised_generated_answers():
    first = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"acceptance.negative_cases": "ok"},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "acceptance.negative_cases" in first.revised_answer_question_ids
    assert "- ok" in first.prepared_contract_markdown

    second = prepare_contract(
        first.resume_arguments["contract_markdown"],
        contract_name="team-invites.md",
        answers={
            "acceptance.negative_cases": (
                "Duplicate pending invites return a deterministic error. Empty emails are rejected."
            )
        },
        project_context={"validation_commands": ["pytest"]},
    )

    assert "## Acceptance criteria" in second.prepared_contract_markdown
    assert second.prepared_contract_markdown.count("## Acceptance criteria") == 1
    assert "- Duplicate pending invites return a deterministic error. Empty emails are rejected." in (
        second.prepared_contract_markdown
    )
    assert "- ok" not in second.prepared_contract_markdown


def test_prepare_contract_preserves_human_open_questions_section():
    task = """# Add team invites

Users should be able to invite teammates by email.

## Open questions

- Confirm the invite email copy with product.
"""

    result = prepare_contract(
        task,
        contract_name="team-invites.md",
        answers={"scope.boundaries": "Add invite creation and acceptance endpoints."},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "## Open questions" in result.prepared_contract_markdown
    assert "- Confirm the invite email copy with product." in result.prepared_contract_markdown


def test_prepare_contract_resume_accepts_accumulated_answers():
    first = prepare_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"scope.boundaries": "Add invite creation and acceptance endpoints."},
        project_context={"validation_commands": ["pytest"]},
    )

    second = prepare_contract(
        first.resume_arguments["contract_markdown"],
        contract_name="team-invites.md",
        answers={
            "scope.boundaries": "Add invite creation and acceptance endpoints.",
            "acceptance.negative_cases": "Duplicate invites return a deterministic error.",
        },
        project_context={"validation_commands": ["pytest"]},
    )

    assert "- Add invite creation and acceptance endpoints." in second.prepared_contract_markdown
    assert "- Duplicate invites return a deterministic error." in second.prepared_contract_markdown
    assert "acceptance.negative_cases" in second.answered_question_ids


def test_prepare_contract_ready_result_includes_safe_save_and_run_guidance():
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

    result = prepare_contract(
        task,
        contract_name="../../Team Invites; rm -rf *.md",
        project_context={
            "sikula_configured": True,
            "validation_commands": ["pytest", "ruff check ."],
        },
    )

    assert result.ready_to_save
    assert result.ready_to_run
    assert result.required_next_step == "save_and_run_contract"
    assert result.safe_task_path == ".sikula/tasks/team-invites-rm-rf.md"
    assert result.suggested_next_steps == [
        "Save the prepared contract to `.sikula/tasks/team-invites-rm-rf.md`.",
        "Run `sikula run .sikula/tasks/team-invites-rm-rf.md`.",
    ]
    assert result.resume_arguments["project_context"] == {
        "sikula_configured": True,
        "validation_commands": ["pytest", "ruff check ."],
    }
    assert result.to_dict()["authoritative_output_markdown"] == result.prepared_contract_markdown


def test_prepare_contract_ready_without_config_does_not_suggest_run():
    task = """# Country search

## Scope
- Add search by country name.
- Keep existing region filtering.
- Keep sorting unchanged.

## Acceptance criteria
- Matching is case-insensitive.
- Clearing search shows the full list.
- No matching countries shows an empty state.
- Existing region filters still apply.

## Out of scope
- Do not add server-side search.
- Do not change sorting.
- Do not change country details.

## Tests
- Search matching test.
- Empty state test.
- Filter interaction test.

## Validation
- `npm test`

## Reviewer focus
- Search/filter interaction.
"""

    result = prepare_contract(
        task,
        contract_name="Country Search",
        project_context={"validation_commands": ["npm test"]},
    )

    assert result.ready_to_run
    assert result.required_next_step == "save_and_run_contract"
    assert any("sikula init" in step for step in result.suggested_next_steps)
    assert all("sikula run" not in step for step in result.suggested_next_steps)


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


def test_suggested_sections_are_specific_to_gap_ids(tmp_path: Path):
    task = """# Add team invites

Users should be able to invite teammates by email.

## Scope
- Add team invite creation.

## Acceptance criteria
- Owners can invite users by email.
- Invites appear in the pending invite list.

## Out of scope
- Billing seat enforcement.

## Security and privacy
- Invite token expiry follows the existing secure token policy.

## Reviewer focus
- Authorization rules.
"""

    result = check_contract(task, project_config=_python_project_config(tmp_path))

    assert "Acceptance criteria: add negative, edge-case, or rejection behaviour" in result.suggested_sections
    assert "Acceptance criteria" not in result.suggested_sections
    assert "Context: name affected files, APIs, domain rules, or project conventions" in result.suggested_sections


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


def test_improve_contract_from_answers_writes_markdown_and_rechecks(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["scope.boundaries"]["answer"] = (
        "Add team invite creation and acceptance endpoints. Keep existing membership role names unchanged."
    )
    answers["answers"]["scope.boundaries"]["notes"] = "Keep the existing team settings navigation unchanged."
    answers["answers"]["acceptance.criteria"]["answer"] = (
        "Owner and admin users can send invites by email.\n"
        "Members cannot send invites.\n"
        "Duplicate pending invites return a deterministic error."
    )
    answers["answers"]["token.lifecycle"]["answer"] = "Invite tokens expire after 24 hours and cannot be reused."
    answers["answers"]["privacy.data_handling"]["answer"] = (
        "Invite tokens are never logged and errors do not reveal whether an email has an account."
    )
    answers["answers"]["scope.out_of_scope"]["answer"] = "Billing changes and bulk invites are out of scope."
    answers["answers"]["reviewer.focus"]["answer"] = "Authorization checks and token lifecycle handling."
    answers["answers"]["context.domain_rules"]["answer"] = "Follow the existing TeamService invite patterns."
    answers["answers"]["acceptance.negative_cases"]["notes"] = (
        "Product owner still needs to decide empty email handling."
    )
    answers["questions"].append(
        {
            "id": "custom.detail",
            "question": "What extra delivery detail should be preserved?",
            "why_it_matters": "Future contract checks may add new question IDs.",
            "blocks_delivery": False,
            "answer_type": "text",
        }
    )
    answers["answers"]["custom.detail"] = {
        "answer": "Keep the existing invite email copy unchanged.",
        "notes": "Custom note.",
    }
    written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    improved = improve_contract_from_answers(
        task_path,
        answers_path=written.answers_path,
        output_path=output_path,
        project_config=_python_project_config(tmp_path),
    )

    output = output_path.read_text(encoding="utf-8")
    assert improved.output_path == output_path
    assert improved.source_sha256 == result.source["sha256"]
    assert "scope.boundaries" in improved.answered_question_ids
    assert "acceptance.criteria" in improved.answered_question_ids
    assert "acceptance.negative_cases" not in improved.open_question_ids
    assert "## Scope" in output
    assert (
        "- Add team invite creation and acceptance endpoints. Keep existing membership role names unchanged." in output
    )
    assert "## Acceptance criteria" in output
    assert "- Owner and admin users can send invites by email." in output
    assert "- Members cannot send invites." in output
    assert "## Security and privacy" in output
    assert "- Invite tokens expire after 24 hours and cannot be reused." in output
    assert "## Out of scope" in output
    assert "- Billing changes and bulk invites are out of scope." in output
    assert "## Reviewer focus" in output
    assert "- Authorization checks and token lifecycle handling." in output
    assert "## Context" in output
    assert "- Follow the existing TeamService invite patterns." in output
    assert "## Clarifications" in output
    assert "- Keep the existing invite email copy unchanged." in output
    assert "## Open questions" not in output
    assert "acceptance.negative_cases" not in output
    assert "- Clarification `" not in output
    assert "Source question" not in output
    assert "`scope.boundaries`" not in output
    assert "## Notes" in output
    assert "- Scope: Keep the existing team settings navigation unchanged." in output
    assert "- Clarifications: Custom note." in output
    assert improved.check_result.source["path"] == str(output_path)
    assert improved.check_result.readiness_score > result.readiness_score


def test_in_memory_improve_matches_file_based_improve(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers_data = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers_data["answers"]["scope.boundaries"]["answer"] = "Add invite creation and acceptance endpoints."
    answers_data["answers"]["acceptance.criteria"]["answer"] = (
        "Owners can invite teammates by email.\nMembers cannot invite teammates."
    )
    answers_data["answers"]["scope.out_of_scope"]["answer"] = "Billing changes are out of scope."
    written.answers_path.write_text(yaml.safe_dump(answers_data, sort_keys=False), encoding="utf-8")

    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    in_memory = improve_contract_text(
        task_path.read_text(encoding="utf-8").strip(),
        contract_name=task_path,
        questions=answers_data["questions"],
        answers=answers_data["answers"],
        source_result=result,
        output_name=output_path,
        project_config=_python_project_config(tmp_path),
    )
    file_based = improve_contract_from_answers(
        task_path,
        answers_path=written.answers_path,
        output_path=output_path,
        project_config=_python_project_config(tmp_path),
    )

    assert in_memory.markdown == output_path.read_text(encoding="utf-8")
    assert in_memory.answered_question_ids == file_based.answered_question_ids
    assert in_memory.open_question_ids == file_based.open_question_ids
    assert in_memory.check_result.to_dict() == file_based.check_result.to_dict()


def test_improve_contract_rejects_hash_mismatch(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    task_path.write_text(
        "# Add team invites\n\nUsers should be able to invite teammates by email. Add audit logs.", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="different task revision"):
        improve_contract_from_answers(
            task_path,
            answers_path=written.answers_path,
            output_path=tmp_path / ".sikula" / "tasks" / "team-invites.v2.md",
            project_config=_python_project_config(tmp_path),
        )


def test_improve_contract_accepts_text_input_but_requires_markdown_output(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.txt"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["acceptance.criteria"]["answer"] = "Owners can invite teammates by email."
    written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    improve_contract_from_answers(
        task_path,
        answers_path=written.answers_path,
        output_path=output_path,
        project_config=_python_project_config(tmp_path),
    )

    assert output_path.read_text(encoding="utf-8").startswith("# Improved implementation contract")
    with pytest.raises(ValueError, match="Markdown"):
        improve_contract_from_answers(
            task_path,
            answers_path=written.answers_path,
            output_path=tmp_path / ".sikula" / "tasks" / "team-invites.v2.txt",
            project_config=_python_project_config(tmp_path),
        )
    with pytest.raises(ValueError, match="non-Markdown"):
        improve_contract_from_answers(
            task_path,
            answers_path=written.answers_path,
            write=True,
            project_config=_python_project_config(tmp_path),
        )


def test_improve_contract_refuses_output_overwrite(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    output_path.write_text("manual content\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        improve_contract_from_answers(
            task_path,
            answers_path=written.answers_path,
            output_path=output_path,
            project_config=_python_project_config(tmp_path),
        )


def test_improve_contract_write_overwrites_markdown_task_and_renders_validation_commands(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path)
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["acceptance.criteria"]["answer"] = (
        "- Owners can invite teammates by email.\n"
        "- Members cannot invite teammates.\n"
        "- Duplicate pending invites return an error."
    )
    answers["answers"]["validation.commands"]["answer"] = "`pytest`\n- ruff check ."
    written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    improved = improve_contract_from_answers(task_path, answers_path=written.answers_path, write=True)

    output = task_path.read_text(encoding="utf-8")
    assert improved.output_path == task_path
    assert "## Acceptance criteria" in output
    assert "- Owners can invite teammates by email." in output
    assert "- Members cannot invite teammates." in output
    assert "## Validation" in output
    assert "- `pytest`" in output
    assert "- `ruff check .`" in output
    assert "Clarification" not in output


def test_improve_contract_rejects_invalid_invocation_targets(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)

    with pytest.raises(ValueError, match="either output_path or write=True"):
        improve_contract_from_answers(
            task_path,
            answers_path=written.answers_path,
            output_path=tmp_path / ".sikula" / "tasks" / "team-invites.v2.md",
            write=True,
            project_config=_python_project_config(tmp_path),
        )
    with pytest.raises(ValueError, match="Provide output_path or write=True"):
        improve_contract_from_answers(
            task_path,
            answers_path=written.answers_path,
            project_config=_python_project_config(tmp_path),
        )
    with pytest.raises(ValueError, match="without write=True"):
        improve_contract_from_answers(
            task_path,
            answers_path=written.answers_path,
            output_path=task_path,
            project_config=_python_project_config(tmp_path),
        )


def test_improve_contract_rejects_invalid_answers_file_metadata(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    answers_path = tmp_path / ".sikula" / "contracts" / "bad.answers.yaml"
    answers_path.parent.mkdir(parents=True)

    cases = [
        ("answers: [\n", "Invalid contract answers YAML"),
        ("- not-a-mapping\n", "must contain a mapping"),
        (
            yaml.safe_dump({"schema_version": 1, "generated_by": "other"}, sort_keys=False),
            "was not generated by sikula contract check",
        ),
        (
            yaml.safe_dump({"schema_version": 999, "generated_by": "sikula.contract_check"}, sort_keys=False),
            "Unsupported contract answers schema version",
        ),
    ]

    for content, error in cases:
        answers_path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=error):
            improve_contract_from_answers(
                task_path,
                answers_path=answers_path,
                output_path=tmp_path / ".sikula" / "tasks" / "team-invites.v2.md",
                project_config=_python_project_config(tmp_path),
            )


def test_improve_contract_rejects_invalid_answers_shape(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    valid_answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    first_question = valid_answers["questions"][0]

    invalid_cases = [
        ({**valid_answers, "questions": None}, "missing the questions list"),
        ({**valid_answers, "questions": [{"id": ""}]}, "invalid question entry"),
        ({**valid_answers, "questions": [first_question, first_question]}, "duplicate question id"),
        ({key: value for key, value in valid_answers.items() if key != "answers"}, "missing the answers mapping"),
        ({**valid_answers, "answers": {"acceptance.criteria": ""}}, "invalid answer entry"),
        ({key: value for key, value in valid_answers.items() if key != "task"}, "missing task metadata"),
        (
            {
                **valid_answers,
                "answers": {
                    **valid_answers["answers"],
                    "legacy.question": {"answer": "stale answer", "notes": ""},
                },
            },
            "unknown question id",
        ),
    ]

    for data, error in invalid_cases:
        written.answers_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match=error):
            improve_contract_from_answers(
                task_path,
                answers_path=written.answers_path,
                output_path=tmp_path / ".sikula" / "tasks" / "team-invites.v2.md",
                project_config=_python_project_config(tmp_path),
            )


def test_contract_improve_cli_writes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["acceptance.criteria"]["answer"] = "Owners can invite teammates by email."
    written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    monkeypatch.chdir(tmp_path)

    with patch(
        "sys.argv",
        [
            "sikula",
            "contract",
            "improve",
            str(task_path),
            "--answers",
            str(written.answers_path),
            "--output",
            str(output_path),
        ],
    ):
        main()

    out = capsys.readouterr().out
    assert output_path.exists()
    assert "Improved contract written:" in out
    assert "Applied answers: 1" in out
    assert "Implementation Contract Readiness:" in out


def test_contract_improve_cli_interactive_writes_answers_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    monkeypatch.chdir(tmp_path)

    def answer(prompt: str) -> str:
        if "scope.boundaries" in prompt:
            return "Add invite creation and acceptance endpoints."
        if "acceptance.criteria" in prompt:
            return "Owners can invite teammates by email."
        return ""

    with (
        patch(
            "sys.argv", ["sikula", "contract", "improve", str(task_path), "--interactive", "--output", str(output_path)]
        ),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=answer),
    ):
        main()

    out = capsys.readouterr().out
    answers_path = tmp_path / ".sikula" / "contracts" / "team-invites.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    output = output_path.read_text(encoding="utf-8")

    assert "Interactive contract answers:" in out
    assert "Contract answers written:" in out
    assert "Improved contract written:" in out
    assert answers["answers"]["scope.boundaries"]["answer"] == "Add invite creation and acceptance endpoints."
    assert answers["answers"]["acceptance.criteria"]["answer"] == "Owners can invite teammates by email."
    assert "## Scope" in output
    assert "- Add invite creation and acceptance endpoints." in output
    assert "## Open questions" in output


def test_contract_improve_cli_interactive_rejects_stale_answers_before_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    original_answers = written.answers_path.read_text(encoding="utf-8")
    task_path.write_text(
        "# Add team invites\n\nUsers should be able to invite teammates by email and role.",
        encoding="utf-8",
    )
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "improve",
                str(task_path),
                "--interactive",
                "--answers",
                str(written.answers_path),
                "--output",
                str(output_path),
            ],
        ),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=AssertionError("stale answers must fail before prompting")),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "generated for a different task revision" in err
    assert written.answers_path.read_text(encoding="utf-8") == original_answers
    assert not output_path.exists()


def test_contract_improve_cli_interactive_requires_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch(
            "sys.argv", ["sikula", "contract", "improve", str(task_path), "--interactive", "--output", str(output_path)]
        ),
        patch("sys.stdin.isatty", return_value=False),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "interactive contract improve requires an interactive terminal" in err
    assert not output_path.exists()


def test_contract_improve_cli_requires_answers_without_interactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "contract", "improve", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "--answers is required unless --interactive is used" in err
    assert not output_path.exists()


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


def test_contract_check_cli_uses_effective_run_pipeline_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    config_path = tmp_path / ".sikula" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "project:\n"
        "  root_path: .\n"
        "  build_tool: python\n"
        "build:\n"
        "  checks:\n"
        "    - name: ruff\n"
        "      command: ruff check .\n",
        encoding="utf-8",
    )
    task_path = tmp_path / ".sikula" / "tasks" / "weak.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch("sys.argv", ["sikula", "contract", "check", ".sikula/tasks/weak.md", "--json"]):
        main()

    data = json.loads(capsys.readouterr().out)
    assert data["readiness_score"] == 30
    assert data["validation"]["configured_commands"] == []
    assert any(gap["id"] == "gap.validation.commands" for gap in data["gaps"])


def test_contract_check_cli_counts_enabled_run_pipeline_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    config_path = tmp_path / ".sikula" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "project:\n"
        "  root_path: .\n"
        "  build_tool: python\n"
        "run_build: true\n"
        "run_tests: true\n"
        "run_checks: true\n"
        "build:\n"
        "  checks:\n"
        "    - name: ruff\n"
        "      command: ruff check .\n",
        encoding="utf-8",
    )
    task_path = tmp_path / ".sikula" / "tasks" / "weak.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch("sys.argv", ["sikula", "contract", "check", ".sikula/tasks/weak.md", "--json"]):
        main()

    data = json.loads(capsys.readouterr().out)
    assert data["readiness_score"] == 36
    assert [command["command"] for command in data["validation"]["configured_commands"]] == [
        "ruff check .",
        "pytest",
        "ruff check .",
    ]
    assert all(gap["id"] != "gap.validation.commands" for gap in data["gaps"])


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
