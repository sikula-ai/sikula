# Calculate effective unit write scope

## Goal

Derive a delivery child's production write scope from both repository configuration and the selected unit declaration, with fail-closed preflight behavior.

## Current behavior

A child can inherit repository-wide production write paths even when its unit declares a narrower non-empty scope. An empty intersection is not distinguished from the legacy repository-wide default.

## Desired behavior

For a non-empty declared unit scope, calculate the effective production write scope as the deterministic intersection with configured repository-wide write paths. An absent or empty unit scope retains legacy repository-wide behavior. Persist whether the child uses legacy/default scope or explicit narrowing. A non-empty declaration with no writable intersection blocks before child creation and never falls back to broader permission. Revalidate the persisted scope against the active assembled worktree on fresh execution and resume immediately before constructing the runtime sandbox.

## Acceptance criteria

- Nested directory and file relationships produce the narrower valid project-relative intersection.
- Repository-wide permission never broadens an explicit unit scope.
- Required manifests, lockfiles, migrations, or generated metadata are writable only when explicitly covered by the unit declaration.
- Deterministic presync, dependency-sync, and check-autofix output cannot bypass the effective unit scope merely because it was produced by a trusted tool phase.
- Absent and empty `scope_paths` preserve existing repository-wide behavior.
- A non-empty scope with no intersection returns a typed scope-violation preflight result before child state, worktree, or agent creation.
- The selected scope mode, declared scope, and effective scope are available in local child audit state when a child is created.
- A dependency that replaces a scoped path with an escaping symlink is rejected in the assembled child worktree before Orchestrator, tools, or providers start.
- Resume repeats the same active-worktree validation and cannot rely on the operator checkout.
- Test-writer permissions remain controlled by the configured test-write policy and do not expand production-agent scope.

## Security and privacy

Retain existing sandbox canonicalization and path-containment protections. Error metadata must contain only sanitized project-relative paths and must not reveal absolute paths or file contents.

## Reviewer focus

Inspect prefix and exact-file intersection semantics, fail-closed handling of invalid or disjoint paths, backward compatibility for empty scopes, and strict separation between production and test write policies.

## Out of scope

Do not infer companion-file permission from package-manager behavior or introduce new repository-wide sandbox permissions.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
