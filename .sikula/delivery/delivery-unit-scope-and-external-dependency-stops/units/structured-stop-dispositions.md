# Parse structured delivery stop dispositions

## Goal

Add parser-owned structured dispositions for external dependency gaps and review findings so orchestration never authorizes write cycles from free-form wording alone.

## Current behavior

Analyst, implementer, reviewer, and security-review output can describe a required dependency change or scope amendment in prose, while orchestration treats the result as a generic failure or ordinary fix request.

## Desired behavior

Define bounded structured dispositions. Analyst and implementer results can report `external_dependency_gap`; review and security-review findings can report `fix_in_scope`, `requires_scope_amendment`, or `external_dependency_gap`. Parsing must reject ambiguous, malformed, nested, or conflicting structures safely. A valid implementer dependency stop remains authoritative whether it produced no changes or left partial changes. Once an implementer advertises the schema marker, malformed output persists bounded parse evidence for a terminal `implementer_disposition_invalid` outcome rather than an ordinary retryable failure.

## Acceptance criteria

- Disposition values are validated against the supported closed set.
- Free-form text alone cannot trigger a scope expansion, external follow-up, or additional write-capable pass.
- A recognizable schema-key advertisement fails closed when malformed, including single-quoted or unquoted key syntax; it cannot fall back to ordinary implementer success.
- Malformed structured output fails safely without authorizing another implementation or fix cycle.
- Malformed advertised implementer output remains terminal when partial writes already exist; neither direct reset nor parent `run-next --reset-failed` may adopt those writes into later phases.
- `fix_in_scope` remains available for ordinary bounded review recovery.
- An implementer `external_dependency_gap` is preserved even when no files changed.
- Review and security-review issue summaries retain their disposition and bounded recovery metadata.
- Existing approval parsing and the security-review ambiguous-output fail-safe remain effective.
- Each LLM invocation continues to produce the required append-only audit record, including exception paths.
- Analyst success and exception records include an explicit empty write list plus step, build, review, security-review, and scope correlation fields.

## Security and privacy

Structured public or recovery metadata must be bounded and sanitized. Raw prompts, provider output, source excerpts, diffs, file contents, secrets, and absolute paths remain confined to existing local audit state.

## Reviewer focus

Check unambiguous parsing, invalid-enum handling, mixed approval-and-issue cases, zero-change implementer stops, and preservation of the security reviewer's fail-closed behavior.

## Out of scope

Do not infer dispositions by keyword matching arbitrary prose or permit an agent to enlarge its own scope.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
