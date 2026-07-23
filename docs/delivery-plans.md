# Delivery Plans

Delivery plans are the planned parent layer for large work that should be split
into small Sikula delivery units. The current MVP offers artifact authoring with
deterministic writing, validates tracked plan files, reports privacy-safe parent
progress, and can run one eligible unit at a time through the normal
`sikula run` pipeline.

Use delivery plans when a request is too large for one implementation contract or
when the work spans multiple streams such as backend, web, Android, iOS, docs, or
release tasks.

```text
large request
  -> delivery plan
  -> delivery units
  -> one normal Sikula run per unit
  -> final delivery branch
```

## Authoring Assistance

Ask Sikula to author reviewable delivery plan source artifacts from one
high-level task file:

```bash
sikula delivery prepare .sikula/tasks/my-task.md
sikula delivery prepare .sikula/tasks/my-task.md --json
```

When `--output` is omitted, Sikula derives the delivery slug from the task
filename and writes:

- `.sikula/delivery/<slug>/plan.yaml`
- `.sikula/delivery/<slug>/units/<unit-slug>.md`

You can select a different tracked delivery-plan directory explicitly:

```bash
sikula delivery prepare .sikula/tasks/my-task.md --output .sikula/delivery/<slug>
```

`delivery prepare` validates the task and output paths, calls the
`delivery_preparer` assistant through plain text generation with no provider
agent/tool mode, parses one strict structured draft, then deterministically
writes `plan.yaml` and `units/<unit>.md` source artifacts. It is an authoring step only: it does not
start implementation, create `TaskState`, run delivery units, mutate
`.sikula/state/delivery/<plan-id>/`, create worktrees, update branches, launch
nested Sikula commands, or record delivery progress.

Existing plan or unit artifacts are refused by default. `--force` may replace
ordinary existing artifacts inside the selected output directory, but symlinks,
path traversal, absolute output paths, path collisions, outside-project writes,
`.git`, `.sikula/state`, `.sikula/worktrees`, or `.sikula/contract-reports`
targets remain rejected.

Generated unit task files should remain product and behavior descriptions with
acceptance criteria, reviewer focus, security/privacy notes, out-of-scope notes,
and verification expectations. They should not become file-by-file
implementation scripts.

Sikula derives writer-facing paths from the output directory and unit IDs. Path
fields from LLM output are rejected instead of trusted. Unit task contracts are
checked for readiness before source artifacts are finalized, and the generated
plan is validated after writing. If readiness, validation, or filesystem writing
fails, Sikula rolls back so half-valid source artifacts are not left behind. Raw
prompts and raw provider output are written to local
`.sikula/contract-reports/<task-stem>.delivery-prepare.auto-llm.jsonl` audit
records; ordinary text and JSON output expose only project-relative,
allowlisted metadata such as written paths, plan validation status, and unit
readiness status.

The assistant output accepted by `delivery prepare` is exactly one JSON object,
optionally wrapped in one fenced `json` block. Top-level fields are `plan_id`,
`title`, `planning_mode`, `warnings`, and `units`; unknown fields are rejected.
`planning_mode`, when present, must be `fixed_window`. `units` must be a
non-empty list of objects with `id`, `title`, `depends_on`, `task_markdown`,
and optional `stream`, `component`, `phase`, `kind`, `platform`, and
`scope_paths`. Units may also include optional sizing metadata:

- `estimated_size`: `small`, `medium`, or `large`
- `risk_tags`: supported tags are `api_surface`, `audit_artifacts`,
  `auth_permissions`, `automation_behavior`, `build_pipeline`, `cli_surface`,
  `configuration`, `data_persistence`, `docs_coverage`,
  `execution_boundary`, `external_execution_boundary`,
  `external_integration`, `migration`, `privacy`, `public_output_contract`,
  `release`, `security_boundary`, `structured_output_contract`,
  `test_hardening`, `ui_surface`, and `validation`
- `budget`: positive integer fields such as `max_planner_steps`,
  `max_elapsed_minutes`, `max_review_cycles`, `max_security_cycles`,
  `max_changed_files`, `max_changed_modules`, and `max_generated_test_files`

`max_planner_steps` defaults to `1`. Delivery authoring writes that default
explicitly. `2` is allowed only for a tightly coupled unit that cannot remain
compile-safe as separate units. Values of `3` or greater are invalid and signal
that the unit must be split. The other budget fields remain advisory until an
execution gate is defined for them.

Writer-facing path fields such as `task_path`, `path`, `unit_path`,
`output_path`, `plan_path`, `units_dir`, and `output_dir` are rejected because
paths are derived deterministically. IDs must be path-safe and unique.
Dependencies must reference known units, contain no duplicates or
self-dependencies, and be acyclic. Scope paths must be project-relative, stay
inside the project, and must not contain parent-directory (`..`) traversal.
Unit Markdown must include non-empty Goal, Current behavior,
Desired behavior, Acceptance criteria, Security/privacy, Reviewer focus, Out of
scope, and Verification sections; Verification must include explicit validation
commands. `## Asset manifest` and `sikula:generated-*` markers are rejected
before writing.

Delivery authoring should prefer smaller units with one primary production
surface each. Units should avoid combining unrelated risk surfaces. When
relevant, split surfaces such as UI/API/CLI behavior, data model or persistence
changes, structured-output parsing or schema validation, automation or
prompt-driven behavior, external provider/tool execution boundaries,
privacy/public output, audit/log artifact persistence, and docs/test-only
hardening. External provider, tool, or integration boundary changes should
usually become their own hardening unit.
Parsing or structured-output validation should usually be separate from
execution or integration behavior, and entry-point preflight/flag/route/request
or path validation should usually be separate from generation or downstream
execution behavior. Docs and coverage can be a final hardening unit unless they
are essential to validate a specific unit.

Sizing metadata is explicit guidance for authoring, `check`, and `status`.
`max_planner_steps` is also enforced before delivery child implementation; the
remaining sizing fields are advisory. No sizing metadata weakens final
review/security gates or makes unsafe units pass. Current plan validation warns
when one unit combines
several high-risk tags, for example external execution boundaries, structured
output contracts, and CLI surface, so operators can split the plan before
running that unit.

## Amend And Split A Unit

An in-progress plan can replace one eligible pending or failed unit with smaller
units without regenerating the whole plan:

```bash
sikula delivery amend prepare .sikula/delivery/<slug>/plan.yaml \
  --split-unit <unit-id>
sikula delivery amend apply .sikula/delivery/<slug>/plan.yaml \
  --proposal <proposal-id> --dry-run
sikula delivery amend apply .sikula/delivery/<slug>/plan.yaml \
  --proposal <proposal-id>
```

`amend prepare` is the only model-assisted phase. It uses the existing
`delivery_preparer` configuration and accepts the same scoped model, provider,
and timeout overrides as `delivery prepare`. The assistant returns only new
replacement units; deterministic code derives task paths, makes replacement
roots inherit the target's upstream dependencies, identifies replacement
leaves, and determines which direct downstream units need rewiring.

Prepare stores a normalized, content-addressed proposal under
`.sikula/contract-reports/delivery-amendments/<plan-id>/`. The proposal includes
the project-relative source plan path, exact replacement definitions and task
Markdown, plus fingerprints of the source plan, selected target task contract,
and sanitized parent progress. Raw prompts and provider output stay in the
separate local authoring audit. Sikula
captures those fingerprints before invoking the assistant and discards the
draft if any input changes before proposal storage. Prepare rechecks the source
fingerprints and deterministic replacement paths before and after publication;
if either changes during publication, Sikula removes the new proposal and
reports preparation as blocked. Pre-existing files or symlinks block
publication. Replacement task Markdown must also pass the normal
contract-readiness and configured validation-coverage gate, and the resulting
amended plan must pass deterministic plan validation.
Proposal publication uses a same-directory temporary file and atomic
no-overwrite publish, so the content-addressed proposal is complete or absent.
Directory fsync is best effort, matching Sikula's other state writers.
Plans under `.git`, `.sikula/state`, `.sikula/worktrees`, configured task state,
or configured contract-report directories cannot be amended. A selected target
task resolving into any of those private trees is rejected before its contents
are read or sent to the authoring model. The authoring boundary validates the
task location before and after reading and requires its bytes to match the
captured snapshot before constructing the model request. Prepare does not modify
the tracked plan, unit files, progress, events, child task state, worktrees,
branches, or Git refs.

`amend apply --dry-run` loads the exact stored proposal and performs the complete
deterministic preflight in memory. It invokes no LLM and writes no proposal,
audit, plan, unit, progress, event, child-state, worktree, branch, or Git-ref
artifact. Mutating apply repeats the same checks under the delivery mutation
lock and rejects stale plan, target task, or progress fingerprints instead of
regenerating the proposal. Preflight reruns replacement contract readiness
against the current effective project configuration. Mutating apply publishes
replacement tasks with atomic no-overwrite semantics, rechecks source
fingerprints, and replaces the plan using the same temporary-file plus
`os.replace` pattern as Sikula's other state writers. It rechecks target task,
progress, and every published replacement fingerprint before success. If plan
rollback cannot be confirmed, replacement task files are retained so the
published plan does not reference missing contracts. Failure to create or open
the delivery progress lock also returns a structured blocked result. External
checkout mutation during mutating apply is unsupported; the delivery lock
serializes cooperating Sikula operations.

Completed units are immutable. Running units must first be resumed or
reconciled. A failed unit may be superseded while its original progress,
failure metadata, and child task link remain inspectable. The tracked plan keeps
the original target entry and task file, adds `superseded_by` to that entry, and
adds `supersedes` to each replacement. Pending direct dependents are rewired from
the target to all replacement leaves. Replacement roots retain all active target
prerequisites; persisted or imported amendment metadata that weakens that
ordering is invalid. Missing references, cycles, path collisions, non-pending
downstream state, unapplied completed prerequisite commits, and any
completed-unit change fail closed.

Optional `amend_reason` and `budget_exceeded.name` metadata use stable codes,
not free-form model output, because they are projected into status and audit
events.

After apply, `delivery status` projects the retained target as `superseded` while
preserving its historical progress metadata. Superseded units are not eligible
for `run-next`, do not keep the effective plan failed or pending, and are ignored
when determining whether the active graph is ready for `finalize`. Successful
apply records `plan.amend_started`, `unit.split_recommended`,
`unit.superseded`, `unit.replacement_added`, and `plan.amended` events. Failed
mutating attempts record `plan.amend_failed` when the plan and proposal identity
can be established. Failure to append that terminal event is surfaced alongside
the original amendment failure. Interruptions roll back artifact changes, append
`plan.amend_failed`, and then propagate to the caller. Existing progress and
earlier events are never rewritten. Blocked results use a blocked message, and
outside-project input paths are redacted from human and JSON diagnostics.

Planner-step budget stops use this amendment flow for recovery. By default the
operator prepares and applies the split proposal after inspecting the stopped
child task and parent status. `delivery run-next --prepare-budget-split` is an
explicit opt-in that verifies an unambiguous parent/child budget stop and invokes
the same proposal preparation flow. It does not apply the proposal.

## After Preparing

After `delivery prepare`, check the tracked plan and preview execution before
running the first unit:

```bash
sikula delivery check .sikula/delivery/<slug>/plan.yaml
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml
```

The tracked source artifacts live under `.sikula/delivery/<slug>/`. Runtime
parent progress, created only by execution commands such as `run-next`, lives
under `.sikula/state/delivery/<plan-id>/`.

## Prepare JSON Output

`sikula delivery prepare --json` returns one allowlisted object, not raw state:

```text
{
  status: str,
  ready: bool,
  prepared: bool,
  force: bool,
  overwrite_allowed: bool,
  selected_plan_id: str | null,
  unit_ids: list[str],
  paths: {
    task_file: str | null,
    output_dir: str | null,
    plan_file: str | null,
    units_dir: str | null
  },
  unit_task_paths: object[str, str],
  written_artifacts: list[{kind: str, path: str}],
  existing_artifacts: list[{kind: str, path: str}],
  plan_validation: {
    status: str,
    valid: bool | null,
    errors: list[issue],
    warnings: list[issue]
  },
  unit_readiness: {
    status: str,
    units: list[{
      unit_id: str,
      path: str,
      readiness_score: int,
      status: str,
      ready_for_autonomous_delivery: bool,
      blocking_gap_count: int,
      warning_gap_count: int,
      blocking_gap_ids: list[str]
    }]
  },
  authoring: {
    drafted: bool,
    unit_count: int,
    planning_mode: str | null,
    audit_path: str | null
  },
  errors: list[{severity: str, code: str, message: str, path: str | null}],
  warnings: list[{severity: str, code: str, message: str, path: str | null}],
  message: str
}
```

## Current MVP Commands

Validate a plan file:

```bash
sikula delivery check .sikula/delivery/<slug>/plan.yaml
sikula delivery check .sikula/delivery/<slug>/plan.yaml --json
```

Show parent progress for a plan:

```bash
sikula delivery status .sikula/delivery/<slug>/plan.yaml
sikula delivery status .sikula/delivery/<slug>/plan.yaml --json
```

`delivery status` evaluates the execution state and marks running and failed units with their actionable next steps. In JSON output, each unit includes `run_next_available` (boolean), `run_next_action` (`"resume_or_reconcile"`, `"retry_failed"`, or omitted), and `run_next_blocked_reason` (`"missing_child_task_id"`, `"unit_budget_exceeded"`, or omitted).
For example, running units with linked child task IDs are marked as recoverable (`resume` or `reconcile` action) by `delivery run-next`, which will resume a non-terminal child or reconcile a terminal child after metadata validation. A running unit without a child task ID is a fail-safe condition marked as `block`: `run-next` blocks and does not select pending work. Failed units with linked child task IDs are marked as retryable (`retry` action) with `delivery run-next --reset-failed`. Failed units without linked child task IDs are not retryable through `run-next`.
Budget-stopped units are the exception: they are marked with
`run_next_blocked_reason: unit_budget_exceeded` and require an amend/split even
when their child task remains linked and inspectable. Both dry-run and mutating
`run-next` report `delivery.unit_budget_exceeded`; passing `--reset-failed` does
not change that recovery action.

Preview the next eligible unit without changing delivery progress:

```bash
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run --json
```

The dry run mirrors execution preflight, including dependency result-commit and
referenced handoff integrity checks, but does not acquire the progress lock,
write parent progress, create child task state, or start agents.

Run the next eligible unit:

```bash
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --json
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --prepare-budget-split
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --reset-failed
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml \
  --agent-provider implementer=antigravity \
  --agent-provider fixer=antigravity
```

`run-next` without `--dry-run` acquires a parent delivery progress lock and first
checks for a recoverable `running` unit. If exactly one unit is running with a
linked non-terminal child task, it appends a `unit.resume_intent` event first,
then resumes that child through `sikula run --task-id <child_task_id>`. The child
task state must carry matching delivery metadata for the same parent plan, unit,
and project-relative plan path before it can be resumed this way. It does not
create a new child task for that unit. Resume and retry paths require the child
task to have an isolated worktree path recorded; `run-next` blocks instead of
forwarding `sikula run --task-id` for child states created before worktree setup.
If the running unit has no linked child task id, the child state is missing, the
child metadata does not match this run, or the child is terminal (`done` or
`failed`) with mismatched metadata, `run-next` blocks with a targeted deterministic
error and does not select a new pending unit.
Without `--reset-failed`, failed units block instead of silently rerunning children.
With `--reset-failed`, a running unit whose linked child task is already failed
is retried through the same child task id immediately after metadata validation.
If no ambiguous running unit exists, `run-next` otherwise selects the first failed
unit with a linked child task id before pending work. It preserves the same parent
unit and child task id, appends a `unit.retry_intent` event, and forwards reset
semantics to the child task path (`sikula run --task-id <child_task_id> --reset-failed`).
`--reset-failed` does not bypass running-unit ambiguity, dependency result-commit checks, or a planner-step budget stop and does not select later pending work while retry selection is active. Child runs preserve normal `sikula run` semantics, and delivery execution still runs one unit at a time.
`--prepare-budget-split` is mutually exclusive with `--dry-run`. It supports a
budget stop produced by the current child run and an already persisted,
unambiguous budget-stopped unit. Sikula first finishes the normal child and
parent failure recording and releases the `delivery.run-next` progress lock.
If another delivery operation owns that lock, split preparation does not invoke
`delivery_preparer` or write proposal/audit artifacts.
It then verifies that parent progress and the linked failed child identify the
same plan and unit. The child stop must come from the planner phase, and its
`max_planner_steps` limit must match parent progress, the child budget snapshot,
and the current unit budget before Sikula invokes `delivery_preparer`. Missing,
ambiguous, stale, or mismatched evidence blocks before authoring. Runtime agent
overrides continue to reach the child, while a `delivery_preparer` override is
used only for split preparation.

Successful opt-in preparation writes the normal local content-addressed
proposal and authoring audit, but does not change the tracked plan, unit task
files, progress, events, child state, worktrees, branches, or Git refs beyond
the budget failure already recorded by `run-next`. Text and JSON output expose
an allowlisted `budget_split_preparation` result with the proposal id,
replacement ids, project-relative artifact paths, and verified budget values.
The delivery unit remains failed and `run-next` exits non-zero because no
implementation completed. The operator must still inspect and run `delivery
amend apply`, then start replacement units separately. Automatic apply,
replacement execution, and delivery-branch assembly are not performed.
The JSON/text output remains privacy-safe and allowlisted.
If the running unit has a terminal child with matching metadata and is not
retrying a failed child through `--reset-failed`, `run-next`
reconciles through shared completion logic instead of starting a new child run:
it records `unit.reconcile_intent` first, then records `unit.done` or `unit.failed`
through the same completion pipeline used after normal child execution.
If multiple units are `running`, `run-next` also blocks so the operator can
manually reconcile parent progress.
When resume-path blocks occur, `run-next --json` returns allowlisted metadata
only (error code, task IDs, and paths) and does not expose raw child task state.
Terminal reconciliation does not duplicate child execution and still reports
allowlisted JSON/text output.
When no running unit blocks, `run-next` marks the selected pending unit as
`running`, creates one child `sikula run` state, then updates parent progress with
that child task id and a `unit.child_linked` event before agent execution. If that
update fails, `run-next` stops before agents start and reports
`delivery.child_link_failed`. It restores parent progress to the pre-start state
and records an audit event for the failed child-link attempt.
When `delivery.child_link_failed` occurs, `run-next` returns the child task id and
deterministic failure code while omitting absolute filesystem paths from JSON/text
output.
After child execution starts, it records the terminal unit status as `done` or
`failed`.
It accepts the same per-agent `--agent-model`, `--agent-provider`, and
`--agent-timeout` overrides as `sikula run` and passes them to the child run.
When starting the child run, Sikula automatically configures and persists the parent delivery metadata in the child's `TaskState` (specifically the parent `delivery_plan_id`, `delivery_unit_id`, a project-relative `delivery_plan_path`, the effective `delivery_unit_budget`, the current `delivery_handoff_schema_version`, and validated `delivery_dependency_handoffs`). Delivery children always run the planner, even when ordinary task planning is disabled. If the planner returns more steps than the unit's effective `max_planner_steps`, Sikula preserves the planner prompt and output, records `delivery_budget_stop`, and fails before the implementer starts. Parent progress and the terminal `unit.failed` event receive `failure_code: unit_budget_exceeded` and allowlisted `budget_exceeded` metadata. This allows the parent plan relationship and stop reason to be recovered from the configured state directory without copying prompts or raw child state into parent progress.
Child task prompts, provider output, diffs, logs, and full task state remain in
the normal child task state and are not embedded in delivery progress JSON.
`delivery run-next --json` reports deterministic failure codes and the child task id
when link creation succeeds but parent progress mutation fails.
The parent unit is marked `done` only when the child run exits successfully, the
child task state is done, and the child result is finalized. A finalized result
has either a recorded result commit or no preserved task worktree left to
deliver, which represents a no-op unit. If a child task is done but still keeps a
worktree without a result commit, the parent unit is recorded as failed with
`child_run_unfinalized`. The same rule applies to terminal-reconciled children.

New delivery children also produce a versioned handoff at
`.sikula/state/delivery/<plan-id>/handoffs/<unit-key>.json` before their parent
unit becomes `done`. The bounded filename key combines a readable unit ID prefix
with a SHA-256 digest of the complete ID, so legacy IDs and IDs that differ only
by case cannot collide on common filesystems. The parent progress entry records
the handoff schema and content fingerprint. The artifact contains only
correlated unit, branch/commit, changed-file path, validation status/count, and
test file/gap-count metadata. Unit title and component labels that exceed the
metadata bound use a bounded prefix with a SHA-256 suffix instead of failing
handoff creation. It does not contain task bodies, prompts, model output, diffs,
logs, validation output, source excerpts, or raw child state.

Before a dependent child is created, `run-next` validates referenced handoffs
across its completed dependency closure and snapshots them into the new child
state. The Analyst receives this evidence as supporting context, while the
current unit task remains the scope authority. A missing, malformed, stale, or
mismatched referenced handoff, including a symlink or path outside the project
root, blocks before child creation. Correlation includes the current plan's unit
title, component, dependencies, and scope paths so edited plan metadata cannot
silently reuse stale evidence. Progress and child states created by older Sikula
versions have no handoff schema marker; they remain compatible and continue
without fabricated handoff context. If writing a new handoff fails after the
child completed, the parent unit is durably persisted as `running`; rerunning
ordinary `delivery run-next` retries terminal reconciliation without rerunning
the child agents, including after a `--reset-failed` attempt.

Before starting a new child, `run-next` assembles completed unit result commits
into the plan's `final_branch` in dependency-safe order. Fast-forward,
already-applied, merge, and no-op outcomes are recorded as compact events.
Independent histories are combined with a two-parent merge commit so every
original unit result SHA remains an ancestor; Sikula does not cherry-pick or
rewrite unit results. The selected child worktree starts from the assembled
commit, so dependent units no longer require a manual merge into the operator
checkout. Before child state or a worktree is created, the assembled commit's
`.sikula/config.yaml` must match the committed config loaded from the operator
checkout; a unit that changes runtime config therefore requires a fresh run
from that updated config. Completed no-op prerequisites have no result commit,
but their own dependency closure is still checked.

Assembly does not check out `final_branch` and does not change the operator
working tree or index. `final_branch` must be a direct ref; symbolic refs are
rejected, and ref updates are non-dereferencing compare-and-swap operations.
Missing commits or recorded refs, checked-out branches, diverged refs, stale
branches ahead of the base without recorded assembly progress, and merge
conflicts fail closed with stable `delivery.assembly_*` codes. A merge between
independent unit commits requires Git 2.38+ and fails in preflight with
`delivery.assembly_git_unsupported` on older installations; pure fast-forward,
already-applied, and no-op assembly does not require this merge capability.
Progress retains the assembly base, current assembled commit, failure code, and
blocked unit. Resolve a reported conflict on `final_branch`, switch that
worktree away, and rerun `delivery run-next`; ancestry checks make the retry
idempotent.

Unlike `check` and `status`, `run-next` loads project runtime config because it
uses the same project settings as `sikula run`.

Preview final delivery branch creation after every unit is done:

```bash
sikula delivery finalize .sikula/delivery/<slug>/plan.yaml --dry-run
sikula delivery finalize .sikula/delivery/<slug>/plan.yaml --dry-run --json
```

Create or fast-forward the plan's final branch:

```bash
sikula delivery finalize .sikula/delivery/<slug>/plan.yaml
sikula delivery finalize .sikula/delivery/<slug>/plan.yaml --json
```

`finalize` requires the delivery plan status to be `done`. It verifies and, for
legacy or interrupted progress, reconciles all completed unit results through
the same dependency-ordered assembly engine. The assembled branch commit,
rather than the operator's current `HEAD`, becomes the final commit. Existing
diverged or checked-out branches are rejected. A branch ahead of the assembly
base is trusted only when progress records an expected assembled commit;
otherwise it is treated as stale and rejected. Sikula never force-updates these
branches. No-op plans retain the recorded assembly base. Like `run-next`,
`finalize` loads project runtime config because it mutates Git refs and parent
delivery progress. `--dry-run` validates static ref and commit preconditions
without writing refs, Git objects, or progress; merge conflicts are
conclusively reported by the mutating command. When pending assembly must
create a new commit, the dry-run remains ready but reports `final_commit: null`
because that commit ID is not available without creating the Git object.
Any later unit progress update clears the recorded final branch metadata, so an
extended or rerun delivery plan must be finalized again after it returns to
`done`.

The MVP validator checks:

- `schema_version: 1`,
- required plan metadata such as `plan_id`, `title`, and a valid local-branch
  `final_branch`,
- delivery unit IDs,
- unit task paths,
- unit dependency references and cycles,
- consistent `supersedes` and `superseded_by` amendment links, preserved target
  prerequisites, and active dependencies that do not point to superseded units,
- optional stream references,
- optional monorepo component references and project-relative scope paths,
- single-repository scope.

`delivery status` first runs the same plan validation, then reads ignored parent
progress from `.sikula/state/delivery/<plan-id>/progress.json` when present. If
that progress file does not exist yet, all delivery units are reported from the
plan as `pending`, with dependency blockers derived from `depends_on`. After
`delivery finalize` succeeds, status includes the final branch, final commit,
and finalization timestamp.

## Plan Shape

Example:

```yaml
schema_version: 1
plan_id: checkout-redesign
title: Checkout redesign
planning_mode: fixed_window
final_branch: sikula/delivery/checkout-redesign
streams:
  - id: backend
    label: Backend
components:
  - id: api
    label: API package
    path: packages/api
    stream: backend
units:
  - id: 01-domain-model
    title: Add checkout domain model
    stream: backend
    component: api
    scope_paths:
      - packages/api/src
      - packages/api/tests
    platform: shared
    task_path: .sikula/delivery/checkout-redesign/units/01-domain-model.md
    depends_on: []
  - id: 02-api
    title: Add checkout API endpoints
    stream: backend
    platform: shared
    task_path: .sikula/delivery/checkout-redesign/units/02-api.md
    depends_on:
      - 01-domain-model
```

Unit task files are ordinary Markdown task descriptions for future Sikula runs.
The parent plan stores structure and ordering; it should not duplicate raw task
content. Current `delivery prepare` output emits the selected plan metadata,
final branch, implicit single-repository entry, streams derived from unit
streams, and unit entries with deterministic task paths, dependencies, and
rendered unit metadata (`stream`, `platform`, `phase`, `kind`, and
`scope_paths`). It does not synthesize top-level `components` metadata.

## Monorepo Components

Plans may define `components` to describe project-local parts of a monorepo, then
tag individual units with `component` and optional `scope_paths`:

```yaml
components:
  - id: android
    label: Android app
    path: apps/android
    stream: mobile
units:
  - id: 03-android-login
    component: android
    scope_paths:
      - apps/android/app/src/main
    task_path: .sikula/delivery/product/units/03-android-login.md
    depends_on: []
```

The MVP validates that component paths and unit scope paths are portable
project-relative paths inside the current Git repository, and that unit
`component` references point to declared components. These fields are metadata
for planning, status, JSON consumers, and future delivery-console grouping. They
do not change `sikula run` behavior, restrict provider filesystem access, filter
validation commands, infer dependencies, or create separate worktrees.

## Repository Scope

The MVP supports one Git repository. If `repositories` is omitted, Sikula treats
the plan as a single implicit repository:

```yaml
repositories:
  - id: main
    root: .
```

Multi-repo plans are reserved for a later delivery-plan phase. Until then,
`delivery check` rejects multiple repositories instead of pretending that Sikula
can coordinate cross-repo branches, locks, validation, and result sets.

## Privacy

`delivery prepare --json`, `delivery check --json`, `delivery status --json`,
`delivery run-next --json`, and `delivery finalize --json` return allowlisted
metadata such as written artifact paths, plan validation status, unit readiness,
plan metadata, validation issues, unit paths, compact progress fields, selected
child task IDs, handoff schema/fingerprint references, assembly/final branch metadata,
and branch/commit pointers when
available. They do not embed child task state, source task bodies, unit task file bodies, prompts,
provider output, diffs, logs, validation output, credentials, tokens, or source excerpts.
Privacy-safe projections such as `delivery prepare --json`, `delivery status --json`,
and `delivery run-next --json` use project-relative paths for local delivery artifacts
where possible. Operator/audit commands such as `delivery check --json` and
`delivery finalize --json` may include local plan, progress, or events paths. The
parent plan path stored in the child task state is saved as a project-relative path
(`delivery_plan_path`). This metadata is strictly allowlisted state metadata and
does not expose raw prompts, provider output, diffs, logs, or source excerpts.
