"""Read-only LLM assistant for task and contract preparation."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from agents.base_agent import AGENT_SECURITY_PREFIX, guidelines_files, tech_stack
from core.contract_auto_prepare import (
    ContractAutoAnswerBatch,
    ContractAutoPrepareRequest,
    parse_contract_auto_answer_output,
)
from core.llm_client import LLMClient
from core.task_auto_refine import (
    TaskAutoRefineDraft,
    TaskAutoRefineRequest,
    parse_task_auto_refine_output,
)

_CONTRACT_ANSWER_PROMPT = """\
You are Sikula's read-only task and implementation-contract preparation assistant.

Your job is to propose answers only for the active preparation questions that can be
answered from the task description, checked-in project files, Sikula config, or project guidelines.

Hard rules:
- Do not write, edit, delete, or create files.
- Do not run network commands or inspect external services.
- Do not invent product requirements, business policy, security policy, privacy policy, or validation commands.
- If the answer is not directly supported by the task or repository, leave that question unanswered.
- Do not use other Sikula workflow artifacts from the configured workflow artifact directories as evidence
  unless the current draft explicitly references them.
- Treat previous/example tasks and contracts as non-authoritative; never infer product, authorization, security,
  privacy, or validation policy from unrelated Sikula task/contract artifacts.
- Answer only active question IDs from the JSON below.
- Return exactly one JSON object and no Markdown.

Project stack: {project_stack}

Configured project context:
```json
{project_context_json}
```

Project guidelines files to inspect when useful:
{guidelines_files}

Configured Sikula workflow artifact directories:
{workflow_artifact_dirs}

Active questions:
```json
{questions_json}
```

Current preparation draft:
```markdown
{contract_markdown}
```

Return this JSON shape:
{{
  "answers": {{
    "question.id": {{
      "answer": "Concise answer supported by the task or repository.",
      "notes": "Optional short rationale or source pointer without quoting source."
    }}
  }},
  "unanswered": [
    {{"id": "question.id", "reason": "Why this requires a human answer."}}
  ],
  "warnings": []
}}
"""

_TASK_NORMALIZATION_PROMPT = """\
You are Sikula's read-only product task-description refinement assistant.

Your job is to normalize the raw task description into clean English Markdown that preserves
the original product intent and is easier for Sikula's deterministic task-refine core to check.

Hard rules:
- Do not write, edit, delete, or create files.
- Do not run network commands or inspect external services.
- Preserve the user's product intent, scope, and constraints.
- Translate non-English input to English when needed.
- Do not invent product requirements, business policy, security policy, privacy policy, validation commands, files, APIs, or implementation details.
- Use project guidelines only for terminology and product context that is already evident; do not infer new requirements from generic best practices.
- Do not use other Sikula workflow artifacts from the configured workflow artifact directories as product evidence
  unless the raw task explicitly references them.
- Do not include a Sikula Open questions section. Sikula will add open questions after deterministic validation.
- Return exactly one JSON object and no Markdown outside the JSON.

Prefer this Markdown shape when supported by the source:

# Short product task title

## Goal

Plain-language user or business outcome.

## Scope

- Product changes that are clearly in scope.

## Acceptance criteria

- Observable behaviours that are clearly required by the source.

## Out of scope

- Adjacent product changes explicitly excluded by the source.

## Context

- Existing product/domain context explicitly mentioned or safely named from the source.

Project stack: {project_stack}

Project guidelines files to inspect when useful:
{guidelines_files}

Configured Sikula workflow artifact directories:
{workflow_artifact_dirs}

Provided product context:
```json
{product_context_json}
```

Task name: {task_name}

Raw task description:
```text
{brief}
```

Return this JSON shape:
{{
  "task_markdown": "# Title\\n\\n## Goal\\n\\n...",
  "input_language": "cs",
  "normalized_to_english": true,
  "warnings": []
}}
"""


class TaskPreparationAgent:
    """Ask a read-only LLM for safe answers to active preparation questions."""

    name = "task_preparer"

    def __init__(self, llm: LLMClient, project_config: dict | None = None) -> None:
        self.llm = llm
        self.project_config = project_config or {}

    def propose_contract_answers(
        self,
        request: ContractAutoPrepareRequest,
        *,
        project_root: Path,
    ) -> ContractAutoAnswerBatch:
        active_ids = {
            str(question.get("id") or "").strip()
            for question in request.user_questions
            if isinstance(question, dict) and str(question.get("id") or "").strip()
        }
        prompt = self._build_prompt(request)
        output = self.llm.run_readonly_agent(prompt, cwd=project_root)
        batch = parse_contract_auto_answer_output(output, active_ids)
        return replace(
            batch,
            audit_records=[
                {
                    "phase": "contract_prepare_auto",
                    "round_index": request.round_index,
                    "prompt": prompt,
                    "raw_output": output,
                    "parsed": {
                        "answered_question_ids": sorted(batch.answers),
                        "unanswered": batch.unanswered,
                        "warnings": batch.warnings,
                    },
                }
            ],
        )

    def normalize_task_description(
        self,
        request: TaskAutoRefineRequest,
        *,
        project_root: Path,
    ) -> TaskAutoRefineDraft:
        prompt = self._build_task_normalization_prompt(request)
        output = self.llm.run_readonly_agent(prompt, cwd=project_root)
        draft = parse_task_auto_refine_output(output)
        return replace(
            draft,
            audit_records=[
                {
                    "phase": "task_refine_auto",
                    "round_index": 1,
                    "prompt": prompt,
                    "raw_output": output,
                    "parsed": {
                        "input_language": draft.input_language,
                        "normalized_to_english": draft.normalized_to_english,
                        "warnings": draft.warnings,
                    },
                }
            ],
        )

    def _build_prompt(self, request: ContractAutoPrepareRequest) -> str:
        project_context_json = json.dumps(request.project_context or {}, indent=2, sort_keys=True)
        questions_json = json.dumps(request.user_questions, indent=2, sort_keys=True)
        return AGENT_SECURITY_PREFIX + _CONTRACT_ANSWER_PROMPT.format(
            project_stack=tech_stack(self.project_config),
            project_context_json=project_context_json,
            guidelines_files=guidelines_files(self.project_config),
            workflow_artifact_dirs=self._workflow_artifact_dirs(),
            questions_json=questions_json,
            contract_markdown=request.contract_markdown,
        )

    def _build_task_normalization_prompt(self, request: TaskAutoRefineRequest) -> str:
        task_name = request.task_name or "task"
        product_context_json = json.dumps(request.product_context or {}, indent=2, sort_keys=True)
        return AGENT_SECURITY_PREFIX + _TASK_NORMALIZATION_PROMPT.format(
            project_stack=tech_stack(self.project_config),
            guidelines_files=guidelines_files(self.project_config),
            workflow_artifact_dirs=self._workflow_artifact_dirs(),
            product_context_json=product_context_json,
            task_name=task_name,
            brief=request.brief,
        )

    def _workflow_artifact_dirs(self) -> str:
        tasks = self.project_config.get("tasks", {}) if isinstance(self.project_config.get("tasks"), dict) else {}
        values = [
            ("task descriptions", tasks.get("task_description_dir") or ".sikula/tasks"),
            ("implementation contracts", tasks.get("contract_dir") or ".sikula/contracts"),
            ("reports and answers", tasks.get("contract_report_dir") or ".sikula/contract-reports"),
        ]
        return "\n".join(f"- {label}: {path}" for label, path in values)
