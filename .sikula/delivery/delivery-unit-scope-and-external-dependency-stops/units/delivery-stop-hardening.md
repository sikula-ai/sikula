# Document and harden delivery stops

## Goal

Complete compatibility, end-to-end, privacy, and documentation coverage for fail-closed unit scope and external dependency behavior.

## Current behavior

Project documentation describes repository-wide sandboxing, planner-budget recovery, delivery status, and amendments, but does not fully explain effective unit scope or the new terminal recovery classifications.

## Desired behavior

Document the distinction between configured repository-wide write paths and effective unit production scope, structured boundary stops, truthful recovery actions, and unchanged planner-budget semantics. Add focused tests across preparation, execution, review recovery, status, reset preflight, amendment preparation, and compatibility paths using existing test infrastructure.

## Acceptance criteria

- Tests cover inherited constraints in generated units and child context, preparation-time contradictions, scope intersection, provider-independent out-of-scope detection, and separation of test-writer permissions.
- Tests prove that scope violations cannot reach validation, review, commit, handoff, or assembly while preserving inspectable local evidence.
- End-to-end tests prove that assembled dependency symlink drift is rejected before provider execution on the next child.
- Tests cover `fix_in_scope`, `requires_scope_amendment`, and analyst, implementer, reviewer, and security-review external dependency stops.
- Tests cover malformed advertised implementer dispositions after partial writes, including direct and parent reset resistance.
- Text and JSON tests assert exact stable codes, blocked-reason fields, retry availability, recovery actions, and public-output sanitization.
- Reset-preflight tests prove that blocked retries do not resume agents or mutate progress.
- Amendment tests cover corrected in-repository proposals, external-follow-up no-proposal results, sanitized evidence, and dry-run non-mutation.
- Amendment tests prove constrained targets transfer applicable constraints to every replacement and remain deterministically valid.
- Regression tests preserve planner-step enforcement, verified budget-split metadata, assembled-branch amendment publication, and multi-budget fingerprint replay.
- Tests use fake LLM clients, temporary repositories, and isolated state, and must not use network access, credentials, external repository writes, or machine-specific paths.
- Architecture, delivery-plan, pipeline, sandbox, guideline, and changelog documentation describe the new behavior. README changes only if its high-level workflow would otherwise be incomplete.

## Security and privacy

Tests must assert that public output excludes raw tasks, prompts, provider output, state blobs, diffs, file contents, validation logs, credentials, environment values, and absolute paths. Documentation must not imply automatic external-repository authorization.

## Reviewer focus

Review the complete failure matrix, backward compatibility for legacy empty scopes and unaffected delivery states, validation of no-side-effect paths, documentation consistency, and use of existing fake-provider and temporary-repository seams.

## Out of scope

Do not introduce new production behavior in this hardening unit, synthesize runtime test harnesses, add external integration tests, or turn advisory non-planner budgets into hard limits.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
