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

Use [Writing Sikula Tasks](writing-tasks.md) for contract commands and task examples. `sikula contract check TASK_FILE` and `sikula run TASK_FILE` use the same effective build/test/check phases from the Sikula config when scoring validation coverage. `sikula task refine` is the explicit product-description refinement step; its optional `--auto` mode can use a read-only LLM assistant to normalize a rough or non-English product request and propose supported product-level answers before deterministic product-question handling. `sikula contract prepare` is the explicit step that writes a project-aware Markdown implementation contract from a task description and answers; it expects product-task asset declarations in `## Assets`, while the generated `## Asset manifest` belongs to prepared implementation contracts. Its optional `--auto` mode can use a read-only LLM assistant to propose supported delivery answers before the same deterministic prepare/recheck logic runs. Both auto modes keep local prompt/raw-response audit records, including provider failures and malformed responses that fail parsing, under `.sikula/contract-reports/*.auto-llm.jsonl`. `sikula run TASK_FILE` then runs the file you pass to it and records a compact, warning-only contract snapshot before agents start; it does not rewrite the task file automatically. Fresh task-file runs can opt into pre-agent readiness gates with `--require-contract-ready` or `--min-contract-score N`; gate-failed states are kept for audit but must be restarted from the task file after the contract is prepared, not reset through `--task-id`. Review modes use the existing branch diff as their primary artifact and do not reuse the delivery contract-readiness gate.

## Delivery Plans

Delivery plans are a parent layer for large work that will be split into small
delivery units. A delivery unit remains a normal Sikula task/contract/run; the
plan records ordering, dependencies, streams, and eventual output branch
metadata.

The current MVP exposes:

```bash
sikula delivery assess .sikula/tasks/<task>.refined.md
sikula delivery prepare .sikula/tasks/<task>.refined.md
sikula delivery check .sikula/delivery/<slug>/plan.yaml
sikula delivery status .sikula/delivery/<slug>/plan.yaml
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml
sikula delivery run .sikula/delivery/<slug>/plan.yaml
sikula delivery finalize .sikula/delivery/<slug>/plan.yaml
```

`delivery assess` recommends a standard run, delivery plan, or task
clarification without starting either workflow. `delivery prepare` authors
tracked plan source artifacts. The remaining commands validate, inspect,
execute, and finalize that explicit plan, and can emit privacy-safe JSON.
`delivery run-next` executes or reconciles one unit. `delivery run` is a bounded
coordinator over that primitive: it reloads durable parent status between units,
stops on the first blocker or failure, and finalizes automatically when every
unit is done. Explicit `--reset-failed` authorizes one retry of the current
failed child before normal bounded execution continues. It does not add another
agent pipeline or replace unit-level Sikula runs.
Before a new unit starts, Sikula intersects its declared production
`scope_paths` with the configured production write paths and snapshots the
unit's inherited constraints. Each delivery agent validates that constraint
context before provider execution. Sikula also audits actual changes after every
Implementer or Fixer call, independently of the provider's file report. A scope
violation, required scope amendment, or external dependency gap is a terminal,
non-retryable child stop: later unit phases and delivery assembly do not run,
while the isolated worktree and sanitized audit evidence remain available for
inspection and amendment or external follow-up.
`delivery status` also reads ignored parent progress from
`.sikula/state/delivery/<plan-id>/progress.json` when present; if progress does
not exist yet, units are reported as pending from the plan. Assessment and
preparation do not create worktrees, task state, or delivery progress.
The validator currently enforces single-repository scope and rejects multi-repo
plans until cross-repo branching, locking, validation, and result-set semantics
exist. See [Delivery Plans](delivery-plans.md).

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

Review and security issues classified as `fix_in_scope` feed back to the
implementer. A structured `requires_scope_amendment` or
`external_dependency_gap` disposition stops the child instead of entering a fix
loop. Build, test, and check failures feed the fixer until validation passes or
the configured iteration limit is reached.

## State File

Each task has persistent state under `.sikula/state/`. Inspect it with:

```bash
sikula show <task-id>
sikula summary <task-id>
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

`sikula show` is the explicit full local audit view. For a completed isolated
task or review-fix with a publishable commit, `sikula summary` instead
emits a deterministic PR-ready Markdown projection. It includes only
allowlisted state-derived metadata: bounded contract identity, final
validation/review status, safe project-relative paths touched during the run,
and aggregate recovered or residual signals. Touched paths are cumulative audit
records and can include changes reverted before completion; they are not
presented as an authoritative terminal diff. It never emits task or contract
bodies, prompts, provider output, review prose, validation diagnostics, or
absolute paths. Failed/incomplete tasks, delivery-unit child states, and
report-only reviews fail closed because they do not represent a publishable
implementation result.

PR-ready summary generation requires a safe recorded branch, a full result or
isolated-fix commit, and no preserved worktree. It rejects explicit cleanup records because they do
not prove automatic finalization. No-change and `--no-isolate` runs do not have
a standalone commit to publish and are intentionally unsupported.

## Learn & Adapt

Sikula does not learn by silently mutating model weights. The learning loop is explicit:

- Improve future implementation contracts with `contract check`, `task refine`, and `contract prepare`.
- Add project conventions to `.sikula/guidelines.md` or existing guidance docs.
- Tune `.sikula/config.yaml` based on validation failures, testability gaps, and review findings.
- Keep useful architecture and testing rules in committed project docs so agents receive them on future runs.

The output of one run should make the next run clearer, better scoped, and easier to validate.
