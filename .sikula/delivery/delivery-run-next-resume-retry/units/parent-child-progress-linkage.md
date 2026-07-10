# Link parent progress to child creation

## Goal

Ensure parent delivery progress records the linked child task id before any long-running child agent work can begin.

## Current behavior

The child task relationship can be known to the active `delivery run-next` process before it is durable in parent delivery progress. If the process stops at the wrong moment, later recovery can require manual reconciliation.

## Desired behavior

After delivery child state is created and before child agents run, parent delivery progress stores `child_task_id` for the selected running unit. If the parent progress update fails, the delivery command stops before agent execution instead of continuing with an unlinked child task.

## Acceptance criteria

- Parent delivery progress records `child_task_id` for the selected running unit before child agents run.
- The parent progress event history remains auditable for the child-linking transition.
- If parent progress cannot be updated after child state creation, child agent execution does not continue.
- Existing normal pending-unit `delivery run-next` behavior remains compatible.
- Tests cover successful linkage ordering and the fail-safe when parent progress cannot be persisted.

## Security and privacy

CLI and JSON output may include task ids, unit ids, plan identifiers, and deterministic failure codes, but must not expose raw task state, prompts, provider output, validation logs, source excerpts, secrets, or local absolute paths. Existing audit artifacts and child task state must remain local and inspectable.

## Reviewer focus

Review the ordering between child task creation, parent progress persistence, and child agent execution. Confirm failures preserve inspectability without creating duplicate child task ids or losing audit events.

## Out of scope

Do not implement running-unit resume selection, terminal child reconciliation, failed child retry, final delivery branch assembly, or multi-repository delivery execution in this unit.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
