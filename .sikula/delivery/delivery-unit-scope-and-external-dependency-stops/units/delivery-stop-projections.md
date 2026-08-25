# Project stable delivery stop recovery

## Goal

Expose truthful, typed delivery status and retry behavior for scope violations, scope-amendment requirements, external dependency gaps, and invalid advertised implementer dispositions.

## Current behavior

Failed units are generally presented as retryable, so an unchanged `--reset-failed` invocation can be suggested even when the contract boundary must change first.

## Desired behavior

Parent progress classifies child boundary stops with stable failure codes. Text and JSON status derive from one typed result and recommend amendment or external follow-up instead of an unchanged retry. `delivery run-next` and reset preflight return stable public errors without resuming agents or mutating progress.

## Acceptance criteria

- Parent progress preserves `unit_scope_violation`, `scope_amendment_required`, and `external_dependency_gap` exactly.
- Parent progress preserves `implementer_disposition_invalid`, blocks ordinary retry, and projects `delivery.implementer_disposition_invalid` without raw provider output.
- For each code, JSON exposes the same value in `failure_code` and `run_next_blocked_reason`, sets `run_next_available` to false, and omits the ordinary `retry_failed` action.
- Plan-level next action recommends `delivery amend prepare` for scope-related stops and an external dependency follow-up for dependency gaps.
- `delivery run-next` returns non-zero with `delivery.unit_scope_violation`, `delivery.scope_amendment_required`, or `delivery.external_dependency_gap` as appropriate.
- An unchanged `delivery run-next --reset-failed` fails preflight without child resume, agent execution, or progress mutation.
- Direct `sikula run --task-id` summaries and verbose/JSON task status recommend the terminal stop's amendment, external-follow-up, or replacement action and never advertise `--reset-failed` for that child.
- Existing public fields keep their meaning and continue to use allowlisted projections.
- Pending, running, ordinary failed, planner-budget, retry, resume, finalize, and successful unit behavior remain compatible.

## Security and privacy

Public text and JSON must not serialize child state, prompts, provider output, logs, diffs, file contents, source excerpts, credentials, or absolute paths. Unsafe metadata remains subject to existing redaction and opaque-identity rules.

## Reviewer focus

Check exact error-code mapping, text and JSON consistency, no-mutation reset preflight, non-zero command behavior, and the absence of handoff or assembly advancement for stopped units.

## Out of scope

Do not add automatic retries, automatic amendment application, or cross-repository execution.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
