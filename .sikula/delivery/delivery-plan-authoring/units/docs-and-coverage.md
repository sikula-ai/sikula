# Document and cover delivery plan authoring

## Goal

Document delivery plan authoring as the normal path from one higher-level task
description to a reviewable delivery plan, and add regression coverage for the
full command flow.

## Current behavior

The public delivery plan documentation describes checking, status, running the
next unit, and finalizing an already-authored plan. It does not describe a
Sikula command that creates the parent plan and unit task files.

## Desired behavior

- Update public documentation for the new `sikula delivery prepare` workflow.
- Explain that delivery prepare creates source artifacts only and does not start
  implementation, create task state, run units, mutate delivery progress, or
  update branches.
- Document the default output layout:
  - `.sikula/delivery/<slug>/plan.yaml`
  - `.sikula/delivery/<slug>/units/<unit-slug>.md`
- Document overwrite behavior, JSON output, audit artifacts, and the expected
  follow-up commands:
  - `sikula delivery check .sikula/delivery/<slug>/plan.yaml`
  - `sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run`
  - `sikula delivery run-next .sikula/delivery/<slug>/plan.yaml`
- Update architecture notes for the new authoring components and privacy
  boundaries.
- Add focused unit and end-to-end tests for the completed authoring workflow
  using fake LLM clients and temporary repositories.

## Compatibility and safety

- Documentation must distinguish generated source artifacts from ignored runtime
  progress under `.sikula/state/delivery/<plan-id>/`.
- Documentation must not suggest using `--no-isolate` for normal self-hosted
  implementation work.
- Tests must not require real provider credentials, network access, external
  repositories, package publishing, deployment targets, or machine-specific
  absolute paths.

## Security and privacy

- Documentation and tests must preserve the privacy boundary between source
  delivery artifacts, local audit artifacts, and ignored runtime state.
- Do not add examples that expose raw prompts, provider output, task state,
  source excerpts, secrets, tokens, local absolute paths, or personal data
  through ordinary CLI or JSON output.
- Fake LLM fixtures and generated sample tasks must use synthetic content only.

## Reviewer focus

- Check that docs describe authoring as source-artifact preparation only, not as
  implementation execution or delivery branch assembly.
- Confirm public docs, architecture notes, and tests agree on command behavior,
  output paths, overwrite behavior, audit artifacts, and privacy boundaries.
- Verify coverage exercises stable command behavior and failure modes without
  depending on real providers or network access.

## Acceptance criteria

- README and `docs/delivery-plans.md` include the authoring workflow and how it
  fits with existing check/status/run-next/finalize commands.
- `ARCHITECTURE.md` describes the command ownership, read-only authoring
  assistant, parser, artifact writer, audit outputs, and privacy boundaries.
- E2E coverage proves that a fake authoring response can create a valid plan and
  unit task files, then `delivery check` succeeds.
- Regression tests cover failure modes that should be stable public behavior.
- The final feature maintains or improves coverage toward the project target.

## Out of scope

- Do not document delivery branch assembly as complete unless that behavior is
  actually implemented.
- Do not change Sikula self-hosting conventions beyond references needed for the
  new public workflow.

## Verification

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
