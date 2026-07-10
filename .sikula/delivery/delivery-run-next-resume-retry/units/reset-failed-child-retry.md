# Select failed child retries

## Goal

Add the `sikula delivery run-next PLAN --reset-failed` command surface and selection behavior for failed delivery units with linked child tasks.

## Current behavior

A failed child task associated with a delivery unit requires manual child-task commands, and `delivery run-next` does not have a first-class way to select that failed parent unit for retry.

## Desired behavior

When `--reset-failed` is present and the parent plan has a failed unit with a linked child task, `delivery run-next` selects that same failed unit instead of a later pending unit. Without the flag, a failed child is not silently rerun and the parent unit remains or becomes failed with an actionable message.

## Acceptance criteria

- `delivery run-next --reset-failed` targets only the selected resumable failed unit with an existing child task id.
- Without `--reset-failed`, a failed child is not rerun silently.
- A failed parent unit with a child task id is retried instead of selecting a later pending unit.
- The flag does not reset completed prerequisite units or unrelated child tasks.
- Selection returns the same parent unit and child task id for the later reset forwarding step.
- Tests cover flag parsing, failed-unit selection, no-silent-rerun behavior, and prerequisite non-reset behavior.

## Security and privacy

Selection output should be concise and deterministic. It must not include raw child state, prompts, provider output, logs, validation output, source snippets, secrets, credentials, or absolute local filesystem details.

## Reviewer focus

Review flag parsing, retry target selection, no-silent-rerun behavior, and completed prerequisite isolation. Confirm this unit does not introduce reset forwarding behavior yet.

## Out of scope

Do not forward reset semantics to the child run path in this unit. Do not change normal standalone `sikula run --task-id --reset-failed` semantics, add automatic retry loops, retry pending units without child ids, alter delivery finalize behavior, or reset completed prerequisite units.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
