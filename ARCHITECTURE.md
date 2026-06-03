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
| `TaskState` | `core/state.py` | Single source of truth; persisted as JSON after every agent operation |
| `JsonStateStore` | `core/state.py` | Stores each task as `<task_id>.json` in the configured state dir; serializes same-process access and writes via temp-file replacement so heartbeat updates and audit saves cannot interleave partial JSON writes |

---

## Run flow (`sikula run`)

`cmd_run()` in `sikula.py`. Wraps `Orchestrator.run()` with worktree setup, finalization, and resume logic.

**New task (`--task-file`):**

```
cmd_run()
   │
   ├─ guard: for isolated runs, loaded config and guidelines.context_files
   │     must exist, be tracked, and be clean relative to HEAD; otherwise
   │     fail before creating TaskState/worktree
   │
   ├─ read task file  →  TaskState created (state.task_id = uuid4().hex)
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

**Cleanup/delete commands:**

```
sikula cleanup <task_id>          # dry run
sikula cleanup <task_id> --force  # remove worktree, keep state JSON
sikula delete <task_id>           # dry run
sikula delete <task_id> --force   # remove worktree and state JSON
```

Both commands refuse dirty worktrees unless `--discard` is passed. `cleanup` records a
`history` entry and clears `worktree_path` / `worktree_base`, preserving the state for
audit while making resume impossible. `delete` removes the state JSON after worktree
cleanup. Forced cleanup/delete also refuse to remove a worktree that contains the
current process directory, so a user's shell is not left inside a deleted tree.

**Status command:** `sikula status` derives a compact task overview from state JSON. It
reports terminal states (`DONE`, `FAILED`, `CLEANED`), interrupted runs whose recorded PID
is no longer alive, current pipeline phase, planner step progress, build/fix iteration
count, and last update age. `--verbose` adds the next useful command for each row,
`--json` emits the same derived rows for scripts, and `--active` / `--failed` /
`--cleaned` / `--done` filter the list. When invoked inside a preserved task worktree,
config auto-discovery maps back to the original project root so status reads the original
`.sikula/state` directory instead of the worktree copy. Fresh `sikula run TASK_FILE`
is refused from inside a task worktree to avoid accidentally starting a new task from
the original checkout while reading the task file from the preserved worktree. Resume
via `sikula run --task-id <task_id>` is supported from inside the worktree; before
finalization Sikula switches the process directory back to the original project so the
worktree can be removed safely.

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
      │        state.review_approved = False                  │
      │        state.security_approved = False                │
      │        state.tests_up_to_date = False                 │
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

`cmd_review()` in `sikula.py`. Isolates an existing branch in a git worktree, runs the reviewer and security reviewer against a PR-style diff, and exits with a summary. Report-only review uses the initial computed diff; `review --fix` refreshes the diff as fixes are made.

**Setup (both modes):**

```
cmd_review()
   │
   ├─ guard: --description or --description-file is required
   │     (review scope must be explicit; no generated fallback)
   │
   ├─ worktree creation (differs by mode):
   │    report-only: git worktree add --detach .sikula/worktrees/<task_id> <sha>
   │                 (detached HEAD — works even when caller is on the reviewed branch)
   │    --fix:       git worktree add .sikula/worktrees/<task_id> <branch>
   │                 (real branch checkout — required so _finalize_worktree can commit)
   │                 + copy gitignored build files via BuildTool.env_files()
   │                   (e.g. local.properties on Android — same as cmd_run())
   │
   ├─ git diff <base_branch>...<branch>  →  state.review_diff
   │    (initial three-dot diff: all commits introduced by branch)
   │
   ├─ git diff --name-only <base_branch>...<branch>  →  state.files_changed
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
```

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

   worktree removed on completion regardless of outcome
   exit 0 (approved) or 1 (issues found)
```

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

```
   Orchestrator.run()
      │
      ├─ state.done = True, files changed →
      │     git commit to <branch>: "sikula: review fixes for <branch>"
      │     worktree removed
      │
      ├─ state.done = True, no files changed →
      │     worktree removed ("no fixes needed")
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
| Resume | Supported via `--task-id` | Report-only review is not resumed; `review --fix` uses regular task resume (`--reset-failed` required for terminal failed state) |
| Report-only path | No | Yes — bypasses orchestrator entirely |

---

## Init flow (`sikula init`)

`cmd_init()` in `sikula.py`. Scans the project, generates `.sikula/config.yaml`, and optionally generates `.sikula/guidelines.md` via `InitAgent`.

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
   ├─ _generate_config():
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
listed as part of the suggested commit too. The first isolated run enforces the loaded
config and every file referenced by `guidelines.context_files`.

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
- A numbered list of 2–N steps — each compilable in isolation

When producing steps, the planner keeps compile dependencies with the step that first
uses them. For example, a step that references a new localization key, route/API
constant, service registration, or interface method must also create/update that
dependency in the same step. If that makes the split unclear, planner should choose
`SINGLE_PASS` or merge the coupled work into one step.

**Output written to state:**
- `state.planner_prompt` — full assembled prompt sent to the planner LLM (system + user sections); stored before the LLM call
- `state.plan_decided = True` — set after every successful decision; guards re-run on resume
- `state.plan` — list of step description strings (only set when splitting; stays empty for SINGLE_PASS)
- `state.current_step = 0` — reset to start (only when splitting)

**Fallback:** if the output is neither `SINGLE_PASS` nor parseable into 2+ numbered steps,
`state.plan` stays empty, `state.plan_decided` is still set, and the orchestrator uses single-pass behavior.
The planner only retries on resume if it previously failed with an exception (`plan_decided` not set).

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
  After each write-capable agent call, Sikula compares the files reported by that call
  with the active write path list and records a non-blocking `write_path_warning` in
  `state.history` when a file falls outside it. This is an audit signal, not a hard
  sandbox failure.
- *Bash restriction* — Sikula prompts agents to use only `grep *`, `find *`, `ls *`, and
  `git rm *`; no network tools and no destructive shell commands. Provider-level
  enforcement varies below. When `git rm` is used, deletions are tracked by git, visible in
  `git diff`, and reversible.
  For `CodexClient`: prompt-level for write agents; `--sandbox read-only` blocks file writes
  at the OS level but does not per-command filter shell execution.
  For `ClaudeClient` this is technically enforced via `--allowedTools` for all agents.
  For `GeminiClient`: technically enforced for read-only agents (`run_shell_command` excluded
  from `tools.core`); prompt-level for write agents.
  For `OpenCodeClient`: technically enforced for read-only agents (`bash: deny`); prompt-level
  for write agents.
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

**Re-review after fixer:** if `FixerAgent` changes any file, the orchestrator resets
`state.review_approved = False` and first reruns deterministic build/test/check
validation. The review loop reruns after validation is green; if review fixes change
files, build/test/check validation runs again before the task or step can be accepted.
In `review --fix`, test-writer changes receive one final validation-only reviewer
pass; rejection fails the task rather than feeding another fix cycle.

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

**Reset after fixer:** if `FixerAgent` changes any file, `state.security_approved` is reset
to `False`. After build/test/check validation is green again, the security review re-runs
after the review loop.

---

### TestWriterAgent (`agents/test_writer_agent.py`)

Runs after the review loop is approved (Phase 4), and again after fixer changes once
deterministic build/test/check validation is green. Skipped when `run_test_writing: false`
or `state.tests_up_to_date` is already set.

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
    when `test_writer.testability_gap_policy: fail` is configured.

**Output written to state:**
- `state.tests_up_to_date = True` — set on success regardless of whether files changed
- `state.files_changed` — test file paths appended (de-duplicated)
- `state.test_files_written` — same paths also appended here (de-duplicated); used by ReviewerAgent to exempt these files from scope violation checks. In `sikula review` mode, the files are still reviewed for correctness and relevance.
- `state.test_write_records` — one record appended per invocation with `step`, `build_iteration`, `scope`, `test_surface_policy`, `test_writer_prompt`, `test_writer_output` (`None` on exception), `files_written`, and `timestamp`
- `state.testability_gaps` — one record per `TESTABILITY GAP` reported by the test writer, with `source`, `step`, `build_iteration`, optional `scope`, the raw gap message, and any parsed `target`, `reason`, `recommended_action`, and `risk` fields. `tests_up_to_date` still becomes `True`; the gap means the test writer did all it safely could for the current diff under the configured test surface, not that full behaviour coverage exists.

**Reset after fixer:** if `FixerAgent` changes any file, the orchestrator resets
`state.tests_up_to_date = False`. After build/test/check validation is green again,
the test write phase reruns after review/security gates (only when `run_test_writing: true`).
If the test writer changes files, build/test/check validation runs again. In `review --fix`,
test-writer changes are reviewed once as a final validation gate and do not trigger another
test-writing loop.

---

### FixerAgent (`agents/fixer_agent.py`)

**Constraints given to the agent:** fix only what the errors describe — no refactoring, no unrelated changes. Both the write-path allowlist and the test file constraint are context-dependent:

| Error type | `allowed_write_paths` used in prompt | Test files |
|---|---|---|
| Build errors (`state.errors` non-empty) | `sandbox.allowed_write_paths` (production dirs) | Off-limits |
| Build/check errors whose diagnostic references all point at test files or recognized test targets | First pass: `sandbox.allowed_test_write_paths`. Retry pass: same test-only write paths, only after the first pass violated scope and Sikula restored that pass's writes. Second production-enabled pass, only after no-change `production_defect` + `production_code` triage: `sandbox.allowed_write_paths` + `sandbox.allowed_test_write_paths` | May repair malformed/stale tests in the first pass. Production writes during a test-only pass are rejected: Sikula restores that pass's writes and retries once; a restore failure or second scope violation fails the task. If diagnostics name production paths or targets too, or name no paths or recognized targets at all, Sikula falls back to the normal build/check scope. |
| Test failures only (`state.test_errors` non-empty, `state.errors` and `state.check_errors` both empty) | First pass: `sandbox.allowed_test_write_paths`. Retry pass: same test-only write paths, only after the first pass violated scope and Sikula restored that pass's writes. Second production-enabled pass, only after no-change `production_defect` + `production_code` triage: `sandbox.allowed_write_paths` + `sandbox.allowed_test_write_paths` | May repair malformed/stale tests in the first pass. If the failing test encodes the original task, implementation prompt, project guidelines, or a structured contract, the test-only pass must report a production defect without changing files so the separate production-enabled pass can fix it. Production writes during a test-only pass are rejected with the same restore-and-retry behaviour as test-origin validation failures. |
| Check errors only (`state.check_errors` non-empty, `state.errors` empty) | `sandbox.allowed_write_paths` + `sandbox.allowed_test_write_paths` | May modify production or test files if explicitly named in the check errors |

**Input:**
- `state.errors[-3:]` — last three build error blobs (if non-empty, labelled "BUILD ERRORS")
- `state.test_errors[-3:]` — last three test failure blobs (if non-empty, labelled "TEST FAILURES")
- `state.check_errors[-3:]` — last three check failure blobs (if non-empty, labelled "CHECK ERRORS")
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
the chosen fix is production code or test code. If it chooses `production_code`, it must leave
files unchanged; Sikula then runs a second production-enabled fixer pass and records both
passes. If a test-only pass writes production files, or reports `production_code` while
changing files, Sikula treats the attempt as tainted: it restores every write from that
attempt to the pre-attempt worktree snapshot, records `test_only_scope_violation`, and
retries the test-only pass once with explicit recovery context. A restore failure, a second
test-only scope violation, or a production-confirmed second pass that changes only tests marks
the task failed before the pipeline can accept the change.
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
| `task_file` | `str \| None` | `cmd_run()` in `sikula.py` | Basename of the task file (e.g. `add-login.md`); set on first run via `--task-file`; used by `status` for display; `None` for tasks created before this field was added or when resuming via `--task-id` only |
| `config_snapshot` | `dict` | Orchestrator | Effective run configuration captured on first run (never overwritten on resume): project name, all `run_*` flags, `max_iterations`, `max_review_iterations`, `max_security_review_iterations`, `progress.*`, `sandbox.allowed_write_paths` / `allowed_test_write_paths` / `allowed_read_paths`, `build.*` settings, `test_writer.*` settings, and per-agent `provider`/`model`/`agent_timeout`. Visible in `show <task_id>`. |
| `analyst_prompt` | `str \| None` | AnalystAgent | Full assembled prompt sent to the analyst LLM (system + user sections, including inlined guidelines content); stored before the LLM call so it captures the exact input even on exception; enables post-run analysis of analyst behaviour |
| `planner_prompt` | `str \| None` | PlannerAgent | Full assembled prompt sent to the planner LLM (system + user sections); stored before the LLM call; `None` when `run_planner: false` or planner not yet reached |
| `implementation_prompt` | `str \| None` | AnalystAgent | Structured prompt fed to ImplementerAgent; the analyst's key output |
| `presync_done` | `bool` | Orchestrator | Set True after Phase 0 presync attempt (success or failure); guards re-run on resume |
| `files_changed` | `list[str]` | Implementer / Fixer | Paths touched so far; used by orchestrator for build-config re-sync detection |
| `build_synced` | `bool` | Orchestrator | Guards unnecessary re-syncs; reset when build-config files change |
| `build_iterations` | `int` | Orchestrator | Total build/fix attempts across the task; used as an audit/correlation counter in validation and agent records |
| `build_loop_key` | `str \| None` | Orchestrator | Active build/fix loop identity (`"task"`, `"step:N"`, or `"final_full_task"`); persisted so resume keeps the same loop budget |
| `build_loop_start_iteration` | `int` | Orchestrator | Global `build_iterations` value at the start of the active build/fix loop; `config.max_iterations` is enforced relative to this value |
| `build_status` | `str \| None` | Orchestrator | `"success"` or `"failed"` |
| `test_status` | `str \| None` | Orchestrator | Final test phase outcome: `"success"`, `"failed"`, or `"skipped"`; `None` until the test phase is reached |
| `check_status` | `str \| None` | Orchestrator | Final configured-check phase outcome: `"success"`, `"failed"`, or `"skipped"`; `None` until the check phase is reached |
| `errors` | `list[str]` | Orchestrator | Build output blobs (stdout+stderr) for current fix cycle; Fixer reads last 3, clears after fix |
| `test_errors` | `list[str]` | Orchestrator | Test failure blobs for current fix cycle; Fixer reads last 3, clears after fix |
| `check_errors` | `list[str]` | Orchestrator | Check failure blobs for current fix cycle (from `run_checks` phase); Fixer reads last 3, clears after fix |
| `review_issues` | `list[str]` | ReviewerAgent | Issue list from last review; cleared on approval; passed to Implementer on fix pass |
| `review_iterations` | `int` | Orchestrator | Fix attempt counter for the current review cycle (counts completed review→implement pairs); resets to 0 after each fixer pass; guarded by `config.max_review_iterations` |
| `review_approved` | `bool` | ReviewerAgent / Orchestrator | Set True on approval; reset to False when Fixer changes files |
| `security_approved` | `bool` | SecurityReviewerAgent / Orchestrator | Set True when security review passes (no blocking issues); reset to False when Fixer changes files or step transitions; guards re-runs |
| `security_review_iterations` | `int` | Orchestrator | Security fix attempt counter for the current cycle (counts completed security-review→implement pairs); independent of `review_iterations`; resets to 0 on step transitions and fixer passes; guarded by `config.max_security_review_iterations` |
| `analyst_warnings` | `list[str]` | AnalystAgent | Warnings produced by the analyst (e.g. ambiguous task scope, missing context); logged for visibility, never block the pipeline |
| `review_diff` | `str \| None` | `cmd_review()` in `sikula.py` / Orchestrator | PR-style diff passed to ReviewerAgent and SecurityReviewerAgent; initially set to `git diff base...branch` (three-dot) in `sikula review` mode; refreshed in `"review_fix"` mode before reviewer/security-reviewer calls so uncommitted fixes are included; `None` in standard `sikula run` flow (agents fall back to `GitTool.diff_head()`) |
| `review_mode` | `str \| None` | `cmd_review()` in `sikula.py` | Review task kind: `"review_report"` for report-only review (not resumable) or `"review_fix"` for `sikula review --fix` (resumable via `sikula run --task-id`) |
| `review_base_branch` | `str \| None` | `cmd_review()` in `sikula.py` | Base branch used to refresh `review_diff` in `"review_fix"` mode. Report-only review keeps the original frozen diff; review-fix refreshes against the merge base before reviewer/security-reviewer calls so fixes are reviewed against the current branch state. |
| `implement_cycle_records` | `list[dict]` | ImplementerAgent | Structured observability — one entry per implementer invocation: `step`, `build_iteration` (`0` = pre-build; `>0` = review/security fix after a post-fixer validation pass), `review_iteration` (`0` = initial or security fix; `>0` = review fix pass N), `security_review_iteration` (`0` = initial or review fix; `>0` = security fix pass N), `scope` (`"task"`, `"step"`, or `"final_full_task"`), `step_description`, `implementer_prompt`, `implementer_output` (`None` on exception), `files_written`, `timestamp`; both iteration counters `== 0` and `build_iteration == 0` means initial implementation; never read for pipeline decisions. **Correlation note:** to find the reviewer record that triggered this implementer, look for a `review_cycle_records` entry with the same `step`, `build_iteration`, and `review_iteration: N-1` |
| `review_cycle_records` | `list[dict]` | ReviewerAgent | Structured observability — one entry per reviewer invocation: `step`, `build_iteration` (`0` = pre-build; `>0` = after a post-fixer validation pass), `review_iteration` (fix-pass index within this step's review loop), `scope` (`"task"`, `"step"`, or `"final_full_task"`), `reviewer_prompt`, `reviewer_output`, `approved`, `has_warnings`, `timestamp`; also read by the reviewer to retrieve its own prior outputs for context. In `final_full_task` scope, reviewer history is limited to earlier final full-task reviews, not step-scoped reviews. **Correlation note:** a reviewer record with `review_iteration: N` that found issues triggered the implementer record with `review_iteration: N+1` — the orchestrator increments the counter before calling the implementer |
| `security_review_cycle_records` | `list[dict]` | SecurityReviewerAgent | Structured observability — one entry per security reviewer invocation: `step`, `build_iteration` (`0` = pre-build; `>0` = after a post-fixer validation pass), `security_review_iteration` (fix-pass index within this step's security review loop), `scope` (`"task"`, `"step"`, or `"final_full_task"`), `reviewer_prompt`, `reviewer_output`, `approved`, `has_warnings`, `timestamp`; also read by the security reviewer to retrieve its own prior outputs for context. In `final_full_task` scope, security history is limited to earlier final full-task security reviews. **Migration note:** state files from schema version 1 stored security reviewer entries inside `review_cycle_records` with `reviewer = "security_reviewer"`; `JsonStateStore.load()` moves them here and removes the redundant `reviewer` field. |
| `test_write_records` | `list[dict]` | TestWriterAgent | Structured observability — one entry per test-writer invocation: `step`, `build_iteration` (`0` = before first build; `>0` = after a post-fixer validation pass), `scope`, `test_surface_policy`, `test_writer_prompt`, `test_writer_output` (`None` on exception), `files_written`, `timestamp`; never read for pipeline decisions |
| `testability_gaps` | `list[dict]` | TestWriterAgent | Structured audit signal for behaviour the test writer could not safely cover within the configured test surface. Entries include `source`, `step`, `build_iteration`, optional `scope`, `message`, `timestamp`, and optional parsed `target`, `reason`, `recommended_action`, and `risk`. Default policy is warning-only; `test_writer.testability_gap_policy: fail` turns reported gaps into task failures. |
| `fix_cycle_records` | `list[dict]` | FixerAgent | Structured observability — one entry per fixer invocation after a failed sync/build/test/check attempt: `build_iteration` (globally unique, never resets), `step`, `scope`, `errors_before` snapshot (build/test/check), `fixer_prompt`, `fixer_output` (`None` on exception), `files_written`, optional `triage_scope` (`test_failure` or `test_origin_validation`), optional `triage_pass` (`test_only`, `test_only_retry`, or `production_confirmed`), optional `confirmed_test_failure_triage`, optional `scope_recovery`, optional `test_only_scope_violation` restore audit, `timestamp`; never read for pipeline decisions |
| `validation_cycle_records` | `list[dict]` | Orchestrator | Structured observability — one entry per presync/sync/build/test/check outcome with `phase`, `status`, `build_iteration`, `step`, `timestamp`, optional `scope`, optional `elapsed_s`, optional `check_name`, and diagnostic `error_excerpt` plus high-signal `diagnostic_summary` lines on failure; excerpts preserve failure-marker blocks from long tool output instead of storing only the final tail, while summaries highlight shortened compiler locations, failed tests, sanitized assertion failures, and linter rules for terminal audit output sampled across failed validation attempts without echoing source-code frames, assertion values, quoted literal payloads, secret-looking key/value tokens, or absolute path prefixes; never read for pipeline decisions |
| `validation_artifact_records` | `list[dict]` | Orchestrator | Structured observability for unexpected non-ignored repository changes produced by build/test/check validation commands. Each record stores `phase`, `status` (`cleaned` or `cleanup_failed`), `build_iteration`, `step`, optional `scope`, optional `check_name`, and changed paths with before/after status. Cleanup success allows validation to continue; cleanup failure is treated as that validation phase failing. |
| `active_operation` | `dict \| None` | Orchestrator | Current long-running operation heartbeat for status visibility while an agent or validation command is blocked. Contains `phase`, optional `agent`, optional `scope`, `started_at`, `last_heartbeat_at`, `heartbeat_count`, optional `heartbeat_interval_seconds`, and optional `message`. Cleared when the operation completes; never drives pipeline decisions. |
| `test_files_written` | `list[str]` | TestWriterAgent | Cumulative list of all files written by the test writer agent across all runs; never cleared; passed to ReviewerAgent so it does not flag those files as implementer scope violations. In normal `sikula run`, these files are not reviewer-owned output; in `sikula review`, changed test files are reviewed as branch output. |
| `fixer_changed_code` | `bool` | Orchestrator | Set True when FixerAgent writes files; used on resume to continue deterministic build/test/check validation before stale semantic gates rerun; cleared after the following compile check succeeds |
| `tests_up_to_date` | `bool` | TestWriterAgent / Orchestrator | Set True after test write; reset to False when Fixer changes files; after validation is green, guards redundant test-writer re-runs |
| `worktree_path` | `str \| None` | `cmd_run()` / `cmd_review()` in `sikula.py` | Absolute path of the effective project root within the worktree — equals `worktree_base` when `root_path` is itself a git root, or `worktree_base/<rel>` for subdirectory projects; used as `cwd` by all agents; `None` for `--no-isolate` runs |
| `worktree_base` | `str \| None` | `cmd_run()` / `cmd_review()` in `sikula.py` | Absolute path of the git worktree root (where `git add/commit/worktree remove` run); equals `worktree_path` when project is its own git root; `None` for `--no-isolate` runs |
| `worktree_branch` | `str \| None` | `cmd_run()` / `cmd_review()` in `sikula.py` | Branch name for the worktree; `sikula/<stem>-<task_id>` for `cmd_run()`; the existing PR branch name for `cmd_review()`; `None` for `--no-isolate` runs |
| `result_commit` | `str \| None` | `_finalize_worktree()` in `sikula.py` | Commit SHA created by Sikula when an isolated `run` or `review --fix` task finalizes with file changes; `None` for report-only review, `--no-isolate`, or runs with no commit to create |
| `history` | `list[dict]` | `state.record()` | Append-only audit log: agent, action, result, timestamp, elapsed_s, plus action-specific entries such as `llm_retry` provider/model/attempt fields and `write_path_warning` write-scope audit messages; in step mode, `step_start` / `step_done` orchestrator entries delimit each step's events |
| `runtime_metadata` | `dict` | `StateStore.create()` / `cmd_review()` | Runtime snapshot captured when the task state is created: Sikula package version when available, Python version, platform, system, and machine. Used for later debugging only |
| `final_summary` | `dict` | `JsonStateStore.save()` | Compact terminal summary written when `done` or `failed` is reached: result, branch, commit, build/test/check status, counts for files, validation records, fix attempts, review records, test-writer runs, LLM retries, history events, timestamps, and wall elapsed time when available. The CLI also derives a human-readable completion report from the same state, including validation status, review status, audit warnings, and recovered issues. |
| `done` | `bool` | Orchestrator | Set True on passing build or after implement (no-build mode) |
| `failed` | `bool` | Orchestrator | Hard abort: set True on review timeout, active build/fix loop iteration limit reached, or unhandled agent exception; loop exits immediately. Use `--reset-failed` CLI flag to clear this and resume; the flag also resets `review_iterations`, `security_review_iterations`, `build_iterations`, and active build-loop markers, clears `errors`/`test_errors`/`check_errors` (prevents stale error blobs from appearing in the fixer's prompt on the first resumed iteration), and auto-populates `files_changed` from `git diff` if empty. Sync, build, and check failures are NOT hard aborts — they store the error and run the fixer |
| `finished_at` | `str \| None` | `JsonStateStore.save()` | ISO-8601 UTC timestamp set once when the task first reaches a terminal `done` or `failed` state; not overwritten by later saves |
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
`env_files()` is a static method called by `cmd_run()` and `cmd_review --fix` in `sikula.py` when creating a worktree.
Everything else (assemble, …) are platform-specific extras on the subclass.

| Method | Contract | AndroidGradleTool impl |
|---|---|---|
| `generate_sources()` | Generate build-time sources before the analyst runs; must tolerate pre-existing compile errors in unrelated modules. Default implementation delegates to `sync()` — override for platform-specific behaviour. | `./gradlew <build.presync_task> --parallel` (default task: `generateDebugSources`; use `openApiGenerateAll` to skip compile dependencies) |
| `sync()` | Resolve deps + generate sources before the first build | `./gradlew generateDebugSources --parallel` |
| `compile_check()` | Compile / type-check the project | `./gradlew <build.compile_task>` — task is configurable per project (see below) |
| `run_tests()` | Run the project unit test suite | `./gradlew <build.test_task>` — task is configurable per project (see below) |
| `run_check(name, task_config)` | Run a named quality check (lint, detekt, …). `task_config` is the opaque dict from `build.checks[i]` in the project YAML — the orchestrator passes it through unchanged; each BuildTool subclass interprets it. | `task_config["command"]` is the shell command to run (falls back to `name` if absent). `task_config["timeout"]` overrides the compile timeout. Uses `_run_shell()` — identical interface to PythonTool. |
| `is_build_config_file(path)` | True if the file affects the build graph | `*.gradle`, `*.gradle.kts`, `*.properties`, `*.toml`, `gradle/`, `buildSrc/`, `build-logic/` |
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
| `compile_command` | `cargo check` | Shell command run by `compile_check()`. Use `cargo check --workspace` for workspace projects |
| `test_command` | `cargo test` | Shell command run by `run_tests()`. Use `cargo test --workspace` for workspace projects |
| `timeout` | `600` | Timeout in seconds for all CargoTool operations (compile, test, check). Rust compilation is slower than interpreted languages — 600 s is a safe default |
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
| `max_steps` | `8` | Maximum number of steps the planner may produce; injected into the prompt as an upper bound |
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the planner's system prompt as `## Project-specific rules` with an explicit override statement. Scope: task-splitting decisions only — which concerns to split, which to keep atomic. Has no effect on what individual agents do. |

#### `reviewer` config keys

All keys live under `reviewer:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the reviewer's system prompt as `## Project-specific rules`. Use for project-specific correctness checks: invariants, architecture constraints, thread safety requirements. The reviewer is read-only — these rules cannot trigger file writes. |

#### `security_reviewer` config keys

All keys live under `security_reviewer:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the security reviewer's system prompt as `## Project-specific rules`. Use for project-specific security requirements: compliance rules (GDPR, PCI), data classification, threat model specifics. Appended before the BLOCKING/WARNING output format — project rules take priority over the defaults. |

#### `test_writer` config keys

All keys live under `test_writer:` in `.sikula/config.yaml`.

| Key | Default | Description |
|---|---|---|
| `coverage_target` | `90` | Minimum branch+line coverage % the agent must aim for on new/changed code within the configured test surface |
| `test_surface_policy` | `existing_infrastructure` | `existing_infrastructure` stays within existing project test infra and does not treat missing heavy UI/browser/device/runtime harnesses as gaps by themselves; `complete` opts in to `TESTABILITY GAP` reports when important behaviour needs missing test infra outside the existing surface. The test writer prefers behavioural seams and should not replace missing UI/browser/device/runtime harnesses with broad source-inspection tests. |
| `testability_gap_policy` | `warn` | `warn` records visible `TESTABILITY GAP` entries and allows the task to continue; `fail` records the same entries and fails the task |
| `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the test writer's prompt as `## Project-specific rules`. Use for project-specific testing conventions: required test doubles, naming patterns, mandatory parametric table rules. Note: unlike the analyst, reviewer, and security reviewer, the test writer does not have guidelines content pre-loaded — it reads `guidelines.context_files` via its file tools. `extra_rules` is the correct configuration point for test-specific conventions that the test writer should apply without needing to read the full guidelines. |

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
   Optionally override `generate_sources()` if `sync()` is too broad for the presync phase —
   the default calls `sync()` which is fine when sync doesn't trigger compilation:
   - **iOS / SPM**: `sync()` resolves SPM dependencies (no compilation) → default is fine.
     Override `generate_sources()` only if the project uses codegen tools (Apollo, Sourcery, SwiftGen).
   - **Maven**: `sync()` resolves deps → default is fine.
     Override `generate_sources()` to run `mvn generate-sources` if the project uses OpenAPI Generator or similar.
3. Add a branch to `_build_tool()` in `core/orchestrator.py`: check `project_config["project"]["build_tool"] == "your_build_tool"` and return an instance of your new class. Add the new build tool name to the docstring comment beside the factory.
4. Add auto-detection to `tools/scanner.py`: add an entry to `_SIGNATURES` (trigger files, build tool name, language, platform) and implement path detection helpers (`_detect_<platform>_paths()`).
5. Extend `_generate_config()` in `sikula.py` to emit the platform-specific `build:` block so that `sikula init` generates a correct config for the new platform.
6. Create `.sikula/config.yaml` in the project directory with:
   - `project.build_tool: your_build_tool` — must match the branch key added in step 3
   - `project.platform: iOS` (or `Android`, `backend`, …) — injected into agent prompts as tech stack context
   - `sandbox.allowed_write_paths` — writable source directories
   - `sandbox.allowed_read_paths` — readable directories (`"."` = entire project root)
   - `guidelines.context_files` — platform-specific guidelines docs
   - `guidelines.max_file_chars` — max chars read per guidelines file
7. Update this document.

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

Four providers are built in: `CodexClient` (`provider: "codex"`), `ClaudeClient` (`provider: "claude"`),
`GeminiClient` (`provider: "gemini"`), and `OpenCodeClient` (`provider: "opencode"`, model in
`provider/model` format).
See `README.md § Adding a new LLM provider` for a step-by-step example. Three methods must be implemented in `core/llm_client.py`:

| Method | Used by | Contract |
|---|---|---|
| `generate(system, user) -> str` | PlannerAgent | Single-shot text generation; returns the model's text response |
| `run_readonly_agent(prompt, cwd) -> str` | AnalystAgent, ReviewerAgent, SecurityReviewerAgent | Runs the model as an autonomous agent with read-only tools in `cwd`; returns text output (stdout) |
| `run_agent(prompt, cwd) -> tuple[list[str], str]` | ImplementerAgent, TestWriterAgent, FixerAgent | Runs the model as an autonomous agent with file read/write tools in `cwd`; returns `(changed_file_paths, agent_text_output)` — paths via git diff, text best-effort |

The `system` argument passed to `generate` and the `prompt` argument passed to `run_readonly_agent` and `run_agent` already contain `AGENT_SECURITY_PREFIX` (defined in `agents/base_agent.py`) — the network and filesystem constraint is injected by each agent before calling the provider. Provider implementations do not need to add it.

---

## Retry behaviour (`core/llm_client.py`)

Retry is implemented by Sikula's built-in CLI-backed providers (`CodexClient`,
`ClaudeClient`, `GeminiClient`, `OpenCodeClient`), not by the abstract `LLMClient`
interface itself. Custom providers must implement their own retry policy if they need one.

The built-in providers retry `generate()` and `run_readonly_agent()` through
`_call_with_retry()` on `RuntimeError` (non-zero CLI exit, provider error, or missing text
output) and `subprocess.TimeoutExpired`. Up to **4 attempts** total are made, with delays
of 30 s, 60 s, and 120 s between them (`_RETRY_DELAYS`).

When an agent is run through Sikula's orchestration or report-only review path, each retry
attempt before the next sleep is appended to `state.history` as `action = "llm_retry"`.
The entry stores provider, model, operation, attempt number, max attempts, delay, error type,
and a truncated provider error message.

The built-in providers also retry `run_agent()` on `RuntimeError` and
`subprocess.TimeoutExpired`, but with an additional safety guard:

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
