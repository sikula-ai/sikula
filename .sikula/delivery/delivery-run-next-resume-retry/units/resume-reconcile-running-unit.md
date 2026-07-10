# Resume linked running child tasks

## Goal

Make `sikula delivery run-next PLAN` resume an existing running delivery unit with a linked, non-terminal child task instead of selecting a new pending unit.

## Current behavior

After interruption, a running parent unit with a linked child task can require the operator to manually run `sikula run --task-id <child-task-id>`.

## Desired behavior

Before pending-unit selection, `delivery run-next` detects a running unit with `child_task_id`, loads the child task from the configured state directory, and resumes a non-terminal child through the normal `sikula run --task-id <child-task-id>` path. It does not create a duplicate child task for the same running unit.

## Acceptance criteria

- A running unit with `child_task_id` prevents selection of a new pending unit.
- A non-terminal child task is resumed with the standard child task resume path.
- Parent progress records resume intent before invoking the child resume path.
- A running unit with no `child_task_id` fails safely with an actionable error and does not guess or select another unit.
- The command does not create a duplicate child task id for the same running unit.
- Existing normal pending-unit `delivery run-next` behavior remains covered.

## Security and privacy

Recovery output must stay allowlisted. It may identify the plan, unit, child task id, and failure code, but must not print child task state content, prompts, provider output, diffs, validation logs, secrets, or absolute local paths.

## Reviewer focus

Review selection ordering, event ordering, idempotency, and the no-child-id fail-safe. Confirm the implementation cannot skip a running unit or create a second child task for it.

## Out of scope

Do not reconcile already-terminal child tasks, add `--reset-failed`, retry failed children, reset child state, change prerequisite result-commit checks, alter final branch behavior, or change provider execution boundaries.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
