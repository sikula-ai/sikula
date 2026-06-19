# Sikula

[![PyPI version](https://img.shields.io/pypi/v/sikula)](https://pypi.org/project/sikula/) [![Python versions](https://img.shields.io/pypi/pyversions/sikula)](https://pypi.org/project/sikula/) [![CI](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml/badge.svg)](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml) [![codecov](https://codecov.io/gh/sikula-ai/sikula/graph/badge.svg)](https://codecov.io/gh/sikula-ai/sikula) [![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)

Sikula is a local AI software delivery pipeline for turning task descriptions into reviewed, tested git branches.

It helps turn product intent into an implementation contract, runs that contract through a gated agentic delivery pipeline, and records the run in an auditable state file you can inspect to understand the result and improve the next run.

## Why Sikula

Most AI coding tools optimize for producing a diff. Sikula optimizes for the delivery path around that diff: is the task clear, did the change stay in scope, did independent agents review it, did validation pass, and can a human audit what happened?

```text
Product task description
  -> implementation contract
  -> gated agentic delivery pipeline
  -> PR-ready branch + state file
  -> better next run
```

- **Before coding**, a product task description can be checked and improved into an implementation contract with scope, acceptance criteria, risks, tests, and validation.
- **During the run**, separate agents implement, review, security-review, write tests, and fix build/test/check failures. Each agent can use the LLM provider, model, and timeout that fits its job.
- **After completion**, Sikula leaves a normal git branch plus an auditable state file. That record helps improve future implementation contracts, project guidelines, and delivery decisions.

Default runs use git worktree isolation, so the task happens on a dedicated branch and worktree instead of modifying your main checkout directly.

## Need Help?

If you want help integrating Sikula into your codebase, do not hesitate to reach out through [sikula.ai](https://sikula.ai).

## Compatibility

### Supported Stacks

| Stack | Build tool |
|---|---|
| Android / Gradle | `gradle-android` |
| JVM backend / Gradle | `gradle-jvm` |
| JVM backend / Maven | `maven` |
| Node.js / TypeScript / JavaScript, including npm, pnpm, Yarn, and Bun | `node` |
| iOS / Xcode | `xcodebuild` |
| Python | `python` |
| Rust / Cargo | `cargo` |

The orchestration loop is platform-neutral. Stack-specific behavior lives in `BuildTool` subclasses under `tools/`.

### LLM Providers

Sikula has built-in CLI integrations for Codex, Claude, Gemini, and OpenCode. See [Providers](docs/providers.md) for authentication, model configuration, per-agent overrides, and data-boundary notes.

## Demo

This demo shows Sikula taking a task through analysis, implementation, independent review, test repair, and final validation on the JVM/Gradle Countries backend example.

![Sikula terminal demo showing analysis, implementation, independent review, test fix loop, and final branch status](docs/assets/sikula-demo.gif)

## Get Started

Prerequisites: Python 3.10+, `git`, `pipx`, a git repository for the target project, and one authenticated LLM CLI provider.

### Install Sikula

Use the latest release:

```bash
pipx install sikula
```

Or use the latest development version from source:

```bash
git clone https://github.com/sikula-ai/sikula
cd sikula/
pipx install --editable .
```

### Set Up Your Project

Generated configs use Codex by default. Claude, Gemini, and OpenCode are supported too; see [Providers](docs/providers.md).

```bash
codex login

cd my-project/

# Basic setup — detects build tool, language, platform, package manager, source paths
sikula init

# With LLM-generated guidelines — reads the codebase and writes .sikula/guidelines.md
sikula init --provider codex --model gpt-5.5 --guidelines

# You can also add generated guidelines later; existing config is preserved
sikula init --guidelines

# Review TODOs, then commit config and generated guidelines before the first isolated run
git add .sikula/
git commit -m "Add Sikula project setup"

# Write a task
mkdir -p .sikula/tasks
$EDITOR .sikula/tasks/my-task.md
```

For other providers, generated guidelines, `--no-isolate`, and contract preparation, see [First Run](docs/first-run.md).

After setup, choose the workflow that fits what you want to do.

## Ways To Use Sikula

**Prepare and check an implementation contract**

```bash
sikula contract check .sikula/tasks/my-task.md
sikula task refine .sikula/tasks/my-task.md --interactive --output .sikula/tasks/my-task.refined.md
sikula contract check .sikula/tasks/my-task.refined.md --write-report
# edit .sikula/contract-reports/my-task.refined.answers.yaml
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --answers .sikula/contract-reports/my-task.refined.answers.yaml \
  --output .sikula/contracts/my-task.contract.md
# or answer contract-preparation questions in the terminal
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --interactive \
  --output .sikula/contracts/my-task.contract.md
# or let a read-only LLM assistant answer only questions supported by the repo
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --auto \
  --output .sikula/contracts/my-task.contract.md
```

Use this when you want to clarify a product task description into an implementation contract before any agents start changing code. The commands are explicit: `task refine` is an optional product-description refinement step, `contract prepare` writes the project-aware Markdown contract you later pass to `sikula run`, and `sikula run` does not rewrite the task file for you. You can skip `task refine` and run `contract prepare` directly on the original task description when you want one step that asks both product-level and delivery-readiness questions.

**Run a task into a branch**

```bash
sikula run .sikula/tasks/my-task.md
# Optional: abort before agents if the task is not ready enough for delivery
sikula run .sikula/tasks/my-task.md --require-contract-ready
sikula status
git diff <base-branch>...sikula/<task-stem>-<task-id>
```

Use this when the task file is ready to act as the implementation contract and you want Sikula to run the gated delivery pipeline. See [Writing Sikula Tasks](docs/writing-tasks.md) for contract readiness gates and task-writing guidance.

**Review an existing branch**

```bash
sikula review \
  --branch feature/login \
  --base-branch main \
  --description-file pr.md
```

Use this when code already exists and you want an independent review/security gate before merge. See [Reviewing Branches](docs/review.md) for report-only mode, `--fix`, security review, and CI usage.

## Common First-Run Issues

- **`pipx: command not found`** - install `pipx` from <https://pipx.pypa.io/stable/installation/>.
- **Config or guidelines missing in the task worktree** - commit `.sikula/config.yaml` and any generated `.sikula/guidelines.md` before the first isolated run.
- **Provider is not authenticated** - run `codex login` or see [Providers](docs/providers.md) for Claude, Gemini, and OpenCode.
- **Task is too vague** - run `sikula contract check .sikula/tasks/my-task.md` and see [Writing Sikula Tasks](docs/writing-tasks.md).
- **Run failed after creating a worktree** - inspect `sikula show <task-id>` and `.sikula/worktrees/<task-id>/`; retry with `sikula run --task-id <task-id> --reset-failed`.

## What You Get

- A dedicated branch named `sikula/<task-stem>-<task-id>`.
- A final commit when the task completes successfully.
- Independent review and security review records.
- Tests written or updated within configured test paths.
- Build/test/check validation records and recovered diagnostics.
- A task state file inspectable with `sikula show <task-id>`.

## Example Projects

Use these projects to inspect working Sikula configs, task files, and validation pipelines across supported stacks.

For a standalone frontend demo, see [sikula-example-web-project](https://github.com/sikula-ai/sikula-example-web-project): a small Bun, Vite, React, and TypeScript repository with its own Sikula config, task file, guidelines, validation pipeline, and auditable run state.

The Rust example includes commented `extra_rules` files you can enable to see project-specific planner, reviewer, security reviewer, and test writer rules in action.

| Example | Stack |
|---|---|
| `example/android/countries/` | Kotlin, Android, Jetpack Compose |
| `example/ios/countries/` | Swift, iOS, SwiftUI |
| `example/jvm/countries-gradle/` | Kotlin, Spring Boot, Gradle |
| `example/jvm/countries-maven/` | Kotlin, Spring Boot, Maven |
| `example/node/countries-react/` | TypeScript, React, Vite |
| `example/node/countries-bun-fullstack/` | TypeScript, Bun full-stack |
| `example/rust/countries/` | Rust CLI |

Example:

```bash
cd example/node/countries-react
sikula run .sikula/tasks/add-search-by-name.md
```

## Documentation

The main README is the documentation entry point. Deeper guides live under `docs/`:

| Need | Read |
|---|---|
| First install, setup, and run | [First Run](docs/first-run.md) |
| Writing good task files | [Writing Sikula Tasks](docs/writing-tasks.md) |
| Gated delivery pipeline and state model | [Pipeline And State](docs/pipeline.md) |
| Project configuration, guidelines, and agent-specific rules | [Configuration](docs/configuration.md) |
| Provider setup and model config | [Providers](docs/providers.md) |
| Worktree isolation, sandboxing, and privacy | [Sandbox](docs/sandbox.md) |
| Reviewing existing branches | [Reviewing Branches](docs/review.md) |
| Internal architecture and state model | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Project website | [sikula.ai](https://sikula.ai) |

## Safety, Privacy, And Auditability

Sikula runs locally in your repository and uses git worktrees by default. The LLM provider you configure determines what task, prompt, source, and diff context may be sent outside your machine.

Task state is useful for debugging and audit, but it can contain prompts, source excerpts, build logs, provider output, and sensitive project context. Review and redact `sikula show <task-id>` output before sharing it publicly.

See [Sandbox](docs/sandbox.md), [SECURITY.md](SECURITY.md), and [PRIVACY.md](PRIVACY.md).

## Contributing

Sikula is a maintainer-led project. Feedback, bug reports, task-result reports, documentation fixes, and small corrections are especially helpful.

For code changes beyond small fixes, please open an issue or discussion before starting so we can align on scope and keep the project focused.

Pull request contributions require the [CLA](CLA.md). See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor setup and development guidelines.

## Security

Please do not report vulnerabilities through public issues. Use GitHub private vulnerability reporting, or email `contact@sikula.ai` if the private flow is unavailable. See [SECURITY.md](SECURITY.md).

## License

Sikula is licensed under [AGPL-3.0-only](LICENSE).
See [NOTICE](NOTICE) for copyright and attribution information.
