# Preserve source-task delivery constraints

## Goal

Introduce a bounded, structured representation of source-task constraints that remains authoritative while delivery units are authored and checked.

## Current behavior

Delivery preparation can produce individually ready unit contracts without proving that they preserve repository ownership, read-only dependency, fail-and-follow-up, security-boundary, or prohibited-fallback constraints from the source task.

## Desired behavior

Delivery preparation identifies applicable hard constraints and attaches their normalized, bounded representation to generated delivery artifacts. Generated units must preserve the relevant constraints, and preparation must stop for review when consistency with a known hard constraint cannot be established. Deterministic plan checking must reject structurally invalid constraint metadata and known contradictions without treating unit readiness as proof of semantic consistency. Existing plans without the additive metadata remain valid where no new consistency claim is required.

## Acceptance criteria

- Structured constraints cover repository ownership, authoritative read-only dependencies, explicit stop-and-follow-up requirements, security boundaries, and prohibited fallback implementations when present.
- Every generated unit receives the relevant normalized constraints.
- A unit that assigns dependency-owned work to the consumer repository is not published as executable.
- Ambiguous or malformed hard-constraint output fails closed for preparation review.
- Constraint metadata is bounded, deterministic, and suitable for later child and amendment context.
- When amendment supersedes a constrained target, every applicable constraint is reassigned to all replacement units before validation and no constraint keeps the superseded target reference.
- Existing delivery-plan validation, unit readiness checks, overwrite protection, and transactional rollback remain effective.

## Security and privacy

Do not place source-task bodies, source excerpts, prompts, provider output, credentials, or absolute paths in public plan metadata or command projections. Preserve the existing local audit boundary for authoring prompts and outputs.

## Reviewer focus

Verify that the checked-in source task remains authoritative, that model-produced unit text cannot erase a hard constraint, and that deterministic parsing rejects malformed or contradictory metadata without inventing product-specific ownership rules.

## Out of scope

Do not automatically change another repository, redesign plan publication, or infer ownership from repository or product names.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
