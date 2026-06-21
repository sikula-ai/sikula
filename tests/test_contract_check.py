from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import patch

import pytest
import yaml

from core.contract_check import (
    check_contract,
    check_contract_file,
    improve_contract_text,
    improve_contract_from_answers,
    load_generated_answer_entries_for_contract,
    prepare_implementation_contract,
    render_contract_check,
    write_contract_report,
    write_prepared_contract,
)
from sikula import (
    _parse_agent_llm_overrides,
    _prepare_answers_path,
    _prepare_project_context_from_config,
    _read_interactive_contract_answer,
    _should_store_interactive_answer,
    main,
)


def _python_project_config(tmp_path: Path) -> dict:
    return {
        "project": {"build_tool": "python", "root_path": str(tmp_path)},
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "build": {"checks": [{"name": "ruff", "command": "ruff check ."}]},
    }


def test_contract_check_reports_available_reference_asset(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing-bug.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    task_path = tmp_path / ".sikula" / "tasks" / "login-spacing.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        """# Fix login spacing

## Scope
- Fix the login form spacing shown in `.sikula/task-assets/login-spacing-bug.png`.
- Use the screenshot as reference only.

## Acceptance criteria
- The email field and submit button match the referenced spacing.
- Existing login validation remains unchanged.

## Out of scope
- Do not redesign the login screen.

## Tests
- Add or update a UI test for the login form spacing.

## Validation
- `pytest`
""",
        encoding="utf-8",
    )

    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))

    assert result.asset_references == [
        {
            "path": ".sikula/task-assets/login-spacing-bug.png",
            "line": 4,
            "kind": "reference",
            "project_path": ".sikula/task-assets/login-spacing-bug.png",
            "status": "available",
            "sha256": "sha256:" + sha256(b"fake-png").hexdigest(),
            "size_bytes": 8,
            "mime_type": "image/png",
            "git_status": "unknown",
        }
    ]
    assert "Referenced local assets are available and hashed." in result.strong_signals
    assert "Asset references:" in render_contract_check(result)


def test_contract_check_blocks_missing_asset(tmp_path: Path):
    task = """# Fix login spacing

## Scope
- Fix the login form spacing shown in `.sikula/task-assets/missing.png`.
- Use the screenshot as reference only.

## Acceptance criteria
- The login form spacing matches the reference.

## Out of scope
- Do not redesign the screen.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert any(gap.id == "gap.assets.missing" and gap.severity == "blocking" for gap in result.gaps)
    assert any(
        question.id == "assets.local_files" and question.blocks_delivery for question in result.clarifying_questions
    )
    assert result.asset_references[0]["status"] == "missing"
    assert result.ready_for_autonomous_delivery is False


def test_contract_check_blocks_assets_outside_project(tmp_path: Path):
    task_path = tmp_path / "repo" / ".sikula" / "tasks" / "task.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        """# Fix login spacing

## Scope
- Use `../outside.png` as a reference screenshot.

## Acceptance criteria
- The login form spacing matches the reference.

## Out of scope
- Do not redesign the screen.

## Validation
- `pytest`
""",
        encoding="utf-8",
    )
    cfg = _python_project_config(tmp_path / "repo")

    result = check_contract_file(task_path, project_config=cfg)

    assert any(gap.id == "gap.assets.outside_project" and gap.severity == "blocking" for gap in result.gaps)
    assert result.asset_references[0]["status"] == "outside_project"


def test_contract_check_blocks_delivery_asset_without_provenance(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "success-check.svg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("<svg />", encoding="utf-8")
    task = """# Add success icon

## Scope
- Use `.sikula/task-assets/success-check.svg` as the success state icon.
- Target: `app/src/main/res/drawable/success_check.svg`

## Acceptance criteria
- The success state shows the new icon.

## Out of scope
- Do not redesign the success screen.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert len(result.asset_references) == 1
    assert result.asset_references[0]["kind"] == "delivery"
    assert result.asset_references[0]["target_specified"] is True
    assert any(gap.id == "gap.assets.provenance" and gap.severity == "blocking" for gap in result.gaps)
    assert any(
        question.id == "assets.provenance" and question.blocks_delivery for question in result.clarifying_questions
    )


def test_contract_check_captures_delivery_asset_target_and_source_license(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "success-check.svg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("<svg />", encoding="utf-8")
    task_path = tmp_path / ".sikula" / "tasks" / "success-icon.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        """# Add success icon

## Assets

### Delivery assets

- Use `.sikula/task-assets/success-check.svg` as the success state icon.
  - Target: app/src/main/res/drawable/success_check.svg
  - Source/license: provided by product team for this project.

## Scope
- Add the success state icon to the confirmation screen.

## Acceptance criteria
- The success screen shows the provided success icon.
- Existing success message text remains unchanged.

## Out of scope
- Do not redesign the success screen.

## Validation
- `pytest`
""",
        encoding="utf-8",
    )

    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))

    assert result.asset_references[0]["requested_target"] == "app/src/main/res/drawable/success_check.svg"
    assert result.asset_references[0]["source_license"] == "provided by product team for this project."
    assert all(gap.id != "gap.assets.provenance" for gap in result.gaps)


def test_contract_check_allows_delivery_asset_target_to_be_inferred(tmp_path: Path):
    asset_path = tmp_path / ".sikula" / "task-assets" / "success-check.svg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("<svg />", encoding="utf-8")
    task = """# Add success icon

## Scope
- Use `.sikula/task-assets/success-check.svg` as the success state icon.
- Source/license: provided by product team for this project.

## Acceptance criteria
- The success state shows the new icon.

## Out of scope
- Do not redesign the success screen.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert result.asset_references[0]["kind"] == "delivery"
    assert result.asset_references[0].get("target_specified") is None
    assert all(gap.id != "gap.assets.target" for gap in result.gaps)
    assert all(question.id != "assets.target" for question in result.clarifying_questions)


def test_contract_check_does_not_treat_basename_docs_as_assets(tmp_path: Path):
    (tmp_path / "README.md").write_text("# Repo docs\n", encoding="utf-8")
    task = """# Update docs

## Scope
- Follow `README.md` conventions.

## Acceptance criteria
- Documentation remains consistent.

## Out of scope
- Do not change runtime behaviour.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert result.asset_references == []


def test_contract_check_does_not_treat_output_file_paths_as_assets(tmp_path: Path):
    task = """# Add guide

## Scope
- Create `docs/new-guide.md`.
- Add `config/example.json` with sample settings.

## Acceptance criteria
- The new guide explains the setup flow.
- The sample settings are committed with the guide.

## Out of scope
- Do not change runtime behaviour.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert result.asset_references == []
    assert all(not gap.id.startswith("gap.assets.") for gap in result.gaps)


def test_contract_check_warns_for_untracked_asset_in_git_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing-bug.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    task = """# Fix login spacing

## Scope
- Use `.sikula/task-assets/login-spacing-bug.png` as a reference screenshot.

## Acceptance criteria
- The login form spacing matches the reference.

## Out of scope
- Do not redesign the screen.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert result.asset_references[0]["git_status"] == "untracked"
    assert any(gap.id == "gap.assets.worktree_availability" and gap.severity == "warning" for gap in result.gaps)


def test_contract_check_warns_for_dirty_tracked_asset_in_git_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing-bug.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"old-png")
    subprocess.run(["git", "add", str(asset_path.relative_to(tmp_path))], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Sikula Test", "-c", "user.email=sikula@example.test", "commit", "-m", "add asset"],
        cwd=tmp_path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    asset_path.write_bytes(b"new-png")
    task = """# Fix login spacing

## Scope
- Use `.sikula/task-assets/login-spacing-bug.png` as a reference screenshot.

## Acceptance criteria
- The login form spacing matches the reference.

## Out of scope
- Do not redesign the screen.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert result.asset_references[0]["git_status"] == "dirty"
    assert result.asset_references[0]["sha256"] == "sha256:" + sha256(b"new-png").hexdigest()
    assert any(gap.id == "gap.assets.worktree_availability" and gap.severity == "warning" for gap in result.gaps)


def test_contract_check_warns_for_staged_new_asset_in_git_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    asset_path = tmp_path / ".sikula" / "task-assets" / "success-check.svg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("<svg />", encoding="utf-8")
    subprocess.run(["git", "add", str(asset_path.relative_to(tmp_path))], cwd=tmp_path, check=True)
    task = """# Add success icon

## Assets

### Delivery assets

- Use `.sikula/task-assets/success-check.svg` as the success state icon.
  - Source/license: provided by product team for this project.

## Scope
- Add the success state icon.

## Acceptance criteria
- The success state shows the new icon.

## Out of scope
- Do not redesign the success screen.

## Validation
- `pytest`
"""

    result = check_contract(
        task, source_path=tmp_path / ".sikula" / "tasks" / "task.md", project_config=_python_project_config(tmp_path)
    )

    assert result.asset_references[0]["git_status"] == "dirty"
    assert any(gap.id == "gap.assets.worktree_availability" and gap.severity == "warning" for gap in result.gaps)


def test_contract_prepare_project_context_filters_placeholder_validation_commands(tmp_path: Path):
    cfg = {
        "project": {
            "build_tool": "xcodebuild",
            "language": "Swift",
            "platform": "iOS",
            "root_path": str(tmp_path),
        },
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "build": {"scheme": "TODO"},
    }

    context = _prepare_project_context_from_config(cfg)

    assert context is not None
    assert context["stack"] == "Swift / iOS / xcodebuild"
    assert context["validation_commands"] == []


def test_contract_prepare_project_context_keeps_effective_validation_commands(tmp_path: Path):
    cfg = _python_project_config(tmp_path)
    cfg["build"] = {
        "compile_command": "TODO",
        "test_command": "pytest",
        "checks": [
            {"name": "placeholder", "command": "TODO"},
            {"name": "ruff", "command": "ruff check .", "fix_command": "ruff format ."},
        ],
    }

    context = _prepare_project_context_from_config(cfg)

    assert context is not None
    assert context["validation_commands"] == ["pytest", "ruff check ."]


def test_task_preparer_override_is_not_a_runtime_agent(capsys):
    with pytest.raises(SystemExit) as exc:
        _parse_agent_llm_overrides(["task_preparer=gpt-5.5"], None, None)

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Unknown agent 'task_preparer'" in out
    assert "task_preparer" not in out.split("Valid agents:", 1)[1]


def test_task_preparer_override_is_valid_for_contract_prepare_auto():
    overrides = _parse_agent_llm_overrides(
        ["task_preparer=gpt-5.5"],
        ["task_preparer=codex"],
        ["task_preparer=1200"],
        valid_agents={"task_preparer"},
    )

    assert overrides == {
        "task_preparer": {
            "model": "gpt-5.5",
            "provider": "codex",
            "agent_timeout": 1200,
        }
    }


def test_task_preparer_override_is_valid_for_preparation_commands():
    overrides = _parse_agent_llm_overrides(
        ["task_preparer=gpt-5.5"],
        None,
        None,
        valid_agents={"task_preparer"},
    )

    assert overrides == {"task_preparer": {"model": "gpt-5.5"}}


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


def test_long_well_bounded_contract_does_not_warn_about_task_size(tmp_path: Path):
    context = " ".join(["Existing API, service, route, model, and repository conventions should stay aligned."] * 125)
    task = f"""# Team invites

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

## Context
- {context}
"""

    result = check_contract(task, source_path="task.md", project_config=_python_project_config(tmp_path))

    assert result.ready_for_autonomous_delivery
    assert "gap.task_size.too_large" not in {gap.id for gap in result.gaps}
    assert "The task may be too large for a single autonomous delivery run." not in render_contract_check(result)


def test_long_vague_task_still_warns_about_task_size():
    task = "# Update product experience\n\n" + "Improve the product experience for users. " * 260

    result = check_contract(task, configured_validation_commands=["pytest"])

    assert "gap.task_size.too_large" in {gap.id for gap in result.gaps}


def test_check_contract_ignores_generated_answer_markers():
    clean_task = """# Team invites

## Acceptance criteria
- Owner/admin can invite a user by email.
- Non-admin users cannot invite users.

## Validation
- `ruff check .`
"""
    marked_task = """# Team invites

## Acceptance criteria
<!-- sikula:generated-answer: acceptance.criteria -->
- Owner/admin can invite a user by email.
- Non-admin users cannot invite users.
<!-- /sikula:generated-answer -->

## Validation
<!-- sikula:generated-answer: validation.commands -->
- `ruff check .`
<!-- /sikula:generated-answer -->
"""

    clean = check_contract(clean_task, configured_validation_commands=["ruff check ."])
    marked = check_contract(marked_task, configured_validation_commands=["ruff check ."])

    assert marked.readiness_score == clean.readiness_score
    assert marked.status == clean.status
    assert marked.scores == clean.scores
    assert marked.validation == clean.validation
    assert marked.gaps == clean.gaps
    assert marked.source["sha256"] != clean.source["sha256"]


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


def test_prepare_implementation_contract_returns_questions_and_requires_context_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)

    result = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
    )

    assert result.stage == "needs_project_context"
    assert result.needs_user_input
    assert not result.ready_to_save
    assert not result.ready_to_run
    assert result.required_next_step == "provide_project_context"
    assert "acceptance.criteria" in result.answers_template
    assert "token.lifecycle" in result.answers_template
    assert result.safe_task_path == ".sikula/contracts/team-invites.contract.md"
    assert result.required_user_action == "provide_project_context"
    assert result.primary_user_action == "provide_project_context"
    assert result.user_questions
    assert result.ready_to_run_blockers[0] == "missing_project_context"
    assert result.open_question_ids == [question.id for question in result.questions_for_user]
    assert result.resume_arguments["contract_markdown"].startswith("# Add team invites")
    assert result.resume_arguments["status_applies_to_sha256"] == result.status_applies_to_sha256
    assert "sikula run" not in "\n".join(result.suggested_next_steps)
    assert "ready_to_run" in result.assistant_response_markdown
    assert result.recheck_result is None
    assert result.status_applies_to_sha256 == result.check_result.source["sha256"]
    assert not (tmp_path / ".sikula").exists()


def test_prepare_implementation_contract_checks_normalized_output_without_answers():
    task = "# Add team invites\n\nUsers should be able to invite teammates by email."

    result = prepare_implementation_contract(task, contract_name="team-invites.md")

    expected_markdown = task + "\n"
    expected_sha = "sha256:" + sha256(expected_markdown.encode("utf-8")).hexdigest()
    assert result.prepared_contract_markdown == expected_markdown
    assert result.authoritative_output_markdown == expected_markdown
    assert result.resume_arguments["contract_markdown"] == expected_markdown
    assert result.status_applies_to_sha256 == expected_sha
    assert result.check_result.source["sha256"] == expected_sha
    assert result.resume_arguments["status_applies_to_sha256"] == expected_sha


def test_prepare_implementation_contract_uses_project_context_validation_commands():
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

    result = prepare_implementation_contract(
        task,
        contract_name="search.md",
        project_context={"validation_commands": ["npm test"]},
    )

    assert result.check_result.validation["coverage_gaps"] == []
    assert result.check_result.validation["configured_commands"] == [
        {"phase": "project_context", "name": "validation-1", "command": "npm test"}
    ]
    assert all(gap.id != "gap.validation.coverage" for gap in result.unresolved_gaps)


def test_prepare_implementation_contract_adds_reference_asset_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    asset_path = tmp_path / ".sikula" / "task-assets" / "login-spacing-bug.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    task = """# Fix login spacing

## Assets

- `.sikula/task-assets/login-spacing-bug.png`
  - Use as reference only.
  - Shows the broken spacing between the email field and submit button.
  - Do not copy this screenshot into app assets.

## Scope
- Fix the login form spacing shown in the reference screenshot.

## Acceptance criteria
- The email field and submit button match the referenced spacing.
- Existing login validation remains unchanged.

## Out of scope
- Do not redesign the login screen.

## Validation
- `pytest`
"""

    result = prepare_implementation_contract(
        task,
        contract_name=".sikula/tasks/login-spacing.md",
        project_context={"validation_commands": ["pytest"]},
    )

    assert "## Asset manifest" in result.prepared_contract_markdown
    assert "- Path: `.sikula/task-assets/login-spacing-bug.png`" in result.prepared_contract_markdown
    assert "Usage: reference only; do not copy this asset into production files." in result.prepared_contract_markdown
    assert f"SHA-256: `{result.check_result.asset_references[0]['sha256']}`" in result.prepared_contract_markdown
    assert "Purpose: reference context for the implementation contract." in result.prepared_contract_markdown
    assert "sikula:generated-" not in result.prepared_contract_markdown
    assert "<!-- sikula:generated-answer: asset_manifest.references -->" in result.resume_arguments["contract_markdown"]
    assert result.recheck_result is not None
    assert result.recheck_result.asset_references[0]["status"] == "available"


def test_prepare_implementation_contract_adds_delivery_asset_manifest_with_target_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    asset_path = tmp_path / ".sikula" / "task-assets" / "success-check.svg"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_text("<svg />", encoding="utf-8")
    task = """# Add success icon

## Assets

### Delivery assets

- Use `.sikula/task-assets/success-check.svg` as the success state icon.
  - Target: app/src/main/res/drawable/success_check.svg
  - Source/license: provided by product team for this project.

## Scope
- Add the success state icon to the confirmation screen.

## Acceptance criteria
- The success screen shows the provided success icon.
- Existing success message text remains unchanged.

## Out of scope
- Do not redesign the success screen.

## Validation
- `pytest`
"""

    result = prepare_implementation_contract(
        task,
        contract_name=".sikula/tasks/success-icon.md",
        project_context={"validation_commands": ["pytest"]},
    )

    assert "## Asset manifest" in result.prepared_contract_markdown
    assert "- Path: `.sikula/task-assets/success-check.svg`" in result.prepared_contract_markdown
    assert (
        "Usage: delivery asset; use this file only for the requested implementation."
        in result.prepared_contract_markdown
    )
    assert "Requested target: `app/src/main/res/drawable/success_check.svg`" in result.prepared_contract_markdown
    assert "Source/license: provided by product team for this project." in result.prepared_contract_markdown
    assert "Target resolution: analyst should choose" not in result.prepared_contract_markdown
    assert all(gap.id != "gap.assets.provenance" for gap in result.recheck_result.gaps)


def test_prepare_implementation_contract_does_not_manifest_unresolved_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    asset_path = tmp_path / ".sikula" / "task-assets" / "ambiguous.png"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"fake-png")
    task = """# Update login visual state

## Assets

- `.sikula/task-assets/ambiguous.png`
- `.sikula/task-assets/missing.png`

## Scope
- Update the login visual state using the referenced files.

## Acceptance criteria
- The login visual state matches the provided references.
- Existing login validation remains unchanged.

## Out of scope
- Do not redesign the login screen.

## Validation
- `pytest`
"""

    result = prepare_implementation_contract(
        task,
        contract_name=".sikula/tasks/login-visual.md",
        project_context={"validation_commands": ["pytest"]},
    )

    assert "## Asset manifest" not in result.prepared_contract_markdown
    assert any(gap.id == "gap.assets.missing" for gap in result.unresolved_gaps)
    assert any(gap.id == "gap.assets.intent" for gap in result.unresolved_gaps)


def test_prepare_implementation_contract_applies_answers_and_rechecks():
    result = prepare_implementation_contract(
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
    assert "sikula:generated-" not in result.prepared_contract_markdown
    assert result.recheck_result.readiness_score > result.check_result.readiness_score
    assert result.status_applies_to_sha256 == result.recheck_result.source["sha256"]
    assert result.required_next_step in {
        "answer_questions",
        "save_contract",
        "save_and_run_contract",
        "revise_contract",
    }


def test_prepare_implementation_contract_reconciles_open_questions_after_recheck():
    result = prepare_implementation_contract(
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


def test_prepare_implementation_contract_applies_project_context_recheck_only_answers():
    task = "# Add dashboard filter\n\nUsers should be able to filter dashboard entries."
    project_context = {
        "known_constraints": "The filter is applied to private account emails and auth tokens must not be leaked.",
        "validation_commands": ["pytest"],
    }

    first = prepare_implementation_contract(
        task,
        contract_name="dashboard-filter.md",
        answers={
            "scope.boundaries": "Add dashboard filtering.",
            "acceptance.criteria": (
                "Users can filter dashboard entries by label. Empty filters show all entries. "
                "No matches show an empty state."
            ),
            "scope.out_of_scope": "Do not redesign the dashboard.",
        },
        project_context=project_context,
    )

    assert "token.lifecycle" not in [question.id for question in first.check_result.clarifying_questions]
    assert "token.lifecycle" in [question.id for question in first.questions_for_user]

    second = prepare_implementation_contract(
        task,
        contract_name="dashboard-filter.md",
        answers={
            "scope.boundaries": "Add dashboard filtering.",
            "acceptance.criteria": (
                "Users can filter dashboard entries by label. Empty filters show all entries. "
                "No matches show an empty state."
            ),
            "acceptance.negative_cases": "Invalid filters are ignored without leaking private account email values.",
            "scope.out_of_scope": "Do not redesign the dashboard.",
            "token.lifecycle": "Filtering must not display, log, or change auth token values.",
            "privacy.data_handling": "Do not log or reveal private account email values in errors.",
            "reviewer.focus": "Privacy handling and filter correctness.",
            "context.domain_rules": "Follow existing dashboard filtering patterns.",
        },
        project_context=project_context,
    )

    assert "token.lifecycle" in second.answered_question_ids
    assert "privacy.data_handling" in second.answered_question_ids
    assert "token.lifecycle" not in second.open_question_ids
    assert "privacy.data_handling" not in second.open_question_ids
    assert "- Filtering must not display, log, or change auth token values." in second.prepared_contract_markdown
    assert "- Do not log or reveal private account email values in errors." in second.prepared_contract_markdown


def test_prepare_implementation_contract_keeps_current_open_questions_after_final_recheck():
    result = prepare_implementation_contract(
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
    assert "sikula:generated-open-questions" not in result.prepared_contract_markdown
    assert "What observable behaviours must be true when this task is complete?" in result.prepared_contract_markdown
    assert result.status_applies_to_sha256 == result.recheck_result.source["sha256"]


def test_prepare_implementation_contract_splits_actual_newlines_in_text_answers():
    result = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"scope.boundaries": "Create invites\nAccept invites"},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "- Create invites" in result.prepared_contract_markdown
    assert "- Accept invites" in result.prepared_contract_markdown


def test_prepare_implementation_contract_splits_actual_newlines_in_validation_answers():
    result = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"validation.commands": "pytest\nruff check ."},
    )

    assert "validation.commands" in result.answered_question_ids
    assert "validation.commands" not in result.revised_answer_question_ids
    assert "- `pytest`" in result.prepared_contract_markdown
    assert "- `ruff check .`" in result.prepared_contract_markdown


def test_prepare_implementation_contract_preserves_literal_backslash_n_in_validation_answers():
    result = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"validation.commands": "python -c \"print('line\\nvalue')\""},
    )

    assert "validation.commands" in result.answered_question_ids
    assert "validation.commands" not in result.revised_answer_question_ids
    assert "- `python -c \"print('line\\nvalue')\"`" in result.prepared_contract_markdown
    assert "- `value')\"`" not in result.prepared_contract_markdown


def test_prepare_implementation_contract_marks_repeated_questions_as_revised_answer_needed():
    result = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"acceptance.negative_cases": "ok"},
        project_context={"validation_commands": ["pytest"]},
    )

    assert result.needs_user_input
    assert "acceptance.negative_cases" in result.answered_question_ids
    assert {question.id for question in result.questions_for_user}.issubset(set(result.open_question_ids))
    assert "acceptance.negative_cases" in result.revised_answer_question_ids
    assert "- ok" not in result.prepared_contract_markdown
    question = next(question for question in result.user_questions if question["id"] == "acceptance.negative_cases")
    assert question["requires_revised_answer"] is True
    assert "more specific answer" in question["reason"]
    assert result.answers_template["acceptance.negative_cases"]["requires_revised_answer"] is True
    assert result.required_user_action == "answer_contract_questions"


def test_prepare_implementation_contract_readiness_ignores_internal_answer_markers():
    result = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={
            "scope.boundaries": "Add invite creation and acceptance endpoints.",
            "acceptance.criteria": "Owners can invite teammates by email.",
            "acceptance.negative_cases": "Duplicate invites return a deterministic error.",
            "scope.out_of_scope": "Billing changes are out of scope.",
            "token.lifecycle": "Invite tokens expire after 24 hours and cannot be reused.",
            "privacy.data_handling": "Invite tokens must not be logged.",
            "reviewer.focus": "Authorization and token lifecycle.",
            "context.domain_rules": "domain",
        },
        project_context={"validation_commands": ["pytest"]},
    )

    assert "context.domain_rules" in result.revised_answer_question_ids
    assert "- domain" not in result.prepared_contract_markdown
    assert "sikula:generated-" not in result.prepared_contract_markdown


def test_prepare_implementation_contract_strips_stale_open_questions_between_answer_rounds():
    first = prepare_implementation_contract(
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

    second = prepare_implementation_contract(
        first.resume_arguments["contract_markdown"],
        contract_name="team-invites.md",
        answers={"acceptance.negative_cases": "Duplicate invites return a deterministic error."},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "- Duplicate invites return a deterministic error." in second.prepared_contract_markdown
    assert stale_question not in second.prepared_contract_markdown


def test_prepare_implementation_contract_replaces_revised_generated_answers():
    first = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"acceptance.negative_cases": "ok"},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "acceptance.negative_cases" in first.revised_answer_question_ids
    assert "- ok" not in first.prepared_contract_markdown

    second = prepare_implementation_contract(
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


def test_prepare_implementation_contract_clears_revised_generated_answers(tmp_path: Path):
    first = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"reviewer.focus": "Authorization checks and token lifecycle handling."},
        project_context={"validation_commands": ["pytest"]},
    )
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    write_prepared_contract(first, output_path=output_path, project_root=tmp_path)
    output_text = output_path.read_text(encoding="utf-8")
    generated_answer_entries = load_generated_answer_entries_for_contract(
        output_path,
        source_text=output_text,
        project_root=tmp_path,
    )

    assert "- Authorization checks and token lifecycle handling." in output_text

    second = prepare_implementation_contract(
        output_text,
        contract_name="team-invites.md",
        answers={"reviewer.focus": {"answer": "", "notes": ""}},
        project_context={"validation_commands": ["pytest"]},
        generated_answer_entries=generated_answer_entries,
    )

    assert "- Authorization checks and token lifecycle handling." not in second.prepared_contract_markdown
    assert "reviewer.focus" not in second.answered_question_ids
    assert "reviewer.focus" in second.open_question_ids


def test_prepare_implementation_contract_resume_arguments_preserve_generated_markers_for_later_revisions():
    first = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"validation.commands": "pytest"},
    )

    assert "sikula:generated-answer" not in first.prepared_contract_markdown
    assert "<!-- sikula:generated-answer: validation.commands -->" in first.resume_arguments["contract_markdown"]

    second = prepare_implementation_contract(
        first.resume_arguments["contract_markdown"],
        contract_name="team-invites.md",
        answers={"validation.commands": "ruff check ."},
        project_context={"validation_commands": ["ruff check ."]},
    )

    assert "- `pytest`" not in second.prepared_contract_markdown
    assert "- `ruff check .`" in second.prepared_contract_markdown
    assert "sikula:generated-answer" not in second.prepared_contract_markdown
    assert "validation.commands" not in second.revised_answer_question_ids


def test_prepare_implementation_contract_preserves_resume_markers_without_new_answers():
    first = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"validation.commands": "pytest"},
    )

    idle = prepare_implementation_contract(
        first.resume_arguments["contract_markdown"],
        contract_name="team-invites.md",
        project_context={"validation_commands": ["pytest"]},
    )

    assert "sikula:generated-answer" not in idle.prepared_contract_markdown
    assert "<!-- sikula:generated-answer: validation.commands -->" in idle.resume_arguments["contract_markdown"]

    second = prepare_implementation_contract(
        idle.resume_arguments["contract_markdown"],
        contract_name="team-invites.md",
        answers={"validation.commands": "ruff check ."},
        project_context={"validation_commands": ["ruff check ."]},
    )

    assert "- `pytest`" not in second.prepared_contract_markdown
    assert "- `ruff check .`" in second.prepared_contract_markdown
    assert "validation.commands" not in second.revised_answer_question_ids


def test_prepare_implementation_contract_preserves_human_open_questions_section():
    task = """# Add team invites

Users should be able to invite teammates by email.

## Open questions

- Confirm the invite email copy with product.
"""

    result = prepare_implementation_contract(
        task,
        contract_name="team-invites.md",
        answers={"scope.boundaries": "Add invite creation and acceptance endpoints."},
        project_context={"validation_commands": ["pytest"]},
    )

    assert "## Open questions" in result.prepared_contract_markdown
    assert "- Confirm the invite email copy with product." in result.prepared_contract_markdown


def test_prepare_implementation_contract_resume_accepts_accumulated_answers():
    first = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.md",
        answers={"scope.boundaries": "Add invite creation and acceptance endpoints."},
        project_context={"validation_commands": ["pytest"]},
    )

    second = prepare_implementation_contract(
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


def test_prepare_implementation_contract_ready_result_includes_safe_save_and_run_guidance():
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

    result = prepare_implementation_contract(
        task,
        contract_name="../../Team Invites; rm -rf *.md",
        project_context={
            "validation_commands": ["pytest", "ruff check ."],
        },
    )

    assert result.ready_to_save
    assert result.ready_to_run
    assert result.required_next_step == "save_and_run_contract"
    assert result.safe_task_path == ".sikula/contracts/team-invites-rm-rf.contract.md"
    assert result.suggested_next_steps == [
        "Save the prepared contract to `.sikula/contracts/team-invites-rm-rf.contract.md`.",
        "Run `sikula run .sikula/contracts/team-invites-rm-rf.contract.md` from a locally configured Sikula project.",
    ]
    assert result.resume_arguments["project_context"] == {
        "validation_commands": ["pytest", "ruff check ."],
    }
    assert result.to_dict()["authoritative_output_markdown"] == result.prepared_contract_markdown


def test_prepare_implementation_contract_safe_path_strips_refined_suffix():
    result = prepare_implementation_contract(
        "# Add team invites\n\nUsers should be able to invite teammates by email.",
        contract_name="team-invites.refined.md",
        project_context={"validation_commands": ["pytest"]},
    )

    assert result.safe_task_path == ".sikula/contracts/team-invites.contract.md"


def test_prepare_implementation_contract_enriches_task_description_with_project_context():
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

## Reviewer focus
- Search/filter interaction.
"""

    result = prepare_implementation_contract(
        task,
        contract_name="country-search.md",
        project_context={
            "stack": "TypeScript/React",
            "package_manager": "pnpm",
            "known_constraints": "Keep the existing countries list route and filter behaviour.",
            "validation_commands": ["pnpm test", "pnpm lint"],
        },
    )

    assert result.ready_to_run
    assert result.recheck_result is not None
    assert result.status_applies_to_sha256 == result.recheck_result.source["sha256"]
    assert "## Project context" in result.prepared_contract_markdown
    assert "- Stack: TypeScript/React" in result.prepared_contract_markdown
    assert "- Package manager: pnpm" in result.prepared_contract_markdown
    assert "- Known constraints: Keep the existing countries list route and filter behaviour." in (
        result.prepared_contract_markdown
    )
    assert "## Validation" in result.prepared_contract_markdown
    assert "- `pnpm test`" in result.prepared_contract_markdown
    assert "- `pnpm lint`" in result.prepared_contract_markdown
    assert "sikula:generated-" not in result.prepared_contract_markdown
    assert "<!-- sikula:generated-answer: project_context.details -->" in result.resume_arguments["contract_markdown"]
    assert (
        "<!-- sikula:generated-answer: project_context.validation_commands -->"
        in (result.resume_arguments["contract_markdown"])
    )


def test_prepare_implementation_contract_does_not_duplicate_existing_validation_commands():
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
- `pnpm test`

## Reviewer focus
- Search/filter interaction.
"""

    result = prepare_implementation_contract(
        task,
        contract_name="country-search.md",
        project_context={"validation_commands": ["pnpm test", "pnpm lint"]},
    )

    assert result.ready_to_run
    assert result.prepared_contract_markdown.count("- `pnpm test`") == 1
    assert result.prepared_contract_markdown.count("- `pnpm lint`") == 1


def test_prepare_implementation_contract_replaces_generated_project_context_on_resume():
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

## Reviewer focus
- Search/filter interaction.
"""

    first = prepare_implementation_contract(
        task,
        contract_name="country-search.md",
        project_context={
            "stack": "React",
            "validation_commands": ["npm test"],
        },
    )
    second = prepare_implementation_contract(
        first.resume_arguments["contract_markdown"],
        contract_name="country-search.md",
        project_context={
            "stack": "Vue",
            "validation_commands": ["npm run test"],
        },
    )

    assert "- Stack: React" not in second.prepared_contract_markdown
    assert "- `npm test`" not in second.prepared_contract_markdown
    assert "- Stack: Vue" in second.prepared_contract_markdown
    assert "- `npm run test`" in second.prepared_contract_markdown
    assert second.prepared_contract_markdown.count("## Project context") == 1
    assert second.prepared_contract_markdown.count("## Validation") == 1


def test_prepare_implementation_contract_ready_without_project_context_requires_context():
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

    result = prepare_implementation_contract(
        task,
        contract_name="Country Search",
    )

    assert result.stage == "needs_project_context"
    assert result.needs_user_input is True
    assert result.ready_to_save is False
    assert result.ready_to_run is False
    assert result.required_next_step == "provide_project_context"
    assert result.required_user_action == "provide_project_context"
    assert result.ready_to_run_blockers == ["missing_project_context"]
    assert result.resume_arguments["project_context"] == {}
    assert "validation_commands" in result.suggested_next_steps[0]
    assert all("sikula run" not in step for step in result.suggested_next_steps)


def test_prepare_implementation_contract_empty_validation_context_blocks_run():
    task = """# Country search

## Scope
- Add search by country name.
- Keep sorting unchanged.

## Acceptance criteria
- Matching is case-insensitive.
- Clearing search shows the full list.
- No matching countries shows an empty state.

## Out of scope
- Do not add server-side search.

## Tests
- Search matching test.

## Validation
- `npm test`

## Reviewer focus
- Search/filter interaction.
"""

    result = prepare_implementation_contract(
        task,
        contract_name="Country Search",
        project_context={"sikula_configured": True, "validation_commands": []},
    )

    assert result.ready_to_save is False
    assert result.ready_to_run is False
    assert result.required_next_step == "provide_project_context"
    assert "missing_validation_commands" in result.ready_to_run_blockers
    assert result.resume_arguments["project_context"] == {
        "validation_commands": [],
        "delivery_environment": {
            "local_sikula_config_present": True,
            "source": "client_reported",
        },
    }


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
    assert not (tmp_path / ".sikula" / "contract-reports").exists()


def test_contract_check_cli_write_report_without_config_uses_task_local_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    task_dir = tmp_path / "repo" / ".sikula" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "team-invites.md"
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    monkeypatch.chdir(caller_dir)

    with patch("sys.argv", ["sikula", "contract", "check", str(task_path), "--write-report"]):
        main()

    out = capsys.readouterr().out
    task_report_dir = tmp_path / "repo" / ".sikula" / "contract-reports"
    caller_report_dir = caller_dir / ".sikula" / "contract-reports"
    assert "Generated contract report artifacts:" in out
    assert (task_report_dir / "team-invites.check.json").exists()
    assert (task_report_dir / "team-invites.answers.yaml").exists()
    assert not caller_report_dir.exists()


def test_write_report_creates_check_json_and_answers_template(tmp_path: Path):
    task_dir = tmp_path / ".sikula" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "team-invites.md"
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))

    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)

    assert written.report_path == tmp_path / ".sikula" / "contract-reports" / "team-invites.check.json"
    assert written.answers_path == tmp_path / ".sikula" / "contract-reports" / "team-invites.answers.yaml"
    report = json.loads(written.report_path.read_text(encoding="utf-8"))
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    assert report["generated_by"] == "sikula.contract_check"
    assert report["checked_at"].endswith("Z")
    assert report["source"]["path"] == ".sikula/tasks/team-invites.md"
    assert report["source"]["sha256"] == result.source["sha256"]
    assert answers["generated_by"] == "sikula.contract_check"
    assert answers["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert answers["task"]["sha256"] == result.source["sha256"]
    assert answers["check_report"] == ".sikula/contract-reports/team-invites.check.json"
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
    contract_reports = tmp_path / ".sikula" / "contract-reports"
    contract_reports.mkdir(parents=True)
    manual_report = contract_reports / "task.check.json"
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
    assert "sikula:generated-answer" not in output
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

    saved_output = output_path.read_text(encoding="utf-8")
    assert in_memory.markdown == saved_output
    assert in_memory.resume_markdown != saved_output
    assert "sikula:generated-answer" not in in_memory.markdown
    assert "sikula:generated-answer" not in saved_output
    assert in_memory.answered_question_ids == file_based.answered_question_ids
    assert in_memory.open_question_ids == file_based.open_question_ids
    assert in_memory.check_result.to_dict() == file_based.check_result.to_dict()


def test_file_based_improve_uses_metadata_for_later_answer_revisions(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    first_result = check_contract_file(task_path)
    first_written = write_contract_report(first_result, task_path=task_path, project_root=tmp_path)
    first_answers = yaml.safe_load(first_written.answers_path.read_text(encoding="utf-8"))
    first_answers["answers"]["validation.commands"]["answer"] = "pytest"
    first_written.answers_path.write_text(yaml.safe_dump(first_answers, sort_keys=False), encoding="utf-8")

    first_output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    improve_contract_from_answers(task_path, answers_path=first_written.answers_path, output_path=first_output_path)
    first_output = first_output_path.read_text(encoding="utf-8")
    assert "sikula:generated-answer" not in first_output
    assert "- `pytest`" in first_output
    metadata_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.v2.generated-answers.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["generated_by"] == "sikula.contract_prepare"
    assert metadata["task"]["sha256"] == "sha256:" + sha256(first_output.strip().encode("utf-8")).hexdigest()
    assert metadata["generated_answers"][0]["question_id"] == "validation.commands"
    assert "pytest" not in metadata_path.read_text(encoding="utf-8")

    ruff_only_config = _python_project_config(tmp_path)
    ruff_only_config["run_tests"] = False
    second_result = check_contract_file(first_output_path, project_config=ruff_only_config)
    second_written = write_contract_report(second_result, task_path=first_output_path, project_root=tmp_path)
    second_answers = yaml.safe_load(second_written.answers_path.read_text(encoding="utf-8"))
    second_answers["answers"]["validation.commands"]["answer"] = "ruff check ."
    second_written.answers_path.write_text(yaml.safe_dump(second_answers, sort_keys=False), encoding="utf-8")

    second_output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v3.md"
    improved = improve_contract_from_answers(
        first_output_path,
        answers_path=second_written.answers_path,
        output_path=second_output_path,
        project_config=ruff_only_config,
    )

    second_output = second_output_path.read_text(encoding="utf-8")
    assert "- `pytest`" not in second_output
    assert "- `ruff check .`" in second_output
    assert "sikula:generated-answer" not in second_output
    assert "validation.commands" not in improved.open_question_ids
    assert all(gap.id != "gap.validation.coverage" for gap in improved.check_result.gaps)


def test_file_based_improve_metadata_anchors_duplicate_answer_lines_to_generated_block(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        """# Add team invites

Users should be able to invite teammates by email.

## Context

Example documentation:

```text
- `pytest`
```
""",
        encoding="utf-8",
    )
    first_result = check_contract_file(task_path)
    first_written = write_contract_report(first_result, task_path=task_path, project_root=tmp_path)
    first_answers = yaml.safe_load(first_written.answers_path.read_text(encoding="utf-8"))
    first_answers["answers"]["validation.commands"]["answer"] = "pytest"
    first_written.answers_path.write_text(yaml.safe_dump(first_answers, sort_keys=False), encoding="utf-8")

    first_output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    improve_contract_from_answers(task_path, answers_path=first_written.answers_path, output_path=first_output_path)
    first_output = first_output_path.read_text(encoding="utf-8")
    assert first_output.count("- `pytest`") == 2
    metadata_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.v2.generated-answers.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata_entry = metadata["generated_answers"][0]
    assert first_output.splitlines()[metadata_entry["start_line"] - 1 : metadata_entry["end_line"]] == ["- `pytest`"]
    assert metadata_entry["start_line"] > first_output.splitlines().index("- `pytest`") + 1

    ruff_only_config = _python_project_config(tmp_path)
    ruff_only_config["run_tests"] = False
    second_result = check_contract_file(first_output_path, project_config=ruff_only_config)
    second_written = write_contract_report(second_result, task_path=first_output_path, project_root=tmp_path)
    second_answers = yaml.safe_load(second_written.answers_path.read_text(encoding="utf-8"))
    second_answers["answers"]["validation.commands"]["answer"] = "ruff check ."
    second_written.answers_path.write_text(yaml.safe_dump(second_answers, sort_keys=False), encoding="utf-8")

    second_output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v3.md"
    improve_contract_from_answers(
        first_output_path,
        answers_path=second_written.answers_path,
        output_path=second_output_path,
        project_config=ruff_only_config,
    )

    second_output = second_output_path.read_text(encoding="utf-8")
    assert second_output.count("- `pytest`") == 1
    assert "```text\n- `pytest`\n```" in second_output
    assert "- `ruff check .`" in second_output
    assert "sikula:generated-answer" not in second_output


def test_file_based_improve_loads_hashed_metadata_for_same_stem_tasks(tmp_path: Path):
    first_task_path = tmp_path / "first" / "task.md"
    second_task_path = tmp_path / "second" / "task.md"
    first_task_path.parent.mkdir(parents=True)
    second_task_path.parent.mkdir(parents=True)
    first_task_path.write_text("# Add search\n\nUsers should be able to search countries by name.", encoding="utf-8")
    second_task_path.write_text("# Add sort\n\nUsers should be able to sort countries by name.", encoding="utf-8")

    first_result = check_contract_file(first_task_path)
    first_written = write_contract_report(first_result, task_path=first_task_path, project_root=tmp_path)
    first_answers = yaml.safe_load(first_written.answers_path.read_text(encoding="utf-8"))
    first_answers["answers"]["validation.commands"]["answer"] = "pytest"
    first_written.answers_path.write_text(yaml.safe_dump(first_answers, sort_keys=False), encoding="utf-8")
    first_output_path = tmp_path / "first" / "task.v2.md"
    improve_contract_from_answers(
        first_task_path,
        answers_path=first_written.answers_path,
        output_path=first_output_path,
    )
    assert (tmp_path / ".sikula" / "contract-reports" / "task.v2.generated-answers.json").exists()

    second_result = check_contract_file(second_task_path)
    second_written = write_contract_report(second_result, task_path=second_task_path, project_root=tmp_path)
    second_answers = yaml.safe_load(second_written.answers_path.read_text(encoding="utf-8"))
    second_answers["answers"]["validation.commands"]["answer"] = "pytest"
    second_written.answers_path.write_text(yaml.safe_dump(second_answers, sort_keys=False), encoding="utf-8")
    second_output_path = tmp_path / "second" / "task.v2.md"
    improve_contract_from_answers(
        second_task_path,
        answers_path=second_written.answers_path,
        output_path=second_output_path,
    )
    hashed_metadata_paths = sorted((tmp_path / ".sikula" / "contract-reports").glob("task.v2-*.generated-answers.json"))
    assert len(hashed_metadata_paths) == 1

    ruff_only_config = _python_project_config(tmp_path)
    ruff_only_config["run_tests"] = False
    second_revision_result = check_contract_file(second_output_path, project_config=ruff_only_config)
    second_revision_written = write_contract_report(
        second_revision_result,
        task_path=second_output_path,
        project_root=tmp_path,
    )
    assert second_revision_written.answers_path == tmp_path / ".sikula" / "contract-reports" / "task.v2.answers.yaml"
    second_revision_answers = yaml.safe_load(second_revision_written.answers_path.read_text(encoding="utf-8"))
    second_revision_answers["answers"]["validation.commands"]["answer"] = "ruff check ."
    second_revision_written.answers_path.write_text(
        yaml.safe_dump(second_revision_answers, sort_keys=False),
        encoding="utf-8",
    )

    second_revision_output_path = tmp_path / "second" / "task.v3.md"
    improve_contract_from_answers(
        second_output_path,
        answers_path=second_revision_written.answers_path,
        output_path=second_revision_output_path,
        project_config=ruff_only_config,
    )

    second_revision_output = second_revision_output_path.read_text(encoding="utf-8")
    assert "- `pytest`" not in second_revision_output
    assert "- `ruff check .`" in second_revision_output
    assert "sikula:generated-answer" not in second_revision_output


def test_file_based_improve_ignores_metadata_after_manual_task_edit(tmp_path: Path):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    first_result = check_contract_file(task_path)
    first_written = write_contract_report(first_result, task_path=task_path, project_root=tmp_path)
    first_answers = yaml.safe_load(first_written.answers_path.read_text(encoding="utf-8"))
    first_answers["answers"]["validation.commands"]["answer"] = "pytest"
    first_written.answers_path.write_text(yaml.safe_dump(first_answers, sort_keys=False), encoding="utf-8")

    first_output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v2.md"
    improve_contract_from_answers(task_path, answers_path=first_written.answers_path, output_path=first_output_path)
    first_output_path.write_text(
        first_output_path.read_text(encoding="utf-8").replace("- `pytest`", "- `pytest`\n- Keep manual note."),
        encoding="utf-8",
    )

    ruff_only_config = _python_project_config(tmp_path)
    ruff_only_config["run_tests"] = False
    second_result = check_contract_file(first_output_path, project_config=ruff_only_config)
    second_written = write_contract_report(second_result, task_path=first_output_path, project_root=tmp_path)
    second_answers = yaml.safe_load(second_written.answers_path.read_text(encoding="utf-8"))
    second_answers["answers"]["validation.commands"]["answer"] = "ruff check ."
    second_written.answers_path.write_text(yaml.safe_dump(second_answers, sort_keys=False), encoding="utf-8")

    second_output_path = tmp_path / ".sikula" / "tasks" / "team-invites.v3.md"
    improve_contract_from_answers(
        first_output_path,
        answers_path=second_written.answers_path,
        output_path=second_output_path,
        project_config=ruff_only_config,
    )

    second_output = second_output_path.read_text(encoding="utf-8")
    assert "- `pytest`" in second_output
    assert "- Keep manual note." in second_output


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
    answers["answers"]["acceptance.criteria"]["answer"] = (
        "A valid email can be invited. Duplicate pending invites show a deterministic error. "
        "Invalid email invites are rejected."
    )
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


def test_contract_prepare_cli_writes_output_from_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["acceptance.criteria"]["answer"] = (
        "A valid email can be invited. Duplicate pending invites show a deterministic error. "
        "Invalid email invites are rejected."
    )
    written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
                str(task_path),
                "--answers",
                str(written.answers_path),
                "--output",
                str(output_path),
            ],
        ),
    ):
        main()

    out = capsys.readouterr().out
    assert output_path.exists()
    assert "Implementation contract written:" in out
    assert "Applied answers: 1" in out
    assert "Implementation Contract Readiness:" in out
    assert "A valid email can be invited." in output_path.read_text(encoding="utf-8")
    metadata_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract.generated-answers.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["generated_by"] == "sikula.contract_prepare"
    assert metadata["task"]["path"] == ".sikula/contracts/team-invites.contract.md"
    assert metadata["task"]["sha256"] == (
        "sha256:" + sha256(output_path.read_text(encoding="utf-8").strip().encode("utf-8")).hexdigest()
    )
    assert metadata["generated_answers"][0]["question_id"] == "acceptance.criteria"
    assert "A valid email can be invited" not in metadata_path.read_text(encoding="utf-8")


def test_contract_prepare_cli_uses_metadata_for_later_answer_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "dashboard-filter.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        "# Add dashboard filter\n\nUsers should be able to filter dashboard entries.", encoding="utf-8"
    )
    result = check_contract_file(task_path, project_config=_python_project_config(tmp_path))
    written = write_contract_report(result, task_path=task_path, project_root=tmp_path)
    answers = yaml.safe_load(written.answers_path.read_text(encoding="utf-8"))
    answers["answers"]["scope.boundaries"]["answer"] = "Add filtering by label."
    answers["answers"]["acceptance.criteria"]["answer"] = (
        "Users can filter dashboard entries by label. No matches show an empty state."
    )
    answers["answers"]["scope.out_of_scope"]["answer"] = "Do not redesign the dashboard."
    written.answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")
    first_output_path = tmp_path / ".sikula" / "contracts" / "dashboard-filter.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
                str(task_path),
                "--answers",
                str(written.answers_path),
                "--output",
                str(first_output_path),
            ],
        ),
    ):
        main()

    capsys.readouterr()
    first_output = first_output_path.read_text(encoding="utf-8")
    assert "- Add filtering by label." in first_output
    assert "- `pytest`" in first_output

    revision_result = check_contract_file(first_output_path, project_config=_python_project_config(tmp_path))
    revision_written = write_contract_report(revision_result, task_path=first_output_path, project_root=tmp_path)
    revision_answers = yaml.safe_load(revision_written.answers_path.read_text(encoding="utf-8"))
    revision_answers.setdefault("answers", {})["scope.boundaries"] = {
        "answer": "Add filtering by label and owner.",
        "notes": "",
    }
    revision_answers["answers"]["acceptance.criteria"] = {
        "answer": "Users can filter dashboard entries by label and owner. No matches show an empty state.",
        "notes": "",
    }
    revision_written.answers_path.write_text(yaml.safe_dump(revision_answers, sort_keys=False), encoding="utf-8")
    second_output_path = tmp_path / ".sikula" / "contracts" / "dashboard-filter.v2.contract.md"

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["ruff check ."]}),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
                str(first_output_path),
                "--answers",
                str(revision_written.answers_path),
                "--output",
                str(second_output_path),
            ],
        ),
    ):
        main()

    second_output = second_output_path.read_text(encoding="utf-8")
    assert "- Add filtering by label." not in second_output
    assert "- Add filtering by label and owner." in second_output
    assert "- `pytest`" not in second_output
    assert "- `ruff check .`" in second_output
    assert "sikula:generated-answer" not in second_output


def test_write_prepared_contract_loads_metadata_and_ignores_stale_or_invalid_entries(tmp_path: Path):
    result = prepare_implementation_contract(
        "# Add dashboard filter\n\nUsers should be able to filter dashboard entries.",
        contract_name="dashboard-filter.contract.md",
        answers={
            "scope.boundaries": "Add filtering by label.",
            "acceptance.criteria": "Users can filter dashboard entries by label. No matches show an empty state.",
            "scope.out_of_scope": "Do not redesign the dashboard.",
        },
        project_context={"validation_commands": ["pytest"]},
    )
    output_path = tmp_path / ".sikula" / "contracts" / "dashboard-filter.contract.md"
    written = write_prepared_contract(result, output_path=output_path, project_root=tmp_path)
    output_text = output_path.read_text(encoding="utf-8")

    entries = load_generated_answer_entries_for_contract(output_path, source_text=output_text, project_root=tmp_path)
    assert {entry["question_id"] for entry in entries} == {
        "scope.boundaries",
        "scope.out_of_scope",
        "project_context.validation_commands",
    }

    assert (
        load_generated_answer_entries_for_contract(
            output_path,
            source_text=output_text + "\nmanual edit",
            project_root=tmp_path,
        )
        == []
    )

    written.generated_answers_path.write_text("{not json", encoding="utf-8")
    assert load_generated_answer_entries_for_contract(output_path, source_text=output_text, project_root=tmp_path) == []


def test_write_prepared_contract_uses_hashed_metadata_for_same_stem_outputs(tmp_path: Path):
    result = prepare_implementation_contract(
        "# Add invite flow\n\nUsers should be able to invite teammates.",
        contract_name="invite.contract.md",
        answers={
            "scope.boundaries": "Add invite creation.",
            "acceptance.criteria": "Owners can invite teammates by email.",
            "scope.out_of_scope": "Billing changes are out of scope.",
        },
        project_context={"validation_commands": ["pytest"]},
    )
    first_output_path = tmp_path / ".sikula" / "contracts" / "web" / "invite.contract.md"
    second_output_path = tmp_path / ".sikula" / "contracts" / "mobile" / "invite.contract.md"

    first_written = write_prepared_contract(result, output_path=first_output_path, project_root=tmp_path)
    second_written = write_prepared_contract(result, output_path=second_output_path, project_root=tmp_path)

    assert first_written.generated_answers_path.name == "invite.contract.generated-answers.json"
    assert second_written.generated_answers_path.name.startswith("invite.contract-")
    assert second_written.generated_answers_path.name.endswith(".generated-answers.json")
    assert first_written.generated_answers_path != second_written.generated_answers_path


def test_contract_prepare_cli_interactive_writes_answers_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    def answer(prompt: str) -> str:
        if "scope.boundaries" in prompt:
            return "Add invite creation and acceptance endpoints."
        if "acceptance.criteria" in prompt:
            return "Owners can invite teammates by email."
        return ""

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch(
            "sys.argv", ["sikula", "contract", "prepare", str(task_path), "--interactive", "--output", str(output_path)]
        ),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=answer),
    ):
        main()

    out = capsys.readouterr().out
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    output = output_path.read_text(encoding="utf-8")

    assert "Interactive contract preparation answers:" in out
    assert "Contract preparation answers written:" in out
    assert "Implementation contract written:" in out
    assert answers["answers"]["scope.boundaries"]["answer"] == "Add invite creation and acceptance endpoints."
    assert answers["answers"]["acceptance.criteria"]["answer"] == "Owners can invite teammates by email."
    assert "## Scope" in output
    assert "- Add invite creation and acceptance endpoints." in output
    assert "## Open questions" in output


def test_task_refine_cli_interactive_writes_clean_refined_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    def answer(prompt: str) -> str:
        if "scope.boundaries" in prompt:
            return "Add invite creation from team settings."
        if "acceptance.criteria" in prompt:
            return (
                "A valid email can be invited from team settings. Duplicate invites show a deterministic error. "
                "Invalid emails are rejected."
            )
        if "acceptance.negative_cases" in prompt:
            return "Duplicate and invalid email invites show deterministic errors."
        if "scope.out_of_scope" in prompt:
            return "Do not add billing seat enforcement or team settings redesign."
        return ""

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--interactive", "--output", str(output_path)]),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=answer),
    ):
        main()

    out = capsys.readouterr().out
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.answers.yaml"
    output = output_path.read_text(encoding="utf-8")

    assert answers_path.exists()
    assert "Interactive task refinement answers:" in out
    assert "Refined task description written:" in out
    assert "task refine only resolves product task-description questions" in out
    assert "sikula:generated" not in output
    assert "Add invite creation from team settings." in output
    assert "A valid email can be invited from team settings." in output
    assert f"Next step: sikula contract prepare {output_path}" in out


def test_task_refine_cli_without_answers_writes_template_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))

    assert exc.value.code == 1
    assert not output_path.exists()
    assert "Task refinement needs answers before writing a refined task description." in out
    assert "Task refinement answers template written:" in out
    assert "Open question details:" in out
    assert "[scope.boundaries] What exactly is in scope" in out
    assert "contract prepare may still ask delivery questions" in out
    assert "sikula task refine" in out
    assert answers["generated_by"] == "sikula.task_refine"
    assert answers["task"]["sha256"].startswith("sha256:")
    assert {"scope.boundaries", "acceptance.criteria"}.issubset(answers["answers"])


def test_task_refine_cli_without_config_uses_task_local_answers_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    task_dir = tmp_path / "repo" / ".sikula" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "team-invites.md"
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = task_dir / "team-invites.refined.md"
    monkeypatch.chdir(caller_dir)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    task_report_dir = tmp_path / "repo" / ".sikula" / "contract-reports"
    caller_report_dir = caller_dir / ".sikula" / "contract-reports"
    assert exc.value.code == 1
    assert "Task refinement answers template written:" in out
    assert (task_report_dir / "team-invites.task-refine.answers.yaml").exists()
    assert not caller_report_dir.exists()
    assert not output_path.exists()


def test_task_refine_cli_without_config_reuses_nested_task_local_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    task_dir = tmp_path / "repo" / ".sikula" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "team-invites.md"
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = task_dir / "team-invites.refined.md"
    monkeypatch.chdir(caller_dir)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()

    capsys.readouterr()
    answers_path = tmp_path / "repo" / ".sikula" / "contract-reports" / "team-invites.task-refine.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    assert answers["task"]["path"] == ".sikula/tasks/team-invites.md"
    answers["answers"]["scope.boundaries"]["answer"] = "Add invite creation from team settings."
    answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()

    capsys.readouterr()
    reused = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    hashed_answers = sorted((tmp_path / "repo" / ".sikula" / "contract-reports").glob("team-invites-*.answers.yaml"))
    assert reused["answers"]["scope.boundaries"]["answer"] == "Add invite creation from team settings."
    assert hashed_answers == []


def test_task_refine_cli_without_config_uses_task_local_default_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    task_dir = tmp_path / "repo" / ".sikula" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "team-invites.md"
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    monkeypatch.chdir(caller_dir)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path)]),
        pytest.raises(SystemExit),
    ):
        main()

    capsys.readouterr()
    answers_path = tmp_path / "repo" / ".sikula" / "contract-reports" / "team-invites.task-refine.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    answers["answers"]["scope.boundaries"]["answer"] = "Add invite creation from team settings."
    answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    with patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--answers", str(answers_path)]):
        main()

    out = capsys.readouterr().out
    task_output_path = task_dir / "team-invites.refined.md"
    caller_output_path = caller_dir / ".sikula" / "tasks" / "team-invites.refined.md"
    assert "Refined task description written:" in out
    assert task_output_path.exists()
    assert not caller_output_path.exists()


def test_task_refine_cli_regenerates_stale_default_answers_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()

    capsys.readouterr()
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.answers.yaml"
    first_answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    first_sha = first_answers["task"]["sha256"]
    first_answers["answers"]["scope.boundaries"]["answer"] = "Add invite creation from team settings."
    answers_path.write_text(yaml.safe_dump(first_answers, sort_keys=False), encoding="utf-8")
    task_path.write_text(
        "# Add team invites\n\nUsers should be able to invite teammates by email and role.",
        encoding="utf-8",
    )

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    regenerated = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    assert exc.value.code == 1
    assert "Task refinement answers template written:" in out
    assert regenerated["task"]["sha256"] != first_sha
    assert regenerated["answers"]["scope.boundaries"]["answer"] == ""
    assert regenerated["previous_answers"][0]["task"]["sha256"] == first_sha
    assert regenerated["previous_answers"][0]["answers"]["scope.boundaries"]["answer"] == (
        "Add invite creation from team settings."
    )


def test_task_refine_cli_with_partial_answers_prints_remaining_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()

    capsys.readouterr()
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    answers["answers"]["scope.boundaries"]["answer"] = "Add invite creation from team settings."
    answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    with patch(
        "sys.argv",
        [
            "sikula",
            "task",
            "refine",
            str(task_path),
            "--answers",
            str(answers_path),
            "--output",
            str(output_path),
        ],
    ):
        main()

    out = capsys.readouterr().out
    assert output_path.exists()
    assert "Refined task description written:" in out
    assert "Applied answers: 1" in out
    assert "Open question details:" in out
    assert "[acceptance.criteria] What observable behaviours should prove this product task is complete?" in out
    assert "Fill/update the answers file:" in out
    assert "new --output path" in out


def test_task_refine_cli_rejects_directory_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_dir = tmp_path / ".sikula" / "tasks"
    task_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_dir)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "Task path is not a file:" in err


def test_task_refine_cli_existing_output_prints_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "country-search.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        """# Add country search

## Scope
- Add search by country name.
- Keep region filtering unchanged.
- Keep country sorting unchanged.

## Acceptance criteria
- Typing a country name filters the list.
- Matching is case-insensitive.
- Clearing the search restores the full list.

## Out of scope
- Do not add server-side search.
- Do not redesign country rows.
""",
        encoding="utf-8",
    )
    output_path = tmp_path / ".sikula" / "tasks" / "country-search.refined.md"
    output_path.write_text("# Existing refined task\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "refusing to overwrite existing output file" in err
    assert "Choose a new --output path" in err


def test_task_refine_cli_auto_writes_normalized_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    class FakeLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            self.prompts.append(prompt)
            assert cwd == tmp_path
            return json.dumps(
                {
                    "task_markdown": """# Add team invites

## Goal

Users can invite teammates by email.

## Scope

- Add invite creation from team settings.

## Acceptance criteria

- A valid email can be invited from team settings.
- Invalid email input is rejected.
- Duplicate pending invites show a deterministic error.

## Out of scope

- Do not add billing seat enforcement.
""",
                    "input_language": "cs",
                    "normalized_to_english": True,
                }
            )

    fake_llm = FakeLLM()
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Pozvat kolegy\n\nUzivatel muze pozvat kolegu emailem.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("core.llm_client.create_llm_client", return_value=fake_llm),
        patch(
            "sys.argv",
            [
                "sikula",
                "task",
                "refine",
                str(task_path),
                "--auto",
                "--agent-model",
                "task_preparer=gpt-5.5",
                "--output",
                str(output_path),
            ],
        ),
    ):
        main()

    out = capsys.readouterr().out
    output = output_path.read_text(encoding="utf-8")
    audit_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.auto-llm.jsonl"
    audit_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])

    assert len(fake_llm.prompts) == 1
    assert "Preserve the user's product intent" in fake_llm.prompts[0]
    assert "Uzivatel muze pozvat kolegu emailem." in fake_llm.prompts[0]
    assert audit_record["generated_by"] == "sikula.task_refine"
    assert audit_record["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert audit_record["output"]["path"] == ".sikula/tasks/team-invites.refined.md"
    assert audit_record["record"]["phase"] == "task_refine_auto"
    assert audit_record["record"]["prompt"] == fake_llm.prompts[0]
    assert "task_markdown" in audit_record["record"]["raw_output"]
    assert "Auto-normalized task description: yes" in out
    assert "Input language: cs" in out
    assert "Normalized to English: yes" in out
    assert f"Next step: sikula contract prepare {output_path}" in out
    assert "## Open questions" not in output
    assert "Users can invite teammates by email." in output


def test_task_refine_cli_auto_applies_product_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    class FakeLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            self.prompts.append(prompt)
            assert cwd == tmp_path
            if "Raw task description:" in prompt:
                return json.dumps(
                    {
                        "task_markdown": "# Add team invites\n\nUsers can invite teammates by email.",
                        "input_language": "en",
                        "normalized_to_english": False,
                    }
                )
            if "Active product task questions:" in prompt:
                return json.dumps(
                    {
                        "answers": {
                            "scope.boundaries": {
                                "answer": "Add invite creation from team settings.",
                                "notes": "Product scope.",
                            },
                            "acceptance.criteria": {
                                "answer": (
                                    "A valid email can be invited from team settings. "
                                    "Invalid emails are rejected. Duplicate pending invites show an error."
                                ),
                                "notes": "",
                            },
                            "scope.out_of_scope": {
                                "answer": "Do not add billing seat enforcement or bulk invites.",
                                "notes": "",
                            },
                        }
                    }
                )
            raise AssertionError(f"unexpected prompt:\n{prompt}")

    fake_llm = FakeLLM()
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("core.llm_client.create_llm_client", return_value=fake_llm),
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--auto", "--output", str(output_path)]),
    ):
        main()

    out = capsys.readouterr().out
    output = output_path.read_text(encoding="utf-8")
    audit_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.auto-llm.jsonl"
    audit_records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert len(fake_llm.prompts) == 2
    assert [record["record"]["phase"] for record in audit_records] == [
        "task_refine_auto",
        "task_refine_auto_answers",
    ]
    assert "Auto-applied answers: 3" in out
    assert "Applied answers: 3" in out
    assert "Open questions: 0" in out
    assert f"Next step: sikula contract prepare {output_path}" in out
    assert "Add invite creation from team settings." in output
    assert "A valid email can be invited from team settings." in output
    assert "Do not add billing seat enforcement" in output
    assert "## Open questions" not in output


def test_task_refine_cli_auto_writes_answers_template_for_remaining_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return json.dumps(
                {
                    "task_markdown": "# Add team invites\n\nUsers can invite teammates by email.",
                    "input_language": "en",
                    "warnings": ["scope still needs product confirmation"],
                }
            )

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--auto", "--output", str(output_path)]),
    ):
        main()

    out = capsys.readouterr().out
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    output = output_path.read_text(encoding="utf-8")

    assert output_path.exists()
    assert "Auto-refine warnings:" in out
    assert "scope still needs product confirmation" in out
    assert "Open question details:" in out
    assert "Use a new --output path" in out
    assert answers["generated_by"] == "sikula.task_refine"
    assert answers["task"]["path"] == ".sikula/tasks/team-invites.refined.md"
    assert {"scope.boundaries", "acceptance.criteria"}.issubset(answers["answers"])
    assert "## Open questions" in output


def test_task_refine_cli_auto_records_parse_failure_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return "{not json"

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--auto", "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    audit_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.task-refine.auto-llm.jsonl"
    audit_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])

    assert exc.value.code == 1
    assert not output_path.exists()
    assert "Failed to auto-refine task" in err
    assert audit_record["generated_by"] == "sikula.task_refine"
    assert audit_record["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert audit_record["output"]["path"] == ".sikula/tasks/team-invites.refined.md"
    assert audit_record["record"]["phase"] == "task_refine_auto"
    assert audit_record["record"]["raw_output"] == "{not json"
    assert audit_record["record"]["parsed"]["status"] == "failed"
    assert "not valid JSON" in audit_record["record"]["parsed"]["error"]


def test_task_refine_cli_auto_rejects_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--auto", "--interactive"]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "--auto cannot be combined with --interactive" in err


def test_task_refine_cli_auto_existing_output_does_not_call_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    output_path.write_text("# Existing refined task\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with (
        patch("core.llm_client.create_llm_client", side_effect=AssertionError("LLM must not be created")),
        patch("sys.argv", ["sikula", "task", "refine", str(task_path), "--auto", "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "refusing to overwrite existing output file" in err
    assert "Choose a new --output path" in err


def test_contract_prepare_cli_interactive_prefills_existing_answers_for_line_editing(
    monkeypatch: pytest.MonkeyPatch,
):
    hooks = []
    inserted = []
    bindings = []
    prompts = []

    fake_readline = types.SimpleNamespace(
        parse_and_bind=lambda binding: bindings.append(binding),
        insert_text=lambda text: inserted.append(text),
        set_startup_hook=lambda hook=None: hooks.append(hook),
    )
    monkeypatch.setitem(sys.modules, "readline", fake_readline)

    with patch("builtins.input", side_effect=lambda prompt: prompts.append(prompt) or "Edited answer"):
        response, default_inserted = _read_interactive_contract_answer("Answer [scope.boundaries]", "Existing answer")

    assert response == "Edited answer"
    assert default_inserted is True
    assert prompts == ["Answer [scope.boundaries]: "]
    assert bindings
    assert hooks[0] is not None
    hooks[0]()
    assert inserted == ["Existing answer"]
    assert hooks[-1] is None


def test_contract_prepare_cli_interactive_stores_empty_response_after_line_editing_clear():
    assert _should_store_interactive_answer("", "Existing answer", default_inserted=True) is True
    assert _should_store_interactive_answer("", "Existing answer", default_inserted=False) is False
    assert _should_store_interactive_answer("Replacement", "Existing answer", default_inserted=True) is True


def test_contract_prepare_cli_interactive_rejects_stale_answers_before_prompting(
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
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
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


def test_contract_prepare_cli_interactive_requires_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch(
            "sys.argv", ["sikula", "contract", "prepare", str(task_path), "--interactive", "--output", str(output_path)]
        ),
        patch("sys.stdin.isatty", return_value=False),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "interactive contract preparation requires an interactive terminal" in err
    assert not output_path.exists()


def test_contract_prepare_cli_without_answers_writes_template_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))

    assert exc.value.code == 1
    assert not output_path.exists()
    assert "Contract preparation needs answers before writing an implementation contract." in out
    assert "Contract preparation answers template written:" in out
    assert "Open question details:" in out
    assert "[acceptance.criteria] What observable behaviours must be true" in out
    assert "sikula contract prepare" in out
    assert answers["generated_by"] == "sikula.contract_prepare"
    assert answers["task"]["sha256"].startswith("sha256:")
    assert "acceptance.criteria" in answers["answers"]


def _team_invites_full_auto_answers_payload() -> dict:
    return {
        "answers": {
            "scope.boundaries": {
                "answer": (
                    "Add single-teammate email invitations for existing team owners and admins only. "
                    "Keep existing authentication and team management behaviour unchanged."
                )
            },
            "acceptance.criteria": {
                "answer": (
                    "Owners/admins can invite a teammate by valid email. Duplicate pending invites show "
                    "a deterministic error. Invalid email input is rejected."
                )
            },
            "acceptance.negative_cases": {
                "answer": (
                    "Reject empty, malformed, unauthorized, duplicate, expired, and reused invitation flows with "
                    "stable user-visible errors."
                )
            },
            "scope.out_of_scope": {
                "answer": "Do not add bulk invites, billing seat management, role redesign, or account signup changes."
            },
            "token.lifecycle": {
                "answer": "Invitation tokens expire, cannot be reused after acceptance, and are not logged."
            },
            "privacy.data_handling": {
                "answer": "Do not log invite tokens or reveal whether an email already belongs to an account."
            },
            "reviewer.focus": {
                "answer": (
                    "Review authorization checks, duplicate handling, token lifecycle, and email enumeration behaviour."
                )
            },
            "context.domain_rules": {
                "answer": "Follow existing team membership, authorization, mailer, and persistence conventions."
            },
        }
    }


def test_contract_prepare_cli_auto_writes_output_from_supported_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    class FakeLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            self.prompts.append(prompt)
            assert cwd == tmp_path
            return json.dumps(_team_invites_full_auto_answers_payload())

    fake_llm = FakeLLM()
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch(
            "sikula._prepare_project_context_from_config",
            return_value={"validation_commands": ["pytest", "ruff check ."]},
        ),
        patch("core.llm_client.create_llm_client", return_value=fake_llm),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
    ):
        main()

    out = capsys.readouterr().out
    audit_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.auto-llm.jsonl"
    audit_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])

    assert output_path.exists()
    assert len(fake_llm.prompts) == 1
    assert "Do not invent product requirements" in fake_llm.prompts[0]
    assert audit_record["generated_by"] == "sikula.contract_prepare"
    assert audit_record["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert audit_record["output"]["path"] == ".sikula/contracts/team-invites.contract.md"
    assert audit_record["record"]["phase"] == "contract_prepare_auto"
    assert audit_record["record"]["prompt"] == fake_llm.prompts[0]
    assert "scope.boundaries" in audit_record["record"]["raw_output"]
    assert "Implementation contract written:" in out
    assert "Auto-applied answers: 8" in out
    assert "Open questions: 0" in out
    assert "Owners/admins can invite a teammate" in output_path.read_text(encoding="utf-8")


def test_contract_prepare_cli_auto_persists_supplied_answers_when_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return json.dumps(_team_invites_full_auto_answers_payload())

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()
    capsys.readouterr()

    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
                str(task_path),
                "--auto",
                "--answers",
                str(answers_path),
                "--output",
                str(output_path),
            ],
        ),
    ):
        main()

    out = capsys.readouterr().out
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    output = output_path.read_text(encoding="utf-8")

    assert "Implementation contract written:" in out
    assert "Auto-applied answers: 8" in out
    assert "Open questions: 0" in out
    assert answers["answers"]["scope.boundaries"]["answer"].startswith("Add single-teammate email invitations")
    assert answers["answers"]["acceptance.criteria"]["answer"].startswith("Owners/admins can invite")
    assert "Owners/admins can invite a teammate" in output


def test_contract_prepare_cli_auto_updates_blank_default_answers_when_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return json.dumps(_team_invites_full_auto_answers_payload())

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()
    capsys.readouterr()

    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
    ):
        main()

    out = capsys.readouterr().out
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))

    assert output_path.exists()
    assert "Auto-applied answers: 8" in out
    assert "Open questions: 0" in out
    assert answers["answers"]["scope.boundaries"]["answer"].startswith("Add single-teammate email invitations")
    assert answers["answers"]["acceptance.criteria"]["answer"].startswith("Owners/admins can invite")


def test_contract_prepare_cli_auto_rejects_filled_default_answers_without_answers_arg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()
    capsys.readouterr()

    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    answers["answers"]["scope.boundaries"] = {
        "answer": "Human-filled scope should stay.",
        "notes": "Human-filled notes should stay.",
    }
    answers_path.write_text(yaml.safe_dump(answers, sort_keys=False), encoding="utf-8")

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", side_effect=AssertionError("LLM must not be created")),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err

    assert exc.value.code == 1
    assert not output_path.exists()
    assert "existing contract answers contain filled values" in err
    assert f"--answers {answers_path}" in err


def test_contract_prepare_cli_auto_archives_stale_filled_default_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return json.dumps(_team_invites_full_auto_answers_payload())

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()
    capsys.readouterr()

    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    stale_answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    first_sha = stale_answers["task"]["sha256"]
    stale_answers["answers"]["scope.boundaries"] = {
        "answer": "Human-filled stale scope.",
        "notes": "Human-filled stale notes.",
    }
    answers_path.write_text(yaml.safe_dump(stale_answers, sort_keys=False), encoding="utf-8")
    task_path.write_text(
        "# Add team invites\n\nUsers should be able to invite teammates by email and role.",
        encoding="utf-8",
    )

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
    ):
        main()

    out = capsys.readouterr().out
    regenerated = yaml.safe_load(answers_path.read_text(encoding="utf-8"))

    assert output_path.exists()
    assert "Auto-applied answers: 8" in out
    assert regenerated["task"]["sha256"] != first_sha
    assert regenerated["answers"]["scope.boundaries"]["answer"].startswith("Add single-teammate email invitations")
    assert regenerated["previous_answers"][0]["task"]["sha256"] == first_sha
    assert regenerated["previous_answers"][0]["answers"]["scope.boundaries"] == {
        "answer": "Human-filled stale scope.",
        "notes": "Human-filled stale notes.",
    }


def test_contract_prepare_cli_auto_preserves_partial_answers_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            return json.dumps(
                {
                    "answers": {
                        "scope.boundaries": {
                            "answer": "Add single-teammate email invitations for owners/admins only.",
                            "notes": "Product intent names teammate invites.",
                        }
                    }
                }
            )

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))

    assert exc.value.code == 1
    assert not output_path.exists()
    assert "Auto-applied answers: 1" in out
    assert answers["answers"]["scope.boundaries"] == {
        "answer": "Add single-teammate email invitations for owners/admins only.",
        "notes": "Product intent names teammate invites.",
    }
    assert answers["answers"]["acceptance.criteria"]["answer"] == ""


def test_contract_prepare_cli_auto_does_not_overwrite_existing_answers_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return json.dumps(
                {
                    "answers": {
                        "scope.boundaries": {
                            "answer": "Auto-generated scope that should not replace human text.",
                            "notes": "Auto-generated notes that should not replace human notes.",
                        },
                        "acceptance.criteria": {
                            "answer": "Owners can invite teammates by email.",
                            "notes": "Supported by the task.",
                        },
                    }
                }
            )

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as first_exc,
    ):
        main()
    capsys.readouterr()

    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    existing_answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    existing_answers["answers"]["scope.boundaries"] = {
        "answer": "Human-filled scope should stay.",
        "notes": "Human-filled notes should stay.",
    }
    answers_path.write_text(yaml.safe_dump(existing_answers, sort_keys=False), encoding="utf-8")

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
                str(task_path),
                "--auto",
                "--answers",
                str(answers_path),
                "--output",
                str(output_path),
            ],
        ),
    ):
        main()

    out = capsys.readouterr().out
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))

    assert first_exc.value.code == 1
    assert output_path.exists()
    assert "Auto-applied answers: 1" in out
    assert answers["answers"]["scope.boundaries"] == {
        "answer": "Human-filled scope should stay.",
        "notes": "Human-filled notes should stay.",
    }
    assert answers["answers"]["acceptance.criteria"] == {
        "answer": "Owners can invite teammates by email.",
        "notes": "Supported by the task.",
    }


def test_contract_prepare_cli_auto_does_not_replace_supplied_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return json.dumps(
                {
                    "answers": {
                        "acceptance.criteria": {
                            "answer": "Auto-generated acceptance criteria should not replace human text.",
                            "notes": "Auto-generated notes should not replace human notes.",
                        },
                        "scope.boundaries": {
                            "answer": "Add email invites only.",
                            "notes": "Supported by the task.",
                        },
                    }
                }
            )

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()
    capsys.readouterr()

    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    existing_answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    existing_answers["answers"]["acceptance.criteria"] = {
        "answer": "Human-filled acceptance criteria should stay.",
        "notes": "Human-filled notes should stay.",
    }
    answers_path.write_text(yaml.safe_dump(existing_answers, sort_keys=False), encoding="utf-8")

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
                str(task_path),
                "--auto",
                "--answers",
                str(answers_path),
                "--output",
                str(output_path),
            ],
        ),
    ):
        main()

    out = capsys.readouterr().out
    answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    output = output_path.read_text(encoding="utf-8")

    assert output_path.exists()
    assert "Auto-applied answers: 1" in out
    assert "Implementation contract written:" in out
    assert answers["answers"]["acceptance.criteria"] == {
        "answer": "Human-filled acceptance criteria should stay.",
        "notes": "Human-filled notes should stay.",
    }
    assert answers["answers"]["scope.boundaries"] == {
        "answer": "Add email invites only.",
        "notes": "Supported by the task.",
    }
    assert "Add email invites only." in output
    assert "Human-filled acceptance criteria should stay." in output
    assert "Auto-generated acceptance criteria" not in output


def test_contract_prepare_cli_auto_records_parse_failure_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    class FakeLLM:
        def run_readonly_agent(self, _prompt: str, cwd: Path) -> str:
            assert cwd == tmp_path
            return "{not json"

    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", return_value=FakeLLM()),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    audit_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.auto-llm.jsonl"
    audit_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])

    assert exc.value.code == 1
    assert not output_path.exists()
    assert "Failed to auto-prepare contract" in err
    assert audit_record["generated_by"] == "sikula.contract_prepare"
    assert audit_record["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert audit_record["output"]["path"] == ".sikula/contracts/team-invites.contract.md"
    assert audit_record["record"]["phase"] == "contract_prepare_auto"
    assert audit_record["record"]["raw_output"] == "{not json"
    assert audit_record["record"]["parsed"]["status"] == "failed"
    assert "not valid JSON" in audit_record["record"]["parsed"]["error"]


def test_contract_prepare_cli_auto_rejects_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--interactive"]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "--auto cannot be combined with --interactive" in err


def test_contract_prepare_cli_auto_project_context_blocker_does_not_call_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        """# Team invites

## Scope
- Add invite creation endpoint.

## Acceptance criteria
- Owner/admin can invite a user by email.
- Non-admin users cannot invite users.

## Validation
- `pytest`
""",
        encoding="utf-8",
    )
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": []}),
        patch("core.llm_client.create_llm_client", side_effect=AssertionError("LLM should not be created")),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert not output_path.exists()
    assert "Contract preparation needs project context before writing an implementation contract." in out
    assert "Contract preparation answers template written:" not in out


def test_contract_prepare_cli_auto_existing_output_does_not_call_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("# Existing contract\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("core.llm_client.create_llm_client", side_effect=AssertionError("LLM must not be created")),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--auto", "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "refusing to overwrite existing output file" in err
    assert "Choose a new --output path" in err


def test_contract_prepare_cli_same_stem_answers_templates_use_task_specific_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    web_task_path = tmp_path / ".sikula" / "tasks" / "web" / "invite.md"
    mobile_task_path = tmp_path / ".sikula" / "tasks" / "mobile" / "invite.md"
    web_task_path.parent.mkdir(parents=True)
    mobile_task_path.parent.mkdir(parents=True)
    task_text = "# Add team invites\n\nUsers should be able to invite teammates by email."
    web_task_path.write_text(task_text, encoding="utf-8")
    mobile_task_path.write_text(task_text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(web_task_path)]),
        pytest.raises(SystemExit) as first_exit,
    ):
        main()

    capsys.readouterr()
    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(mobile_task_path)]),
        pytest.raises(SystemExit) as second_exit,
    ):
        main()

    assert first_exit.value.code == 1
    assert second_exit.value.code == 1
    report_dir = tmp_path / ".sikula" / "contract-reports"
    base_answers_path = report_dir / "invite.contract-prepare.answers.yaml"
    hashed_answers_paths = sorted(report_dir.glob("invite-*.contract-prepare.answers.yaml"))
    assert base_answers_path.exists()
    assert len(hashed_answers_paths) == 1
    base_answers = yaml.safe_load(base_answers_path.read_text(encoding="utf-8"))
    hashed_answers = yaml.safe_load(hashed_answers_paths[0].read_text(encoding="utf-8"))
    assert base_answers["task"]["path"] == ".sikula/tasks/web/invite.md"
    assert hashed_answers["task"]["path"] == ".sikula/tasks/mobile/invite.md"


def test_prepare_answers_path_rejects_base_and_hashed_collisions(tmp_path: Path):
    source_path = tmp_path / ".sikula" / "tasks" / "mobile" / "invite.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# Add mobile invite\n", encoding="utf-8")
    report_dir = tmp_path / ".sikula" / "contract-reports"
    report_dir.mkdir(parents=True)
    cfg = {
        "_config_path": str(tmp_path / ".sikula" / "config.yaml"),
        "project": {"root_path": str(tmp_path)},
        "tasks": {"contract_report_dir": ".sikula/contract-reports"},
    }
    base_path = report_dir / "invite.contract-prepare.answers.yaml"
    hashed_path = report_dir / (
        f"invite-{sha256(str(source_path.resolve()).encode('utf-8')).hexdigest()[:8]}.contract-prepare.answers.yaml"
    )
    other_task_template = {
        "schema_version": 1,
        "generated_by": "sikula.contract_prepare",
        "task": {
            "path": ".sikula/tasks/web/invite.md",
            "sha256": "sha256:other",
        },
        "answers": {},
    }
    base_path.write_text(yaml.safe_dump(other_task_template, sort_keys=False), encoding="utf-8")
    hashed_path.write_text(yaml.safe_dump(other_task_template, sort_keys=False), encoding="utf-8")

    with pytest.raises(FileExistsError, match="answers path already exists for a different task"):
        _prepare_answers_path(source_path, cfg, generated_by="sikula.contract_prepare")


@pytest.mark.parametrize(
    "base_contents",
    [
        "not: [valid",
        "- not a mapping\n",
        yaml.safe_dump(
            {
                "schema_version": 1,
                "generated_by": "sikula.contract_check",
                "task": {"path": ".sikula/tasks/mobile/invite.md"},
            },
            sort_keys=False,
        ),
        yaml.safe_dump(
            {
                "schema_version": 1,
                "generated_by": "sikula.contract_prepare",
                "task": {"path": ""},
            },
            sort_keys=False,
        ),
    ],
)
def test_prepare_answers_path_hashes_when_base_file_is_not_same_prepare_task(
    tmp_path: Path,
    base_contents: str,
):
    source_path = tmp_path / ".sikula" / "tasks" / "mobile" / "invite.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("# Add mobile invite\n", encoding="utf-8")
    report_dir = tmp_path / ".sikula" / "contract-reports"
    report_dir.mkdir(parents=True)
    cfg = {
        "_config_path": str(tmp_path / ".sikula" / "config.yaml"),
        "project": {"root_path": str(tmp_path)},
        "tasks": {"contract_report_dir": ".sikula/contract-reports"},
    }
    base_path = report_dir / "invite.contract-prepare.answers.yaml"
    base_path.write_text(base_contents, encoding="utf-8")

    answers_path = _prepare_answers_path(source_path, cfg, generated_by="sikula.contract_prepare")

    assert answers_path != base_path
    assert answers_path.name.startswith("invite-")
    assert answers_path.name.endswith(".contract-prepare.answers.yaml")


def test_prepare_answers_path_without_config_falls_back_to_cwd_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source_path = tmp_path / "task.md"
    source_path.write_text("# Add task\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    answers_path = _prepare_answers_path(source_path, {}, generated_by="sikula.task_refine")

    assert answers_path == tmp_path / ".sikula" / "contract-reports" / "task.task-refine.answers.yaml"


def test_contract_prepare_cli_regenerates_stale_default_answers_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit),
    ):
        main()

    capsys.readouterr()
    answers_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract-prepare.answers.yaml"
    first_answers = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    first_sha = first_answers["task"]["sha256"]
    first_answers["answers"]["acceptance.criteria"]["answer"] = "Owners can invite teammates by email."
    answers_path.write_text(yaml.safe_dump(first_answers, sort_keys=False), encoding="utf-8")
    task_path.write_text(
        "# Add team invites\n\nUsers should be able to invite teammates by email and role.",
        encoding="utf-8",
    )

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": ["pytest"]}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    regenerated = yaml.safe_load(answers_path.read_text(encoding="utf-8"))
    assert exc.value.code == 1
    assert "Contract preparation answers template written:" in out
    assert regenerated["task"]["sha256"] != first_sha
    assert regenerated["answers"]["acceptance.criteria"]["answer"] == ""
    assert regenerated["previous_answers"][0]["task"]["sha256"] == first_sha
    assert regenerated["previous_answers"][0]["answers"]["acceptance.criteria"]["answer"] == (
        "Owners can invite teammates by email."
    )


def test_contract_prepare_cli_writes_ready_contract_without_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
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
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch(
            "sikula._prepare_project_context_from_config",
            return_value={"validation_commands": ["pytest", "ruff check ."]},
        ),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
    ):
        main()

    out = capsys.readouterr().out
    assert output_path.exists()
    metadata_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.contract.generated-answers.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["generated_by"] == "sikula.contract_prepare"
    assert metadata["task"]["path"] == ".sikula/contracts/team-invites.contract.md"
    assert metadata["generated_answers"] == []
    assert "Implementation contract written:" in out
    assert "Open questions:" in out
    assert f"Next step: sikula run {output_path}" in out


def test_contract_prepare_cli_project_context_blocker_does_not_write_answers_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
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
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sikula._prepare_project_context_from_config", return_value={"validation_commands": []}),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert not output_path.exists()
    assert not (tmp_path / ".sikula" / "contract-reports").exists()
    assert "Contract preparation needs project context before writing an implementation contract." in out
    assert "No effective validation commands were found in the Sikula project config." in out
    assert "Contract preparation answers template written:" not in out
    assert "Fill the answers file" not in out
    assert "sikula contract prepare" in out


def test_contract_prepare_cli_without_config_does_not_invent_gradle_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
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

## Reviewer focus
- Authorization rules.
- Token expiry and reuse.
- Email enumeration behaviour.
""",
        encoding="utf-8",
    )
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert not output_path.exists()
    assert "Contract preparation needs project context before writing an implementation contract." in out
    assert "No project context was provided." in out
    assert "./gradlew" not in out
    assert "compileDebugKotlin" not in out
    assert "Contract preparation answers template written:" not in out


def test_contract_prepare_cli_interactive_without_config_does_not_prompt_for_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
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

## Reviewer focus
- Authorization rules.
- Token expiry and reuse.
- Email enumeration behaviour.
""",
        encoding="utf-8",
    )
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with (
        patch(
            "sys.argv", ["sikula", "contract", "prepare", str(task_path), "--interactive", "--output", str(output_path)]
        ),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=AssertionError("project context must be requested before answers")),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert not output_path.exists()
    assert not (tmp_path / ".sikula" / "contract-reports").exists()
    assert "Contract preparation needs project context before writing an implementation contract." in out
    assert "Interactive contract preparation answers:" not in out


def test_contract_prepare_cli_filters_autofix_commands_from_project_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    config_path = tmp_path / ".sikula" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """project:
  root_path: .
  build_tool: python
  language: Python
build:
  test_command: pytest
  checks:
    - name: format
      command: ruff format --check .
      fix_command: ruff format .
run_build: true
run_tests: true
run_checks: true
""",
        encoding="utf-8",
    )
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
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

## Reviewer focus
- Authorization rules.
- Token expiry and reuse.
- Email enumeration behaviour.
""",
        encoding="utf-8",
    )
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    monkeypatch.chdir(tmp_path)

    with patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]):
        main()

    out = capsys.readouterr().out
    output = output_path.read_text(encoding="utf-8")
    assert "Implementation contract written:" in out
    assert "- `ruff check .`" in output
    assert "- `ruff format --check .`" in output
    assert "- `ruff format .`" not in output


def test_contract_prepare_cli_existing_output_prints_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    task_path = tmp_path / ".sikula" / "tasks" / "team-invites.refined.md"
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
    output_path = tmp_path / ".sikula" / "contracts" / "team-invites.contract.md"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("# Existing contract\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with (
        patch(
            "sikula._prepare_project_context_from_config",
            return_value={"validation_commands": ["pytest", "ruff check ."]},
        ),
        patch("sys.argv", ["sikula", "contract", "prepare", str(task_path), "--output", str(output_path)]),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "refusing to overwrite existing output file" in err
    assert "Choose a new --output path" in err


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
    assert "Generated contract report artifacts:" in out
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
