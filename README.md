# Sikula

[![PyPI version](https://img.shields.io/pypi/v/sikula)](https://pypi.org/project/sikula/) [![Python versions](https://img.shields.io/pypi/pyversions/sikula)](https://pypi.org/project/sikula/) [![CI](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml/badge.svg)](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml) [![codecov](https://codecov.io/gh/sikula-ai/sikula/graph/badge.svg)](https://codecov.io/gh/sikula-ai/sikula) [![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)

Sikula is a local AI software delivery pipeline for turning written engineering tasks into reviewed, tested git branches.

It checks task clarity through an implementation contract, runs the work through a gated agentic delivery pipeline, and records the run in an auditable state file you can inspect to understand the result and improve the next run.

## Why Sikula

Most AI coding tools optimize for producing a diff. Sikula optimizes for the delivery path around that diff: is the task clear, did the change stay in scope, did independent agents review it, did validation pass, and can a human audit what happened?

```text
Rough intent -> implementation contract -> gated agentic delivery pipeline -> PR-ready branch + state file -> better next run
```

- **Before coding**, the implementation contract checks whether the request is clear and deliverable, then turns it into scope, acceptance criteria, risks, tests, and validation.
- **During the run**, separate agents implement, review, security-review, write tests, and fix build/test/check failures. Each agent can use the LLM provider, model, and timeout that fits its job.
- **After completion**, Sikula leaves a normal git branch plus an auditable state file. That record helps improve future task contracts, project guidelines, and delivery decisions.

Default runs use git worktree isolation, so the task happens on a dedicated branch and worktree instead of modifying your main checkout directly.

## Need Help?

If you want help integrating Sikula into your codebase or discussing alternative licensing terms, visit [sikula.ai](https://sikula.ai).

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

Prerequisites: Python 3.10+, `git`, `pipx`, a git repository for the target project, and one authenticated LLM CLI provider. If `pipx` is not installed, use the official installation guide: <https://pipx.pypa.io/stable/installation/>.

| Goal | Install |
|---|---|
| Use the latest release | `pipx install sikula` |
| Use the latest development version from source | `pipx install --editable .` |

```bash
# Install Sikula
pipx install sikula

# Authenticate the default provider used by generated configs
codex login

# Initialize Sikula inside your project
cd my-project/
sikula init

# Review TODOs, then commit config before the first isolated run
git add .sikula/config.yaml .sikula/.gitignore
git commit -m "Add Sikula config"

# Write a task
mkdir -p .sikula/tasks
$EDITOR .sikula/tasks/my-task.md
```

For source installs, other providers, generated guidelines, `--no-isolate`, and contract improvement, see [First Run](docs/first-run.md).

After setup, choose the workflow that fits what you want to do.

## Ways To Use Sikula

**Check and improve a task contract**

```bash
sikula contract check .sikula/tasks/my-task.md
sikula contract check .sikula/tasks/my-task.md --write-report
# edit .sikula/contracts/my-task.answers.yaml
sikula contract improve .sikula/tasks/my-task.md \
  --answers .sikula/contracts/my-task.answers.yaml \
  --output .sikula/tasks/my-task.v2.md
```

Use this when you want to clarify a task before any agents start changing code.

**Run a task into a branch**

```bash
sikula run .sikula/tasks/my-task.md
sikula status
git diff <base-branch>...sikula/<task-stem>-<task-id>
```

Use this when the task is ready and you want Sikula to run the gated delivery pipeline.

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
- **Run failed after creating a worktree** - inspect `sikula show <task-id>` and `.sikula/worktrees/<task-id>/`.

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
| Project configuration | [Configuration](docs/configuration.md) |
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

Feedback, bug reports, task-result reports, documentation fixes, and small corrections are welcome. For larger changes, open an issue or discussion first so we can align on scope.

Pull requests require a signed Contributor License Agreement. See [CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md).

## Security

Please do not report vulnerabilities through public issues. Use GitHub private vulnerability reporting, or email `contact@sikula.ai` if the private flow is unavailable. See [SECURITY.md](SECURITY.md).

## License

Sikula is licensed under [AGPL-3.0-only](LICENSE).
See [NOTICE](NOTICE) for copyright and attribution information.
