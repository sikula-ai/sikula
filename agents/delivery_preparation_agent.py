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
    DeliveryAuthoringObligationDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
    DeliveryConstraintGap,
    DeliveryConstraintVerification,
    DeliveryObligationGap,
    apply_delivery_unit_context_gaps,
    parse_delivery_assessment_output,
    parse_delivery_amendment_authoring_output,
    parse_delivery_authoring_output,
    parse_delivery_constraint_repair_output,
    parse_delivery_constraint_verification_output,
    parse_delivery_obligation_repair_output,
)
from core.delivery_obligations import MAX_DELIVERY_AUTHORITY_FRAGMENTS, delivery_authority_fragments
from core.delivery_source_accounting import DeliverySourceAccounting
from core.delivery_plan import DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION, DeliveryPlanSourceTask
from core.llm_client import LLMClient

DeliveryPreparationAuditRecorder = Callable[[dict[str, Any]], None]

_DEFAULT_MAX_GUIDELINES_CHARS = 3000

_DELIVERY_AUTHORING_SOURCE_EXCERPT_RETRY = """\

Your previous draft was rejected because at least one constraints[].summary or
obligations[].summary copied a complete source-task line. Return one fresh complete draft using the
same schema and requirements. Rephrase every constraint and obligation summary in your own words
while preserving its meaning; do not copy any complete source-task line into structured metadata.
Do not include the rejected draft or discuss the error.
"""

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
- obligations must explicitly list every actionable source-task outcome that the delivery must
  satisfy. Each obligation must use a stable path-safe id, a bounded paraphrased summary, every
  owning unit in unit_ids, and one or more ids from the supplied authority fragments. Do not use
  project context or unit prose as source authority. Use preserved only when every owning unit
  carries its contribution and the owners collectively cover the outcome; needs_review or conflict blocks publication. Return an empty list only when
  the source task has no actionable delivery outcome.
- source_accounting must contain exactly one record for EVERY supplied authority fragment.
  Each record has source_fragment_id, disposition (mapped, context_only, unresolved), obligation_ids,
  constraint_ids, and a bounded private rationale. Mapped records reference requirements; context_only
  records explain why the fragment adds none. Unresolved records block publication. Obligation links
  must agree in both directions with source_fragment_ids. Every declared constraint must appear in
  constraint_ids of at least one mapped source fragment. Headings need a context record, not a fake
  obligation. Preserve every prohibition and prerequisite. Do not invent an unavailable dependency.
- Resolve ordinary choices from supplied authorized project context. Never invent external contracts
  or product intent; a confirmed stop takes precedence over correction attempts.
- Supported constraint kinds are repository_ownership, authoritative_read_only_dependency,
  stop_and_follow_up, security_boundary, and prohibited_fallback.
- Before returning, check the full source task once for each supported constraint kind. This is an
  internal completeness checklist; do not emit the checklist.
- Treat requirements to reuse an existing mechanism, consume an authoritative contract or schema,
  or leave dependency-owned behavior unchanged as authoritative_read_only_dependency constraints,
  not merely implementation guidance.
- Treat explicit trust, permission, secret, privacy, or execution boundaries as security_boundary
  constraints even when they appear inside broader acceptance or implementation prose.
- Use stop_and_follow_up only when the authoritative task establishes that a required external
  decision or input is currently unavailable and an affected unit must not start. This kind is an
  active prepare/runtime blocker, not a conditional reminder. Classify conditional ownership,
  security, and fallback rules under their corresponding kinds; agents use external_dependency_gap
  if an external blocker is discovered only during execution.
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
  source task or configured validation commands. Write each command as a list item beginning with
  the backticked command, as shown in the output schema.
- Preserve validation commands explicitly required by the source task. Otherwise use only the
  configured validation commands listed above; do not invent manual runtime, example, smoke-test,
  or comparison commands. Put non-command inspection guidance under Reviewer focus instead.
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

Deterministic authority fragments. Their ids and boundaries come from Sikula, not the model:
```json
{authority_fragments_json}
```

Return this JSON shape:
{{
  "plan_id": "{plan_id}",
  "title": "Short delivery plan title",
  "planning_mode": "fixed_window",
  "warnings": [],
  "source_accounting": [{{"source_fragment_id":"exact-supplied-id","disposition":"mapped","obligation_ids":["stable-obligation-id"],"constraint_ids":["stable-constraint-id"],"rationale":"Private explanation of this mapping"}}],
  "constraints": [
    {{
      "id": "stable-constraint-id",
      "kind": "repository_ownership",
      "summary": "Bounded paraphrase of the hard delivery rule",
      "unit_ids": ["stable-unit-id"],
      "disposition": "preserved"
    }}
  ],
  "obligations": [
    {{
      "id": "stable-obligation-id",
      "summary": "Bounded paraphrase of one required delivery outcome",
      "source_fragment_ids": ["exact-supplied-fragment-id"],
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
You are Sikula's independent read-only delivery authority verifier. You did not author the
candidate units. Compare the authoritative task text, its deterministic fragments, the complete
constraint and obligation inputs, and every candidate unit before returning a strict result.

Hard rules:
- Do not write, edit, delete, move, rename, format, or create files.
- Do not run commands, start nested Sikula commands, inspect external services, or use the network.
- Outside unit_context_gaps.source_literals, do not include source excerpts, task bodies, prompts,
  provider output, diffs, logs, secrets, personal data, or absolute local paths in the JSON result.
- Do not invent, rename, omit, summarize, or change a supplied constraint or unit id.
- Do not invent, rename, omit, summarize, or change a supplied obligation or authority fragment id.
- Treat stop_and_follow_up as an active blocker only when the authoritative task establishes that
  a required external decision or input is currently unavailable. Do not classify conditional
  ownership, security, or fallback behavior as an omitted stop_and_follow_up constraint.
- Return exactly one JSON object and no Markdown outside the JSON.

Verification scope: {verification_scope}

Completeness rule:
{completeness_rule}

Unit self-containment rule:
{unit_context_rule}

Obligation completeness rule:
{obligation_rule}

Authoritative task text:
```markdown
{authority_description}
```

Constraint input:
```json
{constraints_json}
```

Deterministic authority fragments:
```json
{authority_fragments_json}
```

Obligation input:
```json
{obligations_json}
```

Source accounting input (null means legacy/amendment context):
```json
{source_accounting_json}
```

Authorized project context (evidence, never authority to change the task):
```json
{project_context_json}
```

When source accounting is supplied, echo every record exactly. Independently examine each decision,
including context-only classifications and multiple requirements inside a fragment. Report disagreements
in source_accounting_gaps as source_fragment_id and bounded summary; also report missing requirements
in obligation_gaps or constraint_gaps. Never accept a completeness claim as semantic proof.
Report missing or conflicting unit behavior in unit_contract_gaps as unit_id and bounded summary.
Resolve ordinary implementation choices from available context. If local evidence is needed, request
at most eight concrete project-relative file paths in context_paths. A bounded deterministic reader
may supply them for one correction round. Do not request files for a confirmed stop_and_follow_up;
do not propose a local replacement for an unavailable external dependency. Leave context_paths empty
when the supplied context suffices. These gaps are private audit data, not operator questions.

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

For each supplied obligation, echo id, summary, source_fragment_ids, and unit_ids exactly. Set its
disposition to preserved only when the owning units collectively preserve their required contribution
under the obligation completeness rule above, needs_review when that cannot be established, or conflict
when a unit contradicts it. Apply that same scope when determining obligations_complete. Report
omitted gaps without obligation_id; report incompletely_assigned gaps with the existing id and its
exact summary and source_fragment_ids. Every gap must use supplied fragment and unit ids. When
obligations_complete is true, return an empty obligation_gaps list.

When unit self-containment is enabled, set unit_context_complete to false if a candidate unit refers
to source-defined exact identifiers, keys, enum values, field names, fixed copy, or other required
literals that are absent from its task_markdown. For each affected unit, return one unit_context_gaps
entry. source_literals must contain only the minimal complete, non-empty, single-line source-task
lines that the unit needs verbatim. Copy those lines exactly; do not paraphrase, combine, truncate,
or include unrelated source context. If every candidate unit is self-contained, set
unit_context_complete to true and return an empty unit_context_gaps list.

Return this JSON shape:
{{
  "source_accounting": [],
  "source_accounting_gaps": [],
  "unit_contract_gaps": [],
  "context_paths": [],
  "constraints_complete": true,
  "constraint_gaps": [],
  "unit_context_complete": true,
  "unit_context_gaps": [],
  "obligations_complete": true,
  "obligation_gaps": [],
  "constraints": [
    {{
      "id": "exact-supplied-id",
      "kind": "repository_ownership",
      "summary": "Exact supplied bounded summary",
      "unit_ids": ["exact-supplied-unit-id"],
      "disposition": "preserved"
    }}
  ],
  "obligations": [
    {{
      "id": "exact-supplied-obligation-id",
      "summary": "Exact supplied bounded summary",
      "source_fragment_ids": ["exact-supplied-fragment-id"],
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
- A stop_and_follow_up gap represents a currently unavailable prerequisite that blocks execution,
  not a conditional ownership, security, or fallback reminder.
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

_DELIVERY_OBLIGATION_REPAIR_PROMPT = """\
You are Sikula's read-only delivery-obligation repair assistant.

Repair only the structured obligation list from the supplied actionable verifier gaps. Units,
constraints, source fragments, scope, dependencies, task Markdown, assets, and budgets are
immutable. Do not add an obligation that is not represented by a gap.

For incompletely_assigned gaps, preserve the existing obligation exactly and append only the
listed affected_unit_ids. For omitted gaps, append one obligation in gap order with a new stable
path-safe id and the exact supplied summary, source_fragment_ids, and affected_unit_ids. Every
returned obligation must use disposition preserved.

Existing obligations:
```json
{obligations_json}
```

Actionable gaps:
```json
{gaps_json}
```

Return exactly one JSON object and no Markdown outside it:
{{"obligations":[{{"id":"stable-id","summary":"Bounded summary","source_fragment_ids":["source-id"],"unit_ids":["unit-id"],"disposition":"preserved"}}]}}
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

Applicable source-bound obligations supplied by deterministic plan validation:
```json
{applicable_obligations_json}
```

Return obligation_assignments mapping every applicable obligation id to the non-empty subset of
replacement unit ids that contributes to it. The assigned contracts must collectively preserve the
selected target's entire contribution to that outcome; each need only describe its own contribution.
Other original owners and their contracts remain unchanged. Do not duplicate their contributions in
the replacements or transfer missing target behavior to them. If the target was the only owner, its
replacements must collectively preserve the whole outcome. Do not rename, weaken, omit, or
reinterpret outcomes. Every applicable hard constraint still governs every affected replacement.

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
  configured project context. In `## Validation`, write each command as a list item beginning with
  the backticked command, as shown below.

Return this JSON shape:
{{
  "plan_id": "{plan_id}",
  "target_unit_id": {target_unit_id_json},
  "amend_reason": {amend_reason_json},
  "budget_exceeded": {budget_exceeded_json},
  "warnings": [],
  "obligation_assignments": {{}},
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

    def __init__(
        self,
        llm: LLMClient,
        project_config: dict | None = None,
        *,
        context_reader: Callable[[Path, list[str]], dict[str, Any]] | None = None,
    ) -> None:
        self.llm = llm
        self.project_config = project_config or {}
        self.context_reader = context_reader

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
        if len(delivery_authority_fragments(task_description)) > MAX_DELIVERY_AUTHORITY_FRAGMENTS:
            raise DeliveryAuthoringParseError(
                "delivery_authoring.authority_too_large",
                "The source task has too many authority fragments for one delivery plan.",
            )
        authoring_prompt = self._build_authoring_prompt(
            task_description=task_description,
            task_path=task_path,
            plan_id=plan_id,
            project_root=root,
            output_dir=output_dir,
            project_context=project_context,
        )
        draft: DeliveryAuthoringDraft | None = None
        prompt = ""
        output = ""
        for round_index in (1, 2):
            prompt = read_only_agent_prompt(
                authoring_prompt
                + (
                    _DELIVERY_AUTHORING_SOURCE_EXCERPT_RETRY
                    + "\nAlso correct any missing or invalid source_accounting records and requirement cross-references.\n"
                    + "Every source-accounting rationale must be valid UTF-8 text without lone surrogate code points.\n"
                    + "Map every declared constraint to at least one source-accounting record.\n"
                    if round_index == 2
                    else ""
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
                    round_index=round_index,
                )
                raise DeliveryPreparationAgentError("Delivery authoring assistant failed.") from None

            try:
                draft = parse_delivery_authoring_output(
                    output,
                    expected_plan_id=plan_id,
                    project_root=root,
                    output_dir=output_dir,
                    source_task_description=task_description,
                    require_obligations=True,
                    require_source_accounting=True,
                )
            except DeliveryAuthoringParseError as exc:
                self._record_failure(
                    audit_recorder,
                    prompt=prompt,
                    output=output,
                    error=exc,
                    error_code=exc.code,
                    round_index=round_index,
                )
                if (
                    exc.code.startswith("source_accounting.")
                    or exc.code
                    in {
                        "delivery_authoring.constraint_summary_source_excerpt",
                        "delivery_authoring.obligation_summary_source_excerpt",
                    }
                ) and round_index == 1:
                    continue
                raise
            break
        assert draft is not None

        source_task_path = self._project_relative_path(task_path, root)
        if source_task_path != "<outside-project>":
            draft = replace(
                draft,
                source_task=DeliveryPlanSourceTask(
                    path=source_task_path,
                    sha256="sha256:" + sha256(task_description.encode("utf-8")).hexdigest(),
                ),
            )

        self._record_success(audit_recorder, prompt=prompt, output=output, draft=draft, round_index=round_index)
        if any(item.kind == "stop_and_follow_up" for item in draft.constraints):
            return draft
        preparation_context = {"project": project_context or {}, "guidelines": self._guidelines_context(root)}
        verification = self._verify_constraint_continuity(
            source_accounting=draft.source_accounting,
            project_context=preparation_context,
            authority_description=task_description,
            constraints=draft.constraints,
            obligations=draft.obligations,
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
            verify_obligations=True,
        )
        needs_draft_recovery = (
            self._needs_draft_recovery(verification)
            or any(item.disposition != "preserved" for item in (*draft.constraints, *draft.obligations))
            or (
                draft.source_accounting is not None
                and any(gap.reason == "omitted" for gap in verification.constraint_gaps)
            )
        )
        if (
            verification.constraints_complete
            and verification.unit_context_complete
            and verification.obligations_complete
            and not needs_draft_recovery
        ):
            return replace(draft, constraint_verification=verification)
        if self._verification_has_terminal_blocker(verification):
            return replace(draft, constraint_verification=verification)
        if needs_draft_recovery:
            return self._recover_draft(
                draft,
                verification,
                task_description=task_description,
                root=root,
                output_dir=output_dir,
                project_context=preparation_context,
                audit_recorder=audit_recorder,
            )

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
        if any(item.kind == "stop_and_follow_up" for item in repaired_constraints):
            return replace(
                draft, constraints=repaired_constraints, units=repaired_units, constraint_verification=verification
            )
        repaired_obligations = list(draft.obligations)
        if not verification.obligations_complete:
            repaired_obligations = self._repair_obligation_gaps(
                authority_description=task_description,
                obligations=draft.obligations,
                units=repaired_units,
                gaps=verification.obligation_gaps,
                audit_recorder=audit_recorder,
            )
        repaired_accounting = self._remap_source_accounting(draft.source_accounting, repaired_obligations)
        repaired_verification = self._verify_constraint_continuity(
            source_accounting=repaired_accounting,
            project_context=preparation_context,
            authority_description=task_description,
            constraints=repaired_constraints,
            obligations=repaired_obligations,
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
            verify_obligations=True,
        )
        return replace(
            draft,
            units=repaired_units,
            constraints=repaired_constraints,
            obligations=repaired_obligations,
            source_accounting=repaired_accounting,
            constraint_verification=repaired_verification,
        )

    @staticmethod
    def _verification_has_terminal_blocker(verification: DeliveryConstraintVerification) -> bool:
        return any(item.kind == "stop_and_follow_up" for item in verification.constraints) or any(
            gap.kind == "stop_and_follow_up" for gap in verification.constraint_gaps
        )

    @staticmethod
    def _needs_draft_recovery(verification: DeliveryConstraintVerification) -> bool:
        return bool(
            verification.source_accounting_gaps
            or verification.unit_contract_gaps
            or verification.context_paths
            or any(record.disposition == "unresolved" for record in verification.source_accounting or [])
            or any(item.disposition != "preserved" for item in (*verification.constraints, *verification.obligations))
        )

    @staticmethod
    def _remap_source_accounting(
        records: list[DeliverySourceAccounting] | None, obligations: Sequence[DeliveryAuthoringObligationDraft]
    ) -> list[DeliverySourceAccounting] | None:
        if records is None:
            return None
        updated = []
        for record in records:
            owners = [item.id for item in obligations if record.source_fragment_id in item.source_fragment_ids]
            if owners == record.obligation_ids:
                updated.append(record)
                continue
            rationale = "Mapping updated from independently verified obligation gaps."
            updated.append(
                replace(
                    record,
                    disposition="mapped",
                    obligation_ids=owners,
                    rationale=rationale,
                    rationale_sha256="sha256:" + sha256(rationale.encode()).hexdigest(),
                )
            )
        return updated

    def _draft_payload(self, draft: DeliveryAuthoringDraft) -> dict[str, Any]:
        payload = {
            "plan_id": draft.plan_id,
            "title": draft.title,
            "units": [self._verification_unit_payload(unit) for unit in draft.units],
            "constraints": [item.to_plan_dict() for item in draft.constraints],
            "obligations": [item.to_verification_dict() for item in draft.obligations],
            "warnings": draft.warnings,
        }
        if draft.source_accounting is not None:
            payload["source_accounting"] = [record.to_verification_dict() for record in draft.source_accounting]
        if draft.planning_mode is not None:
            payload["planning_mode"] = draft.planning_mode
        return payload

    def _read_recovery_context(
        self,
        root: Path,
        paths: list[str],
        *,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        audit_prefix: str,
    ) -> dict[str, Any] | None:
        try:
            retrieved = (
                self.context_reader(root, paths)
                if self.context_reader is not None
                else {"status": "context_reader_unavailable"}
            )
        except Exception as exc:
            retrieved = {"status": "context_reader_failed", "error_type": type(exc).__name__}
        if not isinstance(retrieved, dict):
            retrieved = {"status": "context_reader_invalid"}
        files = retrieved.get("files")
        if (
            isinstance(files, list)
            and len(files) == len(paths)
            and all(isinstance(item, dict) and item.get("status") == "read" for item in files)
        ):
            return retrieved
        # Required evidence is a runtime gate; a later provider response cannot waive it.
        if audit_recorder is not None:
            audit_recorder(
                {
                    "phase": f"{audit_prefix}_context_retrieval",
                    "round_index": 1,
                    "prompt": None,
                    "raw_output": None,
                    "requested_paths": list(paths),
                    "retrieved": retrieved,
                    "parsed": {"status": "failed", "error_code": f"{audit_prefix}.context_unavailable"},
                }
            )
        return None

    def _recover_draft(
        self,
        draft: DeliveryAuthoringDraft,
        verification: DeliveryConstraintVerification,
        *,
        task_description: str,
        root: Path,
        output_dir: str | Path,
        project_context: dict[str, Any],
        audit_recorder: DeliveryPreparationAuditRecorder | None,
    ) -> DeliveryAuthoringDraft:
        context = dict(project_context)
        if verification.context_paths:
            retrieved = self._read_recovery_context(
                root,
                verification.context_paths,
                audit_recorder=audit_recorder,
                audit_prefix="delivery_prepare",
            )
            if retrieved is None:
                return replace(draft, constraint_verification=replace(verification, context_unavailable=True))
            context["retrieved"] = retrieved
        gaps = {
            "constraints": [item.to_plan_dict() for item in verification.constraints],
            "constraint_gaps": [gap.to_dict() for gap in verification.constraint_gaps],
            "obligations": [item.to_verification_dict() for item in verification.obligations],
            "obligation_gaps": [gap.to_dict() for gap in verification.obligation_gaps],
            "source_accounting_gaps": verification.source_accounting_gaps,
            "unit_contract_gaps": verification.unit_contract_gaps,
            "unit_context_gaps": [gap.to_dict() for gap in verification.unit_context_gaps],
        }
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX
            + """
You are Sikula's bounded delivery draft correction assistant. Return one complete corrected authoring
JSON draft with the same schema as the candidate. This is the only correction round. Resolve ordinary
choices from the accepted source and supplied project evidence. Evidence cannot change source authority.
Correct only reported gaps and affected unit task Markdown. Preserve all unit identities, titles,
metadata, dependencies, scopes, assets and budgets, and every unrelated contract. Preserve existing
constraint and obligation identities, meanings and provenance; append only verifier-reported omissions
or missing assignments. Existing needs_review/conflict dispositions may become preserved only when
resolved. Treat needs_review/conflict in either the candidate or independent findings as a blocker,
even when the other assessment says preserved. Resolve disagreements using the accepted authority
and supplied evidence; leave unresolved decisions explicit in the corrected candidate.
Map every newly added constraint to its authoritative source fragments in source_accounting.
Outside reported source-accounting gaps, preserve existing mappings and only append new constraint
references with their rationales.
Correct the affected source-accounting records and their private rationales. Leave unaffected
records unchanged. All obligations must be collectively implemented by their assigned unit contracts.
Never reinterpret a prohibition, invent a dependency/API/product choice, or create substitute work.
A missing external prerequisite must remain stop_and_follow_up; do not work around it. If available
context cannot settle a decision, keep it unresolved. Do not write files or run commands.

Authoritative source:
"""
            + task_description
            + "\n\nCandidate:\n"
            + json.dumps(self._draft_payload(draft))
            + "\n\nIndependent findings:\n"
            + json.dumps(gaps)
            + "\n\nAuthorized context:\n"
            + json.dumps(context)
        )
        output = None
        try:
            output = self.llm.generate("", prompt)
            repaired = parse_delivery_authoring_output(
                output,
                expected_plan_id=draft.plan_id,
                project_root=root,
                output_dir=output_dir,
                source_task_description=task_description,
                require_obligations=True,
                require_source_accounting=draft.source_accounting is not None,
            )
            if not any(item.kind == "stop_and_follow_up" for item in repaired.constraints):
                self._assert_draft_recovery(draft, repaired, verification)
        except Exception as exc:
            self._record_constraint_verification_failure(
                audit_recorder,
                phase="delivery_prepare_draft_recovery",
                prompt=prompt,
                output=output,
                error=exc,
                error_code=getattr(exc, "code", "delivery_prepare.recovery_failed"),
                round_index=1,
            )
            if isinstance(exc, DeliveryAuthoringParseError):
                raise
            raise DeliveryPreparationAgentError("Bounded delivery preparation recovery failed.") from None
        if audit_recorder is not None:
            audit_recorder(
                {
                    "phase": "delivery_prepare_draft_recovery",
                    "round_index": 1,
                    "prompt": prompt,
                    "raw_output": output,
                    "parsed": {"status": "parsed", "unit_ids": [unit.id for unit in repaired.units]},
                }
            )
        repaired = replace(repaired, source_task=draft.source_task)
        if any(item.kind == "stop_and_follow_up" for item in repaired.constraints):
            return repaired
        final_verification = self._verify_constraint_continuity(
            authority_description=task_description,
            constraints=repaired.constraints,
            obligations=repaired.obligations,
            units=repaired.units,
            source_accounting=repaired.source_accounting,
            project_context=context,
            verification_scope="source_task_to_units_after_bounded_recovery",
            completeness_rule="Verify all source constraints, outcomes, coverage decisions and corrected unit contracts independently; unresolved decisions stay unresolved.",
            audit_recorder=audit_recorder,
            audit_phase="delivery_prepare_constraint_verification",
            round_index=2,
            verify_unit_context=True,
            verify_obligations=True,
        )
        return replace(repaired, constraint_verification=final_verification)

    def _assert_draft_recovery(
        self,
        original: DeliveryAuthoringDraft,
        repaired: DeliveryAuthoringDraft,
        verification: DeliveryConstraintVerification,
    ) -> None:
        def reject() -> None:
            raise DeliveryAuthoringParseError(
                "delivery_prepare.recovery_scope_changed",
                "Draft recovery changed unrelated identity, metadata, or authority.",
            )

        if (original.plan_id, original.title, original.planning_mode, original.warnings) != (
            repaired.plan_id,
            repaired.title,
            repaired.planning_mode,
            repaired.warnings,
        ):
            reject()
        affected = self._recovery_unit_ids(verification)
        affected.update(
            unit_id
            for item in (*original.constraints, *original.obligations)
            if item.disposition != "preserved"
            for unit_id in item.unit_ids
        )
        if [unit.id for unit in original.units] != [unit.id for unit in repaired.units]:
            reject()
        for before, after in zip(original.units, repaired.units):
            if replace(after, task_markdown=before.task_markdown) != before or (
                before.id not in affected and before != after
            ):
                reject()
        self._assert_constraint_repair(
            [replace(item, disposition="preserved") for item in original.constraints],
            [replace(item, disposition="preserved") for item in repaired.constraints],
            verification.constraint_gaps,
        )
        self._assert_obligation_repair(
            [replace(item, disposition="preserved") for item in original.obligations],
            [replace(item, disposition="preserved") for item in repaired.obligations],
            verification.obligation_gaps,
        )
        changed_fragments = {gap["source_fragment_id"] for gap in verification.source_accounting_gaps}
        changed_fragments.update(ref for gap in verification.obligation_gaps for ref in gap.source_fragment_ids)
        changed_fragments.update(
            record.source_fragment_id
            for record in original.source_accounting or []
            if record.disposition == "unresolved"
        )
        corrected = {record.source_fragment_id: record for record in repaired.source_accounting or []}
        new_constraint_ids = {item.id for item in repaired.constraints} - {item.id for item in original.constraints}
        for record in original.source_accounting or []:
            if record.source_fragment_id in changed_fragments:
                continue
            after = corrected.get(record.source_fragment_id)
            if after == record:
                continue
            if after is None:
                reject()
            added_refs = set(after.constraint_ids) - set(record.constraint_ids)
            if not added_refs or not added_refs <= new_constraint_ids:
                reject()
            if (
                replace(
                    after,
                    constraint_ids=[ref for ref in after.constraint_ids if ref not in added_refs],
                    disposition=record.disposition,
                    rationale=record.rationale,
                    rationale_sha256=record.rationale_sha256,
                )
                != record
            ):
                reject()

    @staticmethod
    def _recovery_unit_ids(verification: DeliveryConstraintVerification) -> set[str]:
        affected = {gap["unit_id"] for gap in verification.unit_contract_gaps}
        affected.update(gap.unit_id for gap in verification.unit_context_gaps)
        affected.update(
            unit_id
            for gap in (*verification.constraint_gaps, *verification.obligation_gaps)
            for unit_id in gap.affected_unit_ids
        )
        affected.update(
            unit_id
            for item in (*verification.constraints, *verification.obligations)
            if item.disposition != "preserved"
            for unit_id in item.unit_ids
        )
        return affected

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
        applicable_obligations: Sequence[dict[str, Any]] = (),
        failure_evidence: dict[str, Any] | None = None,
        amend_reason: str | None = None,
        budget_exceeded: dict[str, Any] | None = None,
        audit_recorder: DeliveryPreparationAuditRecorder | None = None,
    ) -> DeliveryAmendmentAuthoringDraft:
        if any(item.get("kind") == "stop_and_follow_up" for item in applicable_constraints):
            return DeliveryAmendmentAuthoringDraft(
                plan_id=plan_id,
                target_unit_id=target_unit_id,
                replacement_units=[],
                disposition="external_dependency_follow_up_required",
                summary="Resolve the authoritative external prerequisite before amendment authoring.",
            )
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
                applicable_obligations_json=json.dumps(list(applicable_obligations), indent=2, sort_keys=True),
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
        if (not applicable_constraints and not applicable_obligations) or not draft.replacement_units:
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
        if set(draft.obligation_assignments) != {str(value["id"]) for value in applicable_obligations}:
            raise DeliveryAuthoringParseError(
                "delivery_amend.obligation_assignments_invalid",
                "Assignments must cover exactly the applicable inherited obligations.",
            )
        obligations = [
            DeliveryAuthoringObligationDraft(
                id=str(value.get("id", "")),
                summary=str(value.get("summary", "")),
                source_fragment_ids=list(value.get("source_fragment_ids", [])),
                unit_ids=list(draft.obligation_assignments.get(str(value["id"]), replacement_ids)),
                disposition="preserved",
            )
            for value in applicable_obligations
        ]
        inspection_context = {
            "project": project_context or {},
            "guidelines": self._guidelines_context(root),
            "amendment": {
                "target_unit_id": target_unit_id,
                "inherited_obligations": list(applicable_obligations),
            },
        }
        verification = self._verify_constraint_continuity(
            project_context=inspection_context,
            authority_description=target_task_description,
            constraints=constraints,
            obligations=obligations,
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
            verify_obligations=bool(obligations),
            known_obligation_source_fragment_ids={
                fragment_id for obligation in obligations for fragment_id in obligation.source_fragment_ids
            },
        )
        if self._verification_has_terminal_blocker(verification):
            return self._stopped_amendment(draft, verification)
        if (
            self._needs_draft_recovery(verification)
            or not verification.constraints_complete
            or not verification.obligations_complete
        ):
            return self._recover_amendment(
                draft,
                verification,
                root=root,
                authoring_prompt=prompt,
                authority=target_task_description,
                constraints=constraints,
                obligations=obligations,
                inspection_context=inspection_context,
                audit_recorder=audit_recorder,
            )
        return replace(draft, constraint_verification=verification)

    @staticmethod
    def _stopped_amendment(
        draft: DeliveryAmendmentAuthoringDraft,
        verification: DeliveryConstraintVerification,
    ) -> DeliveryAmendmentAuthoringDraft:
        return replace(
            draft,
            replacement_units=[],
            obligation_assignments={},
            constraint_verification=verification,
            disposition="external_dependency_follow_up_required",
            summary="Independent verification identified an unresolved external prerequisite.",
        )

    def _recover_amendment(
        self,
        draft: DeliveryAmendmentAuthoringDraft,
        verification: DeliveryConstraintVerification,
        *,
        root: Path,
        authoring_prompt: str,
        authority: str,
        constraints: Sequence[DeliveryAuthoringConstraintDraft],
        obligations: Sequence[DeliveryAuthoringObligationDraft],
        inspection_context: dict[str, Any],
        audit_recorder: DeliveryPreparationAuditRecorder | None,
    ) -> DeliveryAmendmentAuthoringDraft:
        context = dict(inspection_context)
        if verification.context_paths:
            retrieved = self._read_recovery_context(
                root,
                verification.context_paths,
                audit_recorder=audit_recorder,
                audit_prefix="delivery_amend",
            )
            if retrieved is None:
                return replace(draft, constraint_verification=replace(verification, context_unavailable=True))
            context["retrieved"] = retrieved
        candidate = {
            "plan_id": draft.plan_id,
            "target_unit_id": draft.target_unit_id,
            "replacement_units": [self._verification_unit_payload(unit) for unit in draft.replacement_units],
            "obligation_assignments": draft.obligation_assignments,
            "warnings": draft.warnings,
        }
        for name in ("amend_reason", "budget_exceeded"):
            value = getattr(draft, name)
            if value is not None:
                candidate[name] = value
        affected = self._recovery_unit_ids(verification)
        findings = {
            "constraints": [item.to_plan_dict() for item in verification.constraints],
            "constraint_gaps": [gap.to_dict() for gap in verification.constraint_gaps],
            "obligations": [item.to_verification_dict() for item in verification.obligations],
            "obligation_gaps": [gap.to_dict() for gap in verification.obligation_gaps],
            "unit_contract_gaps": verification.unit_contract_gaps,
        }
        prompt = (
            authoring_prompt
            + "\n\nThis is the only bounded amendment correction round. Return the corrected candidate in the "
            "same JSON schema. Change only task_markdown of these affected replacement units: "
            + json.dumps(sorted(affected))
            + ". Preserve every identity, dependency, scope, asset, budget, obligation assignment, inherited "
            "requirement and unrelated contract. Resolve ordinary implementation choices using authorized "
            "evidence; evidence cannot override source authority. Do not invent a substitute dependency or "
            "API, expand scope, or weaken a prohibition. If an external prerequisite is unavailable, return "
            "external_dependency_follow_up_required. Otherwise leave unresolved decisions explicit. "
            "Do not write files or run commands.\n\nCandidate:\n"
            + json.dumps(candidate)
            + "\n\nIndependent findings:\n"
            + json.dumps(findings)
            + "\n\nAuthorized context:\n"
            + json.dumps(context)
        )
        output = None
        try:
            output = self.llm.generate("", prompt)
            repaired = parse_delivery_amendment_authoring_output(
                output,
                expected_plan_id=draft.plan_id,
                expected_target_unit_id=draft.target_unit_id,
                project_root=root,
            )
            if repaired.disposition != "external_dependency_follow_up_required":
                if (
                    replace(repaired, replacement_units=draft.replacement_units) != draft
                    or [unit.id for unit in repaired.replacement_units] != [unit.id for unit in draft.replacement_units]
                    or any(
                        replace(after, task_markdown=before.task_markdown) != before
                        or (before.id not in affected and after != before)
                        for before, after in zip(draft.replacement_units, repaired.replacement_units)
                    )
                ):
                    raise DeliveryAuthoringParseError(
                        "delivery_amend.recovery_scope_changed",
                        "Amendment recovery changed unrelated identity, metadata, or authority.",
                    )
        except Exception as exc:
            self._record_constraint_verification_failure(
                audit_recorder,
                phase="delivery_amend_draft_recovery",
                prompt=prompt,
                output=output,
                error=exc,
                error_code=getattr(exc, "code", "delivery_amend.recovery_failed"),
                round_index=1,
            )
            if isinstance(exc, DeliveryAuthoringParseError):
                raise
            raise DeliveryPreparationAgentError("Bounded amendment preparation recovery failed.") from None
        if audit_recorder is not None:
            audit_recorder(
                {
                    "phase": "delivery_amend_draft_recovery",
                    "round_index": 1,
                    "prompt": prompt,
                    "raw_output": output,
                    "parsed": {"status": "parsed", "unit_ids": [unit.id for unit in repaired.replacement_units]},
                }
            )
        if repaired.disposition == "external_dependency_follow_up_required":
            return repaired
        final_verification = self._verify_constraint_continuity(
            project_context=context,
            authority_description=authority,
            constraints=constraints,
            obligations=obligations,
            units=repaired.replacement_units,
            verification_scope="amendment_target_to_replacements_after_bounded_recovery",
            completeness_rule="Verify every deterministic inherited constraint and collective outcome against the corrected replacement contracts; unresolved decisions stay unresolved.",
            audit_recorder=audit_recorder,
            audit_phase="delivery_amend_constraint_verification",
            round_index=2,
            verify_unit_context=False,
            verify_obligations=bool(obligations),
            known_obligation_source_fragment_ids={ref for item in obligations for ref in item.source_fragment_ids},
        )
        if self._verification_has_terminal_blocker(final_verification):
            return self._stopped_amendment(repaired, final_verification)
        return replace(repaired, constraint_verification=final_verification)

    def _verify_constraint_continuity(
        self,
        *,
        authority_description: str,
        constraints: Sequence[DeliveryAuthoringConstraintDraft],
        obligations: Sequence[DeliveryAuthoringObligationDraft],
        units: Sequence[DeliveryAuthoringUnitDraft],
        verification_scope: str,
        completeness_rule: str,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        audit_phase: str,
        round_index: int,
        verify_unit_context: bool,
        verify_obligations: bool,
        known_obligation_source_fragment_ids: set[str] | None = None,
        source_accounting: list[DeliverySourceAccounting] | None = None,
        project_context: dict[str, Any] | None = None,
    ) -> DeliveryConstraintVerification:
        constraints_payload = [constraint.to_plan_dict() for constraint in constraints]
        obligations_payload = [obligation.to_verification_dict() for obligation in obligations]
        units_payload = [self._verification_unit_payload(unit) for unit in units]
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX
            + _DELIVERY_CONSTRAINT_VERIFICATION_PROMPT.format(
                source_accounting_json=json.dumps([record.to_verification_dict() for record in source_accounting])
                if source_accounting is not None
                else "null",
                project_context_json=json.dumps(project_context or {}),
                verification_scope=verification_scope,
                completeness_rule=completeness_rule,
                unit_context_rule=(
                    "Check every candidate unit against the authoritative source task and report exact missing "
                    "source literals as described below."
                    if verify_unit_context
                    else "This is a constraint-only amendment check. Return unit_context_complete=true and "
                    "unit_context_gaps=[]."
                ),
                obligation_rule=(
                    (
                        "The supplied obligation set and replacement ownership are deterministic. Echo every supplied "
                        "obligation exactly. Use disposition preserved only when its assigned replacement contracts collectively "
                        "preserve the target contract's entire contribution to the outcome. The amendment context supplies "
                        "original ownership: other owners and their contracts remain unchanged and retain their contributions. "
                        "Do not require replacements to duplicate those contributions or assume other owners will absorb "
                        "missing target behavior. If the target was the only owner, replacements must collectively deliver "
                        "the whole outcome. Each replacement must specify its contribution; otherwise use needs_review, "
                        "or conflict when its contract opposes the outcome. Do not infer obligations or gaps beyond "
                        "the supplied deterministic set."
                        if known_obligation_source_fragment_ids is not None
                        else "Set obligations_complete to false if any actionable source requirement is absent from "
                        "the obligation input, lacks source-fragment provenance, or is not assigned to every "
                        "affected unit. Use disposition preserved only when the assigned contracts collectively "
                        "deliver the whole source outcome."
                    )
                    if verify_obligations
                    else "This amendment check does not assess source obligations. Return obligations_complete=true, "
                    "obligation_gaps=[], and obligations=[]."
                ),
                authority_description=authority_description,
                constraints_json=json.dumps(constraints_payload, indent=2, sort_keys=True),
                authority_fragments_json=json.dumps(
                    [fragment.to_prompt_dict() for fragment in delivery_authority_fragments(authority_description)],
                    indent=2,
                    sort_keys=True,
                ),
                obligations_json=json.dumps(obligations_payload, indent=2, sort_keys=True),
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
                require_source_accounting=source_accounting is not None,
                require_obligations=verify_obligations,
                known_obligation_source_fragment_ids=known_obligation_source_fragment_ids,
            )
            self._assert_constraint_verification_echo(constraints, verification)
            self._assert_obligation_verification_echo(obligations, verification)
            if (
                source_accounting is not None
                and verification.source_accounting != source_accounting
                and not self._verification_has_terminal_blocker(verification)
            ):
                raise DeliveryAuthoringParseError(
                    "source_accounting.verification_mismatch",
                    "Independent verification must echo the supplied accounting and report disagreements as gaps.",
                )
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

    def _repair_obligation_gaps(
        self,
        *,
        authority_description: str,
        obligations: Sequence[DeliveryAuthoringObligationDraft],
        units: Sequence[DeliveryAuthoringUnitDraft],
        gaps: Sequence[DeliveryObligationGap],
        audit_recorder: DeliveryPreparationAuditRecorder | None,
    ) -> list[DeliveryAuthoringObligationDraft]:
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX
            + _DELIVERY_OBLIGATION_REPAIR_PROMPT.format(
                obligations_json=json.dumps(
                    [obligation.to_verification_dict() for obligation in obligations],
                    indent=2,
                    sort_keys=True,
                ),
                gaps_json=json.dumps([gap.to_dict() for gap in gaps], indent=2, sort_keys=True),
            )
        )
        try:
            output = self.llm.generate("", prompt)
        except Exception as exc:
            self._record_obligation_repair_failure(
                audit_recorder,
                prompt=prompt,
                output=None,
                error=exc,
                error_code="delivery_obligation_repair.authoring_failed",
            )
            raise DeliveryPreparationAgentError("Delivery obligation repair assistant failed.") from None
        try:
            repaired = parse_delivery_obligation_repair_output(
                output,
                unit_ids={unit.id for unit in units},
                source_task_description=authority_description,
            )
            self._assert_obligation_repair(obligations, repaired, gaps)
        except DeliveryAuthoringParseError as exc:
            self._record_obligation_repair_failure(
                audit_recorder,
                prompt=prompt,
                output=output,
                error=exc,
                error_code=exc.code,
            )
            raise
        self._record_obligation_repair_success(
            audit_recorder,
            prompt=prompt,
            output=output,
            obligations=repaired,
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
    def _assert_obligation_verification_echo(
        obligations: Sequence[DeliveryAuthoringObligationDraft],
        verification: DeliveryConstraintVerification,
    ) -> None:
        expected = [(item.id, item.summary, item.source_fragment_ids, item.unit_ids) for item in obligations]
        actual = [(item.id, item.summary, item.source_fragment_ids, item.unit_ids) for item in verification.obligations]
        if actual != expected:
            raise DeliveryAuthoringParseError(
                "delivery_obligation_verification.obligations_mismatch",
                "Obligation verification must echo every supplied obligation exactly and in order.",
            )

    @staticmethod
    def _assert_obligation_repair(
        original: Sequence[DeliveryAuthoringObligationDraft],
        repaired: Sequence[DeliveryAuthoringObligationDraft],
        gaps: Sequence[DeliveryObligationGap],
    ) -> None:
        omitted = [gap for gap in gaps if gap.reason == "omitted"]
        if len(repaired) != len(original) + len(omitted):
            raise DeliveryAuthoringParseError(
                "delivery_obligation_repair.count_invalid",
                "Obligation repair must add exactly one obligation for every omitted gap.",
            )
        assignments: dict[str, list[str]] = {}
        for gap in gaps:
            if gap.reason != "incompletely_assigned" or gap.obligation_id is None:
                continue
            assigned = assignments.setdefault(gap.obligation_id, [])
            for unit_id in gap.affected_unit_ids:
                if unit_id not in assigned:
                    assigned.append(unit_id)
        for index, existing in enumerate(original):
            candidate = repaired[index]
            if (
                candidate.id != existing.id
                or candidate.summary != existing.summary
                or candidate.source_fragment_ids != existing.source_fragment_ids
                or candidate.disposition != existing.disposition
            ):
                raise DeliveryAuthoringParseError(
                    "delivery_obligation_repair.existing_changed",
                    "Obligation repair must preserve existing obligation identity and provenance.",
                )
            expected_units = list(existing.unit_ids)
            for unit_id in assignments.get(existing.id, []):
                if unit_id not in expected_units:
                    expected_units.append(unit_id)
            if candidate.unit_ids != expected_units:
                raise DeliveryAuthoringParseError(
                    "delivery_obligation_repair.assignment_invalid",
                    "Obligation repair may add only verifier-identified missing unit assignments.",
                )
        for candidate, gap in zip(repaired[len(original) :], omitted):
            if (
                candidate.summary != gap.summary
                or candidate.source_fragment_ids != gap.source_fragment_ids
                or candidate.unit_ids != gap.affected_unit_ids
                or candidate.disposition != DELIVERY_CONSTRAINT_PRESERVED_DISPOSITION
            ):
                raise DeliveryAuthoringParseError(
                    "delivery_obligation_repair.omitted_mismatch",
                    "New obligations must match omitted verifier gaps exactly.",
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
            authority_fragments_json=json.dumps(
                [fragment.to_prompt_dict() for fragment in delivery_authority_fragments(task_description)],
                indent=2,
                sort_keys=True,
            ),
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
        round_index: int,
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": "delivery_prepare_authoring",
                "round_index": round_index,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "parsed",
                    "plan_id": draft.plan_id,
                    "unit_ids": [unit.id for unit in draft.units],
                    "unit_count": len(draft.units),
                    "obligation_ids": [obligation.id for obligation in draft.obligations],
                    "obligation_count": len(draft.obligations),
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
        round_index: int,
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
                "round_index": round_index,
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
                    "obligations_complete": verification.obligations_complete,
                    "obligation_ids": [obligation.id for obligation in verification.obligations],
                    "obligation_dispositions": [obligation.disposition for obligation in verification.obligations],
                    "obligation_gaps": [gap.to_dict() for gap in verification.obligation_gaps],
                    "source_accounting": [record.to_verification_dict() for record in verification.source_accounting]
                    if verification.source_accounting is not None
                    else None,
                    "source_accounting_gaps": verification.source_accounting_gaps,
                    "unit_contract_gaps": verification.unit_contract_gaps,
                    "context_paths": verification.context_paths,
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

    def _record_obligation_repair_success(
        self,
        audit_recorder: DeliveryPreparationAuditRecorder | None,
        *,
        prompt: str,
        output: str,
        obligations: Sequence[DeliveryAuthoringObligationDraft],
    ) -> None:
        if audit_recorder is None:
            return
        audit_recorder(
            {
                "phase": "delivery_prepare_obligation_repair",
                "round_index": 1,
                "prompt": prompt,
                "raw_output": output,
                "parsed": {
                    "status": "parsed",
                    "obligation_ids": [obligation.id for obligation in obligations],
                    "dispositions": [obligation.disposition for obligation in obligations],
                },
            }
        )

    def _record_obligation_repair_failure(
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
                "phase": "delivery_prepare_obligation_repair",
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
