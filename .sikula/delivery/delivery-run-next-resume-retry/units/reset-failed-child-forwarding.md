# Forward failed child reset runs

## Goal

Forward selected failed delivery child retries through the existing child `sikula run --task-id <child-task-id> --reset-failed` path and record the final parent result.

## Current behavior

Even after a failed delivery unit is selected for retry, the operator still needs manual child reset/run handling and parent progress reconciliation.

## Desired behavior

When `delivery run-next --reset-failed` selects a failed unit with a linked child task, the command records retry intent, invokes the existing child reset-and-run path with the same child task id, then classifies and records the final parent unit status.

## Acceptance criteria

- The retry keeps the same parent unit and child task id where safe.
- The command records retry intent before invoking child reset-and-run behavior.
- Reset semantics are forwarded to the existing child run path instead of reimplementing child reset logic in delivery code.
- Final parent progress is classified through the same done and failed rules used for normal child completion.
- Retry failure preserves parent progress, child task state, and audit evidence for inspection.
- Tests cover successful retry forwarding, retry failure, and parent terminal event recording.

## Security and privacy

Retry output should be concise and deterministic. It must not include raw child state, prompts, provider output, logs, validation output, source snippets, secrets, credentials, or absolute local filesystem details.

## Reviewer focus

Review reset forwarding, event ordering, same-child-id preservation, and reuse of existing child reset semantics. Confirm delivery code does not reimplement or weaken normal `sikula run --task-id --reset-failed` behavior.

## Out of scope

Do not change normal standalone `sikula run --task-id --reset-failed` semantics. Do not add automatic retry loops, retry pending units without child ids, alter delivery finalize behavior, or reset completed prerequisite units.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
