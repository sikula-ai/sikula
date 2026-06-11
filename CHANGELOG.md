# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- `sikula contract check TASK_FILE` now provides a deterministic, by-default read-only
  implementation-contract readiness preflight for Markdown/plain-text task files, with
  human-readable and `--json` output covering scope, acceptance criteria,
  security/privacy, validation coverage, gaps, and stable clarifying question IDs, plus
  optional `.sikula/contracts` report and hash-scoped answers-template artifacts via
  `--write-report`; `sikula init` now ignores generated contract artifacts by default.
- Task terminal summaries now include a platform-neutral audit report with validation status, review status, non-blocking audit warnings, and recovered issues such as validation failures fixed by the build/fix loop.
- Failed validation records now include high-signal diagnostic summary lines, and task terminal summaries sample and deduplicate those lines across recovered build/test/check failures so successful self-healed runs still reveal the concrete compiler error, failed test, sanitized assertion failure, or linter rule that was repaired without echoing source-code frames or assertion values, with an explicit pointer to `sikula show` for full state details.
- The test writer now more strongly prefers behavioural seams over broad source-inspection tests, especially for UI implementation details that cannot be meaningfully exercised by existing project test infrastructure.
- Cargo projects now support `build.sync_command`; the default Cargo sync uses `cargo fetch --locked` when `Cargo.lock` exists at the Cargo workspace/project root and `cargo fetch` otherwise, with a one-time plain `cargo fetch` fallback and sync validation metadata when Cargo reports that the lockfile needs updating.
- Build sync now has platform-neutral adoption and audit for source-controlled generated outputs such as lockfiles and dependency verification metadata: existing tracked sync outputs are added to `files_changed`, semantic review/test gates are invalidated, unexpected non-ignored artifacts are cleaned or fail closed, and project-specific new output patterns can be configured with `build.sync_adopt_paths`.
- Sikula now has platform-neutral generated-test audits for synthetic runtime harnesses and
  skipped, disabled, ignored, assumption-gated, or environment-gated placeholder tests.
  Findings are deduplicated, fed back into later test-writer/fixer prompts, surfaced in task
  summaries, and can recover generated test output or record an auditable `TESTABILITY GAP`
  instead of accepting non-executable coverage.

### Fixed
- `sikula review` no longer asks the optional referenced-file enrichment agent
  to return an empty response when no local files are named or found. It now
  uses an explicit sentinel internally, avoiding unnecessary no-output retries
  while leaving review behaviour unchanged when there is no extra context.
- OpenCode runs that exit successfully but produce no assistant text now include cleaned
  stderr and structured tool-call diagnostics in the reported no-output/no-change error,
  so rejected tool calls and provider diagnostics are visible without dumping raw JSON events.
- OpenCode write-agent runs now monitor structured provider error events and OpenCode's
  `--print-logs` stderr stream for `responseBody` / `responseHeaders` fields carrying fatal
  quota/auth/config failures, so exhausted credits fail immediately instead of waiting for the
  long agent timeout while avoiding false positives from normal agent prose that mentions API
  keys, 401s, or invalid models.
- OpenCode, Codex, and Gemini non-zero exits now prefer provider-owned structured stdout
  error events before stderr fallback text, so fatal quota/auth/config errors are not hidden
  by unrelated CLI warning/log output.
- Fatal LLM provider failures such as quota exhaustion, authentication failures, and invalid
  provider/model configuration are now classified separately from transient failures and fail
  without retry; reviewer, security reviewer, test writer, and fixer phases now fail
  immediately when the agent returns an unsuccessful technical/provider result instead of
  continuing from stale state or looping until an iteration limit.
- Review-fix and security-fix implementer passes now abort immediately when the implementer
  returns an unsuccessful result, preserving the underlying provider/agent failure instead of
  treating an unchanged diff as a completed fix attempt.
- Codex and Claude prompts are now passed through stdin instead of as command-line
  arguments, preventing large reviewer/analyst prompts from failing before the provider starts
  with OS argument-length errors.
- Write-agent CLI providers now write prompts through a timeout-aware stdin writer and
  tolerate early stdin pipe closure when the provider exits immediately with a
  quota/auth/config error, allowing Sikula to keep enforcing `agent_timeout` while draining
  and classifying provider output instead of surfacing an unexpected broken-pipe agent
  exception or hanging during prompt delivery.
- Planner outputs that exceed `planner.max_steps` are now rejected, retried once with a stricter
  format prompt, and then failed before implementation if still over limit; planner config is
  also captured in task config snapshots for auditability.
- Generated-test prompting and recovery now prefer stable behavioural seams over broad
  source-inspection tests, synthetic runtime/framework harnesses, or skipped/disabled
  placeholder coverage. Repeated generated-test failures require structured re-triage, and
  missing safe runtime coverage is recorded as an auditable `TESTABILITY GAP` with optional
  `covered_by` metadata.
- Generated-test audit recovery is now resume-safe, sandbox-safe, and prompt-safe: Sikula
  persists pending audit state, rolls back partial test-writer output before rerun, rejects
  symlinked restore paths, omits raw source excerpts from durable state and prompts, clears
  transient recovery snapshots on cleanup/delete, and routes active execution gates through
  build/fix validation or fails no-build completion.
- Fixer follow-up agent passes now log why they are launching, making restored
  generated-test re-triage violations, test-only scope violations, and
  production-confirmed passes visible in live task logs.
- Terminal audit summaries now sample unique testability gap details, including `reason`
  and `covered_by` when available, while preserving total-vs-unique counts so repeated gap
  records remain auditable without duplicating the visible summary.
- Test-only fixer changes on recognized test artifact paths now preserve already-approved reviewer and test-writer gates while still forcing deterministic build/test/check validation and a fresh security review, preventing unchanged production diffs from repeatedly triggering new test-writer passes without accepting unreviewed executable test changes.
- Build/fix loops now give the last allowed fixer change one final validation-only pass before failing; if that validation still fails, Sikula aborts without starting another fixer attempt.
- Test writer and fixer prompts now avoid brittle framework/container wiring tests that hand-copy production registrations into local test-only harnesses; test-failure fixer prompts also identify Sikula-generated tests so malformed generated tests can be replaced or removed without weakening pre-existing tests or accepted contracts.
- Test-only fixer scope violations are now recoverable: Sikula restores all writes from the violating test-only pass, records the violation for audit, and retries once before failing closed.
- Analyst outputs that are empty, generic, or meta-completion text are now rejected before
  `implementation_prompt` is stored; Sikula retries analysis once and then fails before
  planner/implementer phases if no usable implementation prompt is produced.
- Codex and Gemini provider failures now preserve readable errors emitted on stdout JSON
  streams for generate, read-only agent, and write-agent calls instead of reporting only
  fallback stderr or `non-zero exit`.

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
