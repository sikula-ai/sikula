# Implement delivery run-next resume and retry for interrupted/running units

## Context

Current delivery MVP can start one unit via `sikula delivery run-next`, records
parent progress, and runs a child `sikula run`. If the process is interrupted
after parent progress marks a unit as `running`, recovery is manual: the
operator must inspect `delivery status`, find the child task id, resume
`sikula run --task-id`, then reconcile parent progress. Failed child retries
also require dropping down to manual `sikula run --task-id ... --reset-failed`.

This task should make the parent delivery command the normal operational entry
point for those recovery paths while preserving the existing child
`sikula run --task-id` semantics.

## Goal

Make `sikula delivery run-next PLAN` resume, reconcile, or retry the selected
delivery unit when a previous run has already created or linked a child task.

## User-facing behavior

- If delivery status has a `running` unit with `child_task_id`, `delivery
  run-next` does not select a new pending unit.
- It loads the child task state from the configured state directory.
- If the child task is not terminal, it invokes the normal child
  `sikula run --task-id <child-task-id>` path while preserving delivery parent
  context.
- If the child task is already terminal, it classifies the child state and
  updates parent unit progress to `done` or `failed` using the same completion
  rules as normal child completion.
- Parent progress records auditable events such as `unit.resumed` before
  rerunning or reconciling, followed by `unit.done` or `unit.failed`.
- The command never creates a duplicate child task id for the same running
  delivery unit.
- The command never marks a unit done unless the child task is done and
  finalized by the existing delivery child completion rules.
- If a running unit has no `child_task_id`, the command fails safely with an
  actionable error instead of guessing or selecting a different unit.

## Reset and retry behavior

- Add `sikula delivery run-next PLAN --reset-failed`.
- `--reset-failed` supports a failed running or resumable child task by
  forwarding reset semantics to
  `sikula run --task-id <child-task-id> --reset-failed`.
- Without `--reset-failed`, if the child task state is failed, delivery does
  not silently rerun it; the parent unit becomes or remains `failed` with an
  actionable message.
- With `--reset-failed`, delivery keeps the same parent unit and child task id
  where safe, resumes via normal child reset/run behavior, then classifies and
  records the final parent unit status.
- The flag applies only to the selected resumable failed child task. It must
  not reset completed prerequisite units.
- If parent unit status is already `failed` with a child task id,
  `delivery run-next --reset-failed` retries that same unit instead of selecting
  a later pending unit or requiring manual child task commands.
- Parent progress/events record retry intent, such as `unit.retrying`, followed
  by normal `unit.done` or `unit.failed`.

## Durable parent-child linkage

- As soon as the child task state is created, parent delivery progress records
  `child_task_id` for the running unit before long-running agent work begins.
- Child task state records delivery metadata, at minimum `delivery_plan_id`,
  `delivery_unit_id`, and `delivery_plan_path`, so the relationship is
  recoverable from either side.
- If child task creation succeeds but parent progress cannot be updated, the
  command fails before running agents rather than leaving an unlinked delivery
  child.
- `delivery status` reports resumable running units clearly using durable parent
  progress and child metadata.
- Resume/retry logic relies on durable parent progress plus child state
  metadata, not on in-process memory from the original `delivery run-next`.

## Acceptance criteria

- `delivery run-next` resumes or reconciles an existing `running` unit with
  `child_task_id` instead of selecting a new pending unit.
- `delivery run-next --reset-failed` retries a failed resumable unit with the
  same child task id by forwarding existing child reset semantics.
- A failed child is not silently rerun without `--reset-failed`.
- Running parent progress without `child_task_id` fails safely and does not
  guess a child task.
- Parent progress records durable resume/retry and final terminal events.
- Child task state persists enough delivery metadata to recover the
  parent-child relationship from either side.
- Existing normal pending-unit `delivery run-next`, child `sikula run
  --task-id`, and child `--reset-failed` behavior remain compatible.

## Scope

- `sikula_cli/delivery.py`
- `core/delivery_progress.py` if status/progress helpers need adjustment
- `core/delivery_run_next.py` if render/status behavior needs adjustment
- `core/state.py` for additive child delivery metadata if needed
- `sikula.py` or run CLI plumbing only if needed to attach delivery metadata or
  invoke a post-child-state-creation hook before agents run
- focused tests covering interrupted/running unit resume, already-terminal
  reconciliation, failed child retry, and no-child-id fail-safe paths

## Implementation constraints

- Do not make this one oversized unit. Split durable parent-child linkage,
  resume/reconcile behavior, failed retry behavior, and status/docs/test
  hardening into separate delivery units if possible.
- The durable child linkage requirement likely needs an explicit hook or
  protocol in the run path after child state creation and before long-running
  agent work begins, because `delivery run-next` cannot safely learn the child
  task id only after `sikula run` returns.
- Reconciliation should use shared child-completion classification rules, but
  it may need a state-only classifier instead of requiring a fresh child run
  result.
- `--reset-failed` must intentionally target only the selected resumable failed
  delivery unit with an existing child task id; it must not reset completed
  prerequisite units or select a later pending unit.
- Current normal `sikula run --task-id` and `--reset-failed` semantics must
  remain unchanged except through existing flags.
- Additive `TaskState` fields are acceptable. Do not remove, rename, or change
  existing `TaskState` field types without a schema migration.
- Do not move provider subprocess behavior into delivery code or agent prompts.
- Do not make reviewer or security reviewer agents write-capable.

## Security and privacy

- Do not expose raw prompts, provider outputs, source excerpts, task state
  blobs, tokens, API keys, local credentials, or private filesystem details in
  ordinary CLI or JSON output.
- Keep audit evidence local in existing Sikula audit/state artifacts.
- Recovery messages should identify task ids, unit ids, plan paths, and
  deterministic failure codes, but not dump child task state content.
- Failed or interrupted recovery must preserve inspectability and auditability;
  do not discard child state, parent progress events, validation records, retry
  records, prompts, or provider outputs.
- Do not weaken existing security-review fail-safe behavior.

## Tests

Add or update focused tests for:

- A `running` delivery unit with `child_task_id` resumes the same child task
  and does not select a new pending unit.
- A `running` delivery unit whose child task is already terminal is reconciled
  to parent `done` or `failed` according to the shared child-completion rules.
- A running unit without `child_task_id` fails safely with an actionable error.
- A failed parent unit with `child_task_id` is retried by
  `delivery run-next --reset-failed` using the same child task id.
- A failed child is not silently rerun without `--reset-failed`.
- `--reset-failed` applies only to the selected resumable failed child task and
  does not reset completed prerequisite units.
- Durable child delivery metadata is persisted and can be loaded from the
  configured state directory.
- Parent progress events record resume/retry intent and final done/failed
  status.
- Existing normal pending-unit `delivery run-next` behavior remains covered.

## Reviewer focus

Reviewers should inspect:

- parent delivery progress state transitions and event ordering
- the child task creation hook or equivalent protocol that records
  `child_task_id` before agents run
- compatibility with existing `sikula run --task-id` and `--reset-failed`
  semantics
- `TaskState` additive metadata and resume compatibility
- failed/interrupted edge cases that could create duplicate child tasks, skip a
  failed unit, or incorrectly mark a unit done
- privacy boundaries for CLI/JSON output versus local audit/state artifacts
- tests for both normal completion and fail-safe paths

## Non-goals

- Do not implement accumulated delivery branch assembly.
- Do not change final branch selection/finalize behavior.
- Do not add multi-repo delivery execution.
- Do not change normal `sikula run --task-id` semantics except through existing
  flags.

## Validation

Validate through Sikula's configured Python pipeline:

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
