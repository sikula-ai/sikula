"""Read-only LLM assistant for task and contract preparation."""

from __future__ import annotations

import json
from pathlib import Path

from agents.base_agent import AGENT_SECURITY_PREFIX, guidelines_files, tech_stack
from core.contract_auto_prepare import (
    ContractAutoAnswerBatch,
    ContractAutoPrepareRequest,
    parse_contract_auto_answer_output,
)
from core.llm_client import LLMClient

_AGENT_PROMPT = """\
You are Sikula's read-only task and implementation-contract preparation assistant.

Your job is to propose answers only for the active preparation questions that can be
answered from the task description, checked-in project files, Sikula config, or project guidelines.

Hard rules:
- Do not write, edit, delete, or create files.
- Do not run network commands or inspect external services.
- Do not invent product requirements, business policy, security policy, privacy policy, or validation commands.
- If the answer is not directly supported by the task or repository, leave that question unanswered.
- Answer only active question IDs from the JSON below.
- Return exactly one JSON object and no Markdown.

Project stack: {project_stack}

Configured project context:
```json
{project_context_json}
```

Project guidelines files to inspect when useful:
{guidelines_files}

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
        return parse_contract_auto_answer_output(output, active_ids)

    def _build_prompt(self, request: ContractAutoPrepareRequest) -> str:
        project_context_json = json.dumps(request.project_context or {}, indent=2, sort_keys=True)
        questions_json = json.dumps(request.user_questions, indent=2, sort_keys=True)
        return AGENT_SECURITY_PREFIX + _AGENT_PROMPT.format(
            project_stack=tech_stack(self.project_config),
            project_context_json=project_context_json,
            guidelines_files=guidelines_files(self.project_config),
            questions_json=questions_json,
            contract_markdown=request.contract_markdown,
        )
