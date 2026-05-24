# Sikula

[![PyPI version](https://img.shields.io/pypi/v/sikula)](https://pypi.org/project/sikula/) [![Python versions](https://img.shields.io/pypi/pyversions/sikula)](https://pypi.org/project/sikula/) [![CI](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml/badge.svg)](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml) [![codecov](https://codecov.io/gh/sikula-ai/sikula/graph/badge.svg)](https://codecov.io/gh/sikula-ai/sikula) [![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)

**Describe the task. Run Sikula. Come back to a working, tested branch — checked by independent agents and ready for human review.**

Sikula is a stateful software engineering pipeline for real codebases, powered by specialized AI agents.

`sikula run` reads your codebase, writes the code, fixes build, test, and check failures, and iterates until done — committing the result to a dedicated branch. From the outside, it behaves as a single agent. Inside, it's a pipeline of specialized ones: analyst → planner → implementer → reviewer → security reviewer → test writer → build/fix loop. For multi-step tasks, implement/review/security/test phases run per step, then a final full-task gate checks the finished branch before final validation. The reviewer and security reviewer do not receive the implementer's reasoning — they review the task description, analyst prompt, changed files, and diff independently.

`sikula review` reviews an existing branch — report-only, or with `--fix` to apply corrections through the same build and fix loop.

**Platforms:** Android/Gradle, JVM backends (Spring Boot, Quarkus, Micronaut)/Gradle, JVM backends/Maven, Node.js/TypeScript/JavaScript, iOS/Xcode, Python, and Rust/Cargo are supported out of the box. The core orchestration and all agents are platform-neutral — supporting a new platform requires a `BuildTool` subclass and a project config YAML.

**LLM providers:** Codex, Claude, Gemini, and OpenCode are built in. Authenticate via CLI login or an API key in `.env` — no vendor lock-in.

```
Task description
      │
      ▼
  Analyst ───────────── reads codebase, produces implementation prompt
      │
      ▼
  Planner ───────────── SINGLE_PASS or ordered steps
      │
      ├─ SINGLE_PASS
      │     │
      │     ▼
      │  Implementer ◄────────── reviewer/security issues
      │     │
      │     ▼
      │  Reviewer → Security Reviewer → Test Writer
      │
      └─ MULTI-STEP
            │
            ▼
         for each planned step:
           Implementer → Reviewer → Security Reviewer → Test Writer
            │
            ▼
         Final full-task gate:
           Reviewer → Security Reviewer → Test Writer

      │
      ▼
 Build / Fix loop ── compile → test → checks → fixer
      │                         │
      │                         └─ if fixer changes files:
      │                              validate again, then rerun review/security/test
      ▼
  ✓ Branch ready for human review
```

## Watch Sikula turn a task into a branch

The task: [`add-search-by-name.md`](example/jvm/countries-gradle/.sikula/tasks/add-search-by-name.md)
adds an optional `name` query parameter to `GET /countries`, keeps the existing
`region` filter working, and returns `[]` when no countries match.

This is a full run on the JVM/Gradle Countries backend example: Sikula takes a
written task, analyzes the codebase, implements name search, writes tests,
catches a failing test case, fixes it, reruns independent review, security
review, tests, and build validation, then commits the finished branch for human
review.

![Sikula terminal demo showing analysis, implementation, independent review, test fix loop, and final branch status](docs/assets/sikula-demo.gif)

```bash
# Describe the work in a task file, then run Sikula
sikula run .sikula/tasks/my-task.md
```

## Quickstart

```bash
pipx install sikula

# Authenticate Codex — see §1 for other providers
codex login
```

**Set up your project config** — pick one path:

**Auto-generate** — `sikula init` scans your project and writes `.sikula/config.yaml`:

```bash
cd my-project/

# Detect build tool, language, platform, and source paths
sikula init

# Also generate a guidelines file (reads your codebase; biggest lever for output quality):
sikula init --provider codex --model gpt-5.5 --guidelines  # adjust provider/model

# Or skip --guidelines and point guidelines.context_files at your existing architecture docs
```

**Copy an example config** and adapt it — faster when you already know what to fill in:

```bash
# Pick the example closest to your stack:
#   Android:  example/android/countries/.sikula/config.yaml
#   iOS:      example/ios/countries/.sikula/config.yaml
#   JVM/Gradle: example/jvm/countries-gradle/.sikula/config.yaml
#   JVM/Maven:  example/jvm/countries-maven/.sikula/config.yaml
#   Node/TS/JS: run `sikula init` in the project to detect package scripts
#   Rust:       example/rust/countries/.sikula/config.yaml
#   Python:   .sikula/config.yaml  (this repo)
cp <example-config> my-project/.sikula/config.yaml
# update: project name, allowed_write_paths, build tasks, guidelines.context_files
```

**Write and run a task** (see [§4](#4-write-a-task) for what to include):

```bash
sikula run .sikula/tasks/my-task.md
```

**Check the result:**

```bash
sikula status
git diff main...sikula/<branch-name>
```

> Tried Sikula? Open a [feedback issue](https://github.com/sikula-ai/sikula/issues/new?labels=feedback) and tell us what worked, what failed, and whether the result was useful. Your stack and provider/model add helpful context.

> Full setup: [§1 One-time setup](#1-one-time-setup) · Writing tasks: [§4 Write a task](#4-write-a-task) · Working examples: [§3 Try an example](#3-try-an-example)

---

## Key features

- **Stateful by design** — every task records persistent state: prompts, outputs, review rounds, security findings, build errors, config snapshot, timestamps, and final result metadata
- **Resumable long-running work** — interrupted isolated tasks keep their worktree and can resume from completed phases instead of starting over
- **Independent review loops** — reviewer and security reviewer run separately from the implementer; blocking issues go back through the implementation loop before the task can finish
- **Build-aware execution** — Sikula compiles, tests, runs configured checks, and feeds failures to a fixer until the task passes or fails explicitly
- **Precise scope control** — the analyst reads your codebase before writing the implementation prompt; the reviewer verifies call sites, structured input contracts, completeness, and scope drift before code reaches you
- **Fits existing git and CI workflows** — output is a normal git branch and commit, ready for human review and whatever CI you already run
- **Stack-flexible core** — Android, iOS, JVM, Node.js/TypeScript/JavaScript, Python, and Rust are supported through platform-specific build tools; the orchestration stays the same
- **Configurable and transparent** — each phase can be enabled or disabled, each agent can use a different model/provider, and every run is inspectable with `sikula show <task-id>`

## Why a pipeline instead of a single agent?

Sikula is task-first rather than chat-first. It is designed for work you can hand
off as a written task and later review as a branch, not for an open-ended
conversation where the path from request to reviewed branch is hard to audit.

Many AI coding workflows focus on producing a code diff. Sikula optimizes for
getting from a task description to an auditable branch that has passed the
configured build, tests, checks, review, and security gates before it reaches you.

A single AI agent writing code and reviewing its own output is like asking a developer to approve their own PR — the same reasoning that produced the bug will miss it in review.

Sikula runs the reviewer and security reviewer as independent agents — neither had any part in the implementation. Each receives the task description, analyst prompt, changed files, and code diff as a fresh starting point. The reviewer verifies call sites and completeness, treating the analyst's scope claims as hypotheses rather than facts. The security reviewer checks independently for vulnerabilities introduced by the change.

The build loop closes the other gap: code that doesn't compile or pass enabled tests and checks is not accepted as a successful result. The fixer iterates until it does, or the task fails explicitly so you know what to fix.

Task descriptions often mention validation commands such as formatters, linters, tests,
or report generators. Sikula treats those as acceptance criteria for the configured
pipeline, not as shell commands for agents to run manually. The reviewer sees the
effective build/test/check commands from the Sikula config file (auto-discovered
`.sikula/config.yaml` by default, or the file passed with `--config`). That configured
validation pipeline is what Sikula can execute. For `sikula run`, validation command
coverage is the preflight/reviewer check that task-described commands are represented by
that pipeline.
To make a command count as task-described validation, write it explicitly: in backticks,
in a shell code fence, with a `$` prompt, or as a command list under a heading/prefix such
as `Verification:` or `Run:`; Markdown blank separator lines after the heading are allowed.
Bare tool names such as `cargo` or `npm` are not treated as validation commands.
If a `sikula run` task command is not covered there, it is reported as a validation
coverage gap so you can add the same command to the effective build/test/check config or
adjust the task before rerunning. In `sikula review` modes, commands found in PR/review
text are informational branch-verification context and do not preflight-abort review/fix.
A generic command from the same tool family is not enough when the task specifies
materially different flags, targets, scripts, packages, schemes, or paths.
Gradle/Maven wrapper spelling for the same invocation (`./gradlew` vs `gradle`,
`./mvnw` vs `mvn`) is treated as equivalent, as are Python module forms
(`python -m pytest` vs `pytest`, `python -m ruff` vs `ruff`) and npm/pnpm/Yarn
`test` script shortcuts (`npm test` vs `npm run test`, etc.); different tasks,
scripts, goals, or flags are not.
For run-task validation coverage gaps, Sikula does not ask the implementer to edit the
pipeline config inside the current task, because the effective pipeline is loaded before
the agent loop starts.

## When Sikula fits

- **Well-defined tasks** — adding a screen, an endpoint, a refactor with clear scope; the analyst reads your codebase and the reviewer verifies the implementer stayed on task
- **Bug fixes** — describe the expected behaviour and where it breaks; the analyst locates the root cause and the full pipeline ensures the fix compiles, passes tests, and doesn't introduce regressions
- **Incremental development** — stack tasks one on top of the other's branch; controlled scope and quality at every step
- **Branch review** — use `sikula review` as an independent quality and security gate on any branch before merge
- **Multi-platform** — the same task description (or a lightly adapted version) can drive implementations across platforms; the analyst reads each codebase independently and the implementer handles platform-specific details; run Android, iOS, web, and backend in parallel from one spec

---

## 1. One-time setup

**Prerequisites:** Python 3.10+, `git` installed, and your target project must be a git repository (Sikula uses `git diff` to detect file changes and generate diffs for the reviewer).

```bash
# Install pipx first if needed: https://pipx.pypa.io/stable/installation/
pipx install sikula
```

Sikula ships with built-in clients for:
- **`CodexClient`** (`provider: "codex"`) — calls the `codex exec` CLI
- **`ClaudeClient`** (`provider: "claude"`) — calls the `claude -p` CLI
- **`GeminiClient`** (`provider: "gemini"`) — calls the `gemini -p` CLI
- **`OpenCodeClient`** (`provider: "opencode"`) — calls the `opencode run` CLI; model must be in `provider/model` format (e.g. `openai/gpt-5.3-codex`)

To use a different model or provider, see [Adding a new LLM provider](#adding-a-new-llm-provider) and [Per-agent LLM config](#10-per-agent-llm-config).

```bash
codex login           # Codex
claude login          # Claude
gemini                # Gemini
opencode auth login   # OpenCode
```

### Set up your project

Run from the project root — Sikula scans the codebase, detects the build tool, and writes `.sikula/config.yaml`:

```bash
cd my-project/

# Basic setup — detects build tool, language, platform, source paths
sikula init

# With LLM-generated guidelines — reads the codebase and writes .sikula/guidelines.md
sikula init --provider codex --model gpt-5.5 --guidelines

# You can also add generated guidelines later; existing config is preserved
sikula init --guidelines
```

`sikula init` auto-detects:
- Build tool and language (Gradle/Kotlin, Maven/JVM, Cargo/Rust, xcodebuild/Swift, Python)
- Xcode scheme (for iOS projects)
- Source and test directories for `allowed_write_paths` and `allowed_test_write_paths`
- Existing guidance/docs to include as guidelines context (`AGENTS.md`, `guidelines.md`,
  `.github/copilot-instructions.md`, `ARCHITECTURE.md`, `README.md`, `CONTRIBUTING.md`, etc.)

Anything that cannot be auto-detected is left as a `TODO` comment in the config. The output lists exactly which fields need manual attention before the first run.

`--guidelines` reads the entire codebase and writes `.sikula/guidelines.md` — a structured guide covering your architecture, naming conventions, and key patterns. It is automatically added to `guidelines.context_files` and received by every agent: the analyst, reviewer, and security reviewer get it pre-loaded; the implementer, fixer, and test writer read it via their file tools. It is the single biggest lever for output quality; review it, extend it based on what the reviewer catches, and keep it up to date. If `.sikula/config.yaml` already exists, `sikula init --guidelines` preserves the config and only writes `.sikula/guidelines.md` plus the missing `guidelines.context_files` entry. `--provider` and `--model` can be omitted when `llm.provider` and `llm.model` already exist in the config.

After `sikula init`, the `.sikula/` directory contains:

```
.sikula/
  config.yaml      # project config — review TODOs before first run
  guidelines.md    # (--guidelines only) generated architecture guide — review and extend
  tasks/           # store your task files here
  .gitignore       # excludes state/ and worktrees/ from source control
```

After filling in any TODOs, run your first task:

```bash
git add .sikula/config.yaml .sikula/.gitignore
# If you generated guidelines:
git add .sikula/guidelines.md
git commit -m "Add Sikula config"
```

Default isolated runs create a git worktree from `HEAD`, so `.sikula/config.yaml` and every file listed in `guidelines.context_files` must be committed first. If any of those files are missing, untracked, staged-only, or have unstaged changes, `sikula run` stops before creating the task. For a local experiment without committing, use `--no-isolate`.

```bash
sikula run .sikula/tasks/my_task.md
```

To adapt an existing project's config for a new project instead of running `init`, copy one of the example configs from `example/*/*/.sikula/config.yaml`.

### Environment variables — `.env`

The orchestrator loads `.env` from the project root automatically at startup (via `python-dotenv`). Existing shell environment variables take precedence. `sikula init` adds `.env` to the project root `.gitignore`; when generating guidelines with Claude or Gemini, it also adds the provider settings directory that provider uses. Fill in any credentials your LLM provider or CLI requires.

---

## 2. Try Sikula on its own codebase

Sikula can run on itself — the repo is already configured in `.sikula/config.yaml` with `agents/`, `core/`, and `tools/` as the writable sandbox.

Install the dev dependencies first (`ruff` and `pytest` are required for the build loop):

```bash
pipx inject sikula pytest pytest-cov ruff
```

A ready-to-run task file is included:

| Task file | What it does |
|---|---|
| `.sikula/tasks/status-emoji-icons.md` | Adds emoji icons to the human-readable `sikula status` table without changing JSON output |

> The generated config uses `provider: codex` — adjust the `llm:` section if you use a different provider.

```bash
# Run from the Sikula repo root — .sikula/config.yaml is auto-discovered
sikula run .sikula/tasks/status-emoji-icons.md
```

---

## 3. Try an example

The repo ships five runnable example projects — each a countries browser or API built around the same domain data:

| Example | Stack | Data source | Config |
|---|---|---|---|
| `example/android/countries/` | Kotlin, Jetpack Compose, Koin | [REST Countries API](https://restcountries.com) | `example/android/countries/.sikula/config.yaml` |
| `example/ios/countries/` | Swift, SwiftUI (iOS 17+), `@Observable` | [REST Countries API](https://restcountries.com) | `example/ios/countries/.sikula/config.yaml` |
| `example/jvm/countries-gradle/` | Kotlin, Spring Boot, Gradle | local JSON dataset sourced from REST Countries | `example/jvm/countries-gradle/.sikula/config.yaml` |
| `example/jvm/countries-maven/` | Kotlin, Spring Boot, Maven | local JSON dataset sourced from REST Countries | `example/jvm/countries-maven/.sikula/config.yaml` |
| `example/rust/countries/` | Rust, Ratatui | local JSON file | `example/rust/countries/.sikula/config.yaml` |

Each example ships with ready-to-run task files. The Android and iOS tasks share the same specifications — the same task description drives both platforms; Sikula's agents handle the platform-specific implementation:

Task files live in `.sikula/tasks/` inside each example project. Run from the example directory — the config is auto-discovered.

| Task | What it does | Android | iOS |
|---|---|---|---|
| Format population | Adds a formatted population string to the model and shows it in the list — minimal, single-pass, no new dependencies | `.sikula/tasks/format-population.md` | `.sikula/tasks/format-population.md` |
| Pull-to-refresh | Adds pull-to-refresh to the countries list — a focused, single-pass change | `.sikula/tasks/add-pull-to-refresh.md` | `.sikula/tasks/add-pull-to-refresh.md` |
| Country detail screen | Adds a full detail screen — multi-step: data & domain layer first, then presentation and navigation (Android also includes DI wiring with Koin) | `.sikula/tasks/add-country-detail-screen.md` | `.sikula/tasks/add-country-detail-screen.md` |

The Rust CLI ships its own set of tasks suited to a local-data command-line tool:

| Task file | What it does |
|---|---|
| `.sikula/tasks/format-population.md` | Formats the population number with B/M/K suffixes |
| `.sikula/tasks/sort-list.md` | Adds sorting options to the country list |
| `.sikula/tasks/add-neighbours.md` | Shows neighbouring countries for a given country |

The Rust example also ships with ready-to-use `extra_rules` files in `example/rust/countries/.sikula/`. They are commented out in the config by default — uncomment the `reviewer`, `security_reviewer`, `test_writer`, and `planner` blocks in `example/rust/countries/.sikula/config.yaml` to activate them and see project-specific rules in action.

The JVM examples ship the same Spring Boot REST API and the same task set in both build systems. Use the Gradle or Maven variant depending on the backend stack you want to test:

| Task | What it does | Gradle | Maven |
|---|---|---|---|
| Search by name | Adds a `name` query parameter to the list endpoint | `.sikula/tasks/add-search-by-name.md` | `.sikula/tasks/add-search-by-name.md` |
| Sorting | Adds `sort` and `order` query parameters to the list endpoint | `.sikula/tasks/add-sorting.md` | `.sikula/tasks/add-sorting.md` |
| Population stats | Adds an endpoint with aggregate population statistics | `.sikula/tasks/add-population-stats.md` | `.sikula/tasks/add-population-stats.md` |

> The example configs use `provider: codex` — adjust the `llm:` section if you use a different provider.

```bash
cd example/android/countries
sikula run .sikula/tasks/add-pull-to-refresh.md
```

---

## 4. Write a task

Create a Markdown task file. Plain text (`.txt`) is supported too, but `.md` is the recommended convention. Store it anywhere; a common location is `.sikula/tasks/` alongside the project config.

```bash
# From the same directory as the task file
cd my-project/.sikula/tasks/
sikula run my_task.md

# Or use any path relative to CWD, or an absolute path
sikula run .sikula/tasks/my_task.md
sikula run /abs/path/to/my_task.md
```

Task file path resolution: absolute path → relative to CWD. Sikula auto-discovers `.sikula/config.yaml` by walking up from CWD, so you can run from any subdirectory of the project.

Focus on requirements and intent. The analyst explores the codebase and works out the implementation details — include anything it cannot infer on its own: API endpoints, third-party service constraints, business rules, out-of-scope items. See `example/android/countries/.sikula/tasks/add-country-detail-screen.md` for a real runnable example, or [Writing Sikula Tasks](docs/writing-tasks.md) for optional task-writing guidance.

The task file must be self-contained — the analyst's tools are limited to reading files in your project. URLs, Jira tickets, Figma links, and other external references cannot be fetched and will be ignored.

When the task introduces new API calls, include the complete response contract: whether the endpoint returns a single object or a collection, and for any new data types the response introduces that don't already exist in the project, the field names and their types. The analyst cannot verify external contracts; without this information the implementer will have to guess.

For any new user-visible text (labels, titles, messages, button text), include string values in the task description — either as prose ("the screen title should be 'Country Detail'") or as an explicit `Strings:` section:

```
Strings:
- country_detail_title: "Country Detail"
- country_detail_population: "Population: {count}" (count: Long)
```

If your project uses a translation management tool (Phrase, Lokalise, etc.), always specify the exact string keys — the analyst cannot invent keys that match your translation workflow. For single-platform projects you can use platform-native string notation directly and the analyst will use it as-is.

---

## 5. Run

Run a task against your project (from the project directory; `.sikula/config.yaml` is auto-discovered):

```bash
sikula run my_task.md

# Or pass the config explicitly (e.g. from outside the project directory):
sikula --config /path/to/.sikula/config.yaml run my_task.md
```

| Agent | What it does |
|---|---|
| `analyst` | Reads the codebase and task; produces an implementation prompt with exact file paths |
| `planner` | Decides whether to run single-pass or split the task into ordered steps |
| `implementer` | Writes the code changes |
| `reviewer` | Read-only review for correctness, completeness, structured input contracts, dead code, and contract-bearing test weakening; issues are fed back to the implementer |
| `security_reviewer` | Read-only security review; blocking issues are fed back to the implementer; warnings are logged only |
| `test_writer` | Writes or updates unit tests after review/security phases complete, including positive/negative contract matrices for structured input |
| `fixer` | Fixes build, test, and check failures; test-failure fixes include production-vs-test triage |

**A few common one-off overrides** (without editing the config YAML — see the full flag reference below):

```bash
# skip compile/test/check validation for this run
sikula run my_task.md --no-build

# force single-pass for this run (skip planner even if config has run_planner: true)
sikula run my_task.md --no-planner

# use a stronger model for the analyst and a different provider for the implementer
sikula run my_task.md \
    --agent-model analyst=gpt-5.5 \
    --agent-provider implementer=claude --agent-model implementer=claude-sonnet-4-6
```

**Full loop** — enable phases in config as needed:

```yaml
# .sikula/config.yaml
run_presync:        true   # run BuildTool.generate_sources() before the analyst — ensures
                           # build-generated sources (OpenAPI DTOs, etc.) exist in build/
run_planner:        true   # triage + split: SINGLE_PASS for focused tasks; 2-N ordered steps for larger ones
run_review:         true   # logical/completeness review after implement; issues fed back to implementer
run_security_review: true  # security review after the review phase; still runs when run_review is false
                           # unless run_security_review is also false
run_test_writing:   true   # write/update unit tests after review/security phases complete
run_build:          true   # compile check; enables compile/test/check validation and fix loop
run_tests:          true   # run unit tests after each passing build
run_checks:         true   # run quality checks (lint, detekt, …) after tests; failures feed the fixer
run_build_per_step: false  # build/fix once after all steps (true = after each step plus final build)
```

Every `run_*` key (and `build.presync_clean`) can be overridden per-run without editing the YAML. Flag omitted = use config value.

The full loop: `presync → analyze → plan → implement → review → security review → test write → sync → build → test → checks → fix → ...` until done or the active build/fix loop reaches `sandbox.max_iterations`. In multi-step runs, `implement → review → security review → test write` runs for each planned step, then a final full-task gate runs before final build/fix validation. After a build/test/check failure, the fixer can iterate directly against deterministic validation; reviewer, security reviewer, and test writer rerun only after build/test/check are green again, and any changes they make are validated by another build/test/check pass. Every phase except `analyze` and `implement` is optional — controlled by `run_*` flags. Commands written in a task description are not executed directly from task text; put executable validation commands in the effective Sikula config file so the pipeline can run and audit the same flags, targets, scripts, packages, schemes, and paths.

`run_build_per_step: true` runs the build/fix loop after each individual step; multi-step runs still get the final full-task gate and final build/fix loop after all planned steps complete. Each per-step loop and the final full-task loop gets its own `max_iterations` budget, while `build_iterations` remains a total audit counter. Leave it `false` unless you explicitly want every step physically built; planner steps should still keep immediate compile dependencies together, such as resource or localization keys, route/API/command constants, service registrations, and interface implementations. Build-fix reviews during per-step builds stay scoped to that step; build-fix reviews in the final phase are scoped to the complete task, not the last planned step. See [ARCHITECTURE.md](ARCHITECTURE.md) for a detailed description of the planner and the step loop.

The **sync** step calls `BuildTool.sync()` once before the first build and again whenever the fixer changes a build-config file. It resolves dependencies and generates any required sources. A sync failure is treated like a build failure — the error is passed to the fixer and the loop continues.

**Isolation (default on):** each run creates a git worktree in `.sikula/worktrees/<task-id>/` (under the git root) and a branch `sikula/<task-stem>-<task-id>`. On success the changes are committed to that branch and the worktree is removed. On failure the worktree is preserved for inspection and resume. `.sikula/worktrees/` is added to `.git/info/exclude` automatically on the first run (local, not committed). Use `--no-isolate` to run directly in the project directory without creating a branch.

**Parallel runs:** because each task uses its own worktree, multiple Sikula processes can run simultaneously against the same project without conflicting. Start each in a separate terminal with its own task file.

**Stacking tasks:** to build task 2 on top of task 1's changes, check out the `sikula/<task1>` branch before running task 2. The new worktree will branch from that commit and inherit all of task 1's changes.

**What Sikula sees:** the worktree starts from HEAD — only committed changes are visible. Staged or unstaged changes in your working directory are not carried over. Commit any changes you want the agent to see before running. `.sikula/config.yaml` and files listed in `guidelines.context_files` are enforced: for isolated runs they must exist, be tracked, and be clean relative to HEAD before Sikula creates the worktree. Other files referenced only by the task description, such as design mockups, screenshots, or specs, are not enforced; commit them if you want the analyst to read them. (With `--no-isolate` the agent runs directly in the working directory and sees all files regardless of git status.)

**Phase flags** (`--flag` enables, `--no-flag` disables):

| Flag | Overrides | Effect |
|---|---|---|
| `--no-isolate` | — | Skip worktree creation; run directly in the project directory. A git repository is still required. |
| `--build` / `--no-build` | `run_build` | Enable/disable compile/test/check validation and the fix loop |
| `--presync` / `--no-presync` | `run_presync` | Enable/disable pre-analyze source generation |
| `--presync-clean` / `--no-presync-clean` | `build.presync_clean` | Run `clean` before the presync task |
| `--planner` / `--no-planner` | `run_planner` | Enable/disable planner (task splitting) |
| `--review` / `--no-review` | `run_review` | Enable/disable reviewer |
| `--security-review` / `--no-security-review` | `run_security_review` | Enable/disable security reviewer |
| `--test-writing` / `--no-test-writing` | `run_test_writing` | Enable/disable test writer |
| `--tests` / `--no-tests` | `run_tests` | Enable/disable running tests after build |
| `--build-per-step` / `--no-build-per-step` | `run_build_per_step` | Also build/fix after each step; final full-task build still runs |
| `--checks` / `--no-checks` | `run_checks` | Enable/disable quality checks after tests |

**Per-agent LLM flags** (repeatable; `agent` accepts `_` or `-`; valid agents: `analyst`, `planner`, `implementer`, `reviewer`, `security_reviewer`, `test_writer`, `fixer`):

| Flag | Overrides | Example |
|---|---|---|
| `--agent-model AGENT=MODEL` | `agents.<agent>.llm.model` | `--agent-model analyst=gpt-5.5` |
| `--agent-provider AGENT=PROVIDER` | `agents.<agent>.llm.provider` | `--agent-provider implementer=claude` |
| `--agent-timeout AGENT=SECONDS` | `agents.<agent>.llm.agent_timeout` | `--agent-timeout implementer=2400` |

CLI values layer on top of `agents:` overrides in the project YAML.

**Additional config keys** (all optional unless noted):

| Section | Key | Default | Description |
|---|---|---|---|
| `project` | `root_path` | `.` | Project root; `"."` (the default) resolves to the directory containing `.sikula/config.yaml`; use an absolute path only when the config lives outside the project tree |
| `project` | `build_tool` | `"gradle-android"` | BuildTool selection: `"gradle-android"` (Android/Gradle), `"gradle-jvm"` (JVM backend/Gradle), `"maven"` (Maven), `"node"` (NodeTool for TypeScript/JavaScript), `"python"` (PythonTool), `"cargo"` (CargoTool), or `"xcodebuild"` (XcodeTool) |
| `project` | `platform` | — | Target platform (e.g. `Android`, `iOS`); injected into agent prompts |
| `project` | `language` | — | Tech stack language (e.g. `Kotlin`, `Python`); injected into agent prompts |
| `project` | `ui` | — | UI framework (e.g. `Jetpack Compose`); injected into agent prompts |
| `planner` | `max_steps` | `8` | Maximum number of steps the planner may produce |
| `planner` | `extra_rules` | — | Path (relative to project root) to a Markdown file appended to the planner's system prompt as `## Project-specific rules`. Use for task-splitting conventions: which concerns to split, which compile dependencies must stay atomic, and which concerns to keep together. Has no effect on what individual agents do — only influences how the planner divides the implementation prompt into steps. |
| `reviewer` | `extra_rules` | — | Path to a Markdown file appended to the reviewer's system prompt. Use for project-specific correctness checks: thread safety requirements, mandatory invariants, architecture constraints. Does not affect implementation — the reviewer is read-only. |
| `security` | `context` | — | Short description of what the application does, what data it handles, and who the users are. Injected into the security reviewer's prompt so it can focus on threats relevant to your project — e.g. `"Mobile app. Fetches user data from our backend — auth tokens in Keychain. Main concerns: token handling and API response validation."` See [Configuring the security reviewer](#configuring-the-security-reviewer). |
| `security_reviewer` | `extra_rules` | — | Path to a Markdown file appended to the security reviewer's system prompt. Use for project-specific security requirements: compliance rules (GDPR, PCI), threat model specifics, data classification rules. Appended before the BLOCKING/WARNING categories — project rules take priority. |
| `test_writer` | `coverage_target` | `90` | Minimum branch+line coverage % for new/changed code |
| `test_writer` | `testability_gap_policy` | `warn` | What to do when the test writer reports behaviour that cannot be safely tested with available seams/infra: `warn` records a visible audit warning; `fail` fails the task |
| `test_writer` | `extra_rules` | — | Path to a Markdown file appended to the test writer's prompt. Use for project-specific testing conventions: required test doubles, naming patterns, parametric table rules. |
| `guidelines` | `context_files` | `[]` | Files loaded as guidelines context into agent prompts (relative to project root); used by analyst, implementer, fixer, test writer, reviewer, and security reviewer |
| `guidelines` | `max_file_chars` | — | Max characters read from each guidelines file |

All paths in the config are relative to the project root. `project.root_path` itself defaults to `"."` and is resolved relative to the config file's parent directory — use an absolute path only when the config lives outside the project tree.

> **Guidelines are the single biggest lever for output quality.** The analyst reads them before writing a word of code — they define architecture, naming conventions, patterns to follow, and anti-patterns to avoid. A well-maintained `guidelines.md`, `AGENTS.md`, or architecture doc produces implementations that fit your codebase; missing or vague guidance produces generic code that the reviewer will flag. Start with your existing agent/project docs and expand based on what the reviewer catches.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full config reference including `build.*` keys (`compile_task`, `test_task`, `presync_task`, timeouts).

#### Using `extra_rules`

`extra_rules` files are plain Markdown. Store them anywhere in your project — a `.sikula/` subdirectory is a natural convention. Wire them up in your project config:

```yaml
reviewer:
  extra_rules: .sikula/reviewer_rules.md

security_reviewer:
  extra_rules: .sikula/security_rules.md

test_writer:
  extra_rules: .sikula/test_writer_rules.md

planner:
  extra_rules: .sikula/planner_rules.md
```

The file content is appended to the agent's system prompt under a `## Project-specific rules` heading with an explicit statement that project rules take priority over any conflicting defaults.

These settings apply across all commands that invoke the respective agents: `reviewer` and `security_reviewer` rules are active in `sikula run`, `sikula review`, and `sikula review --fix`; `test_writer` rules apply in `sikula run` and `sikula review --fix`; `planner` rules apply in `sikula run` only (the planner is always disabled in `sikula review`).

**Example — `.sikula/reviewer_rules.md`:**

```markdown
- All public API methods must be documented in `docs/api.md`.
- Any change to a shared repository must verify that all ViewModels that inject it
  are still passing the correct arguments after the change.
- Do not approve changes that introduce a new dependency without a corresponding
  entry in `docs/dependencies.md`.
```

**Example — `.sikula/security_rules.md`:**

```markdown
- This project processes payment data. Any field that may contain card numbers,
  CVVs, or bank account details must never appear in logs, even as a partial value.
- All API endpoints under `/admin` require role `ADMIN` — flag any new endpoint
  in that path that does not explicitly check for this role.
```

**What belongs here vs `guidelines.md`:** `guidelines.md` reaches every agent — the analyst and reviewers get the full content injected into their prompt; the implementer, fixer, and test writer receive it as filenames they read via file tools. `extra_rules` never reaches the implementer or fixer — use it for constraints that should apply at the planning, review, or test-writing stage without influencing what the implementer writes directly.

#### Configuring the security reviewer

The security reviewer runs after the review phase and checks for vulnerabilities introduced by the change. With the default `run_review: true`, that means after reviewer approval; if review is disabled for a run, security review still runs unless `run_security_review` is also disabled. It has a built-in list of blocking categories — hardcoded credentials, injection vulnerabilities, missing auth, weak crypto, PII in logs, path traversal, disabled TLS — but without project context it has to guess what matters most for your specific application.

Two config fields let you tune it:

**`security.context`** — tell the reviewer what your application is. One to three sentences covering what data it handles, who the users are, and where the trust boundaries lie. The reviewer uses this to focus on relevant threat categories and skip the ones that don't apply.

```yaml
security:
  context: Mobile app. Fetches user data from our backend API — auth tokens stored in EncryptedSharedPreferences. No PII beyond email; no financial data. Main concerns: token handling, API response validation, and navigation argument sanitisation.
```

Without this field the reviewer still catches the universal categories above, but has no way to judge what is high-risk for your specific application.

**`security_reviewer.extra_rules`** — add mandatory checks on top of the built-in categories. Use this for compliance requirements, data classification rules, or security invariants specific to your codebase.

```yaml
security_reviewer:
  extra_rules: .sikula/security_rules.md
```

The two fields are complementary: `security.context` tells the reviewer *what the application is*, `extra_rules` tells it *what specific rules apply*. A well-configured security reviewer is a meaningful gate — not just a pass-through.

---

## 6. Review an existing branch

`sikula review` runs the same reviewer and security reviewer on an existing branch without interrupting your working directory. It requires `--description` or `--description-file`; that text is the review scope, similar to a PR description. Project-specific rules (`reviewer.extra_rules`, `security_reviewer.extra_rules`) from the project config are honored in all modes; `test_writer.extra_rules` is honored in `--fix` mode when the test writer runs. See [Using `extra_rules`](#using-extra_rules) in §5. In report-only mode the branch is isolated as a detached-HEAD worktree, so you can run it even while sitting on the branch being reviewed. In `--fix` mode a real branch checkout is used so that fixes can be committed back. The diff shown to reviewers is computed as `git diff base...branch` (three-dot — all commits introduced by the branch); in `--fix` mode it is refreshed before each reviewer pass so Sikula's own uncommitted fixes are included. Unlike the normal `sikula run` pipeline, review mode treats changed test files as branch output and reviews them for correctness and relevance. Test-writer changes made during `--fix` receive one final reviewer/security validation pass; if that pass rejects them, the task fails instead of entering another test-writing loop.

**Report-only** — branch is never modified:

```bash
sikula review \
    --branch feature/login \
    --base-branch main \
    --description "Add login screen with JWT authentication"

# Pass the PR description from a file:
sikula review \
    --branch feature/login \
    --description-file login_pr.md
```

Runs `ReviewerAgent`, then `SecurityReviewerAgent` (only if review passes; controlled by `--security-review` / project config, default on). Prints the full review output and exits `0` (approved) or `1` (issues found). The worktree is removed on completion. Task state is saved — inspect with `show <task-id>`.

**Fix mode** (`--fix`) — applies suggested fixes to the branch:

```bash
sikula review \
    --branch feature/login \
    --base-branch main \
    --description-file login_pr.md \
    --fix
```

Uses the full orchestrator loop: reviewer finds issues → implementer fixes them → build and checks run per the project config. The planner is always disabled and the reviewer is always enabled for `review --fix`. Gitignored build files (e.g. `local.properties` on Android) are copied into the worktree automatically — same as `sikula run`. If the reviewer approves without any fixes, build, tests, and checks are skipped — the branch is assumed to be already CI-validated. On success with fixes, changes are committed to the PR branch with the message `sikula: review fixes for <branch>`; if no fixes were needed, the worktree is simply removed. On failure, the worktree is preserved at `.sikula/worktrees/<task-id>/` (under the git root) for inspection. Resume is supported via `sikula run --task-id <id>` — Sikula reuses the task state and worktree, keeps planner disabled and reviewer enabled, and reuses the original security-review setting unless you override it with `--security-review` / `--no-security-review`.

```bash
# Review with a stronger model for both reviewers:
sikula review \
    --branch feature/login \
    --description-file login_pr.md \
    --agent-model reviewer=gpt-5.5 \
    --agent-model security_reviewer=gpt-5.5
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `--branch` | required | Branch to review (must already exist) |
| `--base-branch` | `main` | Base branch to diff against |
| `--description TEXT` | required* | PR description — scope and context for the reviewer |
| `--description-file FILE` | — | Path to a file containing the PR description |
| `--fix` | off | Apply fixes: run implementer on review issues and commit to the branch |
| `--security-review` / `--no-security-review` | from config (`true`) | Enable/disable SecurityReviewerAgent |
| `--agent-model AGENT=MODEL` | — | Override model for one agent (repeatable) |
| `--agent-provider AGENT=PROVIDER` | — | Override provider for one agent (repeatable) |
| `--agent-timeout AGENT=SECONDS` | — | Override timeout for one agent (repeatable) |

*Use either `--description` or `--description-file`.

---

## 7. Check results

```bash
# List all tasks (sorted oldest → newest)
# STATUS: DONE, FAILED, CLEANED, INTERRUPTED, or the current phase
# STEP shows current planner step; BUILD shows build/fix iterations
sikula status

# Include the next useful command for each task
sikula status --verbose

# Filter the list
sikula status --active
sikula status --failed
sikula status --cleaned
sikula status --done

# Machine-readable task overview
sikula status --json

# Show full task state as JSON (includes config_snapshot with all effective run settings)
sikula show <task-id>

# See what the agent changed (branch name is printed in the run summary)
git diff main...sikula/<task-stem>-<task-id>

# Merge into your working branch or open a PR
git merge sikula/<task-stem>-<task-id>
# or: git push origin sikula/<task-stem>-<task-id> && gh pr create ...
```

Sikula state commands such as `status` and `show` can also be run from inside a
preserved task worktree; Sikula resolves task state from the original project, not
from the worktree copy.
Start fresh tasks from the original project, not from inside another task worktree.
Use `sikula run --task-id <task-id>` to resume the current task instead.
`cleanup --force` and `delete --force` must be run from outside the worktree they
would remove.

The `config_snapshot` field in the state JSON records every effective setting used for the run: `project` name, all `run_*` flags (including `run_checks`), `max_iterations`, `max_review_iterations`, `max_security_review_iterations`, `sandbox.*` paths (`allowed_write_paths`, `allowed_test_write_paths`, `allowed_read_paths`), `build.*` settings (presync task, compile task, timeouts, `checks` list), and the resolved `provider`/`model`/`agent_timeout` for each agent. If `extra_rules` is configured for an agent, its path is also captured in the snapshot. It is written once at the start of the first run and never overwritten on resume, so it always reflects the original run's configuration.

Terminal task state also records `finished_at`, `result_commit` when Sikula creates a commit, and final `test_status` / `check_status` values (`success`, `failed`, or `skipped`) for audit and debugging.

> **Privacy note:** the state JSON contains full LLM prompts and outputs, which may include your task description, source code excerpts, inlined guidelines content, and build error output. Before sharing a state file in a bug report or publicly, review it and redact any proprietary or sensitive content.

---

## 8. Resume an interrupted task

```bash
sikula run --task-id <task-id>
```

Resume applies to `sikula run` tasks and `sikula review --fix` tasks. Report-only `sikula review` does not support resume — re-run it to start fresh.

Resume works for tasks that were **interrupted** mid-run (process killed, timeout, crash). Each phase has a guard flag in the state so already-completed phases are skipped automatically.

Tasks that explicitly **failed** (e.g. implementer produced no file changes in single-pass mode, max iterations reached) are in a terminal `failed` state and cannot be resumed directly. Use `--reset-failed` to clear the failed flag and retry:

```bash
sikula run --task-id <task-id> --reset-failed
```

`--reset-failed` also clears any pending error blobs (`errors`, `test_errors`, `check_errors`) so the fixer doesn't see stale failures from before the reset on the first resumed iteration. It also inspects the current `git diff` in the worktree (or project root for `--no-isolate` runs): if `files_changed` is empty but files within `allowed_write_paths` are already dirty (e.g. the implementer ran but change detection produced a false negative), those files are written into `files_changed` so the orchestrator skips the implement phase and proceeds directly to review → test_writer → build.

For isolated tasks the worktree is preserved on failure so resume works out of the box — the worktree path is stored in the task state and reused automatically when you pass `--task-id`. The currently checked-out branch in your working directory is irrelevant for resume (including `--reset-failed`): the orchestrator always works inside the task's own worktree on its dedicated `sikula/<task>` branch. Only a fresh run with a new task file is affected by which branch you have checked out, because the new worktree branches from the current HEAD.

### Cleaning Up Task Worktrees

Failed or interrupted isolated runs keep their git worktree so you can inspect changes,
resume the task, or recover a patch manually. When you no longer need that workspace,
use `cleanup`:

```bash
sikula cleanup <task-id>          # dry run; prints what would be removed
sikula cleanup <task-id> --force  # remove the preserved worktree, keep state JSON
```

`cleanup` preserves the state JSON and records a cleanup entry in `history`, so `sikula show`
still works for audit/debugging. Resume is no longer possible after the worktree is removed.

To remove both the worktree and the state JSON:

```bash
sikula delete <task-id>          # dry run
sikula delete <task-id> --force  # remove worktree and state JSON
```

Dirty worktrees are protected by default. If the worktree has uncommitted changes, cleanup
and delete fail unless you explicitly acknowledge data loss:

```bash
sikula cleanup <task-id> --force --discard
sikula delete <task-id> --force --discard
```

---

## 9. Sandbox — what agents are allowed to do

Agents operate within several layers of protection:

**Git isolation** — by default each run works in a dedicated git worktree and branch
(`sikula/<task-stem>-<task-id>`). All changes are visible via `git diff` before you merge.
Nothing reaches your main branch without a deliberate merge. With `--no-isolate` the changes
land as uncommitted working-tree modifications — equally visible and equally under your control
before any commit.

**Filesystem scope** — agents run with `cwd=project_root`, anchoring all relative paths
to the project. Workspace boundary enforcement depends on the provider — see the sandbox notes
under each provider in [§ Adding a new LLM provider](#adding-a-new-llm-provider). `allowed_read_paths` and
`allowed_write_paths` subdirectory restrictions within the workspace are passed as constraints
in the agent prompt; they are not enforced at the OS level.
After each write-capable agent call, Sikula compares the files reported by that call with
the active write path list and records a non-blocking `write_path_warning` in task history
when a file falls outside it. Inspect with `sikula show <task-id>`.
This is an audit signal, not pipeline control flow: it does not fail the task and it only
covers files reported by the provider's `run_agent()` result.

**Sandbox config** — defined in `.sikula/config.yaml` under `sandbox`:

| Key | Used by | Purpose |
|---|---|---|
| `allowed_write_paths` | ImplementerAgent, FixerAgent (build errors, test failures, check errors) | Production source directories agents may write to |
| `allowed_test_write_paths` | TestWriterAgent, FixerAgent (test failures, check errors) | Test source directories; agents may write here when fixing test failures or check violations (e.g. detekt) |
| `allowed_read_paths` | ImplementerAgent, FixerAgent, TestWriterAgent | Directories agents may read from (prompt constraint); `"."` means the entire project root |
| `max_iterations` | Orchestrator build/fix loop | Max attempts per active build/fix loop before the task is aborted |
| `max_review_iterations` | Orchestrator review loop | Max review+implement-fix cycles before task is aborted |
| `max_security_review_iterations` | Orchestrator security review loop | Max security-review+fix cycles (independent of `max_review_iterations`); default equals `max_review_iterations` if not set |

**Bash** is constrained by Sikula's agent prompts to read-only commands (`grep`, `find`,
`ls`) plus `git rm` for tracked file deletion — no `rm`, `mv`, or other destructive shell
commands. Provider-level enforcement varies below; where `git rm` is used, deletions are
tracked, visible in `git diff`, and reversible.

Enforcement varies by provider:

| Provider | Write agents (bash restriction) | Read-only calls |
|---|---|---|
| `CodexClient` | prompt-level — CLI does not support per-command filtering | file writes blocked by `--sandbox read-only`; shell command filtering is prompt-level |
| `ClaudeClient` | technically enforced via `--allowedTools` | technically enforced via `--allowedTools` |
| `GeminiClient` | prompt-level — `run_shell_command` is in `tools.core` but agent is instructed to limit its use | technically enforced — `run_shell_command` excluded from `tools.core` |
| `OpenCodeClient` | prompt-level — CLI does not support per-command filtering | technically enforced via `bash: deny` in OpenCode config |

**Network access** is forbidden by Sikula's agent prompts via `AGENT_SECURITY_PREFIX` —
agents are instructed not to make network requests or access external services. Provider-specific
tool restrictions may further reduce network-capable shell/tool access, but Sikula does not rely on
an explicit provider-level network-deny setting.

---


## 10. Per-agent LLM config

Each agent can use a different model, provider, or timeout. All five fields (`provider`, `model`,
`max_tokens`, `temperature`, `agent_timeout`) can be overridden per agent. Any field omitted
falls back to the top-level `llm:` section:

```yaml
llm:
  provider: codex
  model: gpt-5.3-codex
  agent_timeout: 1800         # seconds; default for all agents

agents:
  analyst:
    llm:
      model: gpt-5.5          # stronger model: analyst output determines the entire task outcome
  reviewer:
    llm:
      model: gpt-5.5          # stronger model: thoroughness matters more than speed here
  security_reviewer:
    llm:
      model: gpt-5.5          # stronger model: must reliably detect subtle security issues
  implementer:
    llm:
      agent_timeout: 2400     # implementer may need more time on large codebases
  # planner, test_writer, fixer inherit the default llm: above
```

The analyst, reviewer, and security reviewer benefit most from a stronger model. The analyst's output determines the outcome of the entire task; the reviewer and security reviewer need strong reasoning to catch subtle issues reliably. The implementer, planner, test writer, and fixer work from precise, structured prompts — a faster model is usually sufficient. The implementer and test writer are most likely to hit the timeout on large codebases — increase `agent_timeout` for them if needed.

Existing configs without an `agents:` block are unaffected.

---

## Capabilities (current state)

This table includes both hard orchestration behaviour and prompt-enforced agent
contracts. Runtime sandbox details are documented separately in
[Sandbox — what agents are allowed to do](#9-sandbox--what-agents-are-allowed-to-do); prompt
contracts are auditable in task state.

| Capability | Status |
|---|---|
| `sikula init` — scans project, auto-detects build tool / language / platform / source paths / Xcode scheme, generates `.sikula/config.yaml` and `.sikula/tasks/`; TODOs printed for anything that needs manual input; `--guidelines` generates `.sikula/guidelines.md` via LLM analysis of the codebase | ✓ |
| Auto-discovery of `.sikula/config.yaml` — walk up from CWD; `--config` to override | ✓ |
| Read task from a text file | ✓ |
| Pre-analyze sync (`run_presync: true`) — runs `BuildTool.generate_sources()` before the analyst to ensure build-generated sources (OpenAPI DTOs, KSP output, …) exist in `build/`; failure is non-fatal | ✓ |
| Analyst agent — runs as read-only agent (Read/grep/find tools); browses codebase, then generates implementation prompt with exact file paths | ✓ |
| Analyst prompt requires reading each affected file in full before finalising the change list (not just grep hits) | ✓ |
| Analyst prompt excludes test file changes — test changes are omitted from the implementation prompt and handled by the test writer | ✓ |
| Analyst prompt requires platform-neutral structured input contracts for parser/validator/expression/DSL/config/schema/rule-engine tasks, including accepted inputs, materially different rejected input classes, expected result types, scope rules, and validation-vs-runtime failure phase | ✓ |
| Guidelines context: analyst, reviewer, and security reviewer receive file content pre-loaded from `guidelines.context_files` (`guidelines.max_file_chars` controls per-file limit; truncated files include a marker instructing the agent to use Read tool for full content); implementer, fixer, and test writer receive filenames and read content via tools | ✓ |
| Planner agent — triage + split (`run_planner: true`): for small/focused tasks outputs `SINGLE_PASS` and flow is unchanged; for larger tasks breaks the prompt into 2–N ordered steps; each step runs implement→review→security review→test phases, then a final full-task gate gives the finished branch one whole-task pass against the original task before final validation; build/fix always runs in the final phase when `run_build` is enabled, and can also run per step with `run_build_per_step` | ✓ |
| Implementer agent — runs LLM as autonomous agent; navigates codebase with file tools, writes changes directly; changed files detected via git diff | ✓ |
| Implementer, fixer, test writer: receive project guidelines filenames from `guidelines.context_files`; read content via tools | ✓ |
| Implementer prompt requires transitive dead-code cleanup — symbols that become unreferenced in production code after the change should be removed; test-only references are ignored | ✓ |
| Per-agent LLM config — each agent can use a different model/provider via `agents.<name>.llm:` in the project YAML | ✓ |
| Per-agent project-specific rules (`extra_rules`) — plain Markdown file appended to the agent's system prompt under `## Project-specific rules` with explicit priority statement; supported for `reviewer`, `security_reviewer`, `test_writer`, `planner`; active in all modes that invoke the respective agent; path stored in `config_snapshot` | ✓ |
| Reviewer agent — read-only review after implement, before build; checks completeness, logical correctness, semantic consistency, dead members, shared function scope, and structured input contracts; task description is sole scope authority — implementation prompt claims ("caller X intentionally affected") are verified against it; for shared functions greps callers independently and reads each out-of-scope caller file; in normal `sikula run`, test files are handled by the test writer/build loop and only production issues block approval, but weakened contract-bearing tests are evidence for re-checking the production contract; in multi-step `sikula run`, step reviews are current-step scoped but the final gate reviews the whole task; in `sikula review`, changed test files are reviewed as branch output; issues are fed back to implementer during the main review loop; test-writer changes in `review --fix` get one final validation pass | ✓ |
| Review loop: enabled via `run_review`; iteration limit via `sandbox.max_review_iterations`; timeout aborts task | ✓ |
| Security reviewer agent — read-only security review after the review phase (`run_security_review: true`; independent of `run_review`); blocking issues (hardcoded secrets, injection, missing auth, weak crypto, PII in logs, path traversal, disabled/missing TLS validation) feed back to implementer; warnings are recorded in `state.security_review_cycle_records` and do not block; reruns after fixer changes once build/test/check validation is green | ✓ |
| Test writer agent — writes/updates unit tests after review/security phases complete (`run_test_writing: true`); receives the original task description to honor explicit testing requirements, scoped by `CURRENT STEP` during planned steps and by the complete task in the final multi-step gate; instructed to stay within `sandbox.allowed_test_write_paths` and avoid production code; prefers behaviour tests through public seams, treats source-file inspection as a last-resort fallback, records visible testability gaps when safe tests require missing project infrastructure, and requires structured input changes to have positive/negative contract matrices that preserve the rejected input class being tested | ✓ |
| Test writer prompt requires existing test conventions (framework, assertions, naming), explicit null-path coverage, caller checks for modified functions, and parametric/data-driven tests when the project already uses them and the fit is natural | ✓ |
| Test writer prompt target is configurable via `test_writer.coverage_target` (default: 90%) | ✓ |
| Test writer agent reruns after fixer changes once build/test/check validation is green; any test-writer changes trigger another validation pass | ✓ |
| Test runner — `BuildTool.run_tests()` after each passing build (`run_tests: true`); failures fed to fixer | ✓ |
| Quality checks — `BuildTool.run_check(name, task_config)` for each entry in `build.checks` after tests pass (`run_checks: true`); failures fed to fixer like build errors; check list is platform-agnostic via opaque `task_config` dict; each check has `name`, `command`, `timeout`, and optional `fix_command` (auto-run before fixer on deterministic formatters) | ✓ |
| Fixer agent — runs LLM as autonomous agent with build/test/check errors + task context + `implementation_prompt`; reads guidelines | ✓ |
| Fixer: test-aware constraint — build errors: test files off-limits; test failures only: production and test files allowed, with explicit production-vs-test triage and instructions to fix production when a failing test encodes the task/project contract; production writes from test-failure fixes require `production_defect` + `production_code` triage or the task fails for audit, including non-test artifacts under broad test write roots such as module build files; mixed source/test files require an opt-in `BuildTool.is_test_only_change()` proof, currently used by Cargo for existing Rust `#[cfg(test)] mod tests` blocks; check errors (no build errors): production or test files allowed if explicitly named in the errors | ✓ |
| Fixer: uses `allowed_write_paths` for build errors and combines `allowed_write_paths` + `allowed_test_write_paths` when fixing test failures or check errors (no build errors) | ✓ |
| Structured observability records — `implement_cycle_records`, `review_cycle_records`, `security_review_cycle_records`, `test_write_records`, `testability_gaps`, `fix_cycle_records` capture full context (prompt, output, errors snapshot, files written, timestamp, and correlation keys such as `step`, `build_iteration`, `review_iteration`, and `security_review_iteration`) for every agent invocation across all iterations; `validation_cycle_records` captures presync/sync/build/test/check outcomes with timing and diagnostic error excerpts that preserve failure blocks from long tool output; terminal states include `final_summary`; never cleared; visible in `show <task-id>` | ✓ |
| Prompt persistence — full assembled prompts stored in state before each LLM call: `state.analyst_prompt`, `state.planner_prompt`, `state.implementation_prompt`; reviewer, security reviewer, and test writer prompts are stored per-cycle in their respective records; enables post-run audit of agent behaviour even if guidelines or `extra_rules` files change after the run; review/redact state JSON before sharing because it may contain source excerpts or other sensitive content | ✓ |
| Sandbox — separate write whitelists for production (`allowed_write_paths`) and test (`allowed_test_write_paths`) code | ✓ |
| Build sync (`BuildTool.sync()`) before first build | ✓ |
| Compile check (`BuildTool.compile_check()`) — task configurable via platform-specific `build.compile_task` or `build.compile_command` | ✓ |
| Build, sync, and test timeouts configurable per project via `build.sync_timeout` / `build.compile_timeout` / `build.test_timeout` | ✓ |
| Re-sync when fixer changes build-config files (`BuildTool.is_build_config_file()`) | ✓ |
| Automatic build/fix loop with `sandbox.max_iterations` applied per active build/fix loop | ✓ |
| Guard: implementer failure aborts before build loop (no false `done: true`) | ✓ |
| Git worktree isolation — each run works in a dedicated branch (`sikula/<task-stem>-<task-id>`) and worktree; on success changes are auto-committed and worktree removed; on failure worktree preserved for resume; `.sikula/worktrees/` auto-added to `.git/info/exclude` (local, not committed); parallel runs never conflict; `--no-isolate` to skip | ✓ |
| Standalone PR review (`sikula review`) — code + security review on an existing branch via `git diff base...branch` with an explicit PR/task description as scope; report-only (read-only, exits 0/1) or `--fix` to apply corrections and commit them to the branch; per-agent model/provider/timeout and `extra_rules` overrides supported in both modes | ✓ |
| JSON state persistence (resumable tasks via `run --task-id`) | ✓ |
| Config snapshot — effective run settings (project name, all `run_*` flags, `sandbox.*` write/read paths, `build.*`, per-agent model/provider/timeout, per-agent `extra_rules` path when configured) persisted in `state.config_snapshot` on first run; never overwritten on resume | ✓ |
| CLI: `run`, `review`, `status`, `show`, `cleanup`, `delete` | ✓ |
| CLI per-run phase overrides — `--flag` / `--no-flag` for every `run_*` key and `build.presync_clean`; no YAML edit needed | ✓ |
| CLI per-agent LLM overrides — `--agent-model`, `--agent-provider`, `--agent-timeout` (repeatable, `agent=value` syntax); available for both `run` and `review` | ✓ |
| LLM via CLI — Codex, Claude, Gemini, and OpenCode providers invoke a local CLI; authentication follows the selected provider's CLI and environment configuration | ✓ |
| Retry on LLM failure — up to 4 attempts (30 s / 60 s / 120 s backoff); retry attempts are recorded in task history as `llm_retry`; `run_agent` skips retry when partial file changes are detected | ✓ |
| Write-scope audit warnings — write-capable agents are prompted with an active write scope; after each agent call, detected files outside that scope are recorded in task history as `write_path_warning` | ✓ |

---


---

## Adding a new LLM provider

`LLMClient` defines three methods that all providers must implement. Adding a new provider
requires changes in one file only (`core/llm_client.py`).

| Method | Used by | What it must do |
|---|---|---|
| `generate(system, user) -> str` | PlannerAgent | Single-shot text generation |
| `run_readonly_agent(prompt, cwd) -> str` | AnalystAgent, ReviewerAgent, SecurityReviewerAgent | Run the model as an autonomous agent with read-only tools in `cwd`; return the model's text output |
| `run_agent(prompt, cwd) -> tuple[list[str], str]` | ImplementerAgent, TestWriterAgent, FixerAgent | Run the model as an autonomous agent with file read/write tools in `cwd`; return `(changed_file_paths, agent_text_output)` — file paths detected via git diff, text output best-effort |

Four providers are built in: `CodexClient` (`provider: "codex"`), `ClaudeClient` (`provider: "claude"`), `GeminiClient` (`provider: "gemini"`), and `OpenCodeClient` (`provider: "opencode"`, model in `provider/model` format). For providers that call an HTTP API directly, you'll need the provider's SDK and credentials in `.env`.

**Step 1 — implement the client** (`core/llm_client.py`):

```python
class CustomClient(LLMClient):
    def __init__(self, config: LLMConfig) -> None:
        from my_provider_sdk import Client
        self._config = config
        self._client = Client()  # reads provider credentials from env

    def generate(self, system: str, user: str) -> str:
        resp = self._client.chat.complete(
            model=self._config.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        # Implement using the provider's agent/tool-use API with read-only tools.
        # Must return the model's text output.
        raise NotImplementedError

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        # Implement using the provider's agent/tool-use API.
        # Must return (changed_file_paths, agent_text_output).
        # Text output is best-effort — return "" if not available.
        raise NotImplementedError
```

The `system` argument passed to `generate` and the `prompt` argument passed to `run_readonly_agent` and `run_agent` already contain `AGENT_SECURITY_PREFIX` (defined in `agents/base_agent.py`) — the network and filesystem constraint is injected by each agent before calling the provider. You do not need to add it in your implementation.

**Step 2 — register in the factory** (`core/llm_client.py`):

```python
def create_llm_client(config: LLMConfig) -> LLMClient:
    if config.provider == "codex":
        return CodexClient(config)
    if config.provider == "custom":       # add this
        return CustomClient(config)
    raise ValueError(...)
```

**Step 3 — switch in the project config** (no code changes):

```yaml
llm:
  provider: custom
  model: your-model-name
  max_tokens: 8096
```

Nothing else needs to change — orchestrator, agents, and tools are unaffected. See [ARCHITECTURE.md § Add an LLM provider](ARCHITECTURE.md#add-an-llm-provider) for the full interface contract.

---

### Using the built-in `CodexClient`

No custom code is needed — `CodexClient` is already registered as `provider: "codex"`.

```yaml
llm:
  provider: codex
  model: gpt-5.3-codex
```

`CodexClient` calls the `codex exec` CLI. Authenticate with `codex login` or via an API key — see the Codex CLI documentation.
Sikula passes `--skip-git-repo-check` to `codex exec`; repository and worktree checks are handled by Sikula before task execution.

**Sandbox notes:**
- *Single-shot and read-only calls* (planner, analyst, reviewer, security reviewer,
  and `sikula init --guidelines`): `--sandbox read-only` is passed to `codex exec` — file
  writes are blocked at the OS level. Shell execution is not per-command filtered; any bash
  constraints are prompt-level only.
- *Write-capable agents* (implementer, fixer, test writer): `--sandbox workspace-write` allows
  file editing in the workspace and normal shell execution under the Codex sandbox. The bash
  restriction (`grep`, `find`, `ls`, `git rm` only) is enforced at the prompt level — Codex CLI
  does not support per-command shell filtering. Sikula does not pass `--add-dir`; any writable paths
  outside the working root are determined by the Codex CLI sandbox policy, not by Sikula.

---

### Using the built-in `ClaudeClient`

No custom code is needed — `ClaudeClient` is already registered as `provider: "claude"`.

```yaml
llm:
  provider: claude
  model: claude-sonnet-4-6
```

`ClaudeClient` calls the `claude -p` CLI, which is Claude Code Agent SDK usage.
Authentication and usage limits follow your Claude Code plan and environment. Local
developer machines may use the normal Claude Code authentication flow; scripted or CI
environments should provide `ANTHROPIC_API_KEY` or an `apiKeyHelper` via Claude settings.

**Sandbox notes:**
- *Workspace boundary*: enforced via `sandbox.filesystem.allowWrite` (Seatbelt on macOS, bubblewrap on Linux). Sikula writes a generated Claude settings file with absolute sandbox paths and passes it explicitly via `--settings`; it does not rely on project-level Claude settings for the workspace boundary.
- *Read-only agents* (analyst, reviewer, security reviewer): technically enforced via `--allowedTools` — write tools and bash are blocked at the CLI level.
- *Write-capable agents* (implementer, fixer, test writer): bash restricted to `grep`, `find`, `ls`, `git rm` via `--allowedTools`; technically enforced.

---

### Using the built-in `GeminiClient`

No custom code is needed — `GeminiClient` is already registered as `provider: "gemini"`.

```yaml
llm:
  provider: gemini
  model: gemini-2.5-pro
```

`GeminiClient` calls the `gemini` CLI. Install and authenticate Gemini CLI according
to the distribution you use; if your setup supports API-key auth, Sikula can load
`GEMINI_API_KEY` from `.env`.

**Sandbox notes:**
- *Workspace boundary*: enforced by the `write_file` tool's own path check (`Path not in workspace`). Sikula passes `--skip-trust` to `gemini`; repository and worktree checks are handled by Sikula before task execution. Note: Gemini CLI permits writes to its own internal temp directory (`~/.gemini/tmp/`); Sikula agents do not use this path.
- *Read-only agents* (analyst, reviewer, security reviewer): write tools and shell are
  excluded from `tools.core` in `.gemini/settings.json` — technically enforced.
- *Write-capable agents* (implementer, fixer, test writer): `run_shell_command` is included
  in `tools.core` but restricted at the prompt level (`grep`, `find`, `ls`, `git rm` only).
  Gemini CLI does not support per-command shell filtering.

---

### Using the built-in `OpenCodeClient`

No custom code is needed — `OpenCodeClient` is already registered as `provider: "opencode"`.
Model must be in `provider/model` format:

```yaml
llm:
  provider: opencode
  model: openai/gpt-5.3-codex
```

Configure authentication in OpenCode according to your provider — see the OpenCode documentation.

Sikula writes generated OpenCode agent definitions to a temporary OpenCode config
directory for each agent run and passes `--dir` with the task project root. It does
not write generated OpenCode files into the project or the original checkout.

**Sandbox notes:**
- *Workspace boundary*: Sikula invokes OpenCode with `cwd` and `--dir` set to the task project root. Sikula does not add an OS-level workspace sandbox for OpenCode; any additional workspace boundary behavior comes from OpenCode itself. `allowed_read_paths` and `allowed_write_paths` are prompt constraints, not OS-level restrictions.
- *Read-only agents* (analyst, reviewer, security reviewer): bash denied entirely via `bash: deny` in OpenCode config — technically enforced.
- *Write-capable agents* (implementer, fixer, test writer): bash restricted to `grep`, `find`, `ls`, `git rm` only at the prompt level — OpenCode CLI does not support per-command filtering.

---

## Adding a new platform

The orchestrator loop is platform-agnostic — all platform-specific logic is isolated in
`BuildTool` subclasses (`tools/base_tool.py`) and `.sikula/config.yaml` project configs.
The build-loop methods (`generate_sources`, `sync`, `compile_check`, `run_tests`, `run_check`,
`is_build_config_file`) and the conservative mixed-file audit hook (`is_test_only_change`) are
defined on `BuildTool`; `AndroidGradleTool`, `JvmGradleTool`, `MavenTool`, `NodeTool`,
`PythonTool`, `CargoTool`, and `XcodeTool` are the current implementations.

| Platform | New file |
|---|---|
| **Java backend / Maven** | `tools/maven_tool.py` — subclass `BuildTool` |
| **Any other** | `tools/<platform>_tool.py` — subclass `BuildTool` |

Each new platform also needs:
- `.sikula/config.yaml` in the project directory with `sandbox.allowed_write_paths`, `guidelines.context_files`, and `guidelines.max_file_chars`
- Platform-specific guidelines docs (listed under `guidelines.context_files`)

The agents and the orchestration loop need no changes.
See [ARCHITECTURE.md § Add a platform](ARCHITECTURE.md#add-a-platform-ios-backend-) for the step-by-step.

---

## Development

The test suite lives in `tests/` and is split into unit tests and end-to-end tests:

```
tests/
├── test_*.py          # Unit tests — all LLM calls mocked via unittest.mock
└── e2e/
    ├── conftest.py    # FakeLLMClient, SequencedFakeLLMClient, shared fixtures
    ├── test_run.py    # E2E tests for `sikula run`
    └── test_review.py # E2E tests for `sikula review`
```

Dev dependencies (`pytest`, `pytest-cov`, `ruff`) are declared in `pyproject.toml` under `[project.optional-dependencies]`. Clone the repo and install in editable mode:

```bash
git clone https://github.com/sikula-ai/sikula
cd sikula/
pip install -e ".[dev]"
```

Editable installs run directly from the checkout. `sikula --version` shows the packaged
version plus a development suffix when the checkout is inside git, for example
`sikula 0.1.0-dev+feature.example.abc1234`.

> If you get an "externally-managed-environment" error (common on Linux and some Homebrew setups), create a venv first: `python3 -m venv .venv && source .venv/bin/activate`

**Run all tests:**

```bash
python3 -m pytest tests/ -v
```

**Run only unit tests:**

```bash
python3 -m pytest tests/ --ignore=tests/e2e -v
```

**Run only e2e tests:**

```bash
python3 -m pytest tests/e2e/ -v
```

**Run with coverage:**

```bash
python3 -m pytest tests/ --cov=agents --cov=core --cov=tools --cov=sikula --cov-report=term-missing
```

Use coverage to check new or changed code where meaningful. Coverage is useful for unit-testable behaviour such as state transitions, tool commands, orchestration logic, prompt construction, and output parsing. LLM calls are always mocked in tests — actual model behaviour is not unit-testable and is validated through end-to-end tests.

**Unit test coverage:**

| Module | What is tested |
|---|---|
| `core/orchestrator.py` | Phase gating, idempotency guards, build/fix loop, review loop, security loop, max-iteration limits, build tool factory |
| `core/state.py` | TaskState field defaults, serialisation, history append, `review_diff` round-trip and backward compat |
| `core/llm_client.py` | Factory (provider selection), call-with-retry, provider-specific response parsing (Codex, Gemini, OpenCode) |
| `agents/base_agent.py` | `AGENT_SECURITY_PREFIX` constant (network/filesystem constraint prepended to all agent prompts), extra-rules loading shared across agents |
| `agents/analyst_agent.py` | Guard conditions, output parsing, implementation prompt population |
| `agents/planner_agent.py` | SINGLE_PASS fallback, step parsing, max_steps enforcement |
| `agents/implementer_agent.py` | Guard conditions, file tracking, prompt construction (step context, review issues, write paths), error handling |
| `agents/reviewer_agent.py` | Guard conditions, approval/rejection parsing, diff truncation, `review_diff` state field takes priority over `git_tool.diff_head()`, plan step context |
| `agents/security_reviewer_agent.py` | Guard conditions, approval/blocking/warning/unexpected-output parsing, diff truncation, `review_diff` state field priority |
| `agents/fixer_agent.py` | Guard conditions, build/test/check error routing, write-path switching (production vs test dirs), error section construction |
| `agents/test_writer_agent.py` | Guard conditions, skip when test paths unconfigured, diff truncation, coverage target, error handling |
| `agents/init_agent.py` | Prompt construction, output parsing |
| `tools/base_tool.py` (Sandbox) | Path enforcement: allowed read/write roots, resolve logic |
| `tools/python_tool.py` | subprocess dispatch, exit-code 5 handling, timeout, sync, `is_build_config_file` |
| `tools/gradle_tool.py` | `GradleBaseTool`: subprocess dispatch, timeout, `run_check`, `is_build_config_file` |
| `tools/gradle_android_tool.py` | `AndroidGradleTool`: task configuration, `generate_sources` with presync clean, sync, compile, test |
| `tools/gradle_jvm_tool.py` | `JvmGradleTool`: configurable tasks (classes/test), presync clean, inheritance from `GradleBaseTool` |
| `tools/maven_tool.py` | `MavenTool`: `./mvnw` auto-detection, command construction, presync clean, `is_build_config_file` |
| `tools/node_tool.py` | `NodeTool`: package-manager detection, package-script defaults, sync/compile/test/check command dispatch, `is_build_config_file` |
| `tools/cargo_tool.py` | subprocess dispatch, timeout, task configuration, `is_build_config_file`, run_check |
| `tools/xcode_tool.py` | subprocess dispatch, project args, compile/test task names, run_check, error extraction, `is_build_config_file` |
| `tools/git_tool.py` | subprocess dispatch, diff/status/checkout/add/commit/worktree operations |
| `tools/file_tool.py` | Sandbox-enforced read/write, path validation |
| `tools/scanner.py` | Build tool detection, guideline file scanning, write path detection, Xcode scheme detection |
| `sikula.py` (helpers) | `_find_project_root`, `_resolve_config`, `_resolve_state_dir`, `_load_config`, `_branch_stem`, `_generate_config`, `_resolve_root_path`, `_resolve_task_path` |
| `sikula.py` (review) | `cmd_review` worktree setup, report-only mode, gitignore management |
| `sikula.py` (status) | `cmd_status` task ordering, version flag |

**E2E test coverage:**

E2E tests exercise the full `sikula run` and `sikula review` command paths with a `FakeLLMClient` that writes deterministic files to disk — no API keys or network required.

| Scenario | What is tested |
|---|---|
| Single-pass happy path | Exit 0, `state.done=True`, files written to disk, history records key phases |
| Single-pass no changes | Exit 1 when implementer writes no files |
| Multi-step plan | Planner splits task into 2 steps, both complete in order |
| Multi-step step skipped | Step with no file changes is skipped, not aborted |
| Review rejection cycle | Reviewer rejects → implementer fixes → re-review approves |
| Max review iterations | Task fails after `max_review_iterations` consecutive rejections |
| Security blocking cycle | Security reviewer blocks → implementer fixes → re-review + re-security approves |
| Max security review iterations | Task fails after `max_security_review_iterations` consecutive security rejections |
| Agent exception | Unhandled agent exception sets `state.failed=True`, exits 1 |
| All steps skipped | Multi-step where every step writes nothing fails the task |
| Worktree isolation | Successful run commits to `sikula/<task>` branch, removes worktree |
| Worktree preserved on failure | Failed run keeps worktree at `.sikula/worktrees/` for inspection |
| Resume interrupted task | `--task-id` resumes from partial state (analyst + planner done, implementer not yet run) |
| Reset failed task | `--reset-failed` clears `failed` flag and resumes to completion |
| `sikula review` approved | Exit 0, `review_approved=True`, `security_approved=True`, `done=True` |
| `sikula review` rejected | Exit 1, `review_approved=False`, `failed=True` |
| `sikula review` security warnings | Warnings-only security output is non-blocking; review approved |

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design, execution flow, and agent descriptions.

---

## Contributing

Sikula is a maintainer-led project. Feedback, bug reports, task-result reports,
documentation fixes, and small corrections are welcome. For larger code changes,
please open an issue or Discussion before starting so we can align on scope and
keep the project focused. Pull request contributions require the [CLA](CLA.md).

## Security

To report a security vulnerability, use GitHub private vulnerability reporting from the repository Security tab. If that flow is unavailable, email **contact@sikula.ai**. Do not open a public issue for vulnerabilities. See [SECURITY.md](SECURITY.md) for the full disclosure policy.

## License

Sikula is licensed under the [GNU Affero General Public License v3.0](LICENSE) only (`AGPL-3.0-only`).
See [NOTICE](NOTICE) for copyright information.
