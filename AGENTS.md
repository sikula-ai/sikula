# AGENTS.md

## Required Context

Before making non-trivial changes, read:

- `guidelines.md`
- `ARCHITECTURE.md`
- `CONTRIBUTING.md`
- `README.md`
- `.sikula/config.yaml`

Use the document that owns the topic:

- `ARCHITECTURE.md` owns target system shape, pipeline phases, state fields,
  worktree behavior, and responsibility boundaries.
- `guidelines.md` owns implementation guardrails, agent rules, state
  invariants, and testing conventions.
- `.sikula/config.yaml` owns the Sikula self-hosting pipeline, sandbox, context
  files, and validation commands.
- `CONTRIBUTING.md` owns human development setup, editable install, test, lint,
  and release workflow.
- `AGENTS.md` owns agent workflow and Sikula artifact handling.

When instructions conflict, follow this order:

1. `guidelines.md`
2. `ARCHITECTURE.md`
3. this file
4. `.sikula/config.yaml`
5. `CONTRIBUTING.md`
6. local code comments and implementation details

## Sikula Task And Contract Workflow

Normal Sikula product work should use Sikula's contract-first flow. Use direct
manual edits only when the user explicitly asks for repository maintenance,
documentation, configuration, review, or workflow changes.

For fresh implementation work, refine the source task first, prepare a runnable
implementation contract, then run the prepared contract through the readiness
gate:

```bash
sikula contract check .sikula/tasks/<task>.md
sikula task refine .sikula/tasks/<task>.md \
  --answers .sikula/contract-reports/<task>.task-refine.answers.yaml \
  --output .sikula/tasks/<task>.refined.md
sikula contract check .sikula/tasks/<task>.refined.md --write-report
sikula contract prepare .sikula/tasks/<task>.refined.md \
  --answers .sikula/contract-reports/<task>.refined.answers.yaml \
  --output .sikula/contracts/<task>.contract.md
sikula run .sikula/contracts/<task>.contract.md --require-contract-ready
```

If no answers file exists, run the refine or prepare command once to generate
its answers template, fill that template, and rerun the same command with
`--answers`. Do not replace the refine step with a
`contract check --write-report` pass on the original task.

Direct runs such as `sikula run .sikula/tasks/<task>.md` are acceptable only
for lightweight local iteration when the task file is already a complete
implementation contract and the user does not need a stored handoff artifact.
Do not start a fresh `sikula run <task-or-contract-file>` without
`--require-contract-ready` unless the user explicitly asks to bypass the
readiness gate.

Artifact ownership:

- `.sikula/tasks/*.md` are source task descriptions or refined task drafts.
  Keep them focused on product intent, current and desired behavior,
  compatibility expectations, constraints, and observable acceptance decisions.
- `.sikula/contracts/*.contract.md` are prepared runnable implementation
  contracts. Run these with `sikula run`. Regenerate them when the source task,
  repository context, or preparation answers change.
- `.sikula/contract-reports/` contains generated reports, answers templates,
  and generated-answer sidecars. Treat these as preparation/debug artifacts.
  Do not commit this directory unless it is explicitly needed for review,
  audit, or debugging.
- `.sikula/state/` and `.sikula/worktrees/` are runtime artifacts and must stay
  uncommitted.

When an existing Sikula task is interrupted, failed, or recoverable, resume it
before attempting manual adoption or direct fixes. Inspect it with
`sikula show <task_id>`, then run `sikula run --task-id <task_id>` or, when the
state is failed, `sikula run --task-id <task_id> --reset-failed`. Do not copy
changes manually from `.sikula/worktrees/...` into the main worktree unless the
user explicitly asks for manual adoption.

## Self-Hosting And Editable Install Caveats

Sikula is often developed through `pipx install --editable .` or an editable
virtualenv install. That means the `sikula` command that starts a run may load
code from the same checkout that a task is changing.

Rules for using Sikula on Sikula:

- Prefer the default isolated worktree flow for code changes. Avoid
  `--no-isolate` for changes to `sikula.py`, `sikula_cli/`, `core/`, `agents/`,
  `tools/`, provider wrappers, state handling, worktree handling, or delivery
  finalization.
- Do not rely on the currently running `sikula` process to reload modified
  Python modules from a task worktree. Treat validation subprocesses and tests
  as the proof for modified code, then start a fresh `sikula` command after the
  change lands when manual CLI verification is needed.
- Do not launch nested real `sikula run` or `sikula review --fix` delivery
  commands from validation or tests unless a task explicitly targets that
  behavior and uses fake providers, temporary repositories, and isolated state.
- If a task changes package metadata, entrypoints, imports used at process
  startup, or CLI-provider setup, follow `CONTRIBUTING.md` and reinstall or
  restart the editable CLI before manual end-to-end verification.
- The effective `.sikula/config.yaml` is captured at run start. A task that
  changes this config does not fix the active run's validation coverage or
  sandbox policy; finish the explicit config change, then start a fresh run.
- Prompt-governing files (`guidelines.md`, `AGENTS.md`, `ARCHITECTURE.md`,
  `.sikula/config.yaml`, and `.sikula/*_rules.md`) affect future agent
  behavior. Until Sikula snapshots all prompt context for active runs, do not
  treat a run that changes these files as final approval of its own governance
  changes; commit the change and run a fresh Sikula review or run from the
  updated context.
- `.sikula/*_rules.md` files are intentionally outside agent write scope. Update
  them as explicit maintainer changes, not as ordinary self-hosted task output.

## Sikula Task Writing For Sikula Core

Write `.sikula/tasks/*.md` as product and behavior descriptions, not as
file-by-file implementation plans.

Good Sikula core tasks should include:

- the user-visible CLI, status, JSON, state, worktree, review, or delivery
  behavior being changed;
- current behavior, desired behavior, and compatibility expectations;
- state/resume/delivery implications when `TaskState`, worktrees, cleanup,
  delete, review, review-fix, or current-branch delivery are touched;
- privacy and audit implications when prompts, provider output, logs, raw
  state, source snippets, diffs, or diagnostics are touched;
- exact validation commands under a clear `Verification:` heading when they are
  acceptance criteria;
- documentation acceptance criteria for changes that alter public CLI behavior,
  architecture, state fields, provider setup, task/contract preparation, or
  project governance.

Avoid over-specifying internal helper names unless the exact public API,
serialized field, CLI option, file path, or migration behavior is itself part
of the contract. Test-file edits do not need to be requested just to remind
Sikula to write tests; the TestWriter owns test updates when enabled.

## Review guidelines

When reviewing this repository, use these project documents as context:

- `README.md`
- `guidelines.md`
- `ARCHITECTURE.md`
- `CONTRIBUTING.md`

Focus especially on:

- Pipeline correctness: `run`, `resume`, `review`, `review --fix`, `--no-isolate`, `cleanup`, and `delete` flows must keep working.
- Task state compatibility: additive fields are fine; removing, renaming, or changing existing `TaskState` field types requires a schema migration in `core/state.py`.
- Auditability: prompts, LLM outputs, validation records, retry records, and relevant state transitions must not be lost.
- Separation of concerns: orchestration belongs in `core/orchestrator.py`; agent behavior belongs in `agents/`; provider subprocess logic belongs in `core/llm_client.py`; platform build behavior belongs in `tools/`.
- Reviewer and security reviewer agents must stay read-only.
- Agent prompts and orchestration must remain platform-agnostic.
- Tests must cover changed state transitions, output parsing, and pipeline branches.
- Do not remove copyright, license, attribution, or notice information.
- Do not log or expose secrets, tokens, API keys, private prompts, source excerpts, task state, or personal data.
- Do not accept changes that weaken security-review fail-safe behavior.
- Do not accept changes that make task state less useful for debugging, auditing, or resume.
- Treat legal, licensing, policy, release, and project-governance documents (`LICENSE`, `NOTICE`, `CLA.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `PRIVACY.md`, `SECURITY.md`, `RELEASE.md`, `CHANGELOG.md`, `THIRD_PARTY_NOTICES.md`, `AGENTS.md`) as maintainer-owned; do not suggest incidental changes unless the PR explicitly targets them.
- Keep review comments focused on material correctness, security, maintainability, and testing risks.
- Encourage PRs to reach towards 90% test coverage, but treat it as a goal rather than a strict merge blocker.
- Sandbox enforcement: Do not accept changes that bypass or weaken the `Sandbox` restrictions (`allowed_read_paths`, `allowed_write_paths`).
- Parser robustness: Agent output parsers (e.g. for structured LLM blocks) must degrade safely and not crash the orchestrator if formatting is hallucinated.
- Output decoupling: CLI text output must remain decoupled from core pipeline logic. Machine-readable formats (for example `--json` and future strict runner/console projections) must be explicit projection contracts, not raw `TaskState`, `sikula show`, or terminal text with fields trimmed after the fact.
- CLI-provider changes: verify prompt transport, timeout handling, diagnostic redaction, retry classification, and read-only/write-mode boundaries.
- Prompt privacy: stored prompts are audit artifacts; do not expose them through ordinary diagnostics or external reports. `sikula show` is the explicit full-state audit exception.
