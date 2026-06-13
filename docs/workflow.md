# Workflow

Sikula is built around four concepts:

1. Implementation contract
2. Gated pipeline
3. State file
4. Learn & adapt

```text
Rough intent
  -> implementation contract
  -> gated AI delivery pipeline
  -> PR-ready branch + state file
  -> better next run
```

## Implementation Contract

The implementation contract is a two-way handshake between you and Sikula: you bring the intent, Sikula checks whether it is clear and deliverable, asks for missing context when needed, and turns it into scope, acceptance criteria, risks, tests, and validation.

Use the contract commands before starting delivery:

```bash
sikula contract check .sikula/tasks/my-task.md
sikula contract check .sikula/tasks/my-task.md --write-report
sikula contract improve .sikula/tasks/my-task.md \
  --answers .sikula/contracts/my-task.answers.yaml \
  --output .sikula/tasks/my-task.v2.md
```

`contract check` is read-only unless `--write-report` is passed. It does not create a branch or start agents. `sikula run TASK_FILE` also records a compact warning-only contract snapshot before agents start.

## Gated Pipeline

The normal run pipeline is:

```text
presync -> analyze -> plan -> implement -> review -> security review
  -> test writing -> sync -> build -> tests -> checks -> fixer loop
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

- task description and implementation contract snapshot
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

- Improve future task files with `contract check` and `contract improve`.
- Add project conventions to `.sikula/guidelines.md` or existing guidance docs.
- Tune `.sikula/config.yaml` based on validation failures, testability gaps, and review findings.
- Keep useful architecture and testing rules in committed project docs so agents receive them on future runs.

The output of one run should make the next run clearer, better scoped, and easier to validate.
