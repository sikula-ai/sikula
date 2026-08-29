# Audit production writes after provider calls

## Goal

Enforce effective delivery-unit scope independently of provider claims after every production write-capable agent invocation.

## Current behavior

Provider-side success and repository-wide sandbox permissions can allow changed files outside a unit's declared production scope to survive into later delivery phases.

## Desired behavior

Snapshot and inspect changed project-relative files after each physical implementer or production-enabled fixer provider attempt, including provider-internal retries and multiple provider calls within one agent run. Any production change outside the effective unit scope stops the child before another provider call and records `unit_scope_violation` before subsequent validation, review, commit, handoff, or assembly. Preserve the failed worktree and bounded local evidence for inspection. Test-writer activity and only those Fixer passes that actually receive test-write authority continue to use the separate test-write policy.

## Acceptance criteria

- A provider-reported success cannot hide an out-of-scope changed file.
- Initial implementation, review fixes, security fixes, and production-enabled validation fixes receive equivalent enforcement.
- Scope auditing occurs before any later delivery side effect can accept the changes.
- A failed or interrupted provider attempt is audited before an internal retry or a later Fixer provider call can begin.
- Presync, dependency sync, and configured check-autofix mutations are audited against the same effective production scope before output adoption, cleanup, revalidation, or another pipeline phase.
- Provider-owned workspace setup rejects tracked configuration collisions before mutation, and ordinary setup exceptions are recorded as task failures without swallowing process interruptions.
- The stop records sanitized changed paths, counts, declared scope, effective scope, and an amendment-oriented action.
- Partial in-scope and out-of-scope changes remain inspectable in the isolated failed worktree and are not adopted as a successful unit.
- Ordinary no-change handling does not overwrite or misclassify a detected scope violation.
- A symlink at or below an active write root is rejected when its resolved target escapes the write set active for that agent invocation, both before and after the provider call.
- Test-only writer behavior remains governed by existing test-path enforcement; a production-only Fixer pass cannot inherit a blanket test-path exemption.
- Interrupted Fixer recovery uses the test-write roots persisted for the interrupted invocation rather than recomputing broader authority.

## Security and privacy

Do not store file contents, diffs, raw provider output, prompts, credentials, or absolute paths in the scope-violation projection. Cleanup must not remove unrelated operator changes or the preserved failed worktree.

## Reviewer focus

Verify that the audit is provider-independent, covers every production-capable pass, runs before validation and delivery finalization, and cannot be bypassed by agent output or broader repository configuration.

## Out of scope

Do not automatically restore, adopt, commit, or amend out-of-scope production changes, and do not change the established test-only fixer recovery rules except where needed to keep scope categories separate.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
