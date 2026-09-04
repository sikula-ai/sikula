"""Read-only LLM assistant for delivery plan authoring."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from agents.base_agent import AGENT_SECURITY_PREFIX, guidelines_files, read_only_agent_prompt, tech_stack
from core.delivery_authoring import (
    DeliveryAmendmentAuthoringDraft,
    DeliveryAssessmentDraft,
    DeliveryAuthoringConstraintDraft,
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
    DeliveryConstraintGap,
    DeliveryConstraintVerification,
    apply_delivery_unit_context_gaps,
    parse_delivery_assessment_output,
    parse_delivery_amendment_authoring_output,
    parse_delivery_authoring_output,
    parse_delivery_constraint_repair_output,
    parse_delivery_constraint_verification_output,
)
from core.delivery_plan import DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION, DeliveryPlanSourceTask
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
- Do not include raw prompts, raw provider output, unrelated source excerpts, task state, diffs,
  logs, secrets, personal data, or absolute local paths in the JSON draft. Source-defined exact
  identifiers and values required by a unit may appear verbatim only in that unit's task_markdown.
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
- constraints must explicitly list every hard source-task constraint that affects delivery, or be
  an empty list when the source task contains none. Do not omit the field.
- Supported constraint kinds are repository_ownership, authoritative_read_only_dependency,
  stop_and_follow_up, security_boundary, and prohibited_fallback.
- Before returning, check the full source task once for each supported constraint kind. This is an
  internal completeness checklist; do not emit the checklist.
- Treat requirements to reuse an existing mechanism, consume an authoritative contract or schema,
  or leave dependency-owned behavior unchanged as authoritative_read_only_dependency constraints,
  not merely implementation guidance.
- Treat explicit trust, permission, secret, privacy, or execution boundaries as security_boundary
  constraints even when they appear inside broader acceptance or implementation prose.
- Constraint summaries must be bounded single-line paraphrases. Do not quote source-task text or
  include source excerpts, absolute paths, prompts, provider output, or private data.
- Each constraint must list every generated unit to which it applies in unit_ids.
- Use disposition preserved only when every listed unit keeps the constraint. Use needs_review
  when consistency cannot be established, and conflict when a unit contradicts the constraint.
  The deterministic writer blocks needs_review and conflict instead of publishing the plan.
- Unit IDs must be stable path-safe IDs using only letters, numbers, dots, underscores, and hyphens.
- Unit IDs must not contain path separators, absolute paths, ".", or "..".
- depends_on must reference known unit IDs only and must not contain duplicates, self-dependencies,
  or dependency cycles.
- Optional metadata fields stream, component, phase, kind, and platform must be non-empty strings
  when present.
- scope_paths are execution-boundary metadata. They must contain only project-relative paths that stay
  inside the project. They may use an explicit repository path required by the source task or established
  by supplied project context, but never derive a filesystem path from a package, namespace, module, or
  import name. Prefer stable existing module or directory ownership boundaries over predictions of
  concrete files. For a new path, its direct parent directory must already exist. If no reliable ownership
  boundary can be identified, return an empty list so the repository's configured write scope remains
  authoritative.
- asset_paths must contain only paths declared in the source task's canonical direct-list
  `## Assets` section or their project-relative equivalents. Assign every declared source asset
  to at least one relevant unit and do not repeat a path within one unit.
- estimated_size must be "small", "medium", or "large" when present.
- risk_tags must use only supported tags: api_surface, audit_artifacts, auth_permissions,
  automation_behavior, build_pipeline, cli_surface, configuration, data_persistence,
  docs_coverage, execution_boundary, external_execution_boundary, external_integration,
  migration, privacy, public_output_contract, release, security_boundary,
  structured_output_contract, test_hardening, ui_surface, validation.
- budget must include max_planner_steps set to 1 or 2. It may also contain positive integer fields:
  max_elapsed_minutes, max_review_cycles, max_security_cycles, max_changed_files,
  max_changed_modules, max_generated_test_files.
- Unit task Markdown must be product/behavior descriptions with acceptance criteria and verification
  expectations, not file-by-file implementation scripts.
- Every unit task must be self-contained because implementation agents cannot read the parent source
  task. Copy every source-defined identifier, enum value, field name, localization key and value,
  fixed user-visible string, or other literal that the unit must use exactly into its task Markdown.
  Never write dangling references such as "use the provided keys", "use the given values", or
  "as listed above" when the referenced values exist only in the source task.
- Unit task Markdown must include all of these exact contract-ready section headings:
  Goal, Current behavior, Desired behavior, Acceptance criteria, Security and privacy, Reviewer focus,
  Out of scope, and Validation.
- Validation sections must include explicit commands that match or are directly supported by the
  source task or configured validation commands.
- Unit task Markdown must not include an asset-root section (`## Assets`, `## Asset`,
  `## Task assets`, or `## Task asset`), `## Asset manifest`, or sikula:generated-* markers.
  Deterministic writer code renders assigned source declarations from asset_paths.
- Paths for plan.yaml and unit task files are derived later from the output directory and unit IDs.

Sizing and split guidance:
- Design every unit for a single implementation pass and set max_planner_steps to 1 by default.
- Use max_planner_steps 2 only when two tightly coupled, compile-safe steps cannot be separated into
  independent delivery units.
- Never set max_planner_steps to 3 or more. Three or more expected planner steps require splitting
  the work into additional delivery units before returning the draft.
- Prefer small units with one primary production surface and narrow validation.
- A small unit changes one module, user workflow, or behavior surface with focused tests.
- A medium unit changes one feature surface plus directly related tests or docs.
- A large unit spans multiple modules, product surfaces, or shared framework behavior and should
  be rare.
- Do not produce a unit that combines multiple independent risk surfaces. Keep one primary
  production surface per unit. When relevant, split surfaces such as UI/API/CLI behavior,
  data model or persistence changes, structured-output parsing or schema validation,
  automation or prompt-driven behavior, external provider/tool execution boundaries, privacy/public output,
  audit/log artifact persistence, and docs/test-only hardening.
- External provider, tool, or integration boundary changes should usually be their own hardening
  unit with risk_tags including external_execution_boundary or external_integration.
- Parsing or structured-output validation should usually be separate from execution or integration
  behavior.
- Entry-point preflight, flag, route, request, or path validation should usually be separate from
  generation or downstream execution behavior.
- Docs and coverage may be a final hardening unit unless they are essential to validate a specific
  behavior introduced by that unit.
- If a unit would need broad cross-module tests or three or more planner steps, split it before
  returning the draft.

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
  "constraints": [
    {{
      "id": "stable-constraint-id",
      "kind": "repository_ownership",
      "summary": "Bounded paraphrase of the hard delivery rule",
      "unit_ids": ["stable-unit-id"],
      "disposition": "preserved"
    }}
  ],
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
      "asset_paths": [],
      "estimated_size": "small",
      "risk_tags": ["cli_surface"],
      "budget": {{"max_planner_steps": 1, "max_changed_files": 8}},
      "task_markdown": "# Unit title\\n\\n## Goal\\n\\n...\\n\\n## Security and privacy\\n\\n...\\n\\n## Validation\\n\\n- `command`"
    }}
  ]
}}
"""

_DELIVERY_ASSESSMENT_PROMPT = """\
You are Sikula's read-only delivery-mode assessment assistant.

Your job is to recommend whether one project task should use one standard Sikula run, a delivery
plan, or task clarification before the operator chooses a workflow. You do not write files,
create delivery artifacts, or start implementation.

Hard rules:
- Do not write, edit, delete, move, rename, format, or create files.
- Do not run commands, start nested Sikula commands, or use external services.
- Treat platform, stack, component, scope, validation, and risk information as project data.
- Use the same decision flow for every project and platform. Do not introduce platform-specific
  orchestration rules.
- Do not classify primarily from task length. Contract readiness and delivery-mode suitability
  are separate decisions.
- Do not include task excerpts, prompts, provider output, paths other than the selected
  project-relative task path, secrets, personal data, logs, diffs, or task state in the result.
- Do not return free-form rationale, warnings, confidence scores, scope paths, task Markdown,
  writer-facing paths, validation output, or implementation instructions.
- Return exactly one JSON object and no Markdown outside the JSON.

Decision rules:
- Recommend single_run when the task has one cohesive implementation surface that fits one
  independently reviewable implementation contract.
- Recommend delivery_plan when the task contains multiple independently reviewable surfaces,
  platforms, components, execution boundaries, risk domains, or dependency-ordered outcomes that
  should become separate contract-sized units.
- Recommend needs_clarification when missing scope, acceptance criteria, ownership, validation, or
  decomposition evidence prevents a defensible choice.
- A delivery_plan recommendation must include at least two proposed units.
- single_run and needs_clarification must use an empty units list.
- Unit IDs must be stable path-safe IDs using only letters, numbers, dots, underscores, and hyphens.
- Unit IDs must be case-insensitively unique.
- Dependencies must reference known unit IDs only and must not contain duplicates,
  self-dependencies, or cycles.
- Optional stream, component, and platform values describe project metadata only.

Supported reason codes by mode:
- single_run: single_cohesive_surface, single_validation_boundary
- delivery_plan: multiple_independent_surfaces, multiple_platforms, multiple_components,
  multiple_risk_boundaries, dependency_order_required
- needs_clarification: scope_unclear, acceptance_criteria_unclear, ownership_unclear,
  validation_unclear, decomposition_unclear

Project stack: {project_stack}
Source task file: {task_path}

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

Source task description:
```markdown
{task_description}
```

Return this JSON shape:
{{
  "recommended_mode": "single_run | delivery_plan | needs_clarification",
  "reason_codes": ["one_supported_code"],
  "units": [
    {{
      "id": "stable-unit-id",
      "title": "Short unit title",
      "depends_on": [],
      "stream": "optional non-empty string",
      "component": "optional non-empty string",
      "platform": "optional non-empty string"
    }}
  ]
}}
"""

_DELIVERY_CONSTRAINT_VERIFICATION_PROMPT = """\
You are Sikula's independent read-only delivery-constraint verifier. You did not author the
candidate units. Compare the authoritative task text, the complete constraint input, and every
candidate unit before returning a strict verification result.

Hard rules:
- Do not write, edit, delete, move, rename, format, or create files.
- Do not run commands, start nested Sikula commands, inspect external services, or use the network.
- Outside unit_context_gaps.source_literals, do not include source excerpts, task bodies, prompts,
  provider output, diffs, logs, secrets, personal data, or absolute local paths in the JSON result.
- Do not invent, rename, omit, summarize, or change a supplied constraint or unit id.
- Return exactly one JSON object and no Markdown outside the JSON.

Verification scope: {verification_scope}

Completeness rule:
{completeness_rule}

Unit self-containment rule:
{unit_context_rule}

Authoritative task text:
```markdown
{authority_description}
```

Constraint input:
```json
{constraints_json}
```

Candidate units:
```json
{units_json}
```

For each supplied constraint, echo id, kind, summary, and unit_ids exactly. Set disposition to:
- preserved only when every listed candidate unit preserves the constraint;
- needs_review when semantic consistency cannot be established;
- conflict when any listed candidate unit contradicts the constraint.

Set constraints_complete to false when the completeness rule is not satisfied. An empty constraint
input is valid only when the completeness rule establishes that no governing hard constraint was
omitted. When constraints_complete is false, constraint_gaps must identify every detected omission
or incomplete assignment. Use reason omitted when the constraint input lacks the rule and omit
constraint_id. Use reason incompletely_assigned when an existing constraint is missing affected
units; constraint_id, kind, and summary must then echo that supplied constraint exactly. Summaries
must be bounded paraphrases, never source excerpts. affected_unit_ids must list every candidate unit
that needs the omitted constraint or every missing assignment for an existing constraint.

When constraints_complete is true, return an empty constraint_gaps list.

When unit self-containment is enabled, set unit_context_complete to false if a candidate unit refers
to source-defined exact identifiers, keys, enum values, field names, fixed copy, or other required
literals that are absent from its task_markdown. For each affected unit, return one unit_context_gaps
entry. source_literals must contain only the minimal complete, non-empty, single-line source-task
lines that the unit needs verbatim. Copy those lines exactly; do not paraphrase, combine, truncate,
or include unrelated source context. If every candidate unit is self-contained, set
unit_context_complete to true and return an empty unit_context_gaps list.

Return this JSON shape:
{{
  "constraints_complete": true,
  "constraint_gaps": [],
  "unit_context_complete": true,
  "unit_context_gaps": [],
  "constraints": [
    {{
      "id": "exact-supplied-id",
      "kind": "repository_ownership",
      "summary": "Exact supplied bounded summary",
      "unit_ids": ["exact-supplied-unit-id"],
      "disposition": "preserved"
    }}
  ]
}}
"""

_DELIVERY_CONSTRAINT_REPAIR_PROMPT = """\
You are Sikula's read-only delivery-constraint repair assistant.

Repair only the structured constraint list after an independent verifier identified actionable
gaps. Do not redesign, rewrite, or return the delivery units. The supplied candidate units,
dependencies, task Markdown, scope paths, asset paths, sizing, risk tags, and budgets are immutable.

Hard rules:
- Do not write files, run commands, use tools, inspect external services, or access the network.
- Return exactly one JSON object and no Markdown outside the JSON.
- Preserve every existing constraint in the same order with exactly the same id, kind, summary,
  disposition, and existing unit_ids.
- For incompletely_assigned gaps, append exactly the listed affected_unit_ids to that existing
  constraint and make no other assignment changes.
- For omitted gaps, append exactly one new constraint per gap in gap order. Create a stable path-safe
  id, preserve the gap kind and summary exactly, and assign exactly the listed affected_unit_ids.
- Do not add constraints that are not represented by a supplied gap.
- Use preserved only when every assigned candidate unit keeps the rule. Use needs_review when
  consistency cannot be established and conflict when a candidate unit contradicts the rule.
- Do not include source excerpts, task bodies, prompts, provider output, diffs, logs, secrets,
  personal data, or absolute local paths in the JSON result.

Authoritative task text:
```markdown
{authority_description}
```

Existing constraints:
```json
{constraints_json}
```

Actionable verifier gaps:
```json
{gaps_json}
```

Immutable candidate units:
```json
{units_json}
```

Return this JSON shape:
{{
  "constraints": [
    {{
      "id": "stable-constraint-id",
      "kind": "authoritative_read_only_dependency",
      "summary": "Bounded paraphrase of the governing rule",
      "unit_ids": ["affected-unit-id"],
      "disposition": "preserved"
    }}
  ]
}}
"""

_DELIVERY_AMENDMENT_PROMPT = """\
You are Sikula's read-only delivery-plan amendment authoring assistant.

Split one selected delivery unit into smaller replacement units. You propose only the replacement
graph and replacement task contracts. Deterministic Sikula code preserves the existing plan,
rewires downstream dependencies, and accepts or rejects the proposal.

Hard rules:
- Do not write, edit, delete, move, rename, format, or create files.
- Do not run commands, start nested Sikula commands, or use external services.
- Do not propose edits to existing units or return the whole delivery plan.
- Do not include path fields such as task_path, path, output_path, or plan_path.
- Do not include raw prompts, provider output, source excerpts, task state, diffs, logs, secrets,
  personal data, or absolute local paths in the JSON result.
- Return exactly one JSON object and no Markdown outside the JSON.

Plan id: {plan_id}
Target unit id: {target_unit_id_json}
Project stack: {project_stack}

Source-plan component guidance:
{component_guidance}

Verified recovery metadata supplied by deterministic Sikula code:
```json
{recovery_metadata_json}
```

Correlated failed-child boundary evidence supplied by deterministic Sikula code:
```json
{failure_evidence_json}
```

Applicable inherited constraints supplied by deterministic plan validation:
```json
{applicable_constraints_json}
```

Target unit metadata:
```json
{target_unit_json}
```

Pending direct dependents that Sikula will rewire to replacement leaves:
```json
{downstream_units_json}
```

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

Selected unit task:
```markdown
{target_task_description}
```

Replacement constraints:
- Produce at least two smaller units with new, path-safe ids.
- depends_on may reference replacement unit ids only. Sikula adds the target's upstream
  dependencies to replacement roots and rewires existing downstream units to replacement leaves.
- Prefer one planner step and one primary production surface per replacement.
- Set max_planner_steps to 1 by default. Use 2 only for a tightly coupled exception, and never use
  3 or more; split the replacement again instead.
- Use only the same metadata, risk tag, sizing, and positive integer budget fields supported by
  delivery prepare.
- amend_reason must be omitted, null, or a stable code containing only letters, numbers, dots,
  underscores, and hyphens.
- When verified recovery metadata is non-null, copy its amend_reason and budget_exceeded values
  exactly. Deterministic Sikula code rejects conflicting recovery metadata.
- When failed-child boundary evidence is non-null, use its inherited constraints, write scope,
  changed and violation paths, review dispositions, and dependency identities to correct the
  ownership or scope boundary. Do not merely restate the original invalid split.
- Every applicable inherited constraint must be preserved by every replacement unit. Do not
  weaken, omit, rename, reinterpret, or transfer it to only a subset of the replacements.
- If that evidence proves a required change is owned outside this repository and no valid
  single-repository replacement graph can satisfy it, return disposition
  external_dependency_follow_up_required, a bounded summary, and an empty replacement_units
  list. Never use this disposition for uncertainty or an in-repository scope correction.
- Every task_markdown must contain these exact headings: Goal, Current behavior, Desired behavior,
  Acceptance criteria, Security and privacy, Reviewer focus, Out of scope, and Validation.
- For a selected unit containing structured `## Assets` or a prepared `## Asset manifest`,
  assign every declared path to at least one relevant replacement through asset_paths. For an
  absolute in-project declaration, use its project-relative equivalent. Do not repeat paths or
  add unknown paths.
- Replacement task_markdown must not include an asset-root section (`## Assets`, `## Asset`,
  `## Task assets`, or `## Task asset`); deterministic Sikula code renders the assigned
  declarations from the selected unit.
- Keep acceptance criteria observable and validation commands supported by the source unit or
  configured project context.

Return this JSON shape:
{{
  "plan_id": "{plan_id}",
  "target_unit_id": {target_unit_id_json},
  "amend_reason": {amend_reason_json},
  "budget_exceeded": {budget_exceeded_json},
  "warnings": [],
  "replacement_units": [
    {{
      "id": "new-unit-a",
      "title": "Short replacement title",
      "depends_on": [],
      "stream": "optional non-empty string",
      "phase": "optional non-empty string",
      "kind": "optional non-empty string",
      "platform": "optional non-empty string",
      "scope_paths": [],
      "asset_paths": [],
      "estimated_size": "small",
      "risk_tags": ["validation"],
      "budget": {{"max_planner_steps": 1}},
      "task_markdown": "# Replacement title\\n\\n## Goal\\n\\n...\\n\\n## Validation\\n\\n- `command`"
    }},
    {{
      "id": "new-unit-b",
      "title": "Short replacement title",
      "depends_on": ["new-unit-a"],
      "task_markdown": "# Replacement title\\n\\n## Goal\\n\\n...\\n\\n## Validation\\n\\n- `command`"
    }}
  ]
}}

For an externally owned requirement, return this shape instead:
{{
  "plan_id": "{plan_id}",
  "target_unit_id": {target_unit_id_json},
  "disposition": "external_dependency_follow_up_required",
  "summary": "One bounded single-line explanation without absolute paths or private content",
  "amend_reason": {amend_reason_json},
  "budget_exceeded": {budget_exceeded_json},
  "warnings": [],
  "replacement_units": []
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
            output = self.llm.generate("", prompt)
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
                source_task_description=task_description,
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

        source_task_path = self._project_relative_path(task_path, root)
        if source_task_path != "<outside-project>":
            draft = replace(
                draft,
                source_task=DeliveryPlanSourceTask(
                    path=source_task_path,
                    sha256="sha256:" + sha256(task_description.encode("utf-8")).hexdigest(),
                ),
            )

        self._record_success(audit_recorder, prompt=prompt, output=output, draft=draft)
        verification = self._verify_constraint_continuity(
            authority_description=task_description,
            constraints=draft.constraints,
            units=draft.units,
            verification_scope="source_task_to_units",
            completeness_rule=(
                "Set constraints_complete to false if any hard ownership, authoritative dependency, "
                "stop-and-follow-up, security-boundary, or prohibited-fallback rule in the authoritative "
                "source task is missing from the constraint input or is not assigned to every affected unit."
            ),
            audit_recorder=audit_recorder,
            audit_phase="delivery_prepare_constraint_verification",
            round_index=1,
            verify_unit_context=True,
        )
        if verification.constraints_complete and verification.unit_context_complete:
            return replace(draft, constraint_verification=verification)
        if any(
            constraint.disposition != DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION
            for constraint in (*draft.constraints, *verification.constraints)
        ):
            return replace(draft, constraint_verification=verification)

        repaired_units = apply_delivery_unit_context_gaps(draft.units, verification.unit_context_gaps)
        repaired_constraints = list(draft.constraints)
        if not verification.constraints_complete:
            repaired_constraints = self._repair_constraint_gaps(
                authority_description=task_description,
                constraints=draft.constraints,
                units=repaired_units,
                gaps=verification.constraint_gaps,
                audit_recorder=audit_recorder,
            )
        repaired_verification = self._verify_constraint_continuity(
            authority_description=task_description,
            constraints=repaired_constraints,
            units=repaired_units,
            verification_scope="source_task_to_units_after_bounded_repair",
            completeness_rule=(
                "Set constraints_complete to false if any hard ownership, authoritative dependency, "
                "stop-and-follow-up, security-boundary, or prohibited-fallback rule in the authoritative "
                "source task remains missing from the repaired constraint input or is not assigned to every "
                "affected unit."
            ),
            audit_recorder=audit_recorder,
            audit_phase="delivery_prepare_constraint_verification",
            round_index=2,
            verify_unit_context=True,
        )
        return replace(
            draft,
            units=repaired_units,
            constraints=repaired_constraints,
            constraint_verification=repaired_verification,
        )

    def assess_delivery_mode(
        self,
        *,
        task_description: str,
        task_path: str | Path,
        project_root: str | Path,
        project_context: dict[str, Any] | None = None,
        audit_recorder: DeliveryPreparationAuditRecorder | None = None,
    ) -> DeliveryAssessmentDraft:
        root = Path(project_root).resolve()
        prompt = read_only_agent_prompt(
            self._build_assessment_prompt(
                task_description=task_description,
                task_path=task_path,
                project_root=root,
                project_context=project_context,
            )
        )
        try:
            output = self.llm.generate("", prompt)
        except Exception as exc:
            self._record_assessment_failure(
                audit_recorder,
                prompt=prompt,
                output=None,
                error=exc,
                error_code="delivery_assessment.authoring_failed",
            )
            raise DeliveryPreparationAgentError("Delivery assessment assistant failed.") from None

        try:
            draft = parse_delivery_assessment_output(output)
        except DeliveryAuthoringParseError as exc:
            self._record_assessment_failure(
                audit_recorder,
                prompt=prompt,
                output=output,
                error=exc,
                error_code=exc.code,
            )
            raise
        self._record_assessment_success(audit_recorder, prompt=prompt, output=output, draft=draft)
        return draft

    def author_delivery_amendment(
        self,
        *,
        plan_id: str,
        target_unit_id: str,
        target_task_description: str,
        target_unit: dict[str, Any],
        downstream_units: list[dict[str, Any]],
        project_root: str | Path,
        project_context: dict[str, Any] | None = None,
        component_ids: Sequence[str] = (),
        applicable_constraints: Sequence[dict[str, Any]] = (),
        failure_evidence: dict[str, Any] | None = None,
        amend_reason: str | None = None,
        budget_exceeded: dict[str, Any] | None = None,
        audit_recorder: DeliveryPreparationAuditRecorder | None = None,
    ) -> DeliveryAmendmentAuthoringDraft:
        root = Path(project_root).resolve()
        component_id_list = list(component_ids)
        if component_id_list:
            component_guidance = (
                "The source plan declares these component IDs as the complete allowlist. "
                "Preserve case and spelling exactly:\n"
                "```json\n"
                "{component_ids_json}\n"
                "```\n"
                "Replacement units may omit component or set it to exactly one of the listed IDs. "
                "Do not invent, normalize, lowercase, or otherwise alter component IDs."
            ).format(component_ids_json=json.dumps(component_id_list, indent=2))
        else:
            component_guidance = (
                "The source plan declares no top-level components. No component IDs are allowed for replacements. "
                "Every replacement unit MUST omit the component field entirely. Do not emit component: null and do "
                "not invent component IDs."
            )
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX
            + _DELIVERY_AMENDMENT_PROMPT.format(
                plan_id=plan_id,
                target_unit_id_json=json.dumps(target_unit_id),
                project_stack=tech_stack(self.project_config),
                recovery_metadata_json=json.dumps(
                    {"amend_reason": amend_reason, "budget_exceeded": budget_exceeded}
                    if amend_reason is not None or budget_exceeded is not None
                    else None,
                    indent=2,
                    sort_keys=True,
                ),
                failure_evidence_json=json.dumps(failure_evidence, indent=2, sort_keys=True),
                applicable_constraints_json=json.dumps(list(applicable_constraints), indent=2, sort_keys=True),
                amend_reason_json=json.dumps(amend_reason),
                budget_exceeded_json=json.dumps(budget_exceeded, sort_keys=True),
                component_guidance=component_guidance,
                target_unit_json=json.dumps(target_unit, indent=2, sort_keys=True),
                downstream_units_json=json.dumps(downstream_units, indent=2, sort_keys=True),
                guidelines_files=guidelines_files(self.project_config),
                guidelines_context=self._guidelines_context(root),
                project_context_json=json.dumps(project_context or {}, indent=2, sort_keys=True),
                target_task_description=target_task_description,
            )
        )
        try:
            output = self.llm.generate("", prompt)
        except Exception as exc:
            self._record_amendment_failure(
                audit_recorder,
                prompt=prompt,
                output=None,
                error=exc,
                error_code="delivery_amend.authoring_failed",
            )
            raise DeliveryPreparationAgentError("Delivery amendment authoring assistant failed.") from None
        try:
            draft = parse_delivery_amendment_authoring_output(
                output,
                expected_plan_id=plan_id,
                expected_target_unit_id=target_unit_id,
                project_root=root,
            )
        except DeliveryAuthoringParseError as exc:
            self._record_amendment_failure(
                audit_recorder,
                prompt=prompt,
                output=output,
                error=exc,
                error_code=exc.code,
            )
            raise
        self._record_amendment_success(audit_recorder, prompt=prompt, output=output, draft=draft)
        if not applicable_constraints or not draft.replacement_units:
            return draft

        replacement_ids = [unit.id for unit in draft.replacement_units]
        constraints = [
            DeliveryAuthoringConstraintDraft(
                id=str(value.get("id", "")),
                kind=str(value.get("kind", "")),
                summary=str(value.get("summary", "")),
                unit_ids=list(replacement_ids),
                disposition="preserved",
            )
            for value in applicable_constraints
        ]
        verification = self._verify_constraint_continuity(
            authority_description=target_task_description,
            constraints=constraints,
            units=draft.replacement_units,
            verification_scope="amendment_target_to_replacements",
            completeness_rule=(
                "Set constraints_complete to false unless every applicable inherited constraint supplied "
                "by deterministic plan validation is represented exactly and preserved by every replacement unit."
            ),
            audit_recorder=audit_recorder,
            audit_phase="delivery_amend_constraint_verification",
            round_index=1,
            verify_unit_context=False,
        )
        return replace(draft, constraint_verification=verification)

    def _verify_constraint_continuity(
        self,
        *,
        authority_description: str,
        constraints: Sequence[DeliveryAuthoringConstraintDraft],
        units: Sequence[DeliveryAuthoringUnitDraft],
        verification_scope: str,
        completeness_rule: str,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        audit_phase: str,
        round_index: int,
        verify_unit_context: bool,
    ) -> DeliveryConstraintVerification:
        constraints_payload = [constraint.to_plan_dict() for constraint in constraints]
        units_payload = [self._verification_unit_payload(unit) for unit in units]
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX
            + _DELIVERY_CONSTRAINT_VERIFICATION_PROMPT.format(
                verification_scope=verification_scope,
                completeness_rule=completeness_rule,
                unit_context_rule=(
                    "Check every candidate unit against the authoritative source task and report exact missing "
                    "source literals as described below."
                    if verify_unit_context
                    else "This is a constraint-only amendment check. Return unit_context_complete=true and "
                    "unit_context_gaps=[]."
                ),
                authority_description=authority_description,
                constraints_json=json.dumps(constraints_payload, indent=2, sort_keys=True),
                units_json=json.dumps(units_payload, indent=2, sort_keys=True),
            )
        )
        try:
            output = self.llm.generate("", prompt)
        except Exception as exc:
            self._record_constraint_verification_failure(
                audit_recorder,
                phase=audit_phase,
                prompt=prompt,
                output=None,
                error=exc,
                error_code="delivery_constraint_verification.authoring_failed",
                round_index=round_index,
            )
            raise DeliveryPreparationAgentError("Delivery constraint verification assistant failed.") from None

        try:
            verification = parse_delivery_constraint_verification_output(
                output,
                unit_ids={unit.id for unit in units},
                source_task_description=authority_description,
                unit_task_markdown_by_id={unit.id: unit.task_markdown for unit in units},
                require_unit_context=verify_unit_context,
            )
            self._assert_constraint_verification_echo(constraints, verification)
        except DeliveryAuthoringParseError as exc:
            self._record_constraint_verification_failure(
                audit_recorder,
                phase=audit_phase,
                prompt=prompt,
                output=output,
                error=exc,
                error_code=exc.code,
                round_index=round_index,
            )
            raise

        self._record_constraint_verification_success(
            audit_recorder,
            phase=audit_phase,
            prompt=prompt,
            output=output,
            verification=verification,
            round_index=round_index,
        )
        return verification

    def _repair_constraint_gaps(
        self,
        *,
        authority_description: str,
        constraints: Sequence[DeliveryAuthoringConstraintDraft],
        units: Sequence[DeliveryAuthoringUnitDraft],
        gaps: Sequence[DeliveryConstraintGap],
        audit_recorder: DeliveryPreparationAuditRecorder | None,
    ) -> list[DeliveryAuthoringConstraintDraft]:
        constraints_payload = [constraint.to_plan_dict() for constraint in constraints]
        gaps_payload = [gap.to_dict() for gap in gaps]
        units_payload = [self._verification_unit_payload(unit) for unit in units]
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX
            + _DELIVERY_CONSTRAINT_REPAIR_PROMPT.format(
                authority_description=authority_description,
                constraints_json=json.dumps(constraints_payload, indent=2, sort_keys=True),
                gaps_json=json.dumps(gaps_payload, indent=2, sort_keys=True),
                units_json=json.dumps(units_payload, indent=2, sort_keys=True),
            )
        )
        try:
            output = self.llm.generate("", prompt)
        except Exception as exc:
            self._record_constraint_repair_failure(
                audit_recorder,
                prompt=prompt,
                output=None,
                error=exc,
                error_code="delivery_constraint_repair.authoring_failed",
            )
            raise DeliveryPreparationAgentError("Delivery constraint repair assistant failed.") from None

        try:
            repaired = parse_delivery_constraint_repair_output(
                output,
                unit_ids={unit.id for unit in units},
                source_task_description=authority_description,
            )
            self._assert_constraint_repair(constraints, repaired, gaps)
        except DeliveryAuthoringParseError as exc:
            self._record_constraint_repair_failure(
                audit_recorder,
                prompt=prompt,
                output=output,
                error=exc,
                error_code=exc.code,
            )
            raise

        self._record_constraint_repair_success(
            audit_recorder,
            prompt=prompt,
            output=output,
            constraints=repaired,
        )
        return repaired

    @staticmethod
    def _verification_unit_payload(unit: DeliveryAuthoringUnitDraft) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": unit.id,
            "title": unit.title,
            "depends_on": list(unit.depends_on),
            "scope_paths": list(unit.scope_paths),
            "asset_paths": list(unit.asset_paths),
            "risk_tags": list(unit.risk_tags),
            "task_markdown": unit.task_markdown,
        }
        for field_name in ("stream", "component", "phase", "kind", "platform", "estimated_size"):
            value = getattr(unit, field_name)
            if value is not None:
                payload[field_name] = value
        if unit.budget is not None:
            payload["budget"] = unit.budget.to_dict()
        return payload

    @staticmethod
    def _assert_constraint_verification_echo(
        constraints: Sequence[DeliveryAuthoringConstraintDraft],
        verification: DeliveryConstraintVerification,
    ) -> None:
        expected = [
            (constraint.id, constraint.kind, constraint.summary, constraint.unit_ids) for constraint in constraints
        ]
        actual = [
            (constraint.id, constraint.kind, constraint.summary, constraint.unit_ids)
            for constraint in verification.constraints
        ]
        if actual != expected:
            raise DeliveryAuthoringParseError(
                "delivery_constraint_verification.constraints_mismatch",
                "Constraint verification must echo every supplied constraint exactly and in order.",
            )

    @staticmethod
    def _assert_constraint_repair(
        original: Sequence[DeliveryAuthoringConstraintDraft],
        repaired: Sequence[DeliveryAuthoringConstraintDraft],
        gaps: Sequence[DeliveryConstraintGap],
    ) -> None:
        omitted = [gap for gap in gaps if gap.reason == "omitted"]
        if len(repaired) != len(original) + len(omitted):
            raise DeliveryAuthoringParseError(
                "delivery_constraint_repair.constraint_count_invalid",
                "Constraint repair must add exactly one constraint for every omitted gap.",
            )

        assignments_by_id: dict[str, list[str]] = {}
        for gap in gaps:
            if gap.reason == "incompletely_assigned" and gap.constraint_id is not None:
                assignments_by_id.setdefault(gap.constraint_id, []).extend(gap.affected_unit_ids)

        for index, existing in enumerate(original):
            candidate = repaired[index]
            if (
                candidate.id != existing.id
                or candidate.kind != existing.kind
                or candidate.summary != existing.summary
                or candidate.disposition != existing.disposition
            ):
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_repair.existing_constraint_changed",
                    "Constraint repair must preserve every existing constraint identity and disposition.",
                )
            expected_unit_ids = list(existing.unit_ids)
            for unit_id in assignments_by_id.get(existing.id, []):
                if unit_id not in expected_unit_ids:
                    expected_unit_ids.append(unit_id)
            if candidate.unit_ids != expected_unit_ids:
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_repair.assignment_invalid",
                    "Constraint repair may add only verifier-identified missing unit assignments.",
                )

        additions = repaired[len(original) :]
        for candidate, gap in zip(additions, omitted):
            if (
                candidate.kind != gap.kind
                or candidate.summary != gap.summary
                or candidate.unit_ids != gap.affected_unit_ids
            ):
                raise DeliveryAuthoringParseError(
                    "delivery_constraint_repair.omitted_constraint_mismatch",
                    "New constraints must match omitted verifier gaps in order, kind, summary, and affected units.",
                )

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

    def _build_assessment_prompt(
        self,
        *,
        task_description: str,
        task_path: str | Path,
        project_root: Path,
        project_context: dict[str, Any] | None,
    ) -> str:
        context = project_context or {}
        validation_commands = context.get("validation_commands") if isinstance(context, dict) else None
        if not isinstance(validation_commands, list):
            validation_commands = []
        safe_validation_commands = [str(command) for command in validation_commands if str(command).strip()]
        return AGENT_SECURITY_PREFIX + _DELIVERY_ASSESSMENT_PROMPT.format(
            project_stack=tech_stack(self.project_config),
            task_path=self._project_relative_path(task_path, project_root),
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
            if len(content) > max_chars:
                content = content[:max_chars]
                if max_chars > 0:
                    content += f"\n... [truncated; inspect {rel_path} for full content]"
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

    def _record_assessment_success(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        prompt: str,
        output: str,
        draft: DeliveryAssessmentDraft,
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": "delivery_assessment",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "parsed",
                    "recommended_mode": draft.recommended_mode,
                    "reason_codes": list(draft.reason_codes),
                    "unit_ids": [unit.id for unit in draft.units],
                    "unit_count": len(draft.units),
                },
            }
        )

    def _record_assessment_failure(
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
        audit_recorder(
            {
                "phase": "delivery_assessment",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error_code": error_code,
                    "error": str(error),
                },
            }
        )

    def _record_amendment_success(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        prompt: str,
        output: str,
        draft: DeliveryAmendmentAuthoringDraft,
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": "delivery_amend_prepare_authoring",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "parsed",
                    "plan_id": draft.plan_id,
                    "target_unit_id": draft.target_unit_id,
                    "replacement_ids": [unit.id for unit in draft.replacement_units],
                    "replacement_count": len(draft.replacement_units),
                    "disposition": draft.disposition,
                    "warnings": list(draft.warnings),
                },
            }
        )

    def _record_amendment_failure(
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
        audit_recorder(
            {
                "phase": "delivery_amend_prepare_authoring",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error_code": error_code,
                    "error": str(error),
                },
            }
        )

    def _record_constraint_verification_success(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        phase: str,
        prompt: str,
        output: str,
        verification: DeliveryConstraintVerification,
        round_index: int,
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": phase,
                "round_index": round_index,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "parsed",
                    "constraints_complete": verification.constraints_complete,
                    "constraint_ids": [constraint.id for constraint in verification.constraints],
                    "dispositions": [constraint.disposition for constraint in verification.constraints],
                    "constraint_gaps": [gap.to_dict() for gap in verification.constraint_gaps],
                    "unit_context_complete": verification.unit_context_complete,
                    "unit_context_gaps": [
                        {"unit_id": gap.unit_id, "source_literal_count": len(gap.source_literals)}
                        for gap in verification.unit_context_gaps
                    ],
                },
            }
        )

    def _record_constraint_verification_failure(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        phase: str,
        prompt: str,
        output: str | None,
        error: Exception,
        error_code: str,
        round_index: int,
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": phase,
                "round_index": round_index,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error_code": error_code,
                    "error": str(error),
                },
            }
        )

    def _record_constraint_repair_success(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        prompt: str,
        output: str,
        constraints: Sequence[DeliveryAuthoringConstraintDraft],
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": "delivery_prepare_constraint_repair",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "parsed",
                    "constraint_ids": [constraint.id for constraint in constraints],
                    "dispositions": [constraint.disposition for constraint in constraints],
                },
            }
        )

    def _record_constraint_repair_failure(
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
        audit_recorder(
            {
                "phase": "delivery_prepare_constraint_repair",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error_code": error_code,
                    "error": str(error),
                },
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
