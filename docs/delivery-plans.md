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
- `budget`: positive integer advisory fields such as `max_planner_steps`,
  `max_elapsed_minutes`, `max_review_cycles`, `max_security_cycles`,
  `max_changed_files`, `max_changed_modules`, and `max_generated_test_files`

Writer-facing path fields such as `task_path`, `path`, `unit_path`,
`output_path`, `plan_path`, `units_dir`, and `output_dir` are rejected because
paths are derived deterministically. IDs must be path-safe and unique.
Dependencies must reference known units, contain no duplicates or
self-dependencies, and be acyclic. Scope paths must be project-relative and stay
inside the project. Unit Markdown must include non-empty Goal, Current behavior,
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

Sizing metadata is explicit guidance for authoring, `check`, `status`, and
future execution policy. It does not silently weaken final review/security gates
or make unsafe units pass. Current plan validation warns when one unit combines
several high-risk tags, for example external execution boundaries, structured
output contracts, and CLI surface, so operators can split the plan before
running that unit.

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

Preview the next eligible unit without changing delivery progress:

```bash
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run --json
```

The dry run mirrors execution preflight, including dependency result-commit
checks, but does not acquire the progress lock, write parent progress, create
child task state, or start agents.

Run the next eligible unit:

```bash
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --json
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
create a new child task for that unit.
If the running unit has no linked child task id, the child state is missing, the
child metadata does not match this run, or the child is terminal (`done` or
`failed`) with mismatched metadata, `run-next` blocks with a targeted deterministic
error and does not select a new pending unit.
If the running unit has a terminal child with matching metadata, `run-next`
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
`delivery.child_link_failed`.
When `delivery.child_link_failed` occurs, `run-next` returns the child task id and
deterministic failure code while omitting absolute filesystem paths from JSON/text
output.
It then records the terminal unit status as `done` or `failed`.
It accepts the same per-agent `--agent-model`, `--agent-provider`, and
`--agent-timeout` overrides as `sikula run` and passes them to the child run.
When starting the child run, Sikula automatically configures and persists the parent delivery metadata in the child's `TaskState` (specifically the parent `delivery_plan_id`, `delivery_unit_id`, and a project-relative `delivery_plan_path`). This allows the parent plan relationship to be fully recovered from the configured state directory, while keeping ordinary delivery progress records compact.
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

For dependent units, `run-next` also walks the selected unit's dependency
closure and checks that each completed prerequisite's recorded result commit,
when present, is already applied to the current checkout. A completed
prerequisite with no result commit is treated as a no-op prerequisite, but its
own prerequisites are still checked. The current execution MVP does not assemble
an accumulated delivery branch, so dependent units are blocked until the
operator has merged or otherwise applied prerequisite unit branches locally.

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

`finalize` requires the delivery plan status to be `done`. It verifies that each
completed unit result commit, when present, is applied to the current checkout.
The current `HEAD` is the final commit candidate, so YAML unit ordering does not
affect final branch selection. It then creates `final_branch` or fast-forwards
it. Existing diverged branches are rejected; Sikula does not force-update a
final branch. No-op plans with no unit result commits also finalize to the
current `HEAD`. Like `run-next`, `finalize` loads project runtime config because
it mutates Git refs and parent delivery progress.
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
child task IDs, final branch metadata, and branch/commit pointers when
available. They do not embed source task bodies, unit task file bodies, prompts,
provider output, diffs, logs, or task state. They do not expose absolute local
paths. The parent plan path stored in the child task state is saved as a
project-relative path (`delivery_plan_path`). This metadata is strictly
allowlisted state metadata and does not expose raw prompts, provider output,
diffs, logs, or source excerpts.
