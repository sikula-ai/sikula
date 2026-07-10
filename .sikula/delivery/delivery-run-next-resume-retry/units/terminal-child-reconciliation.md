# Reconcile terminal child tasks

## Goal

Make `sikula delivery run-next PLAN` reconcile an already-terminal linked child task into parent delivery progress without starting a duplicate child run.

## Current behavior

If a delivery child task has already reached a terminal state while parent progress still shows the unit as running, the operator may need to inspect child state and manually reconcile parent progress.

## Desired behavior

When `delivery run-next` finds a running unit with a linked child task that is already terminal, it classifies the child state using the same delivery child completion rules as normal child completion and updates parent progress to `done` or `failed` accordingly.

## Acceptance criteria

- An already-terminal child task is classified without starting a duplicate child run.
- Parent progress records reconciliation intent before the terminal result event.
- Parent progress is marked done only when existing child completion rules say the child is done and finalized.
- Parent progress is marked failed when the child is terminal but not deliverable under those rules.
- Terminal reconciliation uses shared child-completion classification rather than a looser terminal-state shortcut.
- Tests cover done, failed, and unfinalized terminal child states.

## Security and privacy

Recovery output must stay allowlisted. It may identify the plan, unit, child task id, and failure code, but must not print child task state content, prompts, provider output, diffs, validation logs, secrets, or absolute local paths.

## Reviewer focus

Review child-state classification, event ordering, idempotency, and compatibility with existing result-commit/finalization rules.

## Out of scope

Do not resume non-terminal children, add `--reset-failed`, retry failed children, reset child state, alter final branch behavior, or change provider execution boundaries in this unit.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
