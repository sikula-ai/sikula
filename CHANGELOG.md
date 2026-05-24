# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Task state observability now includes validation-cycle records for presync, sync, build, test, and quality-check outcomes, plus runtime metadata and a compact terminal summary for completed or failed tasks.
- Multi-step task state now records `plan_completed`, `active_scope`, `final_full_task_review_done`, and per-record scope metadata so final whole-task validation is auditable and resume-safe.

### Changed
- `sikula --version` now appends a development suffix with branch and commit when run from a git checkout, making editable installs distinguishable from packaged releases.
- Security reviewer audit entries are stored in `security_review_cycle_records`, separate from code reviewer entries, with schema migration for existing state files.
- Analyst, reviewer, and test-writer prompts now explicitly cover parser/validator/DSL/config/schema contracts, including expected-result-type validation, materially different negative contract cases, and validation-vs-runtime failure phase.
- Reviewer prompts now include the effective configured validation pipeline and validation command coverage, so uncovered task-described commands are reported as validation coverage gaps instead of being treated as manual agent commands or implementer-fixable code issues.
- Validation command extraction treats only explicit validation contexts as commands, including command lists under validation headings with Markdown blank separator lines, so prose that happens to start with a known tool name or mentions a bare tool name in backticks does not trigger pre-agent validation coverage failures.
- Validation command coverage treats Gradle/Maven wrapper spelling, Python module forms, and npm/pnpm/Yarn `test` script shortcuts as equivalent for otherwise identical commands, while same-tool-family commands with materially different flags, targets, scripts, packages, schemes, or paths remain uncovered; near matches are included only as diagnostic context before agents run.
- `sikula review` modes now treat validation commands found in PR/review text as informational branch-verification context instead of hard preflight validation coverage gates.

### Fixed
- Multi-step `sikula run` now performs a final full-task reviewer/security/test-writer gate after all step-scoped validations complete, so the finished branch gets one whole-task pass against the original task before final validation.
- Build/fix follow-up reviews in the final multi-step phase now stay in full-task scope after fixer changes, while per-step build/fix reviews remain scoped to the current step.
- Build, test, sync, and check failure excerpts now preserve diagnostic blocks from the middle of long command output, so fixer prompts and validation records do not lose the concrete failing test, assertion, compiler error, or stack trace when tool output continues after the failure.
- Reviewer loops no longer need to block on deterministic formatter/linter/test commands that are already covered by the configured build/test/check pipeline; the orchestrator remains responsible for executing those commands and any configured `fix_command`.
- Test-failure fixer prompts now allow production fixes when a failing test encodes the task, project guidelines, or a structured contract, require production-vs-test triage in the saved fixer output, fail the task when production writes lack explicit `production_defect` + `production_code` triage, and give the reviewer that recent triage so weakened contract tests can be treated as evidence of production defects.
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
