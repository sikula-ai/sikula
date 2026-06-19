from __future__ import annotations

from pathlib import Path

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
