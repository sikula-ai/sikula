# Fail delivery units closed on scope and external dependency gaps

## Context

Sikula delivery units currently carry `scope_paths`, budgets, dependencies, and
standalone task contracts, while child runs also inherit repository-wide
`sandbox.allowed_write_paths`. These layers do not yet form one fail-closed
execution boundary.

An observed single-repository delivery run exposed the gap:

- The high-level source task said that an external protocol repository remained
  authoritative, must not be changed in the current milestone, and that any
  protocol gap must stop the Console implementation and become a follow-up in
  the protocol repository.
- A generated delivery unit instead required the Console repository to repeat
  structural schema, fixture, and OpenAPI validation while also prohibiting
  changes to the protocol repository.
- The unit declared `scope_paths` for repository scripts and root package
  metadata, but the child implementer used the broader repository-level write
  configuration and changed API project dependency files as well.
- A reviewer explicitly said that adding validator dependencies required a
  unit-scope amendment. The run treated that finding as an ordinary fix request
  and consumed every configured review-fix cycle instead of stopping for
  amendment.
- The failed worktree ended with three changed files and approximately 925
  changed lines, including a large duplicate validator in the consumer
  repository. No build, test, security review, commit, handoff, or assembly was
  produced.
- The subsequent manual `delivery amend prepare` received the original unit
  contract but not the failed child evidence or parent-task invariant. It
  produced a formally valid split that repeated the same ownership and scope
  error.

Since that incident, Sikula `0.3.0-dev+main.92a0f1c` has added several relevant
delivery guarantees that this task must preserve:

- `max_planner_steps` is enforced before delivery child implementation and a
  verified planner-budget stop can feed `run-next --prepare-budget-split`.
- Amendment preparation validates against the assembled delivery head, and
  amendment apply publishes the updated plan and replacement contracts directly
  to `final_branch` without changing the operator checkout.
- Amendment proposals with multiple budget fields normalize those fields in a
  stable order so proposal fingerprint replay and apply remain idempotent.
- Run invocation configuration and provider-neutral usage evidence are retained
  for later audit.

These changes improve budget recovery, assembly continuity, and observability,
but they do not narrow child write permissions to non-empty unit `scope_paths`,
classify external dependency gaps, preserve source-task ownership constraints,
or provide failed-child scope/review evidence to ordinary amendment authoring.

The deterministic gates behaved correctly for the information they received,
but Sikula did not preserve source-task ownership constraints through planning,
execution, review recovery, and amendment authoring.

## Goal

Make delivery execution fail closed as soon as a unit requires work outside its
declared repository scope or discovers that an authoritative read-only external
dependency must change. Preserve the relevant source-task constraints and
failure evidence so the recommended recovery action is truthful and actionable.

## Desired behavior

### Source-task constraint continuity

- Delivery preparation identifies durable source-task constraints that govern
  all generated units, including repository ownership, read-only dependencies,
  explicit fail-and-follow-up rules, security boundaries, and prohibited
  fallback implementations.
- Generated units must not contradict those constraints. A unit cannot direct a
  consumer repository to implement behavior that the source task assigns to an
  external dependency first.
- The checked-in source task remains authoritative. Unit readiness must not be
  treated as proof of source-to-unit semantic consistency.
- Child analysis, implementation, review, and amendment recovery receive the
  relevant inherited constraints through an auditable, deterministic context
  boundary. They must not rely on an agent rediscovering the parent task by
  searching the repository.
- If Sikula cannot establish that a generated unit is consistent with a hard
  source constraint, preparation stops for review instead of silently dropping
  the constraint.

### Effective unit write scope

- A delivery child with non-empty `scope_paths` has an effective production
  write scope derived from the intersection of repository-wide
  `sandbox.allowed_write_paths` and the selected unit's declared scope.
- Existing plans whose selected unit omits `scope_paths` or declares an empty
  list retain the repository-wide write scope for backward compatibility. The
  child state must distinguish this legacy/default scope from an explicitly
  narrowed unit scope.
- A declared unit scope with no writable intersection blocks before child
  agents start instead of silently falling back to repository-wide permission.
- On fresh execution and resume, the persisted scope is revalidated against
  the active child worktree after dependency assembly and immediately before
  runtime sandbox construction. A dependency that replaces an allowed path
  with an escaping symlink therefore stops before any provider call.
- The narrowed scope is supplied to every production write-capable agent and is
  enforced after each provider call by a provider-independent changed-file
  audit.
- Repository-wide write permission must never broaden a delivery unit's scope.
- Required manifests, lockfiles, generated metadata, migrations, or other
  companion files are writable only when the unit declares them. Sikula must
  not infer permission merely because a package manager normally changes them.
- Test-writer output remains governed by the configured test-write policy and
  test-agent rules. It must not implicitly expand production-agent scope.
- An out-of-scope production write stops the child before build, test, review,
  commit, handoff, or assembly. The changed worktree and local audit evidence
  remain inspectable, but the changes cannot be adopted as a successful unit.
- The child stop and parent unit progress use the stable failure code
  `unit_scope_violation`; public command errors use
  `delivery.unit_scope_violation`. The stop records sanitized changed paths,
  the declared unit scope, and an amendment-oriented next action. Public output
  must not include file contents or absolute paths.

### External dependency gaps

- A unit may declare or inherit that another repository or checkout is an
  authoritative read-only dependency.
- When analysis, implementation, or review establishes that satisfying the
  contract requires changing that dependency, the child stops with a stable,
  structured external-dependency failure instead of implementing a duplicate,
  fallback, shim, copied contract, or consumer-only replacement.
- Analyst and implementer output contracts provide a parser-owned structured
  disposition for this stop. Orchestration must not infer it from arbitrary
  prose, and malformed structured output must degrade safely without
  authorizing another write-capable cycle. An advertised but malformed
  implementer disposition persists `implementer_disposition_invalid` as a
  non-retryable terminal stop even when partial changes were already written.
  An implementer stop remains valid
  with zero changes or with preserved partial changes, so the ordinary
  no-change failure path cannot erase or misclassify it.
- The stop occurs before additional implementation or review-fix cycles. A
  finding that says the dependency must change or the unit scope must be amended
  is not an ordinary code-fix request.
- Delivery status distinguishes an external dependency gap from an ordinary
  implementation failure and recommends an external follow-up or plan
  amendment. A plain `--reset-failed` retry must not be presented as sufficient
  when no relevant context has changed.
- The child stop and parent unit progress use the stable failure code
  `external_dependency_gap`; public command errors use
  `delivery.external_dependency_gap`.
- Sikula does not write to another repository automatically. Multi-repository
  execution requires separate explicit design and authorization.

### Structured review recovery

- Review and security-review findings can carry a structured disposition at
  least for `fix_in_scope`, `requires_scope_amendment`, and
  `external_dependency_gap`.
- Only `fix_in_scope` findings enter the normal implementer fix loop.
- Amendment and external-dependency dispositions stop the loop immediately,
  persist the finding, preserve the worktree, and update parent delivery
  progress with a stable failure code and recommended action.
- `requires_scope_amendment` maps to parent failure code
  `scope_amendment_required` and public command error
  `delivery.scope_amendment_required`; `external_dependency_gap` maps to the
  same-named parent failure code and `delivery.external_dependency_gap` public
  command error.
- Free-form reviewer wording alone must not be the only signal available to the
  orchestrator for deciding whether another write-capable cycle is authorized.
- Existing review and security-review approval requirements remain fail-closed.

### Stable stop and recovery projection

- `delivery status --json` preserves the existing explicit projection shape.
  For `unit_scope_violation`, `scope_amendment_required`,
  `external_dependency_gap`, and `implementer_disposition_invalid`, the failed
  unit exposes that exact value in both
  `failure_code` and `run_next_blocked_reason`, sets
  `run_next_available: false`, and omits the ordinary `retry_failed`
  `run_next_action`.
- Plan-level `next_action` text recommends `delivery amend prepare` for
  `unit_scope_violation` and `scope_amendment_required`, and an external
  dependency follow-up for `external_dependency_gap`. Text and JSON projections
  must be derived from the same typed status result.
- `delivery run-next` returns non-zero for all three stops. An unchanged
  `delivery run-next --reset-failed` invocation fails preflight with the matching
  `delivery.*` public error code and does not resume the child, start agents, or
  mutate progress.
- When amendment authoring proves that the current repository cannot own the
  required change, `delivery amend prepare` returns non-zero with
  `delivery_amend.external_dependency_follow_up_required`, does not publish a
  proposal, and projects only sanitized failure metadata and the external
  follow-up action.
- Existing public fields and failure codes keep their current meaning. These
  additions must be explicit allowlisted projections rather than serialized raw
  task state or terminal text.

### Amendment evidence

- Preserve the existing verified planner-budget recovery metadata and its
  `run-next --prepare-budget-split` behavior without reimplementing that flow.
- `delivery amend prepare` for a scope or external-dependency stop receives
  sanitized, structured evidence from the linked child state, including:
  - the failure classification and recommended recovery action;
  - applicable inherited source-task constraints;
  - declared unit scope and effective write scope;
  - changed-file paths and counts without diff contents;
  - reviewer and security-review issue summaries and dispositions;
  - dependency handoff identities already available to the unit.
- Amendment authoring uses this evidence to correct ownership and scope, not
  merely split the original task wording into smaller versions of the same
  invalid approach.
- If the evidence requires changes in an external repository that the plan
  cannot own, amendment preparation returns an explicit external follow-up
  requirement instead of a misleading in-repository proposal, using the
  no-proposal behavior and stable public error defined above.
- Deterministic amendment validation, assembled-head checks, bounded
  `final_branch` publication, and apply recovery remain the final authority. No
  tracked plan or unit file changes during `amend prepare` or apply dry-run.
- When a constrained target is superseded, every applicable inherited
  constraint is reassigned to all replacement units before deterministic plan
  validation; the superseded target is removed from those references.
- Public text and JSON output remain sanitized. Raw prompts, provider output,
  task-state blobs, diffs, source excerpts, credentials, and absolute local
  paths stay in existing ignored local audit artifacts.

### Existing budget behavior compatibility

- Preserve the existing hard `max_planner_steps` stop, verified values, and
  amendment recovery behavior.
- Existing non-planner sizing and budget fields remain advisory as currently
  documented; this task does not turn them into new hard runtime limits.
- A planner-budget stop remains distinct from a scope violation or external
  dependency gap and continues to recommend the existing amendment recovery.
- Proposal serialization preserves the canonical ordering of all supported
  budget fields so multiple-field proposal fingerprints replay and apply
  successfully without weakening stale-input checks.

## User-facing behavior

- `delivery prepare`, `delivery check`, and structured output identify units
  that conflict with inherited hard constraints before implementation whenever
  the conflict is known during preparation.
- `delivery run-next` shows the selected unit's effective write scope in local
  inspectable state and applies it to the child run.
- `delivery status` reports stable failure codes and one truthful next action:
  retry an in-scope failure, prepare an amendment, or resolve an external
  dependency follow-up.
- `delivery amend prepare` explains when no valid single-repository replacement
  graph can satisfy an external dependency requirement.
- Existing successful single-repository delivery units continue to run,
  review, hand off, assemble, and finalize without additional operator steps.

## Scope

- Delivery preparation and generated-unit context in `agents/` and `core/`.
- Delivery child orchestration, progress classification, status projection,
  retry decisions, and amendment evidence in `core/` and `sikula_cli/`.
- Provider-independent changed-file auditing and effective sandbox calculation.
- Additive local state, proposal, and audit metadata needed for inherited
  constraints, structured dispositions, and sanitized failure evidence.
- Focused documentation and tests for preparation, run-next, review recovery,
  amendment preparation, existing planner-budget compatibility, and
  public-output sanitization.

The task is intentionally a delivery-plan candidate. Do not implement all
surfaces as one autonomous child run.

## Acceptance criteria

- A source task that says "fail and create an external follow-up" cannot produce
  an executable unit that silently implements the missing dependency behavior
  in the consumer repository.
- A child unit scoped to `scripts/` cannot modify `apps/` even when the global
  repository sandbox permits both paths.
- A legacy unit with absent or empty `scope_paths` retains the configured
  repository-wide production write scope, while an explicit scope with an
  empty intersection blocks before child creation.
- Out-of-scope writes are detected independently of provider claims and stop
  before validation, commit, handoff, and assembly.
- A reviewer disposition requiring scope amendment stops the fix loop on that
  review cycle and does not consume another implementer attempt.
- An implementer that reports an `external_dependency_gap` after making no
  authorized in-scope solution stops immediately and cannot be treated as a
  successful no-change pass or ordinary implementation failure.
- An advertised but malformed implementer disposition remains a durable,
  non-retryable terminal stop after partial writes and cannot continue through
  review or validation after `--reset-failed`.
- An external dependency gap produces a stable failure classification and does
  not recommend an unchanged `--reset-failed` retry.
- Amendment authoring for that failure receives sanitized source constraints,
  changed paths, and review dispositions and either proposes corrected scope or
  reports that an external follow-up is required.
- A valid in-scope reviewer issue still uses the existing bounded fix loop.
- Test-writer changes remain possible only through test-write policy and do not
  grant production agents broader access.
- No failed or stopped unit writes a handoff, advances the final delivery branch,
  or becomes an assembled dependency.
- Existing pending, running, failed, retry, budget-split, amendment, finalize,
  and resume behavior remains compatible for unaffected units.
- Existing planner-budget split proposals and amendment publication to the
  assembled `final_branch` remain compatible.
- A dependency-created symlink escape is rejected against the assembled child
  worktree on fresh execution and resume before runtime construction.
- Splitting a constrained unit transfers each applicable constraint to every
  replacement so the amended plan remains valid.
- Amendment proposals carrying multiple supported budget fields retain stable
  fingerprints across load, validation, dry-run, and apply replay.

## Tests

Add focused unit and end-to-end tests covering:

1. Source-task hard ownership constraints are present in generated unit and
   child execution context.
2. A generated unit that contradicts an inherited fail-and-follow-up rule is
   blocked before execution.
3. Effective write scope is the intersection of global write paths and unit
   scope paths.
4. A provider that writes outside effective scope is detected by changed-file
   inspection even if it reports success.
5. Scope violation preserves local audit/worktree evidence but skips build,
   tests, reviews, commit, handoff, and assembly.
6. Test-writer policy remains separate from production write scope.
7. Reviewer `fix_in_scope` findings continue through the normal fix loop.
8. Reviewer `requires_scope_amendment` findings stop immediately and persist an
   amendment action.
9. Reviewer, analyst, or implementer `external_dependency_gap` findings stop
   immediately and persist an external follow-up action.
10. `delivery status` and JSON output expose the exact failure codes,
    `run_next_blocked_reason`, `run_next_available`, and recovery behavior
    defined above without raw content or absolute paths.
11. Unchanged `delivery run-next --reset-failed` attempts for scope or external
    dependency stops fail preflight without resuming agents or mutating progress.
12. Amendment authoring receives sanitized failed-child evidence and produces a
    corrected proposal when the current repository can own the change.
13. Amendment authoring reports
    `delivery_amend.external_dependency_follow_up_required`, exits non-zero, and
    publishes no proposal when the current single-repository plan cannot own the
    required change.
14. Preparation and amendment dry-runs do not mutate tracked files, progress,
    child state, worktrees, branches, or Git refs.
15. Existing `max_planner_steps` enforcement, verified budget-split metadata,
    dry-run behavior, assembled-branch amendment publication, and multi-budget
    proposal fingerprint replay remain unchanged.
16. Fresh and resumed children revalidate scope against the assembled worktree
    and make no provider call when a dependency introduced an escaping symlink.
17. Malformed advertised implementer dispositions with partial writes persist
    a terminal stop and cannot be bypassed through direct or parent reset.
18. Constrained-unit amendments reassign applicable constraints to all
    replacements and pass deterministic plan validation.

Tests must use fake LLM clients, temporary repositories, isolated state, and no
network access, provider credentials, machine-specific paths, or external
repository writes.

## Security and privacy

- Preserve the existing distinction between public CLI/JSON projections and
  ignored local audit/state evidence.
- Do not place raw task bodies, parent source excerpts, provider output, diffs,
  file contents, credentials, environment values, or absolute paths in public
  failure or amendment output.
- Do not weaken repository sandbox validation, review approval, security review,
  dependency handoff validation, assembly ancestry checks, or worktree
  isolation.
- Do not automatically authorize or perform writes in an external repository.
- Scope-violation cleanup must not destroy unrelated operator changes or remove
  the preserved failed worktree needed for audit and recovery.

## Reviewer focus

Reviewers should inspect especially:

- whether source-task ownership constraints survive preparation and are
  authoritative during child execution without leaking raw source text;
- whether effective production write scope is narrower than or equal to both
  global repository permission and unit scope for every write-capable pass;
- whether changed-file auditing is provider-independent and runs before any
  validation, commit, handoff, or assembly side effect;
- whether structured review dispositions cannot be spoofed to bypass review or
  expand write authority;
- whether external dependency and amendment stops preserve auditability without
  authorizing cross-repository writes;
- whether amendment prompts receive enough sanitized evidence to correct the
  failed boundary without exposing raw state, diffs, prompts, or absolute paths;
- whether hard `max_planner_steps` behavior remains unchanged, advisory sizing
  fields remain advisory, and existing plans and progress state stay
  backward-compatible.

## Documentation

- Document the difference between repository-wide write configuration and
  effective delivery-unit write scope.
- Document structured stop and recovery behavior for scope amendments and
  external dependency gaps.
- Keep the existing distinction between hard `max_planner_steps` enforcement
  and advisory sizing fields accurate.
- Update architecture and changelog documentation. Update README only when its
  current high-level delivery workflow becomes incomplete.

## Out of scope

- Do not add automatic multi-repository implementation or cross-repository
  commits.
- Do not redesign amendment assembly, proposal fingerprinting, bounded
  `final_branch` publication, or interruption recovery.
- Do not add hard enforcement for currently advisory non-planner sizing or
  budget fields.
- Do not infer repository ownership from product-specific names.
- Do not allow agents to expand their own write scope.
- Do not silently rewrite a source task, plan, unit, or amendment proposal to
  make a run pass.
- Do not weaken deterministic amendment validation or existing public-output
  sanitization.
- Do not adopt or commit changes from the observed external reproduction.

## Suggested delivery decomposition

This task should be assessed for delivery-plan mode. A reasonable decomposition
is:

1. Source-constraint continuity and preparation-time conflict reporting.
2. Effective unit write-scope enforcement and provider-independent auditing.
3. Structured review dispositions and terminal recovery classifications.
4. Failed-child evidence for amendment authoring and external follow-up output.
5. Documentation, compatibility coverage, and end-to-end hardening.

The decomposition is advisory. Use Sikula assessment and preparation to choose
the final graph and keep each unit independently reviewable.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
