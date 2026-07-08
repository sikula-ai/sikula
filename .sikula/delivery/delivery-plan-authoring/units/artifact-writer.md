# Write generated delivery plan artifacts safely

## Goal

Connect delivery plan authoring to tracked repository artifacts so
`sikula delivery prepare` can create a reviewable parent plan and unit task
files from one higher-level task description.

## Current behavior

Even after a delivery decomposition is decided, an operator must manually create
the parent YAML file and every unit task file in the correct directory layout.
The existing delivery validator can check those files only after they already
exist.

## Desired behavior

- `sikula delivery prepare TASK_FILE --output .sikula/delivery/<slug>/` writes:
  - `.sikula/delivery/<slug>/plan.yaml`
  - `.sikula/delivery/<slug>/units/<unit-slug>.md`
- If `--output` is omitted, the command derives the default directory from the
  source task stem under `.sikula/delivery/<slug>/`.
- The generated directory slug, `plan_id`, unit IDs, and unit task paths are
  stable, portable, project-relative, and valid for `sikula delivery check`.
- The writer consumes the parsed authoring draft, not raw LLM output. It derives
  `plan.yaml` and unit task paths from the selected output directory and parsed
  unit IDs.
- The command refuses to overwrite existing plan or unit files unless an
  explicit safe overwrite mode is provided.
- The command validates the generated plan after writing or before finalizing
  the write and reports deterministic errors if validation fails.
- The command checks each generated unit task with the same contract-readiness
  logic used by `sikula contract check`. A prepare result is not ready if any
  unit task has blocking readiness gaps.
- Normal terminal and JSON output show allowlisted metadata only: plan path,
  plan ID, unit IDs, output paths, delivery-plan validation status, and unit
  readiness status.
- Raw prompts, raw provider output, and generated-answer diagnostics are kept in
  local audit artifacts under the configured contract report directory.

## Compatibility and safety

- Existing delivery progress under `.sikula/state/delivery/<plan-id>/` must not
  be created or mutated by prepare.
- Existing delivery `run-next` and `finalize` semantics must not change.
- The writer must avoid partial source artifacts when generation or validation
  fails.
- The implementation must preserve auditability without exposing secrets,
  prompts, raw task text, source excerpts, provider output, or task state through
  ordinary diagnostics.

## Security and privacy

- Treat generated plan and unit task files as source artifacts, but keep raw
  prompts, raw provider output, source task text, source excerpts, task state,
  API keys, tokens, local absolute paths, and personal data out of ordinary text
  and JSON output.
- Validate and normalize project-relative output paths before writing. Reject
  absolute paths, path traversal, symlink escapes, and writes outside the
  selected delivery output directory.
- Never trust LLM-supplied file paths. Derive delivery source paths from the
  selected output directory, `plan_id`, and validated unit IDs.
- Avoid partial writes that could leave misleading source artifacts after
  generation, parsing, validation, or filesystem failure.

## Reviewer focus

- Inspect filesystem safety, overwrite semantics, rollback/cleanup behavior,
  and final validation before reporting success.
- Confirm generated `plan.yaml` and unit task paths pass existing delivery plan
  validation and follow self-hosting conventions.
- Confirm unit task readiness is checked deterministically after generation and
  that blocking contract gaps keep the prepare result from being reported as
  ready.
- Confirm the writer is deterministic after parsing and does not inspect or
  reinterpret raw provider text.
- Check audit records preserve debuggability without leaking prompt or provider
  content through normal command output.

## Acceptance criteria

- A successful prepare creates a valid delivery plan that passes
  `sikula delivery check`.
- A successful ready prepare also produces unit task files that pass contract
  readiness without blocking gaps.
- Existing files are protected by default and produce actionable errors.
- Failed generation, failed parsing, failed validation, and filesystem errors do
  not leave misleading half-valid source artifacts.
- JSON output is stable enough for automation and does not expose raw prompt or
  provider content.
- Tests cover successful writes, default output path derivation, overwrite
  refusal, `--force` overwrite opt-in, delivery validation failure cleanup or
  rollback, unit readiness failures, JSON output, and audit file creation.

## Out of scope

- Do not run generated units.
- Do not assemble or finalize the delivery branch.
- Do not add multi-repository delivery execution semantics.

## Verification

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
