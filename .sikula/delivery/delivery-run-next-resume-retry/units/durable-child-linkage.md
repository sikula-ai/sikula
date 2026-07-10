# Persist child delivery metadata

## Goal

Persist delivery metadata on child Sikula task state when a delivery unit creates that child task, without changing ordinary `sikula run --task-id` behavior.

## Current behavior

Child task state does not carry enough delivery-specific metadata to recover the parent plan and unit relationship from the configured state directory alone.

## Desired behavior

When a delivery child task is created, its `TaskState` stores additive delivery metadata for the parent plan id, unit id, and plan path. The metadata defaults safely for existing task state files and is populated only when delivery execution creates the child task.

## Acceptance criteria

- Child `TaskState` gains additive delivery metadata for delivery plan id, delivery unit id, and delivery plan path.
- Existing task state files without those fields still load successfully.
- Ordinary non-delivery `sikula run` and `sikula run --task-id` behavior remains compatible.
- Delivery child creation can pass the metadata into the child state creation path.
- No existing `TaskState` field is removed, renamed, or type-changed.
- Tests cover metadata persistence and backwards-compatible defaults.

## Security and privacy

Do not expose raw task state, prompts, provider output, validation logs, source excerpts, secrets, or local absolute paths in ordinary CLI or JSON output. Delivery metadata may identify plan ids, unit ids, and project-relative plan paths.

## Reviewer focus

Review additive `TaskState` compatibility, child state serialization, and ordinary `sikula run --task-id` resume behavior. Confirm delivery metadata does not move provider subprocess or agent responsibilities into state code.

## Out of scope

Do not implement parent progress linkage, failed child retry, running unit selection, terminal reconciliation, final delivery branch assembly, or multi-repository delivery execution in this unit.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
