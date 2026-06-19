from __future__ import annotations

from pathlib import Path

import pytest

from agents.task_preparation_agent import TaskPreparationAgent
from core.contract_auto_prepare import ContractAutoPrepareRequest
from core.task_auto_refine import TaskAutoRefineRequest


class CapturingLLM:
    def __init__(self, output: str) -> None:
        self.output = output
        self.prompts: list[str] = []

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.prompts.append(prompt)
        return self.output


def test_task_preparer_contract_prompt_disallows_unreferenced_sikula_artifacts(tmp_path: Path):
    llm = CapturingLLM('{"answers": {}}')
    agent = TaskPreparationAgent(
        llm=llm,
        project_config={
            "tasks": {
                "task_description_dir": "docs/product-tasks",
                "contract_dir": "docs/contracts",
                "contract_report_dir": ".generated/sikula-contract-reports",
            }
        },
    )

    agent.propose_contract_answers(
        ContractAutoPrepareRequest(
            contract_markdown="# Add team invites\n",
            contract_name="team-invites.contract.md",
            project_context={"validation_commands": ["pytest"]},
            user_questions=[{"id": "privacy.data_handling", "question": "What data must not be logged?"}],
            round_index=1,
        ),
        project_root=tmp_path,
    )

    prompt = llm.prompts[0]
    assert "Configured Sikula workflow artifact directories:" in prompt
    assert "- task descriptions: docs/product-tasks" in prompt
    assert "- implementation contracts: docs/contracts" in prompt
    assert "- reports and answers: .generated/sikula-contract-reports" in prompt
    assert "unless the current draft explicitly references them" in prompt
    assert "previous/example tasks and contracts as non-authoritative" in prompt


def test_task_preparer_contract_answer_result_includes_audit_record(tmp_path: Path):
    llm = CapturingLLM(
        '{"answers": {"scope.boundaries": {"answer": "Add email invites only.", "notes": "Task title."}}}'
    )
    agent = TaskPreparationAgent(llm=llm)

    batch = agent.propose_contract_answers(
        ContractAutoPrepareRequest(
            contract_markdown="# Add team invites\n",
            contract_name="team-invites.contract.md",
            project_context={"validation_commands": ["pytest"]},
            user_questions=[{"id": "scope.boundaries", "question": "What is in scope?"}],
            round_index=2,
        ),
        project_root=tmp_path,
    )

    assert len(batch.audit_records) == 1
    record = batch.audit_records[0]
    assert record["phase"] == "contract_prepare_auto"
    assert record["round_index"] == 2
    assert "Active questions:" in record["prompt"]
    assert record["raw_output"] == llm.output
    assert record["parsed"]["answered_question_ids"] == ["scope.boundaries"]


def test_task_preparer_contract_answer_records_parse_failure(tmp_path: Path):
    llm = CapturingLLM("{not json")
    agent = TaskPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(ValueError, match="not valid JSON"):
        agent.propose_contract_answers(
            ContractAutoPrepareRequest(
                contract_markdown="# Add team invites\n",
                contract_name="team-invites.contract.md",
                project_context={"validation_commands": ["pytest"]},
                user_questions=[{"id": "scope.boundaries", "question": "What is in scope?"}],
                round_index=2,
            ),
            project_root=tmp_path,
            audit_recorder=audit_records.append,
        )

    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "contract_prepare_auto"
    assert record["round_index"] == 2
    assert "Active questions:" in record["prompt"]
    assert record["raw_output"] == llm.output
    assert record["parsed"]["status"] == "failed"
    assert "not valid JSON" in record["parsed"]["error"]


def test_task_preparer_refine_prompt_disallows_unreferenced_sikula_artifacts(tmp_path: Path):
    llm = CapturingLLM('{"task_markdown": "# Add team invites\\n\\nUsers can invite teammates by email."}')
    agent = TaskPreparationAgent(
        llm=llm,
        project_config={
            "tasks": {
                "task_description_dir": "docs/product-tasks",
                "contract_dir": "docs/contracts",
                "contract_report_dir": ".generated/sikula-contract-reports",
            }
        },
    )

    agent.normalize_task_description(
        TaskAutoRefineRequest(
            brief="Potrebujeme pozvanky do tymu.",
            task_name="team-invites.txt",
        ),
        project_root=tmp_path,
    )

    prompt = llm.prompts[0]
    assert "Configured Sikula workflow artifact directories:" in prompt
    assert "- task descriptions: docs/product-tasks" in prompt
    assert "- implementation contracts: docs/contracts" in prompt
    assert "- reports and answers: .generated/sikula-contract-reports" in prompt
    assert "unless the raw task explicitly references them" in prompt


def test_task_preparer_refine_result_includes_audit_record(tmp_path: Path):
    llm = CapturingLLM(
        '{"task_markdown": "# Add team invites\\n\\nUsers can invite teammates by email.", '
        '"input_language": "cs", "normalized_to_english": true}'
    )
    agent = TaskPreparationAgent(llm=llm)

    draft = agent.normalize_task_description(
        TaskAutoRefineRequest(
            brief="Uzivatel muze pozvat kolegu emailem.",
            task_name="team-invites.txt",
        ),
        project_root=tmp_path,
    )

    assert len(draft.audit_records) == 1
    record = draft.audit_records[0]
    assert record["phase"] == "task_refine_auto"
    assert record["round_index"] == 1
    assert "Raw task description:" in record["prompt"]
    assert record["raw_output"] == llm.output
    assert record["parsed"]["input_language"] == "cs"
    assert record["parsed"]["normalized_to_english"] is True


def test_task_preparer_refine_records_parse_failure(tmp_path: Path):
    llm = CapturingLLM("{not json")
    agent = TaskPreparationAgent(llm=llm)
    audit_records: list[dict] = []

    with pytest.raises(ValueError, match="not valid JSON"):
        agent.normalize_task_description(
            TaskAutoRefineRequest(
                brief="Uzivatel muze pozvat kolegu emailem.",
                task_name="team-invites.txt",
            ),
            project_root=tmp_path,
            audit_recorder=audit_records.append,
        )

    assert len(audit_records) == 1
    record = audit_records[0]
    assert record["phase"] == "task_refine_auto"
    assert record["round_index"] == 1
    assert "Raw task description:" in record["prompt"]
    assert record["raw_output"] == llm.output
    assert record["parsed"]["status"] == "failed"
    assert "not valid JSON" in record["parsed"]["error"]
