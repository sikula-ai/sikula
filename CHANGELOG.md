# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- **Delivery Usage Observability**: Task completion and delivery status now report provider invocation attempts, failed attempts, measured provider time, content-free input/output sizes, and explicit provider-reported token usage when available, without estimating missing tokens or monetary cost.
- **Bounded Delivery Execution**: Added `sikula delivery run` to execute current plan units one at a time through the existing `run-next` path, optionally retry the current failed child once with explicit `--reset-failed`, stop safely at explicit unit or elapsed limits and operational blockers, and automatically finalize completed plans.
- **Delivery Mode Assessment**: Added `sikula delivery assess` to recommend a standard run, delivery plan, or further task clarification from project-aware, platform-neutral evidence without starting implementation or writing delivery artifacts.
- **Delivery Branch Assembly**: `delivery run-next` now assembles completed unit result commits into the plan's final branch in dependency order, starts later child worktrees from that assembled commit, preserves original result SHA ancestry, and records resumable fail-closed conflict metadata without changing the operator checkout. `delivery finalize` reconciles legacy or interrupted completed plans through the same assembly engine.
- **Delivery Handoffs**: Completed delivery units now produce versioned, fingerprinted handoffs with allowlisted result and validation metadata. Dependent units validate and consume those handoffs as Analyst context while legacy progress remains compatible.
- **Delivery Unit Sizing And Budgets**: Delivery preparation now emits sizing, risk, and budget metadata; units default to one planner step, tightly coupled two-step units remain explicit exceptions, and oversized planner results stop before implementation. `run-next --prepare-budget-split` can prepare a verified split proposal without applying it.
- **Delivery Recovery And Amendments**: Delivery units can resume, reconcile terminal children, and retry linked failed children through `run-next --reset-failed`. Added model-assisted `delivery amend prepare` plus deterministic `amend apply` preview/application for safely splitting eligible units without losing completed work or audit history.
- **Delivery Plans**: Added `sikula delivery prepare`, `check`, `status`, `run-next`, and `finalize` for authoring, validating, executing, inspecting, and finalizing large requests as tracked plans of isolated Sikula units, with dry-run and privacy-safe JSON projections.
- **Delivery Plan Metadata**: Plans can describe monorepo components and project-relative unit scope paths, and delivery execution accepts the standard per-agent model, provider, and timeout overrides.
- **Self-hosting Guidance**: Added Sikula-specific agent guidance, role-specific review/security/test-writer rules, and init-template comments for safe self-hosted Sikula development.
- **Review Fix Current Branch**: Added `sikula review --fix --current-branch` so operators can apply review fixes to the currently checked-out branch while Sikula keeps write-capable agent work in an isolated worktree, delivers by local fast-forward only after safety checks pass, and never pushes or opens pull requests.

### Changed
- **Task Branch Names**: New isolated runs omit known `.refined`, `.contract`, and `.vN` workflow suffixes from the task stem used in `sikula/<task-stem>-<task-id>` branches.
- **Test Writer Context**: During multi-step runs, per-step TestWriter passes now use the active step's changed files and focused diff instead of the full accumulated task diff; the final full-task pass still receives the integrated change.
- **Self-hosting Write Scope**: Expanded the repository's self-hosted Sikula write scope to include user-facing documentation and `CHANGELOG.md` while keeping configuration, agent guidance, contributing docs, generated guidelines, and package metadata maintainer-owned.

### Fixed
- **Failure Diagnostics**: Claude calls now use structured CLI result output so persistent stderr warnings no longer hide provider-owned failure status or change retry classification; retry and task history keep only safe categorized failure messages. Long Gradle validation output preserves multiple bounded raw `What went wrong` blocks in private validation state.
- **Windows CLI Execution**: CLI providers installed as `.cmd` or `.bat` wrappers, Gradle projects using `gradlew.bat`, and Maven projects using `mvnw.cmd` now launch through the Windows command processor with safe encoding for arguments and wrapper paths containing literal percent signs. Provider text pipes use UTF-8 independently of the process locale; streaming agents, batch-backed provider calls, and shell-backed build-tool calls use job-backed process-tree cleanup; build-tool output replaces undecodable bytes without overriding the locale encoding; and Python dependency sync supports interpreter paths containing spaces. Native provider command resolution stays direct, while streaming native agents gain the same Windows cleanup guarantees. Non-Windows provider launches remain unchanged.
- **Worktree Prompt Context**: Isolated run and review guards now require committed `extra_rules` files only for enabled agent phases, while still failing fast when consumed prompt-context files would be missing, stale, or non-file paths in the worktree start ref.
- **Task Worktree Detection**: Starting a task for another project from inside an unrelated Sikula worktree no longer triggers the current-project worktree guard, while task files and project roots that belong to the active worktree remain blocked.

## [0.3.0] - 2026-06-29

### Added
- **Antigravity Integration**: Added support for Antigravity CLI as an LLM provider via `agy --print -`.
- **Contracts & Delivery**: Added `sikula contract check` for implementation-contract preflights and JSON reporting.
- **Task Refinement**: Added `sikula task refine` and `sikula contract prepare` to separate product requirement gathering from technical implementation, with interactive and `--auto` LLM-assisted modes.
- **MCP Adapters**: Contract preparation now supports side-effect-free core helpers for future chat and MCP integrations.
- **Assets Support**: Full lifecycle support for local task assets, including `sikula task attach`, deterministic validation, hashing, and delivery provenance checks.
- **Terminal UX**: Task summaries now include a comprehensive audit report with validation status, recovered issues, and sampled diagnostics for self-healed compiler/test failures.
- **Test Generation**: The test-writer agent now more strongly prefers behavioural seams over broad source-inspection tests.
- **Generated-Test Audits**: Added platform-neutral audits for synthetic test harnesses and skipped/disabled tests, surfacing "Testability Gaps" instead of accepting non-executable coverage.
- **Cargo Improvements**: Cargo projects now support `build.sync_command` with smart lockfile resyncing.
- **Build Sync**: Added platform-neutral adoption of source-controlled generated outputs (like lockfiles), configurable via `build.sync_adopt_paths`.

### Fixed
- **Terminal UX**: Terminal audit summaries now sample unique testability gaps and improved compiler diagnostic extraction to center on the actual error line instead of generic tail logs.
- **LLM Error Handling**: Fatal provider errors (quota, auth, read-only filesystem) now fail pipelines immediately across all providers (Codex, Claude, Gemini, OpenCode, Antigravity), with better JSON parsing for root-cause surfacing.
- **LLM Delivery**: Very large prompts are now delivered via stdin (instead of arguments) for Codex and Claude, avoiding OS argument-length limits and handling broken pipes safely.
- **Pipeline Recovery**: Empty Analyst outputs and Planner step-limit violations are now safely rejected and retried with stricter format constraints before failing the task.
- **Test-Only Fixes**: Test-only fixer scope violations are now recoverable, and valid test-only fixes safely preserve reviewer gates while still forcing deterministic CI validation.
- **Agent Logging**: Fixer follow-up passes now clearly log their launch reason (e.g., test-only scope violation, re-triage).
- **Review Integrity**: `sikula review` cleanly tears down detached worktrees upon interruption and uses explicit sentinels to avoid pointless retries when no local context files are found.
- **Auditability**: Task state now records the same development version suffix as `sikula --version` when Sikula runs from a source checkout, without misattributing project-local virtualenv git metadata.
- **Example Dependencies**: Updated the React/Vite countries example dependencies to resolve Dependabot alerts for Vite and form-data.

## [0.2.0] - 2026-05-31

### Added
- Long-running agents and validation commands now publish an active-operation heartbeat to task state and logs, configurable with `progress.heartbeat_interval_seconds` and visible through `sikula status --verbose` / `--json`.
- Task state observability now includes validation-cycle records for presync, sync, build, test, and quality-check outcomes, plus runtime metadata and a compact terminal summary for completed or failed tasks.
- Multi-step task state now records `plan_completed`, `active_scope`, `final_full_task_review_done`, and per-record scope metadata so final whole-task validation is auditable and resume-safe.
- Test writer `TESTABILITY GAP` reports are now captured as first-class `testability_gaps` state records and surfaced in terminal summaries, with `test_writer.testability_gap_policy: fail` for teams that want missing safe test seams to block tasks.
- Test writer now supports `test_writer.test_surface_policy`; the default `existing_infrastructure` mode keeps generated tests within already available test infra without warning solely because heavy UI/browser/device/runtime harnesses are absent, while `complete` opts in to stricter gap reporting for missing test infra.
- Built-in `node` BuildTool support for Node.js / TypeScript / JavaScript projects, including npm/pnpm/yarn/bun detection, package-script based init defaults, and Node build-config resync detection.
- Runnable TypeScript React/Vite countries example under `example/node/countries-react/`, with Sikula config, project guidelines, Vitest/Testing Library coverage, and ready-to-run tasks.
- Runnable TypeScript Bun full-stack countries example under `example/node/countries-bun-fullstack/`, with `Bun.serve`, strict TypeScript type checking, browser bundling, `bun:test`, Sikula config, project guidelines, and ready-to-run tasks.

### Changed
- `sikula --version` now appends a development suffix with branch and commit when run from a git checkout, making editable installs distinguishable from packaged releases.
- Security reviewer audit entries are stored in `security_review_cycle_records`, separate from code reviewer entries, with schema migration for existing state files.
- Analyst, reviewer, and test-writer prompts now explicitly cover parser/validator/DSL/config/schema contracts, including expected-result-type validation, materially different negative contract cases, and validation-vs-runtime failure phase.
- Reviewer and test-writer prompts now explicitly map changed behavior through production entry points and async/deferred error boundaries, so UI handlers, routes, CLI commands, callbacks, background jobs, and similar platform hooks are checked and tested in their own contexts.
- Reviewer prompts now include the effective configured validation pipeline and validation command coverage, so uncovered task-described commands are reported as validation coverage gaps instead of being treated as manual agent commands or implementer-fixable code issues.
- Validation command extraction treats only explicit validation contexts as commands, including command lists under validation headings with Markdown blank separator lines, so prose that happens to start with a known tool name or mentions a bare tool name in backticks does not trigger pre-agent validation coverage failures.
- Validation command coverage treats Gradle/Maven wrapper spelling, Python module forms, the npm `test` shortcut, and pnpm/Yarn package-script shorthands for common validation scripts as equivalent for otherwise identical commands, while same-tool-family commands with materially different flags, targets, scripts, packages, schemes, or paths remain uncovered; near matches are included only as diagnostic context before agents run.
- `sikula review` modes now treat validation commands found in PR/review text as informational branch-verification context instead of hard preflight validation coverage gates.
- After build/test/check failures, fixer changes are validated by build/test/check again before stale reviewer, security reviewer, and test-writer gates rerun; if those gates change files, Sikula performs another deterministic validation pass before accepting the task or step.
- Test-writer prompts prefer behaviour tests through public seams and make source-file inspection a last-resort fallback that must not depend on current working directory or require build/config changes to pass.

### Fixed
- Multi-step `sikula run` now performs a final full-task reviewer/security/test-writer gate after all planned steps complete, so the finished branch gets one whole-task pass against the original task before final validation.
- Build/fix follow-up reviews in the final multi-step phase now stay in full-task scope after fixer changes, while per-step build/fix reviews remain scoped to the current step.
- Build, test, sync, and check failure excerpts now preserve diagnostic blocks from the middle of long command output, so fixer prompts and validation records do not lose the concrete failing test, assertion, compiler error, or stack trace when tool output continues after the failure.
- Cargo test failures now preserve Cargo's structured `failures:` block and focused rerun command before generic truncation, so noisy workspace output with many successful test-binary summaries does not hide the actual failing Rust test diagnostics from the fixer.
- Reviewer loops no longer need to block on deterministic formatter/linter/test commands that are already covered by the configured build/test/check pipeline; the orchestrator remains responsible for executing those commands and any configured `fix_command`.
- Test-failure fixer triage now distinguishes production defects from malformed or stale tests when a failing test encodes the task, project guidelines, or a structured contract, saves that decision for audit, and gives the reviewer the recent triage so weakened contract tests can be treated as evidence of production defects.
- Test-failure and test-origin validation fixes now start with a test-only triage/fix pass; production writes are enabled only in a second pass after no-change `production_defect` + `production_code` triage, and that second pass must actually change production code, preventing malformed generated tests from being fixed by immediate production-code edits.
- Build/check failures whose diagnostics reference only test files or recognized test targets now use the same production-vs-test fixer triage as test failures, and those fixer records are marked for reviewer audit, so malformed generated tests can be repaired without opening production writes unless the fixer explicitly classifies the issue as a production defect. Target-only diagnostics are matched conservatively, so unknown, production, or mixed targets fall back to normal build/check scope.
- Test-failure fixer audit treats non-test artifacts under broad test write roots, including build configuration and dependency manifests, as production writes so malformed generated tests cannot be fixed by changing project/build configuration without production-defect triage.
- Test-failure fixer audit now supports opt-in platform proofs for mixed source/test files. Cargo treats edits limited to an already-existing Rust `#[cfg(test)] mod tests` block as test-only, while production hunks and newly-created inline test blocks still require production-defect triage.
- Build, test, and check validation commands now clean unexpected non-ignored repository artifacts before the final diff is accepted, so generated test/runtime files cannot leak into commits or force production-code shims.
- Resuming a task after all planned steps completed now continues with the final full-task gate/build instead of rerunning the last step.
- Per-step build/fix loops no longer consume the final full-task build budget when `run_build_per_step` is enabled; `max_iterations` now applies to each active build/fix loop while `build_iterations` remains a total audit counter.

## [0.1.0] - 2026-05-21

### Added
- Initial public release of Sikula.
- Multi-agent software engineering pipeline: analyst, planner, implementer, reviewer, security reviewer, test writer, and build/fix loop.
- `sikula init` for project detection and `.sikula/config.yaml` generation.
- Optional `sikula init --guidelines` to generate `.sikula/guidelines.md` from codebase analysis without overwriting an existing config.
- `sikula run` for task-file driven implementation on a dedicated branch.
- `sikula review` for report-only branch review or `--fix` mode that applies corrections through the pipeline.
- `sikula status`, `sikula show`, `sikula cleanup`, and `sikula delete`.
- Git worktree isolation by default, with `--no-isolate` for direct local experiments.
- Resume support for interrupted runs and `sikula review --fix` tasks, including `--reset-failed` for explicit failed-task retries.
- Config guards for isolated runs: `.sikula/config.yaml` and configured guidelines files must be tracked and clean before task execution.
- Built-in LLM providers: Codex, Claude, Gemini, and OpenCode.
- Per-agent model, provider, and timeout overrides in config and CLI flags.
- Built-in platform support for Android/Gradle, JVM/Gradle, JVM/Maven, iOS/Xcode, Rust/Cargo, and Python.
- Configurable pipeline phases, including planner, review, security review, test writing, build, tests, quality checks, presync, and per-step build mode.
- Build-aware fix loop for compile, test, and quality-check failures.
- Security reviewer with blocking findings and non-blocking warnings.
- Project guidelines and per-agent `extra_rules` for planner, reviewer, security reviewer, and test writer.
- Auditable task state containing prompts, outputs, config snapshot, phase history, retry records, review/security findings, and structured cycle records.
- LLM retry history recorded in task state, with agent retries skipped after partial file changes to avoid compounding ambiguous edits.
- State privacy warnings in user-facing docs and issue templates.
- Write-path audit warnings when write-capable agents report files outside the active write scope.
- Example projects with ready-to-run tasks: Android, iOS, JVM/Gradle, JVM/Maven, and Rust.
- README terminal demo GIF and task-writing guide.
- GitHub issue templates for bugs, task result quality, feature requests, and documentation issues.
- AGPL-3.0-only license notice, third-party notices, Contributor License Agreement, and contributor privacy notice.
