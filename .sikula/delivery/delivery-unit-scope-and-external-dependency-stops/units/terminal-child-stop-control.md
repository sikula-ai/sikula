# Stop child pipelines on boundary findings

## Goal

Make scope and external-dependency dispositions terminal child outcomes while preserving normal in-scope review fixes.

## Current behavior

A reviewer request for a scope amendment can enter the ordinary implementer fix loop, and dependency gaps can be misclassified as generic implementation or no-change failures.

## Desired behavior

Orchestration uses dedicated validated state fields to stop immediately for `unit_scope_violation`, `scope_amendment_required`, `external_dependency_gap`, or an advertised malformed implementer disposition. Only `fix_in_scope` findings enter the existing bounded fix loop. Terminal stops preserve the worktree and audit evidence while preventing every later pipeline or delivery-finalization side effect.

## Acceptance criteria

- A scope audit failure sets child failure code `unit_scope_violation` and prevents later phases.
- A review or security-review `requires_scope_amendment` disposition sets `scope_amendment_required` on that cycle without another implementer call.
- Analyst, implementer, reviewer, or security-review dependency dispositions set `external_dependency_gap` and stop immediately.
- A valid `fix_in_scope` issue continues through the existing bounded fix loop.
- Terminal stop control uses dedicated state fields rather than append-only observability records.
- An implementer disposition parse failure maps to `implementer_disposition_invalid`, preserves bounded parser evidence without raw output, and remains authoritative across resume.
- Stopped children cannot become done, create a handoff, commit a result, or advance assembly.
- Existing review and security approval gates remain fail-closed.
- Existing planner-budget stops remain distinct and retain their current verified metadata and recovery behavior.

## Security and privacy

Persist sufficient local evidence for audit without projecting raw state, prompts, provider output, diffs, file contents, credentials, or absolute paths. Preserve isolated failed worktrees without weakening sandbox checks.

## Reviewer focus

Verify ordering of stop checks, absence of extra provider calls after a terminal disposition, correct distinction from technical failures and planner-budget stops, and unchanged behavior for valid in-scope fixes.

## Out of scope

Do not redesign review iteration limits, planner-budget enforcement, handoff validation, assembly ancestry, or task-state decision ownership.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
