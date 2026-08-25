# Use failed-child evidence in amendments

## Goal

Provide amendment authoring with sanitized boundary-failure evidence and return an explicit external follow-up when no valid single-repository replacement is possible.

## Current behavior

Ordinary amendment preparation receives the original unit contract and plan context but can miss the linked child's scope failure, review disposition, inherited ownership constraints, and partial changed-file evidence.

## Desired behavior

For eligible scope and dependency stops, assemble bounded evidence from the linked child and parent plan. Amendment authoring uses that evidence to correct ownership or declared scope. If the current repository cannot own the required change, preparation returns `delivery_amend.external_dependency_follow_up_required`, publishes no proposal, and recommends the external follow-up.

## Acceptance criteria

- Amendment context includes the failure classification, recommended action, applicable inherited constraints, declared and effective write scope, sanitized changed paths and counts, review and security dispositions, and available dependency-handoff identities.
- Evidence is correlated with the expected plan, unit, and child before it is trusted.
- Evidence capture and delivery recovery use the active injected `StateStore`; a non-JSON store is never bypassed by reconstructing `JsonStateStore` from configuration.
- In-repository scope corrections can produce a valid replacement graph subject to existing deterministic validation.
- A constrained target can be split: its applicable constraints transfer to every replacement, the superseded target reference is removed, and replacement child context retains the constraint.
- An externally owned required change produces `delivery_amend.external_dependency_follow_up_required`, a non-zero result, and no proposal publication.
- Public output contains only bounded identifiers, project-relative paths, sanitized issues, and the external follow-up action.
- Existing assembled-head checks, stale-input protection, transactional publication, dry-run guarantees, and apply recovery remain authoritative.
- Existing planner-budget split preparation is reused rather than reimplemented.
- All supported budget fields retain canonical ordering and stable proposal fingerprints across load, validation, dry-run, and apply replay.

## Security and privacy

Do not expose raw child state, source task text, prompts, provider output, diffs, file contents, validation output, credentials, environment values, or absolute paths. Never authorize or perform writes in an external repository.

## Reviewer focus

Verify evidence correlation and sanitization, no-proposal behavior for externally owned work, preservation of deterministic amendment and assembly guards, and compatibility with verified planner-budget recovery and multi-budget fingerprints.

## Out of scope

Do not redesign amendment assembly, direct-ref publication, proposal identity, interruption recovery, or multi-repository semantics.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
