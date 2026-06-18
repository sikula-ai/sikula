# Pipeline And State

Detailed explanation of Sikula's gated agentic delivery pipeline, task state, and learn/adapt loop.

Sikula is built around four concepts:

1. Product task description and implementation contract
2. Gated pipeline
3. State file
4. Learn & adapt

```text
Product task description
  -> implementation contract
  -> gated agentic delivery pipeline
  -> PR-ready branch + state file
  -> better next run
```

## Implementation Contract

The product task description captures the user or business intent. The implementation contract is the delivery artifact Sikula can run: it preserves that intent while making scope, acceptance criteria, constraints, risks, tests, and validation explicit.

Use [Writing Sikula Tasks](writing-tasks.md) for contract commands and task examples. `sikula contract check TASK_FILE` and `sikula run TASK_FILE` use the same effective build/test/check phases from the Sikula config when scoring validation coverage. `sikula task refine` is the explicit product-description refinement step, and `sikula contract prepare` is the explicit step that writes a project-aware Markdown implementation contract from a task description and answers. `sikula run TASK_FILE` then runs the file you pass to it and records a compact, warning-only contract snapshot before agents start; it does not rewrite the task file automatically. Fresh task-file runs can opt into pre-agent readiness gates with `--require-contract-ready` or `--min-contract-score N`; gate-failed states are kept for audit but must be restarted from the task file after the contract is prepared, not reset through `--task-id`. Review modes use the existing branch diff as their primary artifact and do not reuse the delivery contract-readiness gate.

## Gated Pipeline

The normal run pipeline is:

```text
presync -> analyze -> plan -> implement -> review -> security review
  -> test writing -> sync -> build -> tests -> checks -> fixer loop
```

The high-level control flow is:

```text
Implementation contract file
  -> implementation contract snapshot
  -> presync (optional)
  -> Analyst
  -> Planner (optional)
     -> SINGLE_PASS or planner disabled
        -> Implementer
        -> Reviewer -> Security Reviewer -> Test Writer
        -> Build / Fix loop
     -> MULTI_STEP
        -> for each planned step:
             Implementer -> Reviewer -> Security Reviewer -> Test Writer
             -> Build / Fix loop (only when run_build_per_step is enabled)
        -> Final full-task gate:
             Reviewer -> Security Reviewer -> Test Writer
        -> Final Build / Fix loop
  -> Branch ready for human review
```

Most phases can be enabled or disabled in `.sikula/config.yaml` or with per-run flags. The key gates are:

- `AnalystAgent` reads the task and project context and produces implementation instructions.
- `PlannerAgent` decides `SINGLE_PASS` or splits larger tasks into ordered steps.
- `ImplementerAgent` writes production changes.
- `ReviewerAgent` performs independent read-only review.
- `SecurityReviewerAgent` performs independent read-only security review.
- `TestWriterAgent` writes or updates tests.
- `FixerAgent` fixes build, test, and quality-check failures.

Review and security issues feed back to the implementer. Build, test, and check failures feed the fixer until validation passes or the configured iteration limit is reached.

## State File

Each task has persistent state under `.sikula/state/`. Inspect it with:

```bash
sikula show <task-id>
```

State records:

- input task or contract text and implementation contract snapshot
- prompts and LLM outputs
- files changed
- review and security review rounds
- test writer runs
- build/test/check records
- recovered validation diagnostics
- provider retry records
- config snapshot
- final result metadata

State is useful for audit and debugging. It may contain sensitive source or prompt context, so redact it before sharing.

## Learn & Adapt

Sikula does not learn by silently mutating model weights. The learning loop is explicit:

- Improve future implementation contracts with `contract check`, `task refine`, and `contract prepare`.
- Add project conventions to `.sikula/guidelines.md` or existing guidance docs.
- Tune `.sikula/config.yaml` based on validation failures, testability gaps, and review findings.
- Keep useful architecture and testing rules in committed project docs so agents receive them on future runs.

The output of one run should make the next run clearer, better scoped, and easier to validate.
