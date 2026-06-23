"""E2E smoke tests for task refine and contract prepare CLI preparation flow."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import yaml

from sikula import main


class ContractPreparationFakeLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, system: str, user: str) -> str:
        raise AssertionError("contract preparation smoke test must not call generate()")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        raise AssertionError("contract preparation smoke test must not call write agents")

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.prompts.append(prompt)
        if "Raw task description:" in prompt:
            return json.dumps(
                {
                    "task_markdown": """# Add team invites

## Goal

Users should be able to invite teammates by email.

## Scope

- Add single-teammate invite creation from team settings.

## Acceptance criteria

- Team owners and admins can invite a teammate by valid email.
- Empty or malformed email input is rejected.
- Duplicate pending invites show a deterministic error.

## Out of scope

- Do not add billing seat enforcement.
- Do not add bulk invites.
""",
                    "input_language": "en",
                    "normalized_to_english": False,
                }
            )
        if "Active questions:" in prompt:
            return json.dumps(
                {
                    "answers": {
                        "token.lifecycle": {
                            "answer": "Invitation tokens expire and cannot be reused after acceptance.",
                            "notes": "Supported by expected invite lifecycle requirements.",
                        },
                        "privacy.data_handling": {
                            "answer": "Do not log invite tokens or reveal whether an email already has an account.",
                            "notes": "Required for invitation privacy.",
                        },
                        "reviewer.focus": {
                            "answer": "Review authorization, duplicate invite handling, token lifecycle, and privacy-safe errors.",
                            "notes": "",
                        },
                        "context.domain_rules": {
                            "answer": "Follow existing team settings, authorization, mailer, and persistence conventions.",
                            "notes": "",
                        },
                    }
                }
            )
        raise AssertionError(f"unexpected readonly prompt:\n{prompt}")


def test_task_refine_auto_to_contract_prepare_auto_writes_auditable_artifacts(
    git_project: Path,
    monkeypatch,
    capsys,
) -> None:
    project_root = git_project
    config_path = project_root / ".sikula" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "contract-prep-smoke",
                    "build_tool": "python",
                    "root_path": str(project_root),
                    "language": "Python",
                },
                "tasks": {
                    "task_description_dir": ".sikula/tasks",
                    "contract_dir": ".sikula/contracts",
                    "contract_report_dir": ".sikula/contract-reports",
                },
                "build": {
                    "test_command": "pytest",
                    "checks": [{"name": "ruff", "command": "ruff check ."}],
                },
                "run_build": True,
                "run_tests": True,
                "run_checks": True,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    task_path = project_root / ".sikula" / "tasks" / "team-invites.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text("# Add team invites\n\nUsers should be able to invite teammates by email.", encoding="utf-8")
    refined_path = project_root / ".sikula" / "tasks" / "team-invites.refined.md"
    contract_path = project_root / ".sikula" / "contracts" / "team-invites.contract.md"
    fake_llm = ContractPreparationFakeLLM()
    monkeypatch.chdir(project_root)

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
                "--output",
                str(refined_path),
            ],
        ),
    ):
        main()

    first_out = capsys.readouterr().out
    assert "Refined task description written:" in first_out
    assert "Next step: sikula contract prepare" in first_out
    assert refined_path.exists()
    refined_markdown = refined_path.read_text(encoding="utf-8")
    assert "## Acceptance criteria" in refined_markdown
    assert "Team owners and admins can invite" in refined_markdown

    with (
        patch("core.llm_client.create_llm_client", return_value=fake_llm),
        patch(
            "sys.argv",
            [
                "sikula",
                "contract",
                "prepare",
                str(refined_path),
                "--auto",
                "--output",
                str(contract_path),
            ],
        ),
    ):
        main()

    second_out = capsys.readouterr().out
    assert "Implementation contract written:" in second_out
    assert "Auto-applied answers:" in second_out
    assert contract_path.exists()
    contract_markdown = contract_path.read_text(encoding="utf-8")
    assert "Invitation tokens expire" in contract_markdown
    assert "Do not log invite tokens" in contract_markdown
    assert "## Validation" in contract_markdown
    assert "`pytest`" in contract_markdown
    assert "`ruff check .`" in contract_markdown

    report_dir = project_root / ".sikula" / "contract-reports"
    task_audit_path = report_dir / "team-invites.task-refine.auto-llm.jsonl"
    contract_audit_path = report_dir / "team-invites.contract-prepare.auto-llm.jsonl"
    assert task_audit_path.exists()
    assert contract_audit_path.exists()

    task_audit = json.loads(task_audit_path.read_text(encoding="utf-8").splitlines()[0])
    contract_audit = json.loads(contract_audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert task_audit["generated_by"] == "sikula.task_refine"
    assert task_audit["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert task_audit["output"]["path"] == ".sikula/tasks/team-invites.refined.md"
    assert task_audit["record"]["phase"] == "task_refine_auto"
    assert "Raw task description:" in task_audit["record"]["prompt"]
    assert "task_markdown" in task_audit["record"]["raw_output"]
    assert contract_audit["generated_by"] == "sikula.contract_prepare"
    assert contract_audit["task"]["path"] == ".sikula/tasks/team-invites.refined.md"
    assert contract_audit["output"]["path"] == ".sikula/contracts/team-invites.contract.md"
    assert contract_audit["record"]["phase"] == "contract_prepare_auto"
    assert "Active questions:" in contract_audit["record"]["prompt"]
    assert "privacy.data_handling" in contract_audit["record"]["raw_output"]
    assert len(fake_llm.prompts) == 2


def test_contract_prepare_asset_manifest_round_trips_through_contract_check(
    git_project: Path,
    monkeypatch,
    capsys,
) -> None:
    project_root = git_project
    config_path = project_root / ".sikula" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "asset-contract-smoke",
                    "build_tool": "python",
                    "root_path": str(project_root),
                    "language": "Python",
                },
                "tasks": {
                    "task_description_dir": ".sikula/tasks",
                    "contract_dir": ".sikula/contracts",
                    "contract_report_dir": ".sikula/contract-reports",
                    "task_asset_dir": ".sikula/task-assets",
                },
                "build": {"test_command": "pytest"},
                "run_build": True,
                "run_tests": True,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    asset_path = project_root / ".sikula" / "task-assets" / "success-check.svg"
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_text("<svg />", encoding="utf-8")
    task_path = project_root / ".sikula" / "tasks" / "success-icon.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        """# Add success icon

## Assets

### Delivery assets

- Path: `.sikula/task-assets/success-check.svg`
  - Usage: delivery asset.
  - Purpose: success state icon.
  - Target: app/assets/success-check.svg
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
    subprocess.run(["git", "add", ".sikula"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add asset contract fixture"], cwd=project_root, check=True, capture_output=True
    )
    contract_path = project_root / ".sikula" / "contracts" / "success-icon.contract.md"
    monkeypatch.chdir(project_root)

    with patch(
        "sys.argv",
        [
            "sikula",
            "contract",
            "prepare",
            str(task_path),
            "--output",
            str(contract_path),
        ],
    ):
        main()

    prepare_output = capsys.readouterr().out
    contract_markdown = contract_path.read_text(encoding="utf-8")
    assert "Implementation contract written:" in prepare_output
    assert "## Asset manifest" in contract_markdown
    assert "### Delivery assets" in contract_markdown
    assert "- Path: `.sikula/task-assets/success-check.svg`" in contract_markdown
    assert "SHA-256: `sha256:" in contract_markdown
    assert "Requested target: `app/assets/success-check.svg`" in contract_markdown
    assert "Source/license: provided by product team for this project." in contract_markdown
    assert "MIME type:" not in contract_markdown
    assert "Size:" not in contract_markdown
    assert "Git status:" not in contract_markdown

    with patch("sys.argv", ["sikula", "contract", "check", str(contract_path), "--json"]):
        main()

    check_data = json.loads(capsys.readouterr().out)
    assert check_data["asset_references"][0]["project_path"] == ".sikula/task-assets/success-check.svg"
    assert check_data["asset_references"][0]["kind"] == "delivery"
    assert check_data["asset_references"][0]["status"] == "available"
    assert all(
        not (gap["id"].startswith("gap.assets.") and gap["severity"] == "blocking") for gap in check_data["gaps"]
    )
