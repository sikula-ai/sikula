# Add the delivery prepare command surface

## Goal

Add the CLI front door for automated delivery plan authoring so a maintainer can
start from one higher-level task file and ask Sikula to prepare reviewable
delivery plan artifacts.

## Current behavior

Sikula can check, inspect, run, and finalize an existing delivery plan, but it
does not provide a command that prepares the parent plan and unit task files
from an initial task description. The only available workflow is hand-writing
`.sikula/delivery/<slug>/plan.yaml` and each unit task file.

## Desired behavior

- Add a `sikula delivery prepare TASK_FILE` command surface.
- The command accepts an output directory option for the delivery plan
  directory, for example `--output .sikula/delivery/<slug>/`.
- The command has a machine-readable `--json` projection for status, resolved
  paths, selected plan ID, generated-or-previewed unit IDs, and errors.
- The command exposes the existing per-agent override flag pattern for the
  future delivery authoring assistant:
  - `--agent-model delivery_preparer=<model>`
  - `--agent-provider delivery_preparer=<provider>`
  - `--agent-timeout delivery_preparer=<seconds>`
- The command refuses missing task files, non-file task paths, output paths that
  escape the project root, and ambiguous overwrite situations with clear
  non-zero exits.
- The command uses `--force` as the explicit overwrite opt-in for replacing an
  existing generated delivery plan directory. Without `--force`, any existing
  plan or unit task file under the selected output directory blocks the write.
- The command does not create task state, worktrees, result branches, delivery
  progress, or implementation commits.
- The command is safe to wire to authoring generation in later units without
  changing the public CLI shape.

## Compatibility and safety

- Existing `sikula delivery check`, `status`, `run-next`, and `finalize` flows
  must keep their current behavior and arguments.
- Existing `sikula run`, `review`, `review --fix`, `cleanup`, and `delete`
  flows must not change.
- The command must not require `--no-isolate` or alter Sikula-generated
  worktree branch naming.
- Errors and JSON output must not expose raw task bodies, prompts, provider
  output, source excerpts, state files, or secrets.

## Security and privacy

- Treat the source task body and generated diagnostics as local evidence. Do not
  expose raw task text, prompts, provider output, source excerpts, task state,
  API keys, tokens, local absolute paths, or personal data through ordinary text
  or JSON output.
- Keep the command read-only with respect to task state, delivery progress,
  worktrees, branches, and provider execution.

## Reviewer focus

- Check that the CLI shape is stable enough for later authoring units to build
  on without renaming public flags or changing exit/status semantics.
- Check that `delivery_preparer` agent override flags match the existing
  `task_preparer` UX pattern used by `task refine` and `contract prepare`.
- Check path validation, `--force` overwrite behavior, and JSON output for
  privacy-safe allowlisted fields.
- Confirm existing delivery and run/review flows are untouched.

## Acceptance criteria

- `sikula delivery prepare --help` documents the new command and options.
- Invalid input and unsafe output paths fail before any generated files are
  written.
- The implementation has focused tests for argument parsing, path resolution,
  delivery-preparer agent overrides, default overwrite refusal, `--force`
  overwrite opt-in, text output, and JSON output.
- The command surface can be used by later units to connect the read-only
  authoring assistant and artifact writer without renaming flags or changing
  status semantics.

## Out of scope

- Do not implement LLM-based unit decomposition in this unit.
- Do not write `plan.yaml` or unit task files in this unit unless a later unit
  is already part of the same change.
- Do not change delivery `run-next` assembly behavior.

## Verification

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
