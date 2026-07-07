"""Read-only LLM assistant for delivery plan authoring."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from agents.base_agent import AGENT_SECURITY_PREFIX, guidelines_files, read_only_agent_prompt, tech_stack
from core.delivery_authoring import (
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
    parse_delivery_authoring_output,
)
from core.llm_client import LLMClient

DeliveryPreparationAuditRecorder = Callable[[dict[str, Any]], None]

_DEFAULT_MAX_GUIDELINES_CHARS = 3000

_DELIVERY_AUTHORING_PROMPT = """\
You are Sikula's read-only delivery-plan authoring assistant.

Your job is to split one source task description into a strict structured delivery-plan draft
for later deterministic writer code. You do not write files or create delivery artifacts.

Hard rules:
- Do not write, edit, delete, move, rename, format, or create files.
- Do not run commands at all, including read-only commands such as grep, find, ls, git, package,
  build, test, or language-runtime commands.
- Do not start nested Sikula commands.
- Do not inspect external services, make network requests, or use network commands.
- Do not include writer-facing path fields in unit objects, including task_path, path, unit_path,
  output_path, plan_path, units_dir, or output_dir.
- Do not include raw prompts, raw provider output, source excerpts, task state, diffs, logs, secrets,
  personal data, or absolute local paths in the JSON draft.
- Do not infer unsupported product, security, privacy, validation, platform, or release requirements
  beyond the source task and checked-in project context.
- Return exactly one JSON object and no Markdown outside the JSON.

Project stack: {project_stack}

Selected delivery plan id: {plan_id}
Source task file: {task_path}
Delivery output directory: {output_dir}

Project guidelines files:
{guidelines_files}

Configured guidelines content:
```markdown
{guidelines_context}
```

Project context:
```json
{project_context_json}
```

Configured validation commands:
```json
{validation_commands_json}
```

Delivery-plan constraints:
- plan_id must equal the selected delivery plan id.
- units must be non-empty.
- Unit IDs must be stable path-safe IDs using only letters, numbers, dots, underscores, and hyphens.
- Unit IDs must not contain path separators, absolute paths, ".", or "..".
- depends_on must reference known unit IDs only and must not contain duplicates, self-dependencies,
  or dependency cycles.
- Optional metadata fields stream, component, phase, kind, and platform must be non-empty strings
  when present.
- scope_paths must contain only project-relative paths that stay inside the project.
- Unit task Markdown must be product/behavior descriptions with acceptance criteria and verification
  expectations, not file-by-file implementation scripts.
- Unit task Markdown must include Goal, Current behavior, Desired behavior, Acceptance criteria,
  Security/privacy notes, Reviewer focus, Out of scope, and Verification sections.
- Verification sections must include explicit commands that match or are directly supported by the
  source task or configured validation commands.
- Unit task Markdown must not include "## Asset manifest" or sikula:generated-* markers.
- Paths for plan.yaml and unit task files are derived later from the output directory and unit IDs.

Source task description:
```markdown
{task_description}
```

Return this JSON shape:
{{
  "plan_id": "{plan_id}",
  "title": "Short delivery plan title",
  "planning_mode": "fixed_window",
  "warnings": [],
  "units": [
    {{
      "id": "stable-unit-id",
      "title": "Short unit title",
      "depends_on": [],
      "stream": "optional non-empty string",
      "component": "optional non-empty string",
      "phase": "optional non-empty string",
      "kind": "optional non-empty string",
      "platform": "optional non-empty string",
      "scope_paths": [],
      "task_markdown": "# Unit title\\n\\n## Goal\\n\\n...\\n\\n## Verification\\n\\n- `command`"
    }}
  ]
}}
"""


class DeliveryPreparationAgentError(RuntimeError):
    """Safe delivery-preparer invocation failure."""


class DeliveryPreparationAgent:
    """Ask a read-only LLM for a structured delivery authoring draft."""

    name = "delivery_preparer"

    def __init__(self, llm: LLMClient, project_config: dict | None = None) -> None:
        self.llm = llm
        self.project_config = project_config or {}

    def author_delivery_plan(
        self,
        *,
        task_description: str,
        task_path: str | Path,
        plan_id: str,
        project_root: str | Path,
        output_dir: str | Path,
        project_context: dict[str, Any] | None = None,
        audit_recorder: DeliveryPreparationAuditRecorder | None = None,
    ) -> DeliveryAuthoringDraft:
        root = Path(project_root).resolve()
        prompt = read_only_agent_prompt(
            self._build_authoring_prompt(
                task_description=task_description,
                task_path=task_path,
                plan_id=plan_id,
                project_root=root,
                output_dir=output_dir,
                project_context=project_context,
            )
        )
        try:
            output = self.llm.run_readonly_agent(prompt, cwd=root, allow_commands=False)
        except Exception as exc:
            self._record_failure(
                audit_recorder,
                prompt=prompt,
                output=None,
                error=exc,
                error_code="delivery_prepare.authoring_failed",
            )
            raise DeliveryPreparationAgentError("Delivery authoring assistant failed.") from None

        try:
            draft = parse_delivery_authoring_output(
                output,
                expected_plan_id=plan_id,
                project_root=root,
                output_dir=output_dir,
            )
        except DeliveryAuthoringParseError as exc:
            self._record_failure(
                audit_recorder,
                prompt=prompt,
                output=output,
                error=exc,
                error_code=exc.code,
            )
            raise

        self._record_success(audit_recorder, prompt=prompt, output=output, draft=draft)
        return draft

    def _build_authoring_prompt(
        self,
        *,
        task_description: str,
        task_path: str | Path,
        plan_id: str,
        project_root: Path,
        output_dir: str | Path,
        project_context: dict[str, Any] | None,
    ) -> str:
        context = project_context or {}
        validation_commands = context.get("validation_commands") if isinstance(context, dict) else None
        if not isinstance(validation_commands, list):
            validation_commands = []
        safe_validation_commands = [str(command) for command in validation_commands if str(command).strip()]
        return AGENT_SECURITY_PREFIX + _DELIVERY_AUTHORING_PROMPT.format(
            project_stack=tech_stack(self.project_config),
            plan_id=plan_id,
            task_path=self._project_relative_path(task_path, project_root),
            output_dir=self._project_relative_path(output_dir, project_root),
            guidelines_files=guidelines_files(self.project_config),
            guidelines_context=self._guidelines_context(project_root),
            project_context_json=json.dumps(context, indent=2, sort_keys=True),
            validation_commands_json=json.dumps(safe_validation_commands, indent=2, sort_keys=True),
            task_description=task_description,
        )

    def _guidelines_context(self, project_root: Path) -> str:
        guidelines_cfg = self.project_config.get("guidelines", {})
        if not isinstance(guidelines_cfg, dict):
            guidelines_cfg = {}
        configured_files = guidelines_cfg.get("context_files", ["README.md"])
        if not isinstance(configured_files, list):
            configured_files = ["README.md"]
        max_chars = guidelines_cfg.get("max_file_chars", _DEFAULT_MAX_GUIDELINES_CHARS)
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = _DEFAULT_MAX_GUIDELINES_CHARS
        max_chars = max(0, max_chars)

        parts: list[str] = []
        for raw_path in configured_files:
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            rel_path = raw_path.strip()
            if self._is_absolute_or_windows_absolute(rel_path):
                continue
            resolved = (project_root / rel_path).resolve()
            if not self._path_is_within(resolved, project_root):
                continue
            try:
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + f"\n... [truncated; inspect {rel_path} for full content]"
            parts.append(f"=== {rel_path} ===\n{content}")
        return "\n\n".join(parts) if parts else "No configured guidelines content found."

    def _project_relative_path(self, path: str | Path, project_root: Path) -> str:
        try:
            raw_path = Path(path)
            resolved = raw_path.resolve() if raw_path.is_absolute() else (project_root / raw_path).resolve()
            return resolved.relative_to(project_root).as_posix()
        except (OSError, RuntimeError, TypeError, ValueError):
            return "<outside-project>"

    def _record_success(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        prompt: str,
        output: str,
        draft: DeliveryAuthoringDraft,
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": "delivery_prepare_authoring",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "parsed",
                    "plan_id": draft.plan_id,
                    "unit_ids": [unit.id for unit in draft.units],
                    "unit_count": len(draft.units),
                    "planning_mode": draft.planning_mode,
                    "warnings": list(draft.warnings),
                },
            }
        )

    def _record_failure(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        prompt: str,
        output: str | None,
        error: Exception,
        error_code: str,
    ) -> None:
        if audit_recorder is None:
            return
        parsed: dict[str, Any] = {
            "status": "failed",
            "error_type": type(error).__name__,
            "error_code": error_code,
            "error": str(error),
        }
        audit_recorder(
            {
                "phase": "delivery_prepare_authoring",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": parsed,
            }
        )

    def _path_is_within(self, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _is_absolute_or_windows_absolute(self, path: str) -> bool:
        windows_path = PureWindowsPath(path)
        return Path(path).is_absolute() or windows_path.is_absolute() or bool(windows_path.drive)
