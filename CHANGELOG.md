# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
