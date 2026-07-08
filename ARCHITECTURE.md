# Architecture

> **Keeping this document current:** every structural change (new agent, new tool, new state field,
> loop change) requires an update here. The document intentionally refers to class and file names
> rather than behaviour so that stale references are immediately obvious.

---

## Component map

| Component | File | Responsibility |
|---|---|---|
| `Orchestrator` | `core/orchestrator.py` | Drives the loop; owns the agent registry and tool instances |
| `AnalystAgent` | `agents/analyst_agent.py` | Reads task + project context, produces an implementation prompt |
| `PlannerAgent` | `agents/planner_agent.py` | Breaks the implementation prompt into ordered steps; populates `state.plan` |
| `ImplementerAgent` | `agents/implementer_agent.py` | Runs the configured LLM as an autonomous agent; the agent reads/writes files directly |
| `ReviewerAgent` | `agents/reviewer_agent.py` | Read-only review of implementation; checks completeness, logical correctness, semantic consistency, dead members, and shared function scope |
| `SecurityReviewerAgent` | `agents/security_reviewer_agent.py` | Read-only security review after the review phase; independent of `run_review`; blocking issues feed back to implementer; warnings logged non-blocking |
| `TestWriterAgent` | `agents/test_writer_agent.py` | Writes and updates unit tests after review/security phases complete; configured to target test source directories |
| `DeliveryPreparationAgent` | `agents/delivery_preparation_agent.py` | Read-only delivery-plan authoring assistant for `sikula delivery prepare`; returns one structured draft for deterministic parsing |
| `FixerAgent` | `agents/fixer_agent.py` | Runs the configured LLM as an autonomous agent to fix build or test errors |
| `FileTool` | `tools/file_tool.py` | Read / write files; enforces sandbox whitelist for direct file-tool calls |
| `GitTool` | `tools/git_tool.py` | `diff_head()` — called by reviewer, security_reviewer, and test_writer agents to obtain the live diff when `state.review_diff` is not set |
| `BuildTool` | `tools/base_tool.py` | **Abstract interface** for platform build systems — implement per platform |
| `GradleBaseTool` | `tools/gradle_tool.py` | Shared Gradle mechanics (`_run`, `run_check`, `is_build_config_file`); subclassed by Android and JVM variants |
| `AndroidGradleTool` | `tools/gradle_android_tool.py` | `BuildTool` implementation for Android / Gradle |
| `JvmGradleTool` | `tools/gradle_jvm_tool.py` | `BuildTool` implementation for JVM backends (Spring Boot, Quarkus, Micronaut, …) |
| `MavenTool` | `tools/maven_tool.py` | `BuildTool` implementation for Maven projects; auto-detects `./mvnw` |
| `NodeTool` | `tools/node_tool.py` | `BuildTool` implementation for Node.js / TypeScript / JavaScript projects; detects npm/pnpm/yarn/bun |
| `PythonTool` | `tools/python_tool.py` | `BuildTool` implementation for Python / pytest |
| `CargoTool` | `tools/cargo_tool.py` | `BuildTool` implementation for Rust / Cargo; failed `cargo test` output is reduced with Cargo-aware failure-block extraction before generic diagnostic truncation |
| `XcodeTool` | `tools/xcode_tool.py` | `BuildTool` implementation for iOS / Xcode |
| `InitAgent` | `agents/init_agent.py` | Generates `.sikula/guidelines.md` from codebase analysis; called by `cmd_init()` only — not part of the orchestrator loop |
| `LLMClient` | `core/llm_client.py` | Abstract interface: `generate()` for single-shot text; `run_readonly_agent()` for read-only autonomous agents; `run_agent()` for autonomous file-editing agents |
| `ContractCheck` helpers | `core/contract_check.py` | Deterministic implementation-contract readiness checks for Markdown/plain-text task files; `sikula run` stores a warning-only state snapshot and `sikula contract check --write-report` explicitly writes report artifacts |
| `DeliveryAuthoring` helpers | `core/delivery_authoring.py` | Side-effect-free parser and derived-path helpers for delivery prepare authoring drafts |
| `DeliveryPrepareWriter` helpers | `core/delivery_prepare_writer.py` | Deterministic source-artifact writer for parsed delivery authoring drafts; renders `plan.yaml` and unit task files with readiness checks, plan validation, overwrite guards, and rollback |
| `TaskAsset` helpers | `core/task_assets.py` | Deterministic local task-asset parsing, path canonicalization, answer mapping, and asset-manifest line rendering used by contract preparation |
| `Worktree` helpers | `core/worktree.py` | Shared low-level git/worktree operations used by run, review, cleanup/delete, and init CLI surfaces; command-specific state mutation stays in the owning command layer |
| `TaskState` | `core/state.py` | Single source of truth; persisted as JSON after every agent operation |
| `JsonStateStore` | `core/state.py` | Stores each task as `<task_id>.json` in the configured state dir; serializes same-process access and writes via temp-file replacement so heartbeat updates and audit saves cannot interleave partial JSON writes |
| `sikula_cli` modules | `sikula_cli/*.py` | Focused CLI command wrappers, parser registration helpers, and CLI config discovery/path helpers; `sikula.py` remains the public entrypoint and compatibility surface |

---

## Run flow (`sikula run`)

`sikula run` parser registration and command flow live in `sikula_cli/run.py`.
`sikula.py` keeps a compatibility wrapper for existing imports and tests. The
handler wraps `Orchestrator.run()` with worktree setup, finalization, contract
preflight, asset audits, and resume logic.

**New task (`--task-file`):**

```
cmd_run()
   │
   ├─ guard: for isolated runs, loaded config, guidelines.context_files,
   │     and extra_rules for enabled planner/reviewer/security_reviewer/test_writer phases
   │     files must exist as file blobs at the worktree start ref, be tracked,
   │     and be clean relative to HEAD; otherwise
   │     fail before creating TaskState/worktree
   │
   ├─ read task file  →  TaskState created (state.task_id = uuid4().hex)
   │     + warning-only implementation contract snapshot stored in state
   │     + optional contract readiness gate may mark state failed before worktree
   │
   ├─ isolation (default on, skip with --no-isolate):
   │     git worktree add .sikula/worktrees/<task_id>  -b sikula/<stem>-<task_id>
   │     copy BuildTool.env_files() from original project root to worktree
   │     (e.g. local.properties for AndroidGradleTool)
   │     state.worktree_path / worktree_base / worktree_branch set
   │
   ├─ Orchestrator.run(task_id)
   │
   ├─ state.done = True →
   │     _finalize_worktree(): auto-commit + remove worktree
   │
   └─ state.done = False →
         worktree preserved at .sikula/worktrees/<task_id>/
```

**Resume (`--task-id`):**

```
cmd_run()
   │
   ├─ load existing TaskState
   ├─ if state.worktree_path set: verify worktree exists, set cwd
   └─ Orchestrator.run(task_id)
         └─ idempotency guards in each phase skip already-completed phases
```

**`--reset-failed`** (requires `--task-id`): clears `state.failed`, resets iteration counters, clears error blobs, and auto-populates `state.files_changed` from `git diff` if empty (recovers from false-negative change detection).

**`--no-isolate`**: skips worktree creation; changes land as uncommitted working-tree modifications in the original project root. No branch is created. A git repository is still required — git is used to detect which files the agent changed.

**Cleanup/delete commands:** `sikula cleanup` and `sikula delete` parser
registration and handlers are implemented by `sikula_cli/cleanup.py` with
compatibility wrappers in `sikula.py`.

```
sikula cleanup <task_id>          # dry run
sikula cleanup <task_id> --force  # remove worktree, keep state JSON
sikula delete <task_id>           # dry run
sikula delete <task_id> --force   # remove worktree and state JSON
```

Both commands refuse dirty worktrees unless `--discard` is passed. `cleanup` records a
`history` entry and clears `worktree_path` / `worktree_base`, preserving the state for
audit while making resume impossible; it also removes transient internal recovery snapshots.
`delete` removes the state JSON and internal snapshots after worktree cleanup. Forced
cleanup/delete also refuse to remove a worktree that contains the current process directory,
so a user's shell is not left inside a deleted tree. Shared git/worktree primitives such
as root detection, dirty checks, path containment, worktree removal, current-branch
inspection, and commit resolution live in `core/worktree.py`; `sikula.py` keeps
compatibility wrappers for existing tests and command contexts.

**Status/show commands:** `sikula status` and `sikula show` parser registration
and handlers are implemented by `sikula_cli/status.py` with compatibility
wrappers in `sikula.py`. `sikula status`
derives a compact task overview from state JSON. It reports terminal states
(`DONE`, `FAILED`, `CLEANED`), interrupted runs whose recorded PID is no longer
alive, current pipeline phase, planner step progress, build/fix iteration count,
and last update age. `--verbose` adds the next useful command for each row,
`--json` emits the same derived rows for scripts, and `--active` / `--failed` /
`--cleaned` / `--done` filter the list. When invoked inside a preserved task worktree,
config auto-discovery maps back to the original project root so status reads the original
`.sikula/state` directory instead of the worktree copy. Fresh `sikula run TASK_FILE`
is refused from inside a task worktree to avoid accidentally starting a new task from
the original checkout while reading the task file from the preserved worktree. Resume
via `sikula run --task-id <task_id>` is supported from inside the worktree; before
finalization Sikula switches the process directory back to the original project so the
worktree can be removed safely.

**Contract check and preparation commands:** `sikula contract check TASK_FILE` is a
read-only preflight whose CLI parser registration and handler logic live in
`sikula_cli/contract.py`; the contract scoring itself is implemented by
`core/contract_check.py`. It parses the task file, scores whether it is specific enough
to act as an implementation contract, reports gaps and stable clarifying question IDs,
and can emit the same result as JSON. By default it does not write files; with
`--write-report` it writes an explicit
`.sikula/contract-reports/*.check.json` report and matching `.answers.yaml` template for
follow-up answers. `sikula contract prepare TASK_FILE --answers ... --output ...`
uses the same `sikula_cli/contract.py` command module and turns the task
description into the delivery artifact by applying answers,
preserving product sections, adding project context plus validation commands from the
effective project config, and rechecking the returned Markdown. It refuses stale answer
hashes, accidental overwrites, and task-description input that already contains the
reserved `## Asset manifest` section. `--interactive` is a terminal convenience layer for
both refine and prepare: it creates or reuses an answers YAML, prompts for answers, saves
that file under `.sikula/contract-reports`, and then writes the clean Markdown output.
`sikula task refine TASK_FILE --output ...` and
`sikula task attach TASK_FILE ASSET_FILE` parser registration and handler logic live in
`sikula_cli/task.py`. `task refine` prepares the product brief side of the flow: it can
normalize a product request into task-description Markdown and ask product-level
clarifying questions, but it does not evaluate Sikula delivery readiness or return run
guidance. `task attach` copies local reference or delivery assets into the configured task
asset directory and optionally appends the generated Markdown snippet to the task file.
The core module also exposes side-effect-free in-memory helpers
(`prepare_task_description()`, `improve_contract_text()`, and
`prepare_implementation_contract()`) so chat/MCP adapters can reuse the same scoring,
question, answer-application, and recheck logic without temporary YAML files or duplicate
business rules. `prepare_implementation_contract()` returns the authoritative delivery
workflow state plus user-facing questions, safe `.sikula/contracts/*.contract.md` path
hints, resume arguments, revised-answer markers, and next-step guidance; adapters should
not infer readiness separately. Chat/MCP callers must provide effective project context,
especially validation commands, before the core result can report `ready_to_run=true`;
client-reported local config presence is guidance only and is not a readiness signal.
`core.contract_prepare_adapter` maps those core results into stable
`prepare_task_description` and `prepare_implementation_contract` response shapes for
future MCP transport without adding scoring or rewrite logic; task-description responses
do not expose implementation-contract readiness fields. Contract preparation helpers
capture local task assets in the prepared contract manifest as path/hash snapshots.
Asset detection treats structured `## Assets` declarations as authoritative for product
task descriptions; paths in prose or configured task-asset directories are reported only
as undeclared-path warnings unless a matching structured declaration exists. `## Asset
manifest` is reserved for prepared implementation contracts. Task-description validation
reports a blocking format gap for that section, while implementation-contract preflight
(`sikula contract check` and `sikula run`) reads it as the prepared manifest. Runtime hash
mismatch handling is warning-only: fresh runs and resumes record asset drift audit entries
instead of blocking delivery by default. These commands and helpers
do not create `TaskState`, start agents, create worktrees, or alter `review` flow. Fresh `sikula run
TASK_FILE` uses the same deterministic checks to store a compact warning-only snapshot in
`TaskState.implementation_contract` and print a one-line summary before agents start; it
does not write `.sikula/contract-reports` artifacts. Fresh task-file runs can opt into strict
pre-agent gating with `--require-contract-ready` or `--min-contract-score N`; the gate
runs only after the snapshot has been saved and before worktree creation or orchestrator
startup. A failed gate marks the task state failed, stores the effective run
`config_snapshot`, and records an audit history entry, but does not start agents. Because
no worktree or branch exists yet, that failed state is not resumable via `--reset-failed`;
prepare the implementation contract and start a fresh task-file run instead. `resume`, `review`,
and `review --fix` reuse existing task state instead of recomputing or re-gating the
check. Review mode uses the existing branch diff as the
primary review artifact; review-context readiness is a separate concern and should not
reuse the delivery contract-readiness gate. When a Sikula config is available, it reuses
`core.validation_coverage` to compare task-described validation commands with the
effective build/test/check phases that `sikula run TASK_FILE` would execute from the same
config. Disabled phases do not count as validation coverage; without config it still runs
the task-content checks and leaves validation coverage empty. Answers templates are
task-hash scoped: when the task content hash changes, existing filled answers are retained
only under `previous_answers`, while active `answers` are reset for the new hash so future
`contract prepare` or run preflight logic does not treat stale answers as authoritative.

**Delivery plan prepare command:** `sikula delivery prepare TASK_FILE` is an
authoring and deterministic source-artifact writing command. Ownership is split
between the CLI wrapper in `sikula_cli/delivery.py`, the assistant in
`agents/delivery_preparation_agent.py`, parser and path derivation helpers in
`core/delivery_authoring.py`, and writer, rollback, readiness, and generated-plan
validation helpers in `core/delivery_prepare_writer.py`. The CLI validates the
source task and output paths, derives the selected plan ID, validates
`delivery_preparer` model/provider/timeout overrides, then calls
`DeliveryPreparationAgent`, which uses `LLMClient.generate()` with a
command-free prompt assembled from the task and checked-in project context.
The assistant must return one strict structured draft, which
`core/delivery_authoring.py` parses without side effects.
`core/delivery_prepare_writer.py` then derives artifact
paths, checks unit task readiness in memory, writes `plan.yaml` and
`units/<unit-id>.md` transactionally, validates the generated plan, and rolls
back on readiness, validation, write, or filesystem failure. Existing artifacts
are refused by default; `--force` may replace ordinary plan/unit files inside
the selected output directory, while absolute output paths, parent traversal,
symlink artifacts, symlink escapes, path collisions, outside-project writes,
Git metadata, and Sikula runtime/debug artifact directories are rejected. `delivery prepare`
is source-artifact-only: it does not create `TaskState`, worktrees, child runs,
nested Sikula commands, command tools, delivery progress, branches, or progress
mutations. Raw prompts and provider output are stored only in local preparation
audit artifacts at
`.sikula/contract-reports/<task-stem>.delivery-prepare.auto-llm.jsonl`.
Ordinary text and JSON output is an explicit allowlisted projection and must not
embed source task bodies, unit Markdown bodies, prompts, raw provider output,
diffs, logs, or task state.
Delivery authoring may include optional unit sizing metadata (`estimated_size`,
`risk_tags`, and advisory `budget` fields). This metadata guides authoring,
plan validation warnings, status display, and future explicit execution policy;
it must not silently weaken review, security, or validation gates.

**Delivery plan check command:** `sikula delivery check PLAN_FILE` is the first
delivery-plan MVP primitive. Its CLI wrapper lives in `sikula_cli/delivery.py`;
deterministic validation is implemented by `core/delivery_plan.py`. It validates
tracked `.sikula/delivery/<slug>/plan.yaml` files without creating
`TaskState`, starting agents, creating worktrees, preparing contracts, or
updating branches. The validator checks schema version, required plan metadata,
delivery unit IDs, unit task paths, dependency references/cycles, optional stream
references, optional monorepo component metadata, unit scope paths, optional
unit sizing/risk/budget metadata, and the MVP single-repository boundary.
Component, scope, sizing, risk, and budget fields are metadata only: they are
preserved for JSON consumers and delivery-console grouping, but they do not
change `sikula run` behavior, provider access, validation command scope, review
or security coverage, or worktree creation. If `repositories` is omitted, the
plan is treated as one implicit repository with `id: main` and `root: .`;
multi-repo plans are rejected until cross-repo execution semantics are added.
The checker emits warnings, not errors, when risk tags indicate that one unit
combines several independent high-risk delivery surfaces and should likely be
split before execution.

**Delivery plan status command:** `sikula delivery status PLAN_FILE` is a
read-only parent-progress view. Its CLI wrapper lives in
`sikula_cli/delivery.py`; progress derivation is implemented by
`core/delivery_progress.py`. It reuses delivery plan validation, then reads
ignored progress from
`.sikula/state/delivery/<plan-id>/progress.json` when present. Missing progress
is normal before the first unit runs; status derives pending units and dependency
blockers from the tracked plan. The JSON result is allowlisted metadata only: it
does not embed unit task bodies, raw child `TaskState`, prompts, provider output,
logs, diffs, or validation output. Unit sizing/risk/budget metadata from the
tracked plan is preserved in status output for operator review.

**Delivery progress mutation foundation:** `core/delivery_progress.py` also owns
the non-agent primitives that future delivery execution uses: atomic
`progress.json` writes, append-only `events.jsonl` records, mutation locks, unit
progress upserts, and deterministic next-unit selection from the status model.
These helpers are intentionally privacy-safe and allowlisted. They do not create
worktrees, prepare contracts, start agents, or update branches by themselves.

**Delivery run-next dry run:** `sikula delivery run-next PLAN_FILE --dry-run`
loads project runtime config, validates delivery status, and reports the first
eligible unit that a future execution command would run. It also mirrors the
execution dependency result-commit guard so a dry run reports blocked when a
completed prerequisite commit is not applied to the current checkout. It is
intentionally side-effect-free: it does not write parent progress, create child
task state, prepare contracts, create worktrees, start agents, or update
branches. Its JSON result is also allowlisted metadata only.

**Delivery run-next execution:** `sikula delivery run-next PLAN_FILE` acquires
the delivery progress lock, selects one eligible pending unit, records it as
`running`, and invokes the existing `sikula run` command path for the unit task
file. The child run keeps all normal Sikula behavior: contract preflight,
worktree isolation, provider execution, validation, review, state persistence,
and task audit reporting. `run-next` accepts the same per-agent
`--agent-model`, `--agent-provider`, and `--agent-timeout` overrides as
`sikula run` and forwards them to this child run; the parent delivery progress
model does not store those prompt/provider settings. When the child run exits,
delivery progress stores only compact parent metadata: unit status, child task
id, branch, result commit when available, timestamps, and a failure code. Full
prompts, provider output, diffs, logs, validation records, and task state remain
in the child task state.
The parent delivery unit is `done` only when the child exits successfully, the
child `TaskState` is done, and the child result is finalized. Finalization means
either a `result_commit` exists or the child left no preserved worktree to
deliver. A done child task with a preserved worktree but no result commit is
recorded as `failed` with `child_run_unfinalized`.
Before starting a selected unit, execution walks the selected unit's dependency
closure and verifies that every completed prerequisite result commit is already
an ancestor of the current checkout. Completed no-op prerequisites have no
commit to verify, but their own prerequisites are still checked.
The execution MVP runs one unit at a time. It intentionally does not assemble
the plan's final delivery branch automatically.

**Delivery final branch command:** `sikula delivery finalize PLAN_FILE` is the
explicit final branch assembly step for a completed delivery plan. Its CLI
wrapper lives in `sikula_cli/delivery.py`; deterministic preflight and Git ref
updates are implemented by `core/delivery_finalize.py`. Finalize requires the
plan status to be `done`, verifies that each completed unit result commit is
applied to the current checkout, then uses the current `HEAD` as the final
commit candidate and creates or fast-forwards `final_branch`. Existing diverged
final branches are rejected; Sikula does not force-update them. The command
records only compact parent metadata in delivery progress: final branch, final
commit, finalized timestamp, and an append-only `plan.finalized` event. It does
not embed child task state, prompts, provider output, diffs, logs, or validation
records. Any later unit progress update clears finalization metadata because the
recorded final branch is a snapshot of a specific completed unit set.
`--dry-run` performs the same preflight without mutating Git refs or progress.

---

## Execution flow (`Orchestrator.run()`)

The orchestrator's main loop. Called by `cmd_run()` after worktree setup, or directly by `cmd_review()` in fix mode.

```
task.md
   │
   ▼
Orchestrator.run()
   │
   ├─ terminal guard: if state.done or state.failed → return immediately (resume-safe)
   │
   ├─ Phase 0: presync  (only when run_presync: true; skipped if state.presync_done)
   │     BuildTool.generate_sources()  [configurable via build.presync_task; default: generateDebugSources]
   │     success → state.presync_done = True
   │     failure → state.presync_done = True (warning logged; analyst proceeds anyway)
   │     purpose → ensures OpenAPI DTOs and other build-generated sources exist in build/
   │               before the analyst reads the codebase
   │
   ├─ Phase 1: analyze  (skipped if state.implementation_prompt already set)
   │     AnalystAgent  [single-pass]
   │       reads  → task_description + guidelines (guidelines.context_files in YAML)
   │       calls  → LLMClient.run_readonly_agent(prompt, cwd=project_root)
   │       writes → state.analyst_prompt (assembled prompt — includes guidelines content)
   │                state.implementation_prompt
   │
   ├─ Phase 1.5: plan  (only when run_planner: true; skipped if state.plan_decided already set)
   │     PlannerAgent  [generate call — no codebase access]
   │       reads  → state.implementation_prompt
   │       calls  → LLMClient.generate(system, user)
   │       decides → SINGLE_PASS: state.plan stays empty → single-pass flow
   │              → numbered list: state.plan populated → step loop
   │
   ├─ if state.plan non-empty → step loop (see below)
   └─ if state.plan empty    → single-pass (phases 2-5 once)
```

**Single-pass flow** (`state.plan` is empty — when `run_planner: false` or planner outputs `SINGLE_PASS`):

```
   ├─ Phase 2: implement  (skipped if state.files_changed already set)
   │     ImplementerAgent
   │       reads  → state.implementation_prompt + sandbox allowed_write_paths
   │       reads  → state.review_issues (if non-empty: review fix mode)
   │       calls  → LLMClient.run_agent(prompt, cwd=project_root)
   │                 the agent navigates the codebase using its file tools
   │                 and makes changes directly — no file content in prompt
   │       detects → changed files via git diff before/after agent call
   │       writes → state.files_changed
   │       guard  → aborts if no files changed (initial pass only)
   │
   ├─ Phase 3: review loop  (only when run_review: true; skipped if review_approved)
   │     ┌─────────────────────────────────────────────────────────┐
   │     │  ReviewerAgent  [read-only]                             │
   │     │    reads  → state.implementation_prompt                 │
   │     │    reads  → state.files_changed + git diff HEAD         │
   │     │    calls  → LLMClient.run_readonly_agent(prompt, cwd)   │
   │     │    writes → state.review_approved = True   ──────────── ┼─► proceed to Phase 3.5
   │     │          or state.review_issues (list of problems)      │
   │     │                                                         │
   │     │  if issues found:                                       │
   │     │      ImplementerAgent  (review fix pass)                │
   │     │        reads  → state.review_issues appended to prompt  │
   │     │        applies fixes, updates state.files_changed       │
   │     │        failure → state.failed = True, task aborted      │
   │     │        resets → state.tests_up_to_date = False          │
   │     │                                                         │
   │     │  repeat until approved or review_iterations ≥          │
   │     │              config.max_review_iterations               │
   │     │  → timeout: state.failed = True, task aborted          │
   │     │  review_iterations resets to 0 after each fixer pass   │
   │     └─────────────────────────────────────────────────────────┘
   │
   ├─ Phase 3.5: security review  (only when run_security_review: true; independent of run_review; skipped if security_approved)
   │     ┌─────────────────────────────────────────────────────────┐
   │     │  SecurityReviewerAgent  [read-only]                     │
   │     │    reads  → state.implementation_prompt                 │
   │     │    reads  → state.files_changed + git diff HEAD         │
   │     │    calls  → LLMClient.run_readonly_agent(prompt, cwd)   │
   │     │    APPROVED → state.security_approved = True ────────── ┼─► proceed to Phase 4
   │     │    warnings only → state.security_approved = True       │
   │     │    blocking issues → state.review_issues populated      │
   │     │    unexpected output (no APPROVED/warnings/issues) ─────┼─► treated as blocking
   │     │                  → state.review_issues populated        │
   │     │                                                         │
   │     │  if blocking issues:                                    │
   │     │      ImplementerAgent  (security fix pass)              │
   │     │        reads  → state.review_issues appended to prompt  │
   │     │        applies fixes, updates state.files_changed       │
   │     │        failure → state.failed = True, task aborted      │
   │     │      review loop re-runs (Phase 3)                      │
   │     │      security review re-runs                            │
   │     │                                                         │
   │     │  repeat until approved or security_review_iterations ≥  │
   │     │              config.max_security_review_iterations      │
   │     │  → timeout: state.failed = True, task aborted          │
   │     └─────────────────────────────────────────────────────────┘
   │
   ├─ Phase 4: test write  (only when run_test_writing: true; skipped if tests_up_to_date)
   │     TestWriterAgent
   │       reads  → state.implementation_prompt + git diff HEAD + changed files
   │       calls  → LLMClient.run_agent(prompt, cwd=project_root)
   │       write scope → prompt restricts writes to sandbox.allowed_test_write_paths
   │       writes → test files; updates state.files_changed
   │       sets   → state.tests_up_to_date = True on success
   │
   └─ Phase 5: build / fix loop  (only when run_build: true)
         │
         ▼
      ┌──────────────────────────────────────────────────────┐
      │  if not state.build_synced:                          │
      │      BuildTool.sync()           [platform-specific]  │
      │      known sync outputs → adopt + audit              │
      │          → append state.files_changed                │
      │          → stale review/security/test-writer gates   │
      │      unexpected repo artifacts → clean + audit        │
      │                                → cleanup failure/fix  │
      │      success → state.build_synced = True             │
      │      failure → state.errors.append(output)           │
      │             → fix phase (same as build failure)      │
      │             → continue (will re-sync next iteration) │
      │                                                       │
      │  BuildTool.compile_check()      [platform-specific]  │
      │      unexpected repo artifacts → clean + audit        │
      │                                → cleanup failure      │
      │                                  enters fix phase     │
      │      └ failure → state.errors.append(output)         │
      │               → fix phase (see below) → continue     │
      │                                                       │
      │  if run_tests: true                                   │
      │      BuildTool.run_tests()      [platform-specific]  │
      │          unexpected repo artifacts → clean + audit    │
      │                                    → cleanup failure  │
      │                                      fix              │
      │          └ failure → state.test_errors.append(output)│
      │                   → fix phase (see below) → continue │
      │                                                       │
      │  if run_checks: true                                  │
      │      for each check in build.checks:                  │
      │          BuildTool.run_check(name, task_config)       │
      │          unexpected repo artifacts → clean + audit    │
      │                                    → cleanup failure  │
      │                                      fix              │
      │          └ failure → state.check_errors.append(out)  │
      │                   → fix phase (see below) → continue │
      │                                                       │
      │  state.done = True  ──────────────────────────────── ┼─► END
      │                                                       │
      │  fix phase (shared for build, test, and check        │
      │  failures):                                           │
      │      FixerAgent                                       │
      │        reads  → state.errors + state.test_errors     │
      │               + state.check_errors                    │
      │        calls  → LLMClient.run_agent(prompt, cwd)     │
      │        detects → changed files via git diff           │
      │        appends → state.files_changed                  │
      │        clears  → state.errors, state.test_errors      │
      │                                                       │
      │      if fixer changed any build-config file:          │
      │        BuildTool.is_build_config_file(path)           │
      │          state.build_synced = False                   │
      │                (triggers re-sync next iteration)      │
      │                                                       │
      │      if fixer changed files:                          │
      │        state.fixer_changed_code = True                │
      │        if every changed file is under                 │
      │        sandbox.allowed_test_write_paths and           │
      │        has a recognized test artifact path/name:      │
      │          preserve review/test-writer gates,           │
      │          invalidate security review                   │
      │        else:                                          │
      │          state.review_approved = False                │
      │          state.security_approved = False              │
      │          state.tests_up_to_date = False               │
      │        continue build/test/check validation first     │
      │                                                       │
      │      after build/test/check are green again:          │
      │        review loop reruns (Phase 3)                   │
      │        security review reruns (Phase 3.5)             │
      │        test write reruns (Phase 4)                    │
      │        if those gates changed files → continue        │
      │        build/test/check validation before accepting   │
      │                                                       │
      │  repeat until state.done or current build/fix loop   │
      │              reaches config.max_iterations            │
      │  if the last allowed fixer pass changed files:        │
      │      run one final validation-only pass; failure      │
      │      aborts without another fixer attempt             │
      └──────────────────────────────────────────────────────┘
```

**Step loop** (`state.plan` is non-empty — when `run_planner: true` and plan parsed successfully):

Per-step flags (`step_implemented`, `review_approved`, `review_issues`, `review_iterations`, `security_approved`, `security_review_iterations`, `tests_up_to_date`) reset on each step transition. `files_changed` and `build_iterations` accumulate across all steps. `max_iterations` is applied per active build/fix loop, not globally across the whole task, so per-step builds do not consume the final full-task build budget.

Build behaviour is controlled by `run_build_per_step` (default: `false`):

| `run_build_per_step` | Build timing |
|---|---|
| `false` (default) | Build/fix loop runs **once after the last step** — avoids repeated builds while the task is still being assembled |
| `true` | Build/fix loop runs after **each individual step**, and the final full-task build/fix loop still runs after all steps complete — use when you want every step physically built |

Planner steps are still expected to be compile-safe after all preceding steps: a step
must include immediate dependencies for anything it introduces or references (for example
resource or localization keys, route/API/command constants, service registrations,
interface contracts and implementations).
Deferred build is a performance choice, not permission for obviously uncompilable steps.

```
   for step in state.plan:
       → state.history: {"agent": "orchestrator", "action": "step_start", "result": "Step N/M: <desc>"}
       Phase 2: implement  (skipped if state.step_implemented)
         prompt includes "CURRENT STEP N/M: <description>" — agent focuses on this step only
       Phase 3: review loop    (same as single-pass; prompt includes current-step scope)
       Phase 3.5: security review  (same as single-pass; prompt includes current-step scope)
       Phase 4: test write     (same as single-pass)
       Phase 5: build/fix loop  (only if run_build_per_step: true)
       → state.history: {"agent": "orchestrator", "action": "step_done", "result": "Step N/M"}
       → advance state.current_step
   → after last step: state.plan_completed = True
   → final full-task review/security/test gate
      prompt includes "FINAL FULL-TASK ..." scope — agents run one whole-task pass
      against the original task and do not restrict findings to the last step
   → final build/fix loop (if run_build: true)
   → on success state.done = True
```

The `step_start` / `step_done` markers in `state.history` make the JSON audit log unambiguous — every agent action between a pair of markers belongs to that step.

After the last step completes, `plan_completed` guards resume so the final step is not
re-run just because the final review/build phase was interrupted. The final full-task
gate resets review/security/test-write flags and reruns those agents over the complete
planned task. If the final build/fix loop later invokes `FixerAgent`, the follow-up
review, security review, and test writer also run with `active_scope = "final_full_task"`;
they are not scoped to the last planned step. When `run_build_per_step: true`, step-local
build/fix reviews remain step-scoped while they are still inside `_run_single_step()`;
the final full-task gate still runs after all steps complete.

Note: `step_start` is recorded at the top of the while loop, before idempotency checks. On resume, the current step gets a second `step_start` entry in the history; this is harmless — all idempotency guards still work correctly.

---

## Review flow (`sikula review`)

`cmd_review()` in `sikula_cli/review.py` (with a compatibility wrapper in `sikula.py`). Isolates an existing branch in a git worktree, runs the reviewer and security reviewer against a PR-style diff, and exits with a summary. Report-only review uses the initial computed diff; `review --fix` refreshes the diff as fixes are made.

**Setup (both modes):**

```
cmd_review()
   │
   ├─ guard: --description or --description-file is required
   │     (review scope must be explicit; no generated fallback)
   │
   ├─ guard: guidelines.context_files and configured review prompt overlays
   │     must exist as file blobs at the review worktree start ref, be tracked,
   │     and be clean before worktree creation. Report-only review checks reviewer rules
   │     plus security reviewer rules when security review is enabled;
   │     review-fix also checks test_writer.extra_rules when test writing is enabled.
   │
   ├─ worktree creation (differs by mode):
   │    report-only: git worktree add --detach .sikula/worktrees/<task_id> <sha>
   │                 (detached HEAD — works even when caller is on the reviewed branch)
   │    --fix --branch:
   │                 git worktree add .sikula/worktrees/<task_id> <branch>
   │                 (real branch checkout — _finalize_worktree commits to <branch>)
   │                 + copy gitignored build files via BuildTool.env_files()
   │                   (e.g. local.properties on Android — same as cmd_run())
   │    --fix --current-branch:
   │                 guard: current branch is named, current worktree is clean,
   │                        base ref resolves, and HEAD resolves
   │                 git worktree add --detach .sikula/worktrees/<task_id> <start_head>
   │                 (detached isolated worktree — avoids checking out the active branch twice)
   │                 + copy gitignored build files via BuildTool.env_files()
   │
   ├─ git diff <base_branch>...<target>  →  state.review_diff
   │    (initial three-dot diff: all commits introduced by branch)
   │
   ├─ git diff --name-only <base_branch>...<target>  →  state.files_changed
   │
   ├─ guard: no files changed → worktree removed, exit 0
   │
   └─ TaskState created with:
        implementation_prompt = description   (PR description serves as task context)
        plan_decided          = True          (planner always skipped)
        review_diff           = <initial PR-style diff>
        review_mode           = "review_fix" or "review_report"
        review_base_branch    = <base_branch>
        files_changed         = <list of changed files>
        current-branch fields = populated only for --fix --current-branch
```

After state creation, `cmd_review()` asks a read-only enrichment agent to inspect the
review description for local filenames such as screenshots, mockups, specs, PDFs, or
spreadsheets. Found files are appended to `state.implementation_prompt` under
`Files referenced in the task` so the reviewer, security reviewer, and `--fix` agents
share the same PR context. The enrichment prompt uses the explicit
`NO_REFERENCED_FILES` sentinel for the normal "nothing to inline" case; Sikula converts
that sentinel to no extra context and leaves `implementation_prompt` as the original
description. Enrichment failures are non-fatal and only skip this optional context.

**Report-only mode (default — no `--fix`):**

Orchestrator is **not** used. Agents are instantiated and called directly.

```
   ReviewerAgent  [read-only; diff from state.review_diff]
      │
      ├─ approved → SecurityReviewerAgent  (only if run_security_review)
      │                  ├─ approved → state.done = True
      │                  └─ issues   → state.failed = True
      │
      └─ issues → state.failed = True

   worktree removed on completion, provider failure, or interruption
   (including during optional enrichment)
   exit 0 (approved) or 1 (issues found)
```

Report-only review state is retained for audit. If the reviewer/security-reviewer
agent or optional referenced-file enrichment raises unexpectedly, `cmd_review()` marks
the state failed, clears the active operation, records the failure and cleanup, and
removes `worktree_path` / `worktree_base` after the detached worktree is removed.
Report-only review records the live Sikula process PID so `sikula status` can keep
showing `wait` while the review process is still running, including before progress
heartbeats start or when they are disabled. Once stale, report-only review is not reset
or resumed through `sikula run --task-id`; operators start a fresh review with
`sikula review`.

**Fix mode (`--fix`):**

Full `Orchestrator.run()` loop with these forced overrides:
- `run_planner: False` — always disabled; preserved on resume
- `run_review: True` — always enabled; preserved on resume
- `run_security_review` — resolved from CLI/config on the initial review run; reused from
  `state.config_snapshot` on resume unless the user passes `--security-review` /
  `--no-security-review`
- All other phases (`run_build`, `run_tests`, `run_checks`, …) follow project config

When `TestWriterAgent` changes test files during `--fix`, the orchestrator runs one
final reviewer/security-reviewer validation pass over the refreshed diff. That pass is
validation-only: if it rejects the test-writer changes, the task fails instead of
starting another review → fix → test-write cycle.

For `--fix --current-branch`, the current checkout is the review and delivery
target but not the writeable agent workspace. Sikula records the target branch
and starting commit in task state, runs the normal review-fix loop in the
detached isolated worktree, commits fixes there, then revalidates the original
checkout before delivery. Delivery requires the operator to still be on the
target branch, the current worktree to be clean, and `HEAD` to equal the
recorded starting commit. If those checks pass, Sikula runs `git merge
--ff-only <isolated_fix_commit>` from the original checkout. If they fail, or
the fast-forward fails, `review_delivery_status` becomes `"failed"` and the
isolated worktree is preserved for inspection or retry. A pending or failed
current-branch delivery is retried with `sikula run --task-id <task_id>` without
rerunning the agents; terminal failed tasks still require `--reset-failed`.

```
   Orchestrator.run()
      │
      ├─ state.done = True, files changed →
      │     --branch:
      │        git commit to <branch>: "sikula: review fixes for <branch>"
      │        worktree removed
      │     --current-branch:
      │        git commit in detached isolated worktree
      │        verify original checkout branch/cleanliness/start HEAD
      │        git merge --ff-only <isolated_fix_commit>
      │        worktree removed after delivery
      │
      ├─ state.done = True, no files changed →
      │     --branch: worktree removed ("no fixes needed")
      │     --current-branch: verify original checkout branch/cleanliness/start HEAD,
      │                       mark review_delivery_status = "no_changes",
      │                       then remove worktree
      │
      └─ state.done = False (failure) →
            worktree preserved at .sikula/worktrees/<task_id>/
            interrupted tasks resume via sikula run --task-id <task_id>
            terminal failed tasks resume via sikula run --task-id <task_id> --reset-failed
```

**Key differences from `cmd_run()`:**

| | `cmd_run()` | `cmd_review()` |
|---|---|---|
| Branch | New: `sikula/<stem>-<task_id>` | Existing: `--branch` |
| Diff source | `GitTool.diff_head()` (live, per agent call) | PR-style diff stored in `state.review_diff`; report-only keeps the initial `git diff base...branch`, `review --fix` refreshes it before reviewer/security-reviewer calls |
| Task context | Analyst output → `implementation_prompt` | PR description as both `task_description` and `implementation_prompt` |
| Planner | Per config | Always disabled (`plan_decided = True` on state creation) |
| Resume | Supported via `--task-id` | Report-only review is not reset or resumed; `review --fix` uses regular task resume (`--reset-failed` required for terminal failed state) |
| Report-only path | No | Yes — bypasses orchestrator entirely |

In current-branch review-fix mode, the `cmd_review()` branch column is the
operator's current branch, but the isolated task worktree is detached at the
recorded starting commit. This preserves the same inspect/resume boundary as
other Sikula worktree runs while avoiding a second checkout of the active
branch.

---

## Init flow (`sikula init`)

`sikula init` parser registration and command flow live in `sikula_cli/init.py`.
`sikula.py` keeps compatibility wrappers for existing imports and tests. The command
scans the project, generates `.sikula/config.yaml`, and optionally generates
`.sikula/guidelines.md` via `InitAgent`.

```
cmd_init()
   │
   ├─ if .sikula/config.yaml exists and --guidelines is passed without --force:
   │     preserves the existing config
   │     generates .sikula/guidelines.md
   │     minimally adds .sikula/guidelines.md to guidelines.context_files if missing
   │     returns
   │
   ├─ scanner.detect_build_tool(project_root)  →  build_tool name
   ├─ scanner.detect_language(project_root)    →  language string
   ├─ scanner.detect_platform(project_root)    →  platform string
   ├─ scanner.scan_source_paths(project_root)  →  allowed_write_paths
   │
   ├─ sikula_cli.init.generate_config():
   │     builds config dict from detected values + project name
   │     fills in appropriate build: block for detected build_tool
   │     writes .sikula/config.yaml
   │
   └─ if --guidelines flag:
         tech_stack = "<language>/<platform>" or just "<language>"
         InitAgent.generate_guidelines(project_root)
           calls LLMClient.run_readonly_agent(prompt, cwd=project_root)
           agent browses codebase, reads source files, returns cleaned Markdown
         writes cleaned Markdown to .sikula/guidelines.md
```

The final output reminds the user to commit `.sikula/config.yaml` and `.sikula/.gitignore`
before the first isolated run. If guidelines were generated, `.sikula/guidelines.md` is
listed as part of the suggested commit too. Isolated worktree creation enforces the loaded
config for task runs, every file referenced by `guidelines.context_files`, and every
`planner.extra_rules`, `reviewer.extra_rules`, `security_reviewer.extra_rules`, or
`test_writer.extra_rules` file consumed by an enabled phase in that workflow so worktrees
cannot silently run with stale or missing prompt policy. Review worktrees also require the
prompt context to be present as files in the reviewed branch or captured start commit.

**CLI flags:**
- `--guidelines` — trigger guidelines generation via `InitAgent`
- `--provider` — LLM provider for the `InitAgent` call; for an existing config, falls back to `llm.provider`
- `--model` — LLM model for the `InitAgent` call; for an existing config, falls back to `llm.model`

---

## Agents

### PlannerAgent (`agents/planner_agent.py`)

Runs after `AnalystAgent`, before `ImplementerAgent`. Only active when `run_planner: true`.

**Mechanism:** calls `LLMClient.generate(system, user)` — no codebase access needed because
the input is purely the `implementation_prompt` text produced by the analyst.

**Input:**
- `state.implementation_prompt` — the full structured prompt from the analyst

**What it does:** acts as a triage agent — first decides whether splitting adds value, then
either signals single-pass or produces an ordered list of steps.

Output is one of:
- `SINGLE_PASS` — task is focused enough for one pass; `state.plan` stays empty
- A numbered list of 2 to `planner.max_steps` steps — each compilable in isolation

When producing steps, the planner keeps compile dependencies with the step that first
uses them. For example, a step that references a new localization key, route/API
constant, service registration, or interface method must also create/update that
dependency in the same step. If that makes the split unclear, planner should choose
`SINGLE_PASS` or merge the coupled work into one step.

**Output written to state:**
- `state.planner_prompt` — full assembled prompt sent to the planner LLM (system + user sections); stored before the LLM call
- `state.planner_retry_records` — rejected over-limit planner outputs, including parsed step count, configured max, rejected output, and retry prompt when another attempt follows
- `state.plan_decided = True` — set after every successful decision; guards re-run on resume
- `state.plan` — list of step description strings (only set when splitting; stays empty for SINGLE_PASS)
- `state.current_step = 0` — reset to start (only when splitting)

**Fallback and retry:** if the output is neither `SINGLE_PASS` nor parseable into 2+ numbered steps,
`state.plan` stays empty, `state.plan_decided` is still set, and the orchestrator uses single-pass behavior.
If the output parses into more than `planner.max_steps` steps, Sikula rejects the output and retries once with a stricter format prompt. If the retry is still over the limit, `plan_decided` remains false and the orchestrator fails before implementation starts. Because the task is then in terminal `failed` state, retrying that planner phase requires the normal `--reset-failed` path.

---

### AnalystAgent (`agents/analyst_agent.py`)

**Single-pass design** — guidelines + codebase exploration → implementation prompt.

**Mechanism:** calls `LLMClient.run_readonly_agent(prompt, cwd=project_root)`.
The LLM runs as an autonomous agent with read-only tools (`Read`, `LS`, `Glob`,
read-only `Bash` — grep/find/ls only). It browses the codebase, locates relevant files,
and produces the implementation prompt as its text output. No file writes occur.

Input (in the prompt):
- guidelines context (files listed under `guidelines.context_files` in the project YAML,
  capped at `guidelines.max_file_chars` chars each; truncated files include a marker
  instructing the agent to use its Read tool for the full content) — pre-loaded as starting context
- `state.task_description`

The agent then reads additional files from the codebase as needed before generating the prompt.

Output written to state:
- `state.analyst_prompt` — the full assembled prompt sent to the LLM (system + user sections, including guidelines content); persisted before the LLM call so it survives exceptions
- `state.implementation_prompt` — structured prompt for the implementer covering:
  context (module/layer), exact files and changes required (based on actual codebase exploration),
  architecture constraints, hard rules, cleanup candidates with verified class names, and acceptance criteria.
- `state.analyst_retry_records` — rejected analyst outputs when the response is empty, generic,
  or looks like a meta completion message instead of an implementation prompt.

Before `state.implementation_prompt` is stored, Sikula validates that the analyst response is
usable as implementation input. Meta responses such as "the prompt above is the final output" or
"the task is complete" are rejected. Sikula retries analysis once with a stricter instruction; if
the retry is still invalid, the orchestrator fails the task before planner or implementer phases run.

**Prompt rules enforced:**
- *No test changes* — the analyst must not suggest changes to test files (test/, androidTest/,
  __tests__/, spec/, …); a dedicated TestAgent handles tests. Test changes are omitted entirely.
- *Dead code ignores test references* — when checking for remaining references during cleanup,
  test files are excluded. A symbol referenced only in test files is treated as dead in
  production and its definition is removed.
- *Completeness* — for every file listed in Required Changes, the agent reads the full file
  before finalising the change list; grep results alone are not sufficient.
- *Structured input contracts* — for parsers, validators, expression engines, schemas, DSLs,
  config loaders, and rule engines, the implementation prompt must include accepted inputs,
  rejected inputs, expected result types for typed contexts, scope rules, literal handling, and
  whether failures belong in validation or runtime. These details stay semantic and
  platform-neutral unless the existing codebase exposes platform-specific contract names.
  Acceptance criteria distinguish materially different rejected input classes instead of
  relying on one generic invalid example.

---

### ImplementerAgent (`agents/implementer_agent.py`)

**Input:**
- `state.implementation_prompt` — what to change and why (from AnalystAgent)
- `sandbox.allowed_write_paths` from project config — passed as constraint in the agent prompt
- project guidelines filenames from `guidelines.context_files` — implementer reads content via its tools
- In step mode: a `CURRENT STEP N/M: <description>` section is appended instructing the agent to focus only on that step; the orchestrator runs exactly as many passes as `state.plan` has entries

**Mechanism:** calls `LLMClient.run_agent(prompt, cwd=project_root)`.
The LLM runs as an autonomous agent with file tools (`Read`, `Edit`, `Write`, `LS`, `Glob`,
read-only `Bash` — grep/find/ls only), navigating the codebase and making changes directly.
No file content is passed in the prompt.

**Sandbox (four layers):**
- *Git isolation* — each run works in a dedicated worktree and branch; all changes are
  visible via `git diff` before merge; nothing reaches main without a deliberate merge.
  With `--no-isolate` changes land as uncommitted working-tree modifications — equally
  visible before any commit.
- *Filesystem scope* — the agent subprocess runs with `cwd=project_root`, anchoring all
  relative paths to the project.
  For `CodexClient`, `--sandbox read-only` is used for single-shot and read-only calls;
  `--sandbox workspace-write` is used for write-capable agents. Sikula does not pass
  `--add-dir`; workspace boundary enforcement and any writable paths outside the working
  root are determined by the Codex CLI sandbox policy, not by Sikula.
  Sikula passes `--skip-git-repo-check` to Codex; task repository and worktree checks are
  enforced by Sikula before execution.
  For `ClaudeClient`, sandbox settings are built dynamically with absolute paths
  (`Path.home()` for `denyWrite`, `str(cwd)` for `allowWrite`) and passed explicitly via
  `--settings`; Sikula does not rely on project-level Claude settings for this boundary.
  OS-level enforcement via Seatbelt (macOS) or bubblewrap (Linux);
  `--permission-mode acceptEdits` auto-approves edits within that boundary.
  For `GeminiClient`, the `write_file` tool enforces a path check (`Path not in workspace`)
  that blocks writes outside the project directory. Sikula passes `--skip-trust` to Gemini;
  task repository and worktree checks are enforced by Sikula before execution.
  Gemini permits writes to `~/.gemini/tmp/`
  — Sikula agents do not use this path.
  For `OpenCodeClient`, Sikula invokes the CLI with `cwd` and `--dir` set to the task
  project root. Sikula does not add an OS-level workspace sandbox for OpenCode; any
  additional workspace boundary behavior comes from OpenCode itself. `allowed_read_paths`
  and `allowed_write_paths` are prompt constraints, not OS-level. Before each OpenCode
  agent run, Sikula writes generated agent definitions to a temporary OpenCode config
  directory and passes it via `OPENCODE_CONFIG_DIR`, so generated OpenCode files are not
  written into either the task worktree or the original checkout.
  For `AntigravityClient`, Sikula invokes the CLI with `--new-project`, `--add-dir`
  set to the task project root, and prompts passed through stdin via `--print -`.
  Before starting `agy`, Sikula rejects absolute symlinks and relative symlinks that
  resolve outside the project root on paths kept by its Antigravity workspace policy.
  Untracked ignored local artifacts such as `.venv` and `node_modules` are pruned so
  ordinary dependency/runtime directories do not block runs; tracked or preserved paths
  inside soft-ignored directories are still checked. Antigravity CLI does not expose a
  verified non-interactive read-only permission mode, so read-only calls validate the
  source project, run against a disposable temporary copy, reject the result if that copy
  changes, and sanitize temporary paths back to project-relative paths. Write-capable calls
  run against the task worktree after the same symlink preflight.
  Sikula also prepends an
  Antigravity-specific workspace-boundary instruction to write-capable prompts so
  the provider uses the task worktree rather than searching for a similar checkout.
  After each write-capable agent call, Sikula compares the files reported by that call
  with the active write path list and records a non-blocking `write_path_warning` in
  `state.history` when a file falls outside it. This is an audit signal, not a hard
  sandbox failure.
- *Bash restriction* — Sikula prompts agents to use only `grep *`, `find *`, `ls *`, and
  `git rm *`; no network tools and no destructive shell commands. Provider-level
  enforcement varies below. When `git rm` is used, deletions are tracked by git, visible in
  `git diff`, and reversible.
  All `run_readonly_agent()` prompts also include a shared read-only instruction that forbids
  using tools or commands to create, modify, delete, move, rename, format, or write files,
  while still allowing the model to return requested generated content in its final response.
  Commands that change files or project state are also forbidden. The same instruction asks
  read-only agents to reference project files with project-relative paths instead of absolute
  local paths or `file://` URIs.
  For `CodexClient`: prompt-level for write agents; `--sandbox read-only` blocks file writes
  at the OS level but does not per-command filter shell execution.
  For `ClaudeClient` this is technically enforced via `--allowedTools` for all agents.
  For `GeminiClient`: technically enforced for read-only agents (`run_shell_command` excluded
  from `tools.core`); prompt-level for write agents.
  For `OpenCodeClient`: technically enforced for read-only agents (`bash: deny`); prompt-level
  for write agents.
  For `AntigravityClient`: read-only calls run in a disposable copy; write-agent
  command restrictions are prompt-level plus any provider behavior from `--sandbox`.
- *No internet access* — all agent prompts include `AGENT_SECURITY_PREFIX`
  (defined in `agents/base_agent.py`), which instructs agents not to use network
  commands or access external services. Provider-specific tool restrictions may further reduce
  network-capable shell/tool access (for example, read-only Gemini/OpenCode calls remove shell
  access), but Sikula does not rely on an explicit provider-level network-deny setting.
  Network activity that occurs during `sync()` or `compile_check()` (e.g. Gradle/Cargo
  downloading dependencies) happens inside the `BuildTool`, outside agent control.

**Changed file detection:** git snapshot (`git diff HEAD` + `git ls-files --others`) before and
after the agent call. Each dirty/untracked file is identified by its SHA-256 content hash, so a
file that was already dirty before the run is still detected as changed if the agent modifies it.

**Output written to state:**
- `state.files_changed` — paths detected via git diff.

---

### ReviewerAgent (`agents/reviewer_agent.py`)

**Read-only** — never writes files. Uses `LLMClient.run_readonly_agent()`.

**Input:**
- `state.task_description` — sole authority on scope; anything not mentioned here is out of scope regardless of what the implementation prompt claims
- `state.implementation_prompt` — the developer's plan; authoritative for required changes only; scope expansion claims ("this caller is intentionally affected") must be verified against the task description
- `state.files_changed` — list of all files modified so far
- diff (capped at 40 000 chars) — `state.review_diff` when set (`cmd_review()` sets the initial `git diff base...branch`; `review --fix` refreshes it before reviewer/security-reviewer calls); otherwise obtained live via `GitTool.diff_head()` (`git diff HEAD`)
- project guidelines content pre-loaded from `guidelines.context_files` (same mechanism as analyst; capped at `guidelines.max_file_chars` per file; truncated files include a Read-tool marker)
- previous reviewer outputs from `state.review_cycle_records` — passed as numbered history so the agent maintains consistent judgments across iterations and does not reverse a finding unless the code genuinely changed
- `state.test_files_written` — list of files written by the test writer agent; if non-empty, passed to the reviewer so generated tests are not flagged as scope violations. In normal `sikula run` mode, these files are not reviewer-owned output. In `sikula review` mode, changed test files are reviewed as normal branch output.
- recent test-related fixer records from `state.fix_cycle_records` — records whose
  `errors_before.test` is non-empty or whose `triage_scope` marks a test-origin validation
  fix are summarized, so the reviewer can audit whether a fix weakened a task, guideline,
  or structured input contract.
- effective configured validation pipeline — compile/test/check commands that the
  orchestrator will run, including `fix_command` entries, plus task-described validation
  commands extracted from `state.task_description` and marked as covered or not covered.

The agent reads the diff, then uses its `Read` tool to inspect any changed file in full.
New files (not in the diff) are read directly via their paths in `state.files_changed`.

**What it checks:**
1. *Completeness* — did the implementation cover everything the prompt required?
2. *Logical correctness* — are changed call sites, handlers, and data flows correct?
3. *Entry-point and async boundary consistency* — for changed behavior, identifies
   production entry points such as UI handlers, API/route handlers, CLI commands,
   lifecycle hooks, callbacks, queue/background jobs, timers, observers, and equivalent
   platform entry points. If multiple entry points call the same operation, each must
   handle success, failure, cancellation/absence where applicable, state transitions,
   and side effects correctly in its own context. Fire-and-forget async/deferred work
   (promises, futures, coroutines, tasks, threads, callbacks, queued work, etc.) must
   either have an observed error path or be explicitly safe to ignore.
4. *Semantic consistency* — do remaining callers of modified symbols still make sense given the task intent?
5. *Dead members* — for every type that had members removed, are all remaining members still referenced in production code?
6. *Shared function scope* — for any shared function or extension modified, greps for all callers in production code independently, reads each out-of-scope caller file, and verifies behavior is unchanged. Scope expansion claims in the implementation prompt ("this caller is intentionally affected") are verified against the task description — if the caller's screen or feature is not mentioned there, it is an unintended side effect and reported as an issue.
7. *Structured input contracts* — for parser, validator, expression engine, schema, DSL,
   config loader, or rule engine changes, verifies production validation enforces the full
   contract, including expected result types, rejected invalid shapes, unknown names,
   forbidden values, and scope/forward-reference violations. A validator that only checks
   syntax or known names when the task requires typed/shape-specific validation is a
   production correctness issue.
8. *Contract-bearing test weakening* — in normal `sikula run`, changed tests are still not
   reviewed as standalone output, but if changed test files or recent test-failure fixer
   records show that a task/guideline/structured-contract test was deleted, relaxed, or
   changed to a different invalid fixture, the reviewer uses that as evidence to re-check
   the production contract and reports the production issue when it still exists.
9. *Validation command coverage* — explicit validation commands in task descriptions are
   treated as acceptance criteria for the configured validation pipeline. The reviewer does not block
   merely because a covered command has not yet run during review; the build/test/check
   loop owns execution. If a `sikula run` task-described command is not covered by
   configured compile/test/check commands, the run fails as a validation coverage gap
   before the agent loop. In `sikula review` modes, commands found in PR/review text are
   informational branch-verification context and do not preflight-abort review/fix. A
   same-tool-family command with materially different flags, targets, scripts, packages,
   schemes, or paths is reported as a near match, not accepted as
   coverage. Gradle/Maven wrapper spelling for the same invocation (`./gradlew` vs
   `gradle`, `./mvnw` vs `mvn`), Python module forms (`python -m pytest` vs `pytest`,
   `python -m ruff` vs `ruff`), the npm `test` shortcut (`npm test` vs `npm run test`),
   and pnpm/Yarn package-script shorthands for common validation scripts (`pnpm typecheck`
   vs `pnpm run typecheck`, `yarn lint` vs `yarn run lint`) are accepted as coverage.
   Report-only review may still report the same gap as a review issue.

Test-file policy is mode-specific. In normal `sikula run` mode, test files are not
reviewer-owned output; the reviewer does not block approval because tests are stale,
missing, or need fixture updates, and may use tests only as evidence of a production
correctness problem. In `sikula review` mode, changed test files are branch-owned
output and are reviewed like any other changed file; stale fixtures, incorrect
assertions, misleading expectations, negative tests changed to easier/different
invalid fixtures, or material missing tests may be reported as review issues.

**Output written to state:**
- Approved: `state.review_approved = True`, `state.review_issues` cleared; output includes a structured verification summary — `Completeness:`, `Correctness:`, and `Callers verified:` lines, each omitted when not applicable — followed by `APPROVED` on its own line; record appended to `state.review_cycle_records` with `approved = True`
- Issues found: `state.review_issues` populated with structured issue list; `state.review_approved` stays `False`; record appended with `approved = False`

**Re-review after fixer:** if `FixerAgent` changes production-impacting files, the
orchestrator resets `state.review_approved = False` and first reruns deterministic
build/test/check validation. Test-only fixer changes whose reported files are all under
`sandbox.allowed_test_write_paths` and have recognized test artifact paths/names preserve
reviewer approval, but still force deterministic validation. The review loop reruns after
production-impacting fixes once validation is green; if review fixes change files,
build/test/check validation runs again before the task or step can be accepted. In
`review --fix`, test-writer changes receive one final validation-only reviewer pass;
rejection fails the task rather than feeding another fix cycle.

---

### SecurityReviewerAgent (`agents/security_reviewer_agent.py`)

Runs in Phase 3.5 after the review phase. With the default `run_review: true`, that
means after reviewer approval; if `run_review: false`, it still runs unless
`run_security_review: false` or `state.security_approved` is already set.

**Read-only** — never writes files. Uses `LLMClient.run_readonly_agent()`.

**Input:**
- `state.task_description` and `state.implementation_prompt`
- `state.files_changed` — list of all files modified so far
- diff (capped at 40 000 chars) — `state.review_diff` when set; otherwise `GitTool.diff_head()` (same priority and refresh behavior as ReviewerAgent)
- project guidelines content pre-loaded from `guidelines.context_files` (same mechanism as analyst; capped at `guidelines.max_file_chars` per file; truncated files include a Read-tool marker)
- `security.context` from the project config — optional free-text description of what the application does, what data it handles, and who the users are; injected into the system prompt to focus the audit on relevant threat categories; omitted from the prompt when blank
- previous security reviewer outputs from `state.security_review_cycle_records` — passed as numbered history for consistency across security review iterations

**What it checks (changed code only — pre-existing issues are ignored):**
- *Blocking:* hardcoded credentials/tokens/secrets; injection vulnerabilities (SQL, command,
  LDAP, …); missing or bypassable auth/authz checks; broken/weak crypto for security-sensitive
  operations; sensitive data (PII, passwords, tokens) logged in plaintext; path traversal;
  disabled or missing TLS certificate validation
- *Warnings (non-blocking):* insecure defaults, missing input validation on public API
  boundaries, potential info leakage in error messages; anything that does not clearly
  qualify as blocking but merits attention

**Output written to state:**
- All-clear approval: `state.security_approved = True`; `state.review_issues` cleared;
  output includes a `Security checks:` summary listing the categories examined, followed by
  `APPROVED` on its own line; record appended to `state.security_review_cycle_records` with
  `approved = True` and `has_warnings = False`
- Warnings only: `state.security_approved = True`; `state.review_issues` cleared; output
  contains `## Warnings` and no `## Security Issues`; record appended with
  `approved = True` and `has_warnings = True`. Warnings are stored in
  `security_review_cycle_records` and do not trigger a fix pass.
- Blocking issues: `state.security_approved = False`; `state.review_issues` populated with
  security issue list; `state.review_approved` reset to `False` → implementer fix pass runs,
  then review loop re-runs, then security review re-runs; record appended with `approved = False`
- Unexpected output (no `APPROVED` signal, no `## Warnings`, no `## Security Issues`):
  treated as blocking — same as blocking issues path

**Iteration limit:** uses `config.max_security_review_iterations` (independent of `config.max_review_iterations`); timeout sets `state.failed = True`.

**Reset after fixer:** if `FixerAgent` changes production-impacting files or executable test
artifacts, `state.security_approved` is reset to `False`. Test-only fixer changes whose
reported files are all under `sandbox.allowed_test_write_paths` and have recognized test
artifact paths/names preserve reviewer and test-writer approval, but still force deterministic
validation and a fresh security review before acceptance. After production-impacting fixes
validate green again, the security review re-runs after the review loop.

---

### TestWriterAgent (`agents/test_writer_agent.py`)

Runs after the review loop is approved (Phase 4), and again after production-impacting
fixer changes once deterministic build/test/check validation is green. Test-only fixer
changes preserve `state.tests_up_to_date`, so edited tests are validated physically
without reopening the test writer for an unchanged production diff. Skipped when
`run_test_writing: false` or `state.tests_up_to_date` is already set.

**Write scope:** the prompt restricts the agent to directories listed under
`sandbox.allowed_test_write_paths` and explicitly forbids production source edits.
Provider-level filesystem enforcement varies by `LLMClient` (see the ImplementerAgent
sandbox section above). After the agent returns, Sikula records a non-blocking
`write_path_warning` if any reported file falls outside the active test write paths.

**Input:**
- `state.task_description` — original task description; used to honor explicit testing requirements
- `state.implementation_prompt` — what was implemented and why
- `state.files_changed` — production files that were changed
- git diff HEAD (capped at 40 000 chars) — the exact changes made
- current planner step description when `state.plan` is non-empty — injected as `CURRENT STEP`
- `test_writer.coverage_target` from project config (default: 90) — injected into the prompt
  as a target within the configured test surface
- `test_writer.test_surface_policy` from project config (default:
  `existing_infrastructure`) — controls whether missing test infrastructure is treated as
  a gap or kept outside the configured test surface
- project guidelines filenames from `guidelines.context_files` — test writer reads content via its tools

**What it does:**
1. Reads changed production files in full to understand new behaviour
2. Finds existing test files for those modules and reads them for conventions — mirrors their
   framework constructs, assertion style, naming patterns, and test doubles exactly; this
   governs HOW tests are written, not WHICH structure to use (see item 4)
3. Writes or updates tests covering new/changed behaviour, edge cases, error paths,
   and null/absent paths for every nullable value involved in the change
   - Explicit testing requirements from `state.task_description` are honored; in multi-step
     tasks, `CURRENT STEP` is the primary scope signal and tests for future steps are not added
   - When multiple production entry points reach the same changed operation and have
     distinct error handling, state transitions, cancellation/absence handling, or side
     effects, each entry point is tested separately. This is platform-neutral: entry
     points include UI handlers, API/route handlers, CLI commands, lifecycle hooks,
     callbacks, queue/background jobs, timers, observers, and equivalent framework hooks.
   - Async/deferred work started from an entry point is tested through observable success
     and failure paths when the configured test surface can do so. If the failure path
     requires new infrastructure outside that surface, the test writer follows
     `test_writer.test_surface_policy` instead of substituting broad source-inspection tests.
   - Coverage targets apply only inside the configured test surface. The test writer must
     not synthesize runtime/framework harnesses that recreate render trees, selector or
     event systems, lifecycle schedulers, navigation/history stacks, dependency containers,
     device/emulator APIs, filesystems, servers, command runners, or similar missing
     infrastructure. A test/helper that combines several fake runtime subsystems is treated
     as a synthetic runtime harness, even if each fake looks small in isolation. The test
     writer should prefer existing project-standard seams, narrow stable contracts, or a
     `TESTABILITY GAP`. Skipped, disabled, ignored, expected-failure, assumption-gated, or
     environment-gated tests that Sikula's configured validation will not execute do not
     count as coverage for changed behaviour.
   - Parser, validator, expression engine, schema, DSL, config loader, and rule engine
     changes get a positive/negative contract matrix, including wrong expected result type
     rejection when typed contexts exist; rejected input classes must stay distinct, so a
     malformed-shape case cannot be replaced with a different invalid fixture merely because
     it is easier to make pass
   - When observable through the public API, tests assert whether rejection belongs to
     parse/load validation, semantic validation, or runtime evaluation
4. Uses parametric tests when the project uses them anywhere in the test suite and the fit
   is natural — even if the specific file being edited does not currently use them;
   parametric structure takes precedence over mirroring the existing file; when multiple
   inputs exercise the same path with different values, or cases differ only in
   null/absent inputs, a parametric test is required (not optional)
5. When the change adds or modifies an enum value or sealed class case, adds that case to
   every existing parametric table that enumerates cases of the same type
6. Targets at least `coverage_target`% branch and line coverage on new/changed code within
   the configured test surface
7. Follows project nullability conventions — null paths tested explicitly, no unsafe unwrapping
8. Updates existing tests whose contract changed; never deletes unrelated tests
9. For every function whose signature or behaviour changed, greps for all its callers in
   production code independently (does not rely on the implementation prompt's caller list);
   for callers not in `files_changed`, checks whether existing tests cover their path through
   the modified function and adds tests if not
10. Prefers behaviour tests through public APIs, public state, public routing contracts,
    command outputs, or project-standard test helpers. Source-file inspection is weak
    coverage and must not be used for UI implementation details such as component
    structure, layout branches, framework modifiers, view-tree shape, or literal calls
    inside screen/view files. Narrow source inspection remains acceptable for stable
    static contracts that are not meaningfully executable through the available surface,
    such as route constants, string/resource keys, API annotations/signatures,
    schema/config keys, or generated registry entries. Such tests must resolve paths
    without relying on the runner's current working directory and must not require
    production source, build configuration, dependency declarations, runtime
    configuration, or pipeline settings to change merely so the test can pass. If
    meaningful coverage requires new test infrastructure outside the configured test
    surface, the agent follows `test_writer.test_surface_policy` instead of replacing
    missing coverage with broad source-inspection tests:
    `existing_infrastructure` uses the best meaningful existing-surface coverage and does
    not report a gap merely because a heavy UI/browser/device/runtime harness is absent;
    `complete` opts in to structured `TESTABILITY GAP` reports for missing test
    infrastructure outside that surface. Sikula records reported gaps in
    `state.testability_gaps`; by default they are visible warnings, or blocking failures
    when `test_writer.testability_gap_policy: fail` is configured. Gap records can include
    `covered_by` to document the existing-surface tests or seams that still cover the
    runnable portion while the out-of-surface behaviour remains excluded from the prompt
    coverage target.
    Before running the test writer, Sikula marks a pending post-agent audit, stores
    per-file execution-gate signature counts in task state, and stores a temporary limited
    restore snapshot in the internal state store outside the task JSON. The signature counts
    can cover all configured test/inline-test candidates, but the text snapshot is limited
    to task-known test artifacts and later to files reported by the test writer; it is not a
    full source snapshot of broad roots such as `"."`. That snapshot is used only to roll
    back the interrupted test-writer pass without discarding valid uncommitted test changes
    from earlier task steps, and it is deleted when the pending audit is cleared. If the
    process is interrupted after the agent saves
    `tests_up_to_date` but before deterministic audits finish, `resume` completes the
    pending audit instead of skipping test write entirely. If resume sees that the pending
    marker was saved before the test-writer invocation completed, it restores any partial
    unknown dirty test output to the git baseline and task-known test output to the limited
    restore snapshot, then reruns the test writer instead of treating the marker as
    audit-only.
    Recovery restores reject symlinked restore paths instead of following them, so rollback
    writes cannot escape the project sandbox boundary.
    After the test writer returns, Sikula audits only test files and inline-test source
    files written or modified in that invocation for newly added skipped, disabled,
    ignored, expected-failure, assumption-gated, or environment-gated execution paths.
    Existing project skips are not blocked. New gates are recorded in
    `state.test_execution_gate_records` and fed into the build/fix loop as test-origin
    validation issues.
    Sikula also audits the current Sikula-modified test file or inline-test source file
    against the task baseline for generated synthetic runtime harnesses that combine
    multiple locally declared fake runtime subsystems such as render trees,
    event dispatch, navigation/history,
    networking, scheduler/lifecycle, dependency-container, filesystem/command-runner, or
    platform/device runtime fakes. This catches harnesses assembled across multiple agent
    passes while leaving baseline project helpers and normal project-standard test
    infrastructure usage alone. Findings are recorded in
    `state.synthetic_test_harness_records`, surfaced as audit warnings, and fed back into
    later test-writer/fixer prompts without raw source excerpts. The orchestrator also
    restores affected generated test files to the pre-agent snapshot and retries once; if
    the process resumes after that in-memory snapshot was lost, Sikula restores the affected
    files to the stored pre-agent snapshot before retrying. If that internal snapshot is
    unavailable, the fallback is the git baseline (or removal for newly added files). If the retry
    recreates the broad harness, Sikula restores it again, records a `TESTABILITY GAP`, and
    continues normal validation without adding `test_errors` solely for the synthetic
    harness audit.
11. For framework/container wiring such as dependency injection modules, provider trees,
    route registries, plugin registries, or service containers, does not hand-copy the
    production registration logic into a local test-only container. It must exercise the
    real production configuration through existing project-standard helpers, test a stable
    public seam reached by that wiring, or follow `test_writer.test_surface_policy` when
    meaningful coverage requires missing infrastructure.

**Output written to state:**
- `state.tests_up_to_date = True` — set on success regardless of whether files changed
- `state.test_writer_audit_pending`, `state.test_writer_audit_agent_completed`,
  `state.test_writer_audit_files_written`, and `state.test_writer_audit_gate_counts` —
  transient resume-safe post-agent audit state; counts are sanitized execution-gate
  signatures, not source excerpts. `test_writer_audit_agent_completed` distinguishes
  "agent finished, audit pending" from "pending marker saved before the agent finished" so
  resume can rerun the test writer when needed. A temporary limited restore snapshot for
  task-known/reported test files is stored separately in the internal state store and removed
  after pending audit recovery completes.
- `state.files_changed` — test file paths appended (de-duplicated)
- `state.test_files_written` — same paths also appended here (de-duplicated); used by ReviewerAgent to exempt these files from scope violation checks. In `sikula review` mode, the files are still reviewed for correctness and relevance.
- `state.test_write_records` — one record appended per invocation with `step`, `build_iteration`, `scope`, `test_surface_policy`, `test_writer_prompt`, `test_writer_output` (`None` on exception), `files_written`, and `timestamp`
- `state.testability_gaps` — one record per `TESTABILITY GAP` reported by the test writer or test-only fixer, with `source`, `step`, `build_iteration`, optional `scope`, the raw gap message, and any parsed `target`, `reason`, `covered_by`, `recommended_action`, and `risk` fields. `tests_up_to_date` still becomes `True` for test-writer gaps; the gap means Sikula did all it safely could for the current diff under the configured test surface, not that full behaviour coverage exists.
- `state.test_execution_gate_records` — one record per deterministic audit finding when a
  Sikula-modified test file or inline-test source file under configured test write paths
  introduces a new skip/disable/ignore/expected-failure/assumption/environment gate. These
  records include `source`, `step`, `build_iteration`, optional `scope`, `status`, and
  `findings`; active findings are rechecked against the current working tree on resume and
  before fixer retries.
- `state.synthetic_test_harness_records` — one record per audit finding when a Sikula-modified test file newly crosses the broad synthetic-runtime-harness threshold relative to the task baseline. Entries include `source`, `step`, `build_iteration`, optional `scope`, `status`, timestamp, and per-finding `path`, `subsystems`, baseline subsystems, sanitized line metadata, and recommendation. Raw source excerpts are intentionally omitted. Active findings are deduplicated prompt context and terminal audit warnings; they also drive soft recovery by restoring affected generated tests and retrying once. Resolved findings remain for audit.

**Reset after fixer:** if `FixerAgent` changes production-impacting files, the
orchestrator resets `state.tests_up_to_date = False`. Test-only fixer changes whose
reported files are all under `sandbox.allowed_test_write_paths` and have recognized test
artifact paths/names preserve `tests_up_to_date`, but the build/test/check loop still
validates the edited tests. After production-impacting fixes validate green again, the
test write phase reruns after review/security gates (only when `run_test_writing: true`).
If the test writer changes files, build/test/check validation runs again. In
`review --fix`, test-writer changes are reviewed once as a final validation gate and do
not trigger another test-writing loop.

---

### FixerAgent (`agents/fixer_agent.py`)

**Constraints given to the agent:** fix only what the errors describe — no refactoring, no unrelated changes. Both the write-path allowlist and the test file constraint are context-dependent:

| Error type | `allowed_write_paths` used in prompt | Test files |
|---|---|---|
| Build errors (`state.errors` non-empty) | `sandbox.allowed_write_paths` (production dirs) | Off-limits |
| Build/check errors whose diagnostic references all point at test files or recognized test targets | First pass: `sandbox.allowed_test_write_paths`. Retry pass: same test-only write paths, only after the first pass violated scope and Sikula restored that pass's writes. Second production-enabled pass, after a triage that classifies any failure as `production_defect` (and did not explicitly choose `test_code`): `sandbox.allowed_write_paths` + `sandbox.allowed_test_write_paths`. Test-only writes may be kept before this pass only when separate `stale_test`/`malformed_test` triage explicitly authorizes `test_code`; otherwise a production-defect triage must not change files before the confirmed pass. | May repair malformed/stale tests in the first pass. Production writes during a test-only pass are rejected: Sikula restores that pass's writes and retries once; a restore failure or second scope violation fails the task. If diagnostics name production paths or targets too, or name no paths or recognized targets at all, Sikula falls back to the normal build/check scope. |
| Test failures only (`state.test_errors` non-empty, `state.errors` and `state.check_errors` both empty) | First pass: `sandbox.allowed_test_write_paths`. Retry pass: same test-only write paths, only after the first pass violated scope and Sikula restored that pass's writes. Second production-enabled pass, after a triage that classifies any failure as `production_defect` (and did not explicitly choose `test_code`): `sandbox.allowed_write_paths` + `sandbox.allowed_test_write_paths`. Test-only writes may be kept before this pass only when separate `stale_test`/`malformed_test` triage explicitly authorizes `test_code`; otherwise a production-defect triage must not change files before the confirmed pass. | May repair malformed/stale tests in the first pass. If the failing test encodes the original task, implementation prompt, project guidelines, or a structured contract, the test-only pass must report a production defect without changing files unless a separate triage block authorizes an actual malformed/stale test repair. Production writes during a test-only pass are rejected with the same restore-and-retry behaviour as test-origin validation failures. |
| Check errors only (`state.check_errors` non-empty, `state.errors` empty) | `sandbox.allowed_write_paths` + `sandbox.allowed_test_write_paths` | May modify production or test files if explicitly named in the check errors |

**Input:**
- `state.errors[-3:]` — last three build error blobs (if non-empty, labelled "BUILD ERRORS")
- `state.test_errors[-3:]` — last three test failure blobs (if non-empty, labelled "TEST FAILURES")
- `state.check_errors[-3:]` — last three check failure blobs (if non-empty, labelled "CHECK ERRORS")
- `state.test_files_written` — included for test-failure and test-origin validation prompts
  so the fixer can distinguish generated tests from pre-existing project tests
- `state.task_description` — original task (high-level context)
- `state.implementation_prompt` — analyst's detailed implementation plan (gives fixer intent behind each file change)
- write-path allowlist from project config — see constraint table above
- project guidelines filenames from `guidelines.context_files` — fixer reads content via its tools so fixes follow architecture conventions

Both error sources are included in the same prompt when present; in normal flow at most one
is non-empty at a time (build errors are cleared by a passing build before tests run).
Build, test, sync, and check error blobs are diagnostic excerpts, not plain tails: Sikula
preserves failure-marker blocks from long command output so the fixer still sees the concrete
compiler diagnostic, failing test, assertion, panic, traceback, or tool error even when the
build tool prints many lines after the failure.
For test failures, and for build/check failures whose diagnostics reference only test files
or recognized test targets, the fixer is explicitly told to decide whether the failure is
caused by production behaviour or by an incorrect/stale test. Target-only diagnostics are
matched conservatively: unknown, production, or mixed production/test references fall back to
the normal build/check scope. The first pass is test-only: production source, build
configuration, runtime configuration, dependency declarations, and pipeline settings are not
allowed outputs for that pass. The fixer must not delete, relax, or rewrite assertions just to
make the run green. Its final response for these test-origin failures must begin with
`TEST FAILURE TRIAGE`, classifying the failure as `production_defect`, `stale_test`,
`malformed_test`, or `unclear`, naming the affected contract when present, and stating whether
the chosen fix is production code or test code (`chosen_fix` must be `production_code` or
`test_code`, never `none`). When several independent failures are reported it emits one block
per failure. If any block classifies a `production_defect` (and did not explicitly choose
`test_code`), Sikula runs a second production-enabled fixer pass after the test-only pass and
records both. A pass may legitimately repair a `stale_test`/`malformed_test` in test files *and*
defer a separate `production_defect` to that confirmed pass; those authorized test-only writes are
kept and merged into the change set so the production change still re-runs the full review. If a
test-only pass writes production files, or requests a production fix while changing files that no
block authorizes as a test fix, Sikula treats the attempt as tainted: it restores every
write from that attempt to the pre-attempt worktree snapshot, records
`test_only_scope_violation`, and
retries the test-only pass once with explicit recovery context. A restore failure, a second
test-only scope violation, or a production-confirmed second pass that changes only tests marks
the task failed before the pipeline can accept the change.
When the failure is in a file listed in `state.test_files_written`, the fixer prompt includes
that generated-test context. The fixer may replace or delete such generated tests only when
they are malformed/stale or depend on unavailable/brittle harness internals and do not encode
the original task, project guidelines, or a structured contract. It must preserve real
coverage through a stable-seam replacement when possible, and it must not delete or weaken
pre-existing tests just to make validation pass. For framework/container wiring tests, it is
also told not to mirror production registration logic in a local test-only container; it
should exercise the real production configuration with existing helpers, use a stable public
seam, or explain the testability gap.
If repeated test-only fixer passes have already changed Sikula-generated tests, tracked in
`state.generated_test_fix_counts`, the next test-origin fixer prompt includes a
`GENERATED TEST RE-TRIAGE` contract. The fixer must
choose between `replace_with_narrower_seam_test`, `remove_malformed_generated_test`,
`report_testability_gap`, or `production_defect`, and state what existing-surface coverage
is preserved or added through `covered_by`. Sikula records that block inside the current
`fix_cycle_records` entry for audit, but the pipeline decision is driven by the dedicated
counter rather than observability records. The block must include all required fields
(`strategy`, `target`, `reason`, and `covered_by`) or it is treated as missing. This does
not add a new automatic fail condition; it changes the model contract so repeated
generated-test failures are re-scoped instead of patched indefinitely. If the fixer edits
Sikula-generated tests while this contract is active but omits the re-triage block, Sikula
treats that pass as recoverably non-compliant:
it restores all writes from the pass, records `generated_test_retriage_violation`, and
retries once with explicit recovery context before running validation again. If Sikula
cannot restore the first pass, the task fails because the worktree can no longer be
returned to a known safe state. A second missing re-triage block on the retry remains
recorded in `generated_test_retriage_violation`, but the retry's file changes continue
into normal validation so prompt-compliance alone does not prevent an otherwise valid task
from finishing.
The fixer must also avoid stabilizing generated tests by adding skipped, disabled, ignored,
expected-failure, assumption-gated, or environment-gated tests for changed behaviour
that the configured validation surface cannot execute. If a generated test failure is in a
test/helper that combines several fake runtime subsystems, the fixer should treat that as a brittle
synthetic harness instead of repeatedly fixing one fake subsystem at a time. If a test-only
fix exposes missing runtime infrastructure instead of a fixable test defect, the fixer may
output a structured `TESTABILITY GAP`; Sikula records fixer gap blocks in task state for
audit.
After a test-writer or fixer pass that creates a synthetic runtime harness finding, Sikula
restores the affected generated test files to the pre-agent snapshot before the harness can
remain in branch output. The agent gets one retry with the active audit context. If the
retry recreates the broad harness, Sikula restores it again and records an orchestrator
`TESTABILITY GAP` instead of failing solely on the soft audit.
After any fixer pass that writes recognized test artifacts or inline-test source files
under configured test write paths, Sikula compares those files against the pre-fixer
snapshot. Newly added execution gates are not accepted as coverage; they are recorded in
`state.test_execution_gate_records`, refreshed against the current working tree to avoid
stale retries, and surfaced as test errors so the next fixer pass can remove the gate, add
real existing-seam coverage, or report a `TESTABILITY GAP`.
This audit does not rely only on `allowed_test_write_paths`: when a project uses broad test
write roots, such as a platform module directory that contains both production and test
sources, Sikula still treats non-test artifacts under that root as production writes. Build
files, dependency manifests, project/workspace files, generated-source config, and runtime
configuration therefore cannot be used to make malformed generated tests pass unless the
fixer explicitly classifies the failure as a production defect and chooses a production-code
fix.
For mixed source/test files, the production-write audit may ask the active `BuildTool`
whether the fixer's incremental before/after diff is test-only. This is opt-in and fail-closed:
the default hook returns `False`, and platform subclasses must use syntax-aware checks. Cargo
currently recognizes only edits inside an already-existing Rust `#[cfg(test)] mod tests`
block; creating a new inline test block or changing any production hunk remains a production
write.

**Mechanism:** calls `LLMClient.run_agent(prompt, cwd=project_root)`.
The agent reads the error output, locates relevant files, and applies fixes directly — no file
content pre-loaded in the prompt.

**Changed file detection:** same git diff mechanism as ImplementerAgent.

**Output written to state:**
- `state.files_changed` — new paths appended (existing entries kept)
- `state.errors`, `state.test_errors`, and `state.check_errors` — all cleared after agent runs successfully
- `state.fix_cycle_records` — stores the fixer prompt, output, errors snapshot, files written,
  scope, build iteration, optional `triage_scope`, optional `triage_pass` (`test_only`,
  `test_only_retry`, or `production_confirmed`), optional `confirmed_test_failure_triage`,
  optional `scope_recovery`, and optional `test_only_scope_violation` restore audit; reviewer
  prompts summarize recent test-related records for contract-weakening audit.

**Re-sync trigger:** orchestrator calls `BuildTool.is_build_config_file()` on each newly
changed file. If any match, `state.build_synced = False` is set so sync runs before the
next build. The patterns are defined by the `BuildTool` implementation, not the orchestrator.

**Sync output adoption:** after `BuildTool.sync()` succeeds, the orchestrator compares the
non-ignored repository snapshot taken before sync with the snapshot after sync. Platform
`BuildTool` subclasses classify source-controlled outputs that sync may intentionally update
through `is_sync_adoptable_file(path)`, for example lockfiles or dependency verification
metadata. Project configs can add project-relative patterns with `build.sync_adopt_paths`.
Built-in platform classifications adopt only tracked files that already existed before sync;
brand-new generated outputs require explicit `build.sync_adopt_paths` opt-in. Adopted outputs
are appended to `state.files_changed`, included in sync validation metadata, and make reviewer,
security reviewer, and test-writer approvals stale so the final branch output is reviewed
after deterministic validation. Paths outside `project.root_path` are not adopted into task
state; known tracked sync outputs there fail closed after cleanup. Other non-ignored sync
artifacts are treated like validation artifacts: restore them to the pre-sync snapshot, record
the cleanup, and fail validation only when cleanup cannot be completed.

---

### InitAgent (`agents/init_agent.py`)

**Not part of the orchestrator loop** — called only by `cmd_init()` when `sikula init --guidelines` is run. Does not use `TaskState`.

**Purpose:** generates a `guidelines.md` file from codebase analysis. The file is intended to be committed and referenced under `guidelines.context_files` in the project config — it is loaded into every agent's context during normal `sikula run` execution.

**Mechanism:** calls `LLMClient.run_readonly_agent(prompt, cwd=project_root)`. The LLM browses the codebase, reads source files and documentation, and produces Markdown as its text output. No files are written by the agent — `cmd_init()` writes the returned string to `.sikula/guidelines.md` after `InitAgent` removes any leading provider progress text before the generated guidelines heading.

**Input:**
- `tech_stack` string (e.g., `"Kotlin/Android"`, `"Python"`) — injected into the system prompt to focus the analysis
- `project_root` Path — the directory the agent explores

**System prompt rules:**
- Extract only conventions evidenced by actual code — do not invent rules not present in the codebase
- Focus: module/file organisation, naming conventions, error handling, testing conventions, architectural constraints, platform-specific patterns
- Output is raw Markdown only — no commentary, no summary, no surrounding text
- First generated content line must be `# Development Guidelines`

**Output:**
- Cleaned Markdown string returned to `cmd_init()` → written to `.sikula/guidelines.md`

---

## TaskState fields (`core/state.py`)

Task state JSON is an audit/debug artefact and can contain sensitive project data. It
stores full LLM prompts and outputs, which may include task descriptions, source-code
excerpts, inlined guidelines content, build/test/check output, and security-review
findings. Review and redact state files before sharing them outside your project.
`JsonStateStore` serializes reads/writes through one store instance and writes state
files by replacing a completed temporary file, so heartbeat updates do not race with
retry/audit saves in the same Sikula process. Running the same task from multiple
Sikula processes at once is still unsupported.

| Field | Type | Set by | Purpose |
|---|---|---|---|
| `task_id` | `str` | `StateStore.create()` | 32-char hex UUID (`uuid4().hex`), used to resume tasks |
| `task_description` | `str` | caller | Original plain-text task |
| `schema_version` | `int` | `StateStore.create()` | State file schema version; used by `JsonStateStore.load()` to run migrations before constructing `TaskState`; current value is `SCHEMA_VERSION = 2` |
| `task_file` | `str \| None` | `cmd_run()` in `sikula_cli/run.py` | Basename of the task file (e.g. `add-login.md`); set on first run via `--task-file`; used by `status` for display; `None` for tasks created before this field was added or when resuming via `--task-id` only |
| `config_snapshot` | `dict` | `cmd_run()` / Orchestrator | Effective run configuration captured on first run before agents start (never overwritten on resume): project name, all `run_*` flags, `max_iterations`, `max_review_iterations`, `max_security_review_iterations`, `progress.*`, `sandbox.allowed_write_paths` / `allowed_test_write_paths` / `allowed_read_paths`, `build.*` settings, `planner.*` settings, `test_writer.*` settings, and per-agent `provider`/`model`/`agent_timeout`. It is also saved for contract-gate failures that exit before `Orchestrator.run()`. Visible in `show <task_id>`. |
| `implementation_contract` | `dict` | `cmd_run()` in `sikula_cli/run.py` | Implementation-contract snapshot for fresh task-file runs: task path/format/hash, readiness status/score, gap metadata, clarifying question IDs, and validation coverage counts. By default it is warning-only additive metadata. Fresh `run TASK_FILE` can opt into pre-agent gating with `--require-contract-ready` or `--min-contract-score N`; resume/review flows do not recompute or re-gate it. |
| `implementation_asset_records` | `list[dict]` | `cmd_run()` in `sikula_cli/run.py` | Sanitized, non-blocking asset metadata snapshot for fresh task-file runs, copied from the implementation-contract preflight asset references. Contains path/kind/status/project path/hash/declared hash/size/MIME/git status/requested target/provenance metadata only; no raw asset content, OCR text, binary data, internal parser fields, or source excerpts. Used for audit and terminal summary counts, not for run/resume/review control-flow decisions. |
| `implementation_asset_drift_records` | `list[dict]` | `cmd_run()` in `sikula_cli/run.py` | Sanitized, non-blocking asset drift audit entries recorded when a prepared contract's Asset manifest hash differs from the current file at fresh run start, or when resume/worktree startup sees current asset files differ from the saved `implementation_asset_records` snapshot. Contains path/kind/phase/status/expected hash/current hash/current status/git status/size/MIME metadata only. It is warning-only audit data and must not drive run/resume/review control-flow decisions. |
| `implementation_asset_target_records` | `list[dict]` | `cmd_run()` in `sikula_cli/run.py` | Sanitized, non-blocking delivery asset target audit entries recorded after successful task completion for delivery assets with explicit requested targets. Contains asset path/project path/phase/status/requested target/matched path/timestamp metadata only. It records exact target matches, existing unchanged targets, missing targets, out-of-project targets, or missing target specifications; it does not infer platform-specific conversions and must not drive run/resume/review control-flow decisions. |
| `contract_gate_blocked` | `bool` | `cmd_run()` in `sikula_cli/run.py` | True when an opt-in contract readiness gate failed before worktree creation or agent startup. Such states are kept for audit but are not reset via `--reset-failed`; users should prepare the implementation contract and start a fresh task-file run. |
| `analyst_prompt` | `str \| None` | AnalystAgent | Full assembled prompt sent to the analyst LLM (system + user sections, including inlined guidelines content); stored before the LLM call so it captures the exact input even on exception; enables post-run analysis of analyst behaviour |
| `planner_prompt` | `str \| None` | PlannerAgent | Full assembled prompt sent to the planner LLM (system + user sections); stored before the LLM call; `None` when `run_planner: false` or planner not yet reached |
| `implementation_prompt` | `str \| None` | AnalystAgent | Structured prompt fed to ImplementerAgent; the analyst's key output |
| `presync_done` | `bool` | Orchestrator | Set True after Phase 0 presync attempt (success or failure); guards re-run on resume |
| `files_changed` | `list[str]` | Implementer / Fixer | Paths touched so far; used by orchestrator for build-config re-sync detection |
| `build_synced` | `bool` | Orchestrator | Guards unnecessary re-syncs; reset when build-config files change |
| `build_iterations` | `int` | Orchestrator | Total build/fix attempts across the task; used as an audit/correlation counter in validation and agent records |
| `build_loop_key` | `str \| None` | Orchestrator | Active build/fix loop identity (`"task"`, `"step:N"`, or `"final_full_task"`); persisted so resume keeps the same loop budget |
| `build_loop_start_iteration` | `int` | Orchestrator | Global `build_iterations` value at the start of the active build/fix loop; `config.max_iterations` is enforced relative to this value, with one final validation-only pass allowed when the last fixer attempt wrote files |
| `build_status` | `str \| None` | Orchestrator | `"success"` or `"failed"` |
| `test_status` | `str \| None` | Orchestrator | Final test phase outcome: `"success"`, `"failed"`, or `"skipped"`; `None` until the test phase is reached |
| `check_status` | `str \| None` | Orchestrator | Final configured-check phase outcome: `"success"`, `"failed"`, or `"skipped"`; `None` until the check phase is reached |
| `errors` | `list[str]` | Orchestrator | Build output blobs (stdout+stderr) for current fix cycle; Fixer reads last 3, clears after fix |
| `test_errors` | `list[str]` | Orchestrator | Test failure blobs for current fix cycle; Fixer reads last 3, clears after fix |
| `check_errors` | `list[str]` | Orchestrator | Check failure blobs for current fix cycle (from `run_checks` phase); Fixer reads last 3, clears after fix |
| `review_issues` | `list[str]` | ReviewerAgent | Issue list from last review; cleared on approval; passed to Implementer on fix pass |
| `review_iterations` | `int` | Orchestrator | Fix attempt counter for the current review cycle (counts completed review→implement pairs); resets to 0 after each fixer pass; guarded by `config.max_review_iterations` |
| `review_approved` | `bool` | ReviewerAgent / Orchestrator | Set True on approval; reset to False when Fixer changes production-impacting files |
| `security_approved` | `bool` | SecurityReviewerAgent / Orchestrator | Set True when security review passes (no blocking issues); reset to False when Fixer changes production-impacting files or step transitions; guards re-runs |
| `security_review_iterations` | `int` | Orchestrator | Security fix attempt counter for the current cycle (counts completed security-review→implement pairs); independent of `review_iterations`; resets to 0 on step transitions and fixer passes; guarded by `config.max_security_review_iterations` |
| `analyst_warnings` | `list[str]` | AnalystAgent | Warnings produced by the analyst (e.g. ambiguous task scope, missing context); logged for visibility, never block the pipeline |
| `analyst_retry_records` | `list[dict]` | AnalystAgent | Append-only records for analyst outputs rejected before `implementation_prompt` is stored, including attempt number, reason, whether another retry follows, timestamp, the rejected output, and the retry prompt when another attempt follows. These records are audit-only and never drive pipeline control flow. |
| `planner_retry_records` | `list[dict]` | PlannerAgent | Append-only records for planner outputs rejected because the parsed step count exceeded `planner.max_steps`, including attempt number, reason, max step count, parsed step count, whether another retry follows, timestamp, the rejected output, and the retry prompt when another attempt follows. These records are audit-only and never drive pipeline control flow. |
| `review_diff` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` / Orchestrator | PR-style diff passed to ReviewerAgent and SecurityReviewerAgent; initially set to `git diff base...branch` (three-dot) in `sikula review` mode; refreshed in `"review_fix"` mode before reviewer/security-reviewer calls so uncommitted fixes are included; `None` in standard `sikula run` flow (agents fall back to `GitTool.diff_head()`) |
| `review_mode` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` | Review task kind: `"review_report"` for report-only review (not reset or resumable) or `"review_fix"` for `sikula review --fix` (resumable via `sikula run --task-id`) |
| `review_base_branch` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` | Base branch used to refresh `review_diff` in `"review_fix"` mode. Report-only review keeps the original frozen diff; review-fix refreshes against the merge base before reviewer/security-reviewer calls so fixes are reviewed against the current branch state. |
| `review_delivery_mode` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` / `cmd_run()` in `sikula_cli/run.py` | Review-fix delivery strategy metadata. `None` for report-only review, normal `--branch` review-fix, and standard task runs. `"current_branch"` for `sikula review --fix --current-branch`; this tells resume/finalization to deliver the isolated fix commit back to the originally current branch instead of treating `worktree_branch` as a checked-out delivery branch. |
| `review_target_branch` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` | Named branch that was current when `--current-branch` started. Delivery and retry require the operator's checkout to still be on this branch before fast-forwarding or declaring a no-change result. |
| `review_target_start_commit` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` | Commit SHA for the target branch `HEAD` captured before creating the detached isolated worktree. Current-branch delivery requires the target branch to still point at this commit unless it already equals the delivered commit. |
| `review_isolated_fix_commit` | `str \| None` | `_deliver_current_branch_review_fix()` in `sikula.py` | Commit SHA created in the detached isolated worktree for current-branch review fixes. Persisted so `sikula run --task-id` can retry delivery without rerunning agents or creating a second isolated commit. |
| `review_delivery_status` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` / `cmd_run()` in `sikula_cli/run.py` | Current-branch delivery state. `None` outside current-branch review-fix. `"pending"` means agents have not produced a terminal delivery result yet; `"committed"` means an isolated fix commit exists and delivery can be retried; `"failed"` means delivery safety checks, commit creation, fast-forward, or cleanup failed and the worktree is preserved; `"delivered"` means the target branch has the isolated fix commit; `"no_changes"` means agents produced no changes after safety checks passed. `"delivered"` and `"no_changes"` are terminal delivery states. |
| `review_delivery_result` | `str \| None` | `cmd_review()` in `sikula_cli/review.py` / `cmd_run()` in `sikula_cli/run.py` | Short human-readable audit result for current-branch delivery, including failure reasons and delivered/no-change summaries. It is for status, `sikula show`, and final summary reporting only; it must not replace the explicit status field for control-flow decisions. |
| `implement_cycle_records` | `list[dict]` | ImplementerAgent | Structured observability — one entry per implementer invocation: `step`, `build_iteration` (`0` = pre-build; `>0` = review/security fix after a post-fixer validation pass), `review_iteration` (`0` = initial or security fix; `>0` = review fix pass N), `security_review_iteration` (`0` = initial or review fix; `>0` = security fix pass N), `scope` (`"task"`, `"step"`, or `"final_full_task"`), `step_description`, `implementer_prompt`, `implementer_output` (`None` on exception), `files_written`, `timestamp`; both iteration counters `== 0` and `build_iteration == 0` means initial implementation; never read for pipeline decisions. **Correlation note:** to find the reviewer record that triggered this implementer, look for a `review_cycle_records` entry with the same `step`, `build_iteration`, and `review_iteration: N-1` |
| `review_cycle_records` | `list[dict]` | ReviewerAgent | Structured observability — one entry per reviewer invocation: `step`, `build_iteration` (`0` = pre-build; `>0` = after a post-fixer validation pass), `review_iteration` (fix-pass index within this step's review loop), `scope` (`"task"`, `"step"`, or `"final_full_task"`), `reviewer_prompt`, `reviewer_output`, `approved`, `has_warnings`, `timestamp`; also read by the reviewer to retrieve its own prior outputs for context. In `final_full_task` scope, reviewer history is limited to earlier final full-task reviews, not step-scoped reviews. **Correlation note:** a reviewer record with `review_iteration: N` that found issues triggered the implementer record with `review_iteration: N+1` — the orchestrator increments the counter before calling the implementer |
| `security_review_cycle_records` | `list[dict]` | SecurityReviewerAgent | Structured observability — one entry per security reviewer invocation: `step`, `build_iteration` (`0` = pre-build; `>0` = after a post-fixer validation pass), `security_review_iteration` (fix-pass index within this step's security review loop), `scope` (`"task"`, `"step"`, or `"final_full_task"`), `reviewer_prompt`, `reviewer_output`, `approved`, `has_warnings`, `timestamp`; also read by the security reviewer to retrieve its own prior outputs for context. In `final_full_task` scope, security history is limited to earlier final full-task security reviews. **Migration note:** state files from schema version 1 stored security reviewer entries inside `review_cycle_records` with `reviewer = "security_reviewer"`; `JsonStateStore.load()` moves them here and removes the redundant `reviewer` field. |
| `test_write_records` | `list[dict]` | TestWriterAgent | Structured observability — one entry per test-writer invocation: `step`, `build_iteration` (`0` = before first build; `>0` = after a post-fixer validation pass), `scope`, `test_surface_policy`, `test_writer_prompt`, `test_writer_output` (`None` on exception), `files_written`, `timestamp`; never read for pipeline decisions |
| `testability_gaps` | `list[dict]` | TestWriterAgent / FixerAgent | Structured audit signal for behaviour Sikula could not safely cover within the configured test surface. Entries include `source`, `step`, `build_iteration`, optional `scope`, `message`, `timestamp`, and optional parsed `target`, `reason`, `covered_by`, `recommended_action`, and `risk`. For test-writer gaps, the default policy is warning-only; `test_writer.testability_gap_policy: fail` turns those gaps into task failures. |
| `test_execution_gate_records` | `list[dict]` | Orchestrator | Structured audit signal for newly added execution gates in Sikula-modified test files or inline-test source files under configured test write paths, including skip/disable/ignore/expected-failure/assumption/environment gates. Entries include `source` (`test_writer` or `fixer`), `step`, `build_iteration`, optional `scope`, `status` (`detected` or `resolved`), timestamp, and per-finding `path`, `line`, `category`, `reason`, `signature`, `baseline_count`, and `occurrence`. Raw source excerpts are intentionally omitted. Active findings are resolved by recounting the added gate occurrence against the current file so pre-existing identical gates do not keep stale findings active; resolved findings remain for audit. |
| `synthetic_test_harness_records` | `list[dict]` | Orchestrator | Structured audit signal for generated/modified tests that newly cross the broad synthetic-runtime-harness threshold relative to the task baseline, including harnesses assembled across multiple agent passes. Entries include `source` (`test_writer` or `fixer`), `step`, `build_iteration`, optional `scope`, `status` (`detected` or `resolved`), timestamp, and per-finding `path`, `subsystems`, `baseline_subsystems`, sanitized line metadata, and recommendation. Raw source excerpts are intentionally omitted. Active findings are deduplicated and included in later test-writer/fixer prompts and terminal audit warnings. The orchestrator uses them for soft recovery by restoring affected generated tests and retrying once, but the findings never directly fail a task. |
| `fix_cycle_records` | `list[dict]` | FixerAgent | Structured observability — one entry per fixer invocation after a failed sync/build/test/check attempt: `build_iteration` (globally unique, never resets), `step`, `scope`, `errors_before` snapshot (sync/build/test/check), `fixer_prompt`, `fixer_output` (`None` on exception), `files_written`, optional `triage_scope` (`test_failure` or `test_origin_validation`), optional `triage_pass` (`test_only`, `test_only_retry`, or `production_confirmed`), optional `confirmed_test_failure_triage`, optional `generated_test_retriage`, optional `generated_test_retriage_violation`, optional `scope_recovery`, optional `test_only_scope_violation` restore audit, `timestamp`; never read for pipeline decisions |
| `validation_cycle_records` | `list[dict]` | Orchestrator | Structured observability — one entry per presync/sync/build/test/check outcome with `phase`, `status`, `build_iteration`, `step`, `timestamp`, optional `scope`, optional `elapsed_s`, optional `check_name`, and diagnostic `error_excerpt` plus high-signal `diagnostic_summary` lines on failure; excerpts preserve failure-marker blocks from long tool output instead of storing only the final tail, while summaries highlight shortened compiler locations, failed tests, sanitized assertion failures, and linter rules for terminal audit output sampled across failed validation attempts without echoing source-code frames, assertion values, quoted literal payloads, secret-looking key/value tokens, or absolute path prefixes; never read for pipeline decisions |
| `validation_artifact_records` | `list[dict]` | Orchestrator | Structured observability for unexpected non-ignored repository changes produced by sync/build/test/check validation commands, plus sync outputs that cannot be adopted safely. Each record stores `phase`, `status` (`cleaned`, `blocked`, or `cleanup_failed`), `build_iteration`, `step`, optional `scope`, optional `check_name`, and changed paths with before/after status. Cleanup success allows validation to continue for unexpected artifacts; cleanup failure, or a blocked sync output such as an adoptable file outside `project.root_path`, is treated as that validation phase failing. |
| `active_operation` | `dict \| None` | Orchestrator | Current long-running operation heartbeat for status visibility while an agent or validation command is blocked. Contains `phase`, optional `agent`, optional `scope`, `started_at`, `last_heartbeat_at`, `heartbeat_count`, optional `heartbeat_interval_seconds`, and optional `message`. Cleared when the operation completes; never drives pipeline decisions. |
| `test_files_written` | `list[str]` | TestWriterAgent | Cumulative list of all files written by the test writer agent across all runs; never cleared; passed to ReviewerAgent so it does not flag those files as implementer scope violations. In normal `sikula run`, these files are not reviewer-owned output; in `sikula review`, changed test files are reviewed as branch output. |
| `generated_test_fix_counts` | `dict[str, int]` | FixerAgent | Pipeline state counting test-only fixer attempts that modify each Sikula-generated test file. Drives the repeated-generated-test re-triage prompt and enforcement without reading `fix_cycle_records` observability data. |
| `test_writer_audit_pending` | `bool` | Orchestrator | Resume-safety marker set before TestWriterAgent runs and cleared after post-agent execution-gate and synthetic-harness audits finish. Allows `resume` to complete audits even if `tests_up_to_date` was saved by the agent before the audit completed. |
| `test_writer_audit_agent_completed` | `bool` | Orchestrator | Set only after TestWriterAgent returns and its reported `files_written` are saved. If `resume` finds `test_writer_audit_pending` with this flag false, it reruns TestWriterAgent instead of treating the marker as audit-only. |
| `test_writer_audit_files_written` | `list[str]` | Orchestrator | Files from the pending test-writer invocation that still need post-agent audit. If an interruption happens before this list is saved, the orchestrator falls back to `test_files_written` or the current configured test-file candidates rather than reading observability records for control flow. |
| `test_writer_audit_gate_counts` | `dict[str, dict[str, int]]` | Orchestrator | Sanitized per-file execution-gate signature counts captured before TestWriterAgent runs. Used only to finish pending execution-gate audits on resume without exposing raw source snapshots in task state. A matching limited text restore snapshot for task-known/reported test files is stored separately as a temporary internal state-store blob for recovery and removed when the pending audit is cleared; broad roots such as `"."` do not cause Sikula to persist a full source snapshot. |
| `fixer_changed_code` | `bool` | Orchestrator | Set True when FixerAgent writes files; used on resume to continue deterministic build/test/check validation before stale semantic gates rerun; cleared after the following compile check succeeds |
| `tests_up_to_date` | `bool` | TestWriterAgent / Orchestrator | Set True after test write; reset to False when Fixer changes production-impacting files; preserved for test-only fixer changes on recognized test artifact paths so validation can rerun without redundant test-writer passes while security review still reruns for the executable test changes. A pending test-writer audit takes precedence over this flag on resume. |
| `worktree_path` | `str \| None` | `cmd_run()` in `sikula_cli/run.py` / `cmd_review()` in `sikula_cli/review.py` | Absolute path of the effective project root within the worktree — equals `worktree_base` when `root_path` is itself a git root, or `worktree_base/<rel>` for subdirectory projects; used as `cwd` by all agents; `None` for `--no-isolate` runs |
| `worktree_base` | `str \| None` | `cmd_run()` in `sikula_cli/run.py` / `cmd_review()` in `sikula_cli/review.py` | Absolute path of the git worktree root (where `git add/commit/worktree remove` run); equals `worktree_path` when project is its own git root; `None` for `--no-isolate` runs |
| `worktree_branch` | `str \| None` | `cmd_run()` in `sikula_cli/run.py` / `cmd_review()` in `sikula_cli/review.py` | Branch name for the worktree; `sikula/<stem>-<task_id>` for `cmd_run()`; the existing PR branch name for `cmd_review()`; `None` for `--no-isolate` runs |
| `result_commit` | `str \| None` | `_finalize_worktree()` in `sikula.py` | Commit SHA created by Sikula when an isolated `run` or `review --fix` task finalizes with file changes; `None` for report-only review, `--no-isolate`, or runs with no commit to create |
| `history` | `list[dict]` | `state.record()` | Append-only audit log: agent, action, result, timestamp, elapsed_s, plus action-specific entries such as `llm_retry` provider/model/attempt fields and `write_path_warning` write-scope audit messages; in step mode, `step_start` / `step_done` orchestrator entries delimit each step's events |
| `runtime_metadata` | `dict` | `StateStore.create()` / `cmd_review()` in `sikula_cli/review.py` | Runtime snapshot captured when the task state is created: Sikula package version when available, Python version, platform, system, and machine. Used for later debugging only |
| `final_summary` | `dict` | `JsonStateStore.save()` | Compact terminal summary written when the state reaches an audit-terminal result: normal `done` / `failed`, or for current-branch review-fix delivery only after `review_delivery_status` becomes `"delivered"`, `"no_changes"`, or `"failed"`. Pending/committed current-branch delivery keeps this empty so audit timing reflects delivery completion, not just orchestrator completion. The summary includes result, branch, commit, build/test/check status, counts for files, validation records, fix attempts, review records, test-writer runs, test audit records, LLM retries, history events, timestamps, and wall elapsed time when available. The CLI also derives a human-readable completion report from the same state, including validation status, review status, audit warnings, sampled unique testability gap details, and recovered issues. |
| `done` | `bool` | Orchestrator | Set True on passing build or after implement in no-build mode when no active deterministic audit finding still requires the build/fix loop |
| `failed` | `bool` | Orchestrator | Hard abort: set True on review timeout, active build/fix loop iteration limit reached, or unhandled agent exception; loop exits immediately. Use `--reset-failed` CLI flag to clear this and resume for normal run and `review --fix` tasks; audit-only failures such as contract-gate failures before worktree creation and report-only review failures are not reset or resumed. The flag resets `review_iterations`, `security_review_iterations`, `build_iterations`, and active build-loop markers, clears `errors`/`test_errors`/`check_errors` (prevents stale error blobs from appearing in the fixer's prompt on the first resumed iteration), and auto-populates `files_changed` from `git diff` if empty. Sync, build, and check failures are NOT hard aborts — they store the error and run the fixer |
| `finished_at` | `str \| None` | `JsonStateStore.save()` | ISO-8601 UTC timestamp set once when the task first reaches an audit-terminal result; not overwritten by later terminal saves. For current-branch review-fix, pending/committed delivery does not set this timestamp even when the orchestrator has set `done = True`; it is set only when delivery reaches `"delivered"`, `"no_changes"`, or `"failed"`. |
| `plan` | `list[str]` | PlannerAgent | Ordered step descriptions; empty = single-pass mode |
| `plan_decided` | `bool` | PlannerAgent | Set True after any successful planner decision (SINGLE_PASS or split); guards re-run on resume; not set on planner failure (allows retry) |
| `plan_completed` | `bool` | Orchestrator | Set True after the final planned step completes its step-scoped implement/review/security/test-write phase. On resume, skips the step loop and continues with the final full-task gate/build instead of rerunning the last step. |
| `active_scope` | `str \| None` | Orchestrator | Transient/persisted scope signal for agent prompts. `None` means normal single-pass or current-step behavior; `"final_full_task"` means all planned steps are complete and review/security/test-writer/implementer-fix prompts must evaluate the complete task instead of the last step. |
| `final_full_task_review_done` | `bool` | Orchestrator | Set True after the final full-task reviewer/security/test-writer gate has completed for the current files. Reset when final-scope fixer changes code, then set True again after the post-fix final-scope review/security/test pass. |
| `current_step` | `int` | Orchestrator | Index into `plan`; advances after each step completes its implement/review/security/test-write phases. With `run_build_per_step: true`, each step also passes build/fix before advancing; otherwise build/fix is deferred until all steps are complete. |
| `step_implemented` | `bool` | Orchestrator | Set True after implementer succeeds for the current step; reset on step transition; guards re-runs on resume |
| `pid` | `int \| None` | `Orchestrator.run()` | PID of the orchestrator process; set at the start of every run (including resume); used by `sikula status` to detect interrupted tasks. A fresh `active_operation` heartbeat takes precedence when the PID is not visible across process namespaces; otherwise, if the PID is no longer running, status shows `INTERRUPTED`. |
| `created_at` | `str` | `StateStore.create()` | ISO-8601 UTC timestamp set once at task creation; never overwritten |
| `updated_at` | `str` | `JsonStateStore.save()` | ISO-8601 UTC timestamp refreshed on every save; reflects last mutation |

---

## BuildTool interface (`tools/base_tool.py`)

The orchestrator loop calls a small fixed interface on the registered `"build"` tool.
`env_files()` is a static method called by `cmd_run()` in `sikula_cli/run.py` and `cmd_review --fix` in `sikula_cli/review.py` when creating a worktree.
Everything else (assemble, …) are platform-specific extras on the subclass. BuildTool methods
return `ToolResult`.

| Method | Contract | AndroidGradleTool impl |
|---|---|---|
| `generate_sources()` | Generate build-time sources before the analyst runs; must tolerate pre-existing compile errors in unrelated modules. Default implementation delegates to `sync()` — override for platform-specific behaviour. | `./gradlew <build.presync_task> --parallel` (default task: `generateDebugSources`; use `openApiGenerateAll` to skip compile dependencies) |
| `sync()` | Resolve deps + generate sources before the first build | `./gradlew generateDebugSources --parallel` |
| `compile_check()` | Compile / type-check the project | `./gradlew <build.compile_task>` — task is configurable per project (see below) |
| `run_tests()` | Run the project unit test suite | `./gradlew <build.test_task>` — task is configurable per project (see below) |
| `run_check(name, task_config)` | Run a named quality check (lint, detekt, …). `task_config` is the opaque dict from `build.checks[i]` in the project YAML — the orchestrator passes it through unchanged; each BuildTool subclass interprets it. | `task_config["command"]` is the shell command to run (falls back to `name` if absent). `task_config["timeout"]` overrides the compile timeout. Uses `_run_shell()` — identical interface to PythonTool. |
| `is_build_config_file(path)` | True if the file affects the build graph | `*.gradle`, `*.gradle.kts`, `*.properties`, `*.toml`, `gradle/`, `buildSrc/`, `build-logic/` |
| `is_sync_adoptable_file(path)` | True if `sync()` may intentionally update this source-controlled project-relative path and the final branch diff should include it when the file already exists as a tracked file. Default returns `False`; platform subclasses only classify paths, while the orchestrator owns adoption, cleanup, audit records, and stale semantic gates. Brand-new generated outputs require explicit `build.sync_adopt_paths` opt-in. | Gradle dependency lock files and verification metadata; other platforms classify their own lockfiles such as `Cargo.lock`, Node package-manager lockfiles, or SwiftPM `Package.resolved`. |
| `is_test_only_change(path, before, after)` | Optional conservative hook for mixed source/test files during test-failure fixer audit. Default returns `False`; platform subclasses may return `True` only when syntax-aware diff analysis proves the fixer changed test-only code. | Cargo treats edits limited to an already-existing Rust `#[cfg(test)] mod tests` block as test-only. |
| `env_files()` *(static)* | Filenames of gitignored files that must be present for the build; copied from the original project root to each new worktree. Default returns `[]`. | `["local.properties"]` (SDK path) |

#### `project` config keys

| Key | Required | Default | Description |
|---|---|---|---|
| `project.root_path` | no | `"."` | Project root; `"."` (the default) resolves to the directory containing `.sikula/config.yaml`; use an absolute path only when the config lives outside the project tree |
| `project.build_tool` | no | `"gradle-android"` | Selects the `BuildTool` implementation: `"gradle-android"` → `AndroidGradleTool`; `"gradle-jvm"` → `JvmGradleTool`; `"maven"` → `MavenTool`; `"node"` → `NodeTool`; `"python"` → `PythonTool`; `"cargo"` → `CargoTool`; `"xcodebuild"` → `XcodeTool` |
| `project.platform` | no | — | Target platform (e.g. `Android`, `iOS`); injected into agent prompts as part of tech stack |
| `project.language` | no | — | Tech stack language (e.g. `Kotlin`, `Python`); injected into agent prompts |
| `project.ui` | no | — | UI framework (e.g. `Jetpack Compose`); injected into agent prompts |
| `project.name` | no | — | Human-readable label; not parsed by any component |

#### Top-level phase flags (`.sikula/config.yaml`)

These keys enable or disable orchestration phases. All default to `true` except `run_presync` and `run_build_per_step`.

| Key | Default | Description |
|---|---|---|
| `run_presync` | `false` | Run `BuildTool.generate_sources()` before the analyst to ensure build-generated sources (OpenAPI DTOs, KSP output, …) exist in `build/`; failure is non-fatal |
| `run_planner` | `true` | Run PlannerAgent after analyze; decides SINGLE_PASS or multi-step split |
| `run_review` | `true` | Run ReviewerAgent after each implement pass; issues feed back to the implementer |
| `run_security_review` | `true` | Run SecurityReviewerAgent after the review phase; independent of `run_review`; blocking issues feed back to the implementer; warnings are logged non-blocking |
| `run_test_writing` | `true` | Run TestWriterAgent after review/security phases complete |
| `run_build` | `true` | Enable the build/fix loop (`compile_check` + optional `run_tests`) |
| `run_tests` | `true` | Run `BuildTool.run_tests()` after each passing build (requires `run_build: true`) |
| `run_checks` | `true` | Run named quality checks (`build.checks`) after tests pass; failures feed the fixer like build/test failures |
| `run_build_per_step` | `false` | Also run build/fix loop after each planned step; the final full-task build/fix loop still runs when `run_build: true` |

#### `progress` config keys

Progress settings live under `progress:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `heartbeat_interval_seconds` | `60` | Seconds between heartbeat updates; `0` disables the heartbeat |

#### `build` config keys — all BuildTools

All keys live under `build:` in `.sikula/config.yaml`.

`sync_adopt_paths` is supported by all BuildTools. It is a string or list of project-relative
path patterns for source-controlled files/directories that `sync()` may intentionally update
and that should be included in the final branch diff. Use it for project-specific generated
artifacts not covered by a platform default, for example `generated/api/` or
`schema/generated/**/*.json`. Keep cache and build-output directories gitignored instead.

| Key | Default | Description |
|---|---|---|
| `sync_adopt_paths` | `[]` | Additional project-relative paths or glob patterns to adopt after successful `sync()`, including brand-new generated files that are intentionally part of the final diff. Directory patterns ending in `/` include all files below that directory. |

#### `build` config keys — PythonTool (`project.build_tool: python`)

All keys live under `build:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `compile_command` | `ruff check .` | Shell command run by `compile_check()` |
| `test_command` | `pytest` | Shell command run by `run_tests()` |
| `timeout` | `300` | Timeout in seconds for all PythonTool operations |
| `checks` | `[]` | List of named quality checks run when `run_checks: true`. PythonTool keys: `name` (display name), `command` (shell command), `timeout` (seconds, defaults to `build.timeout` = 300), optional `fix_command` (shell command to run automatically on failure — see below). Example: `{name: ruff-check, command: "python3 -m ruff check .", timeout: 60}` |

#### `build` config keys — CargoTool (`project.build_tool: cargo`)

All keys live under `build:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `sync_command` | lockfile-aware (`cargo fetch --locked` when `Cargo.lock` exists at the Cargo workspace/project root; otherwise `cargo fetch`) | Shell command run by `sync()`. Omit this key to preserve existing lockfiles while keeping library-style projects without committed lockfiles usable. If Cargo reports that the lockfile needs to be updated during the locked default sync, CargoTool retries once with `cargo fetch` and includes retry details in the sync validation record metadata. Explicit `sync_command` values are run exactly as configured and do not get default fallback behavior. |
| `compile_command` | `cargo check` | Shell command run by `compile_check()`. Use `cargo check --workspace` for workspace projects |
| `test_command` | `cargo test` | Shell command run by `run_tests()`. Use `cargo test --workspace` for workspace projects |
| `timeout` | `600` | Timeout in seconds for all CargoTool operations (sync, compile, test, check). Rust compilation is slower than interpreted languages — 600 s is a safe default |
| `checks` | `[]` | List of named quality checks run when `run_checks: true`. Keys: `name` (display name), `command` (shell command), `timeout` (seconds, defaults to `build.timeout` = 600), optional `fix_command`. Example: `{name: clippy, command: "cargo clippy -- -D warnings", timeout: 120}` |

Failed Cargo test commands preserve Cargo's structured `failures:` block and
`error: test failed, to rerun pass ...` line before generic diagnostic truncation,
so large workspace runs do not let repeated successful harness summaries crowd out
the failing test name, panic/assertion message, or focused rerun command.

#### `build` config keys — NodeTool (`project.build_tool: node`)

All keys live under `build:` in `.sikula/config.yaml`.

`sikula init` detects the package manager from lockfiles or `package.json#packageManager`,
then chooses package-script defaults in this order:

- compile/type-check: `typecheck`, `type-check`, `check-types`, `check`, `build`; when no script exists but `tsconfig*.json` exists, it falls back to `tsc --noEmit` through the package manager.
- tests: `test`
- checks: `lint` and a non-mutating format check script such as `format:check`, with `fix_command` set only when a separate formatter script exists.

| Key | Default | Description |
|---|---|---|
| `package_manager` | auto-detected `npm`, `pnpm`, `yarn`, or `bun` | Package manager used only for default command generation; explicit command fields always win |
| `sync_command` | lockfile-aware install (`npm ci`, `pnpm install --frozen-lockfile`, `yarn install --frozen-lockfile`, `bun install --frozen-lockfile`; plain `install` when no matching lockfile exists) | Shell command run by `sync()` |
| `compile_command` | detected package script, `tsc --noEmit`, or build script fallback | Shell command run by `compile_check()`. Common values: `npm run typecheck`, `pnpm typecheck`, `yarn typecheck`, `npm run build` |
| `test_command` | package-manager test command (`npm test`, `pnpm test`, `yarn test`, `bun run test`) | Shell command run by `run_tests()` |
| `sync_timeout` | `600` | Timeout in seconds for `sync()` |
| `compile_timeout` | `600` | Timeout in seconds for `compile_check()` |
| `test_timeout` | `600` | Timeout in seconds for `run_tests()` |
| `checks` | detected non-mutating package scripts, otherwise `[]` | Named quality checks. Keys: `name`, `command`, `timeout`, optional `fix_command`. Example: `{name: lint, command: "npm run lint", timeout: 120}` |

`is_build_config_file` triggers on `package.json`, lockfiles, workspace files
(`pnpm-workspace.yaml`, `lerna.json`, `rush.json`), `tsconfig*.json`, `jsconfig.json`,
common framework/tool config files (`vite.config.*`, `next.config.*`, `eslint.config.*`,
`vitest.config.*`, etc.), files under `.yarn/`, and files under `patches/`.

#### `build` config keys — XcodeTool (`project.build_tool: xcodebuild`)

All keys live under `build:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `scheme` | `"Countries"` | Xcode scheme passed to all `xcodebuild` invocations; set to the scheme name that builds and tests the app |
| `destination` | `"generic/platform=iOS Simulator"` | Build destination for `compile_check()` — generic simulator avoids requiring a specific device |
| `test_destination` | `"platform=iOS Simulator,OS=latest,name=iPhone 16"` | Test destination for `run_tests()` — requires a running simulator or one resolvable by name |
| `compile_timeout` | `1800` | Timeout in seconds for `compile_check()` and `sync()` (SPM dependency resolution) |
| `test_timeout` | `1800` | Timeout in seconds for `run_tests()` |
| `checks` | `[]` | List of named quality checks run when `run_checks: true`. Keys: `name` (display name), `command` (shell command), `timeout` (seconds, defaults to `compile_timeout`), optional `fix_command`. Example: `{name: swiftlint, command: "swiftlint lint --strict .", timeout: 120}` |

`is_build_config_file` triggers on: `Package.swift`, `Package.resolved`, `*.xcconfig`.

#### `build` config keys — AndroidGradleTool (`project.build_tool: gradle-android`, default)

All keys live under `build:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `presync_task` | `generateDebugSources` | Gradle task run by `generate_sources()` (presync phase). Use `openApiGenerateAll` when `generateDebugSources` fails due to pre-existing compile errors in unrelated modules |
| `presync_clean` | `false` | Run `clean` before `presync_task`. Removes stale generated files (e.g. old DTOs from removed spec entries). Significantly slower — enable only when incremental build produces outdated sources |
| `compile_task` | `compileDebugKotlin` | Gradle task run by `compile_check()` — see table below |
| `test_task` | `testDebugUnitTest` | Gradle task run by `run_tests()` (only when `run_tests: true`) |
| `sync_timeout` | `1800` | Timeout in seconds for `sync()` and `generate_sources()` |
| `compile_timeout` | `1800` | Timeout in seconds for `compile_check()` |
| `test_timeout` | `1800` | Timeout in seconds for `run_tests()` |
| `checks` | `[]` | List of named quality checks run when `run_checks: true`. Keys: `name` (display name), `command` (shell command, e.g. `"./gradlew lintDebug"`), `timeout` (seconds, defaults to `compile_timeout`), optional `fix_command` (shell command to run automatically on failure — see below). Example: `{name: lint, command: "./gradlew lintDebug", timeout: 1800}` |

**`compile_task` options:**

| Task | Speed | Catches |
|---|---|---|
| `compileDebugKotlin` | Fast | Kotlin compilation errors only |
| `assembleDebug` | Slow | Kotlin errors + resource errors (R class, strings.xml, …) |

Use `compileDebugKotlin` for Kotlin-only tasks. Use `assembleDebug` when tasks may touch resources (layouts, strings, drawables).

#### `build` config keys — JvmGradleTool (`project.build_tool: gradle-jvm`)

All keys live under `build:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `compile_task` | `classes` | Gradle task run by `compile_check()`. `classes` compiles all sources and triggers annotation processors (Lombok, MapStruct, OpenAPI codegen, …). Use `compileKotlin` or `compileJava` for faster incremental builds when the project has no codegen. |
| `test_task` | `test` | Gradle task run by `run_tests()` |
| `sync_task` | `classes` | Gradle task run by `sync()` |
| `presync_task` | `classes` | Gradle task run by `generate_sources()` (presync phase) |
| `presync_clean` | `false` | Run `clean` before `presync_task` |
| `sync_timeout` | `600` | Timeout in seconds for `sync()` and `generate_sources()` |
| `compile_timeout` | `600` | Timeout in seconds for `compile_check()` |
| `test_timeout` | `600` | Timeout in seconds for `run_tests()` |
| `checks` | `[]` | Named quality checks — same structure as AndroidGradleTool |

#### `build` config keys — MavenTool (`project.build_tool: maven`)

All keys live under `build:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `compile_command` | `./mvnw compile` (or `mvn compile`) | Shell command run by `compile_check()`. Auto-detects `./mvnw`; falls back to `mvn` on PATH. |
| `test_command` | `./mvnw test` | Shell command run by `run_tests()` |
| `sync_command` | `./mvnw dependency:resolve --batch-mode` | Shell command run by `sync()` |
| `presync_command` | `./mvnw generate-sources --batch-mode` | Shell command run by `generate_sources()` (presync phase) |
| `presync_clean` | `false` | Run `mvn clean` before `presync_command` |
| `sync_timeout` | `300` | Timeout in seconds for `sync()` and `generate_sources()` |
| `compile_timeout` | `600` | Timeout in seconds for `compile_check()` |
| `test_timeout` | `600` | Timeout in seconds for `run_tests()` |
| `checks` | `[]` | Named quality checks — same structure as AndroidGradleTool |

`is_build_config_file` triggers on: `pom.xml`, files under `.mvn/`.

#### `fix_command` in check entries (all BuildTools)

Any check entry may include an optional `fix_command` key — a shell command run automatically when the check fails, before the fixer agent is considered. All BuildTools (PythonTool, AndroidGradleTool, JvmGradleTool, MavenTool, NodeTool, CargoTool, XcodeTool) support it: the orchestrator calls `run_check(f"{name}_autofix", {"command": fix_command})`, forwarding `timeout` only when the check entry explicitly sets it; otherwise the BuildTool falls back to its own default.

```yaml
# PythonTool example
checks:
  - name: ruff-format
    command: "python3 -m ruff format --check ."
    fix_command: "python3 -m ruff format ."
    timeout: 60

# AndroidGradleTool / JvmGradleTool example
checks:
  - name: ktlint-format
    command: "./gradlew ktlintCheck"
    fix_command: "./gradlew ktlintFormat"
    timeout: 600
```

When a check fails and `fix_command` is set, the orchestrator runs the fix immediately instead of calling the fixer agent. If the fix succeeds, the check re-runs. If the re-run passes, the task proceeds as if the check had passed on the first try — the fixer agent is never called. If the fix fails or the re-run still fails, the error is treated as a normal check failure and the fixer agent runs on the next loop iteration.

Use `fix_command` only for deterministic, idempotent formatters (e.g. `ruff format`, `ktlint --format`, `./gradlew spotlessApply`) — not for linters or checks that require human judgment.

Do not rely on task descriptions to execute validation commands. Agents may mention or
review them, but only configured build/test/check commands are executable pipeline steps.
Validation command extraction is intentionally explicit: commands are recognized from
backticks, shell code fences, `$`-prompted lines, or command lists under validation-oriented
headings/prefixes such as `Verification:` or `Run:`; Markdown blank separator lines after
the heading are allowed. Prose that happens to start with a tool name is not treated as a
command, and bare tool names such as `cargo` or `npm` are not treated as executable
validation commands.
When `sikula run` task text requires a validation command that is not represented by the
effective pipeline config, Sikula reports a validation coverage gap instead of asking an
agent to run the command manually. In `sikula review` modes, commands found in PR/review
text are informational branch-verification context and do not preflight-abort review/fix.
A command from the same tool family is only a diagnostic near match when flags, targets,
scripts, packages, schemes, or paths differ. Gradle/Maven wrapper spelling for the same
invocation (`./gradlew` vs `gradle`, `./mvnw` vs `mvn`) is accepted
as coverage, as are Python module forms (`python -m pytest` vs `pytest`,
`python -m ruff` vs `ruff`), the npm `test` shortcut (`npm test` vs `npm run test`),
and pnpm/Yarn package-script shorthands for common validation scripts (`pnpm typecheck`
vs `pnpm run typecheck`, `yarn lint` vs `yarn run lint`). Run-task validation coverage
gaps are not fixed inside the current task worktree: update the Sikula config file used
for the run (default `.sikula/config.yaml`, or the file passed with `--config`) or the
task and rerun so the effective pipeline is loaded with the right command set.

#### `planner` config keys

All keys live under `planner:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `max_steps` | `8` | Maximum number of steps the planner may produce; injected into the prompt as an upper bound and enforced before implementation starts |
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the planner's system prompt as `## Project-specific rules` with an explicit override statement. Scope: task-splitting decisions only — which concerns to split, which to keep atomic. Has no effect on what individual agents do. When the planner runs in an isolated worktree, the file must exist as a file blob in the worktree start ref, be tracked by git, and be clean before worktree creation. |

#### `reviewer` config keys

All keys live under `reviewer:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the reviewer's system prompt as `## Project-specific rules`. Use for project-specific correctness checks: invariants, architecture constraints, thread safety requirements. The reviewer is read-only — these rules cannot trigger file writes. When the reviewer runs in an isolated worktree, the file must exist as a file blob in the worktree start ref, be tracked by git, and be clean before worktree creation. |

#### `security_reviewer` config keys

All keys live under `security_reviewer:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the security reviewer's system prompt as `## Project-specific rules`. Use for project-specific security requirements: compliance rules (GDPR, PCI), data classification, threat model specifics. Appended before the BLOCKING/WARNING output format — project rules take priority over the defaults. When the security reviewer runs in an isolated worktree, the file must exist as a file blob in the worktree start ref, be tracked by git, and be clean before worktree creation. |

#### `test_writer` config keys

All keys live under `test_writer:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `coverage_target` | `90` | Minimum branch+line coverage % the agent must aim for on new/changed code within the configured test surface |
| `test_surface_policy` | `existing_infrastructure` | `existing_infrastructure` stays within existing project test infra and does not treat missing heavy UI/browser/device/runtime harnesses as gaps by themselves; `complete` opts in to `TESTABILITY GAP` reports when important behaviour needs missing test infra outside the existing surface. The test writer prefers behavioural seams and should not replace missing UI/browser/device/runtime harnesses with broad source-inspection tests, synthetic runtime/framework harnesses, or skipped/disabled tests that the configured validation will not execute. |
| `testability_gap_policy` | `warn` | `warn` records visible test-writer `TESTABILITY GAP` entries and allows the task to continue; `fail` records the same entries and fails the task |
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the test writer's prompt as `## Project-specific rules`. Use for project-specific testing conventions: required test doubles, naming patterns, mandatory parametric table rules. Note: unlike the analyst, reviewer, and security reviewer, the test writer does not have guidelines content pre-loaded — it reads `guidelines.context_files` via its file tools. `extra_rules` is the correct configuration point for test-specific conventions that the test writer should apply without needing to read the full guidelines. When the test writer runs in an isolated worktree, the file must exist as a file blob in the worktree start ref, be tracked by git, and be clean before worktree creation. |

---

## Extending the system

### Add an agent

1. Create `agents/your_agent.py`, subclass `BaseAgent`, implement `run(state) -> AgentResult`.
2. Register in `Orchestrator.__init__`: `self._agents["your_agent"] = YourAgent(_llm("your_agent"), self._tools, pc)`.
   The `_llm()` helper returns the per-agent override from config if present, otherwise the default LLM.
   Also add the agent name to the `agent_llms` comprehension in `sikula.py` so per-agent LLM overrides are picked up.
3. Call it from the right method in `Orchestrator`:
   - Phases that run once per task (before or after the main cycle): `_loop()`
   - Phases that run once per step in a multi-step plan: `_run_single_step()`
   - Phases that apply to both single-pass and per-step mode: both `_run_single_pass()` and `_run_single_step()`
4. Update this document and the capabilities table in `README.md`.

### Add a platform (iOS, backend, …)

1. Create `tools/maven_tool.py` (or `tools/<platform>_tool.py`), subclass `BuildTool`.
2. Implement `sync()`, `compile_check()`, `run_tests()`, `run_check()`, `is_build_config_file()` for that platform.
   Override `env_files()` if the platform needs gitignored files copied to new worktrees (e.g. `Secrets.xcconfig`, `.env`).
   Override `is_sync_adoptable_file()` only for source-controlled outputs that `sync()` may
   intentionally update and that should enter the final diff. Override
   `is_test_only_change()` only when syntax-aware diff analysis can prove mixed
   source/test file edits are test-only; otherwise leave it fail-closed.
   Optionally override `generate_sources()` if `sync()` is too broad for the presync phase —
   the default calls `sync()` which is fine when sync doesn't trigger compilation:
   - **iOS / SPM**: `sync()` resolves SPM dependencies (no compilation) → default is fine.
     Override `generate_sources()` only if the project uses codegen tools (Apollo, Sourcery, SwiftGen).
   - **Maven**: `sync()` resolves deps → default is fine.
     Override `generate_sources()` to run `mvn generate-sources` if the project uses OpenAPI Generator or similar.
   If `generate_sources()` emits source/IDL files under gitignored build output paths
   that analyst/reviewer/security-reviewer agents must inspect, update the
   Antigravity read-only generated-source preservation rules in `core/llm_client.py`
   and add regression coverage in `tests/test_llm_client.py`; keep gitignored secrets
   and environment files excluded from provider workspace copies.
3. Add a branch to `_build_tool()` in `core/orchestrator.py`: check `project_config["project"]["build_tool"] == "your_build_tool"` and return an instance of your new class. Add the new build tool name to the docstring comment beside the factory.
4. Add auto-detection to `tools/scanner.py`: add an entry to `_SIGNATURES` (trigger files, build tool name, language, platform) and implement path detection helpers (`_detect_<platform>_paths()`).
5. Extend `generate_config()` in `sikula_cli/init.py` to emit the platform-specific `build:` block so that `sikula init` generates a correct config for the new platform.
6. Create `.sikula/config.yaml` in the project directory with:
   - `project.build_tool: your_build_tool` — must match the branch key added in step 3
   - `project.platform: iOS` (or `Android`, `backend`, …) — injected into agent prompts as tech stack context
   - `sandbox.allowed_write_paths` — writable source directories
   - `sandbox.allowed_test_write_paths` — writable test directories (`"."` = entire project root for projects with inline tests anywhere under the repo)
   - `sandbox.allowed_read_paths` — readable directories (`"."` = entire project root)
   - `guidelines.context_files` — platform-specific guidelines docs
   - `guidelines.max_file_chars` — max chars read per guidelines file
7. Update `tests/test_platform_onboarding.py` so the supported build tool set, env-file
   factory, orchestrator factory, scanner surface, and generated init config stay in sync.
8. If the platform supports inline tests in source files with suffixes not already covered,
   update `_TEST_GATE_AUDIT_SOURCE_SUFFIXES` in `core/orchestrator.py`. If it introduces
   test framework skip/disable/ignore/expected-failure/assumption idioms that are not
   already covered, update
   the test execution gate audit registry in `core/test_execution_gate_audit.py` and its
   coverage in `tests/test_test_execution_gate_audit.py`. These are audit registries, not
   platform-specific orchestration logic.
9. If the platform introduces common fake runtime idioms not already covered by the
   synthetic harness detector, update `core/synthetic_test_harness_audit.py` and
   `tests/test_synthetic_test_harness_audit.py`. This remains a soft audit and recovery
   pattern registry, not platform-specific orchestration logic.
10. Update this document.

The agents and the orchestration loop need no changes — they are platform-neutral by design.

### Add a general-purpose tool

1. Create `tools/your_tool.py`, subclass `BaseTool`.
2. Instantiate in `Orchestrator.__init__`: `self._tools["your_tool"] = YourTool(sandbox, root)`.
3. Pass `self._tools` to any agent that needs it (agents receive the full dict).
4. Update this document.

### Configure per-agent LLM

Each agent can use a different LLM model or provider. Add an `agents:` block to the project
YAML; any field omitted falls back to the top-level `llm:` section:

```yaml
llm:
  provider: codex
  model: gpt-5.3-codex

agents:
  analyst:
    llm:
      model: gpt-5.5           # stronger model: analyst output determines the entire task outcome
  reviewer:
    llm:
      model: gpt-5.5           # stronger model: thoroughness matters more than speed here
  security_reviewer:
    llm:
      model: gpt-5.5           # stronger model: must reliably detect subtle security issues
```

`build_orchestrator()` in `sikula.py` reads the per-agent overrides and passes a
`agent_llms: dict[str, LLMClient]` to `Orchestrator`. Agents without an override use the
default client. The analyst, reviewer, and security reviewer benefit most from a stronger model:
the analyst's output determines the outcome of the entire task; the reviewer and security reviewer
need strong reasoning to catch subtle issues reliably.

---

### Add an LLM provider

Five providers are built in: `CodexClient` (`provider: "codex"`), `ClaudeClient` (`provider: "claude"`),
`GeminiClient` (`provider: "gemini"`), `OpenCodeClient` (`provider: "opencode"`, model in
`provider/model` format), and `AntigravityClient` (`provider: "antigravity"`).
See [Providers](docs/providers.md) for provider setup and the extension entry point. Three methods must be implemented in `core/llm_client.py`:

| Method | Used by | Contract |
|---|---|---|
| `generate(system, user) -> str` | PlannerAgent, DeliveryPreparationAgent | Single-shot text generation; returns the model's text response |
| `run_readonly_agent(prompt, cwd) -> str` | AnalystAgent, ReviewerAgent, SecurityReviewerAgent | Runs the model as an autonomous read-only agent in `cwd`; returns text output (stdout) |
| `run_agent(prompt, cwd) -> tuple[list[str], str]` | ImplementerAgent, TestWriterAgent, FixerAgent | Runs the model as an autonomous agent with file read/write tools in `cwd`; returns `(changed_file_paths, agent_text_output)` — paths via git diff, text best-effort |

The `system` argument passed to `generate` and the `prompt` argument passed to `run_readonly_agent` and `run_agent` already contain `AGENT_SECURITY_PREFIX` (defined in `agents/base_agent.py`) — the network and filesystem constraint is injected by each agent before calling the provider. Provider implementations do not need to add it.
For CLI-backed providers, `LLMConfig.agent_timeout` applies to provider subprocess calls for `generate`, `run_readonly_agent`, and `run_agent`; `delivery_preparer` timeout overrides therefore apply to delivery prepare authoring even though it uses `generate()`.

CLI-backed providers should pass large prompts through stdin or another non-argv input channel
when the provider CLI supports that mode. Reviewer, analyst, and implementation prompts can
exceed operating-system command-line argument limits on large tasks. If a provider CLI requires
the prompt as an option value for non-interactive mode, preserve that provider contract.

CLI-backed agent calls use `_run_agent_subprocess_streaming()` for subprocess lifecycle
management. It starts stdout/stderr reader threads, reads real provider pipes with non-line
`os.read()` chunks plus incremental text decoding, streams chunks through queues, keeps
provider output for diagnostics and structured error parsing, and enforces `agent_timeout`
while both waiting for the provider process and draining queued provider output. The non-line
pipe read is intentional: structured provider errors may be emitted without a trailing newline
while the provider process remains alive. The timeout path terminates the whole provider process
group before raising `subprocess.TimeoutExpired`, which the built-in provider wrappers convert
into `LLMTimeoutError`.

---

## Retry behaviour (`core/llm_client.py`)

Retry is implemented by Sikula's built-in CLI-backed providers (`CodexClient`,
`ClaudeClient`, `GeminiClient`, `OpenCodeClient`, `AntigravityClient`), not by the abstract `LLMClient`
interface itself. Custom providers must implement their own retry policy if they need one.

Provider subprocess output is classified into typed `LLMProviderError` subclasses. Fatal
provider/account failures such as quota exhaustion (`LLMQuotaExceeded`), authentication
failure (`LLMAuthError`), and invalid provider/model configuration
(`LLMConfigurationError`) are not retried. Retryable failures use `LLMTransientError`;
subprocess timeouts are wrapped as `LLMTimeoutError`. Local provider subprocess
`OSError` failures with permission, read-only filesystem, quota, or disk-space errno
values are converted to `LLMEnvironmentError` for read-only and write-agent calls;
free-form provider stderr/stdout text is not classified as an environment error by
filesystem phrases alone.

The built-in providers retry `generate()` and `run_readonly_agent()` through
`_call_with_retry()` only for retryable failures. Up to **4 attempts** total are made, with
delays of 30 s, 60 s, and 120 s between them (`_RETRY_DELAYS`).

When an agent is run through Sikula's orchestration or report-only review path, each retry
attempt before the next sleep is appended to `state.history` as `action = "llm_retry"`.
The entry stores provider, model, operation, attempt number, max attempts, delay, error type,
and a truncated provider error message.

The built-in providers also retry `run_agent()` only for retryable failures, with an
additional safety guard:

**`run_agent` safety guard:** after each failure the implementation takes a fresh git snapshot.
If files were changed before the failure, the retry is skipped — running the agent again on a
partially-modified workspace would produce an inconsistent state. The error is re-raised
immediately in that case.

## Audit history signals

`state.history` stores lightweight audit events that are useful for debugging but do not
drive pipeline decisions. The CLI surfaces these signals in terminal completion reports
without changing the terminal success/failure decision. Two important examples:

- `llm_retry` — recorded by `core/retry_history.py` while a provider call is being retried.
  The entry includes provider, model, operation, attempt, max attempts, delay, error type,
  and a truncated provider error message.
- `write_path_warning` — recorded by `agents/base_agent.py` after a write-capable agent
  returns changed files outside the active write scope. Implementer uses
  `allowed_write_paths`, test writer uses `allowed_test_write_paths`, and fixer uses the
  active scope selected for the current error type. This is an audit signal only: it does
  not fail the task and it only covers files reported by the provider's `run_agent()` result.
