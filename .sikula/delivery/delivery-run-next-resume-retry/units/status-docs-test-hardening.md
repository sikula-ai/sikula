# Harden status, docs, and tests

## Goal

Make the delivery resume and retry behavior clear in status output, documented user guidance, and focused regression tests.

## Current behavior

Delivery status and documentation describe the MVP delivery flow, but interrupted running-unit recovery and failed-child retry are not first-class operational paths. Existing tests do not fully cover the new resume, reconciliation, and retry edge cases.

## Desired behavior

`delivery status` reports resumable running units clearly from durable parent progress and child metadata. Public docs explain how `delivery run-next` resumes, reconciles, and retries linked child tasks while preserving normal child run semantics. Tests cover the operational paths and privacy-safe output boundaries.

## Acceptance criteria

- Status output clearly identifies running or failed units that can be resumed or retried through `delivery run-next`.
- JSON status and run-next output remain explicit allowlisted projections and do not embed child task state or raw audit payloads.
- Documentation describes running-unit resume, terminal reconciliation, failed-child retry with `--reset-failed`, and the no-child-id fail-safe.
- Tests cover interrupted running-unit resume, terminal child reconciliation, no-child-id failure, failed child without reset, failed child retry with reset, prerequisite non-reset behavior, durable metadata loading, event recording, and unchanged pending-unit execution.
- Configured validation commands remain the acceptance verification path.

## Security and privacy

Docs and outputs must preserve Sikula privacy boundaries. Do not expose raw prompts, provider outputs, source excerpts, task state blobs, diffs, validation logs, credentials, tokens, or absolute local paths in ordinary CLI or JSON output.

## Reviewer focus

Review public output contracts, docs accuracy, and test coverage across success, failure, retry, and fail-safe paths. Confirm documentation does not imply new final branch assembly, multi-repo execution, or changed standalone child run behavior.

## Out of scope

Do not add new delivery execution features beyond observability, documentation, and tests for the resume and retry behavior. Do not relax validation, review, security-review, or privacy gates.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
