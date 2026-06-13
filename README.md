# Sikula

[![PyPI version](https://img.shields.io/pypi/v/sikula)](https://pypi.org/project/sikula/) [![Python versions](https://img.shields.io/pypi/pyversions/sikula)](https://pypi.org/project/sikula/) [![CI](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml/badge.svg)](https://github.com/sikula-ai/sikula/actions/workflows/ci.yml) [![codecov](https://codecov.io/gh/sikula-ai/sikula/graph/badge.svg)](https://codecov.io/gh/sikula-ai/sikula) [![License: AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-blue.svg)](LICENSE)

**From prompt to delivery-ready code.**

AI writes code fast. Teams still struggle to ship it.

Sikula is a contract-first delivery system for AI coding and agentic software delivery. It turns rough intent into an implementation contract, runs the work through gated implementation, AI code review, security review, test, and validation loops, and commits the result to a PR-ready branch with an auditable state file.

Use Sikula when the work is no longer just a prompt, but a real ticket that should become reviewed, tested code.

Website: [sikula.ai](https://sikula.ai)
Quickstart: [docs/quickstart.md](docs/quickstart.md)

---

## Core Concepts

- **Implementation contract** - a two-way handshake between you and Sikula: you bring the intent, Sikula checks whether it is clear and deliverable, asks for missing context when needed, and turns it into scope, acceptance criteria, risks, tests, and validation.
- **Gated pipeline** - code does not stop at generation. Sikula runs implementation through independent review, security review, test writing, and build/test/check validation before the branch is considered ready.
- **State file** - every run records what happened: prompts, outputs, decisions, review rounds, security findings, validation results, config snapshot, and final metadata.
- **Learn & adapt** - each run leaves behind structured context that helps improve future contracts, project guidelines, and delivery decisions.

```text
Rough intent
  -> implementation contract
  -> gated AI delivery pipeline
  -> PR-ready branch + state file
  -> better next run
```

## Why Sikula

Most AI coding tools optimize for producing code. Sikula optimizes for getting a real task to a branch a human can review with confidence.

- **Task-first, not chat-first** - a written task is the input; a branch is the output.
- **Independent review loops** - reviewer and security reviewer are separate read-only agents.
- **Build-aware delivery** - compile, test, and configured quality checks feed a fixer loop until validation passes or the task fails explicitly.
- **Git worktree isolation** - default runs happen on a dedicated branch and worktree, leaving your main checkout untouched.
- **LLM provider routing** - each agent can use the provider, model, and timeout that fits its job.
- **Existing workflow fit** - output is a normal git branch and commit, ready for your PR and CI process.
- **Auditable by design** - task state captures configuration, prompts, results, validation records, and review/security findings.

## Quickstart

Prerequisites: Python 3.10+, `git`, a git repository for the target project, and one authenticated LLM CLI provider.

Choose the install path that matches what you are doing:

| Goal | Install |
|---|---|
| Use Sikula in your projects | `pipx install sikula` |
| Use the latest development version from source | `pipx install --editable .` |

For normal project use:

```bash
pipx install sikula
```

For the latest development version from source:

```bash
git clone https://github.com/sikula-ai/sikula
cd sikula/
pipx install --editable .
```

If you want to run Sikula's own test suite from that checkout, add the dev tools:

```bash
pipx inject sikula pytest pytest-cov ruff
```

If `pipx` is not installed, use the official installation guide: <https://pipx.pypa.io/stable/installation/>. Contributor details are in [CONTRIBUTING.md](CONTRIBUTING.md).

Authenticate a provider. Codex is the default in generated configs:

```bash
codex login
```

Initialize Sikula in your project:

```bash
cd my-project/
sikula init
```

Review `.sikula/config.yaml`, then commit the generated config before your first isolated run. Default runs create a git worktree from `HEAD`, so untracked or uncommitted config/guideline files are not visible to the task worktree.

```bash
git add .sikula/config.yaml .sikula/.gitignore
git commit -m "Add Sikula config"
```

Create a task:

```bash
mkdir -p .sikula/tasks
$EDITOR .sikula/tasks/my-task.md
```

Check whether the task is clear enough to deliver:

```bash
sikula contract check .sikula/tasks/my-task.md
```

More contract tools are covered in [Writing Sikula Tasks](docs/writing-tasks.md) and [Workflow](docs/workflow.md).

Run it:

```bash
sikula run .sikula/tasks/my-task.md
```

Inspect the result:

```bash
sikula status
git diff <base-branch>...sikula/<task-stem>-<task-id>
```

For a local experiment without a worktree branch, use `sikula run --no-isolate .sikula/tasks/my-task.md`.

## What You Get

- A dedicated branch named `sikula/<task-stem>-<task-id>`.
- A final commit when the task completes successfully.
- Independent review and security review records.
- Tests written or updated within configured test paths.
- Build/test/check validation records and recovered diagnostics.
- A task state file inspectable with `sikula show <task-id>`.

## Supported Stacks

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

## LLM Providers

Sikula has built-in CLI integrations for:

| Provider config | Backing CLI |
|---|---|
| `codex` | `codex exec` |
| `claude` | `claude -p` |
| `gemini` | `gemini` |
| `opencode` | `opencode run` |

Model names and authentication depend on the provider. Configure defaults in `.sikula/config.yaml`, or override individual agents per run:

```bash
sikula run .sikula/tasks/my-task.md \
  --agent-provider implementer=claude \
  --agent-timeout implementer=2400
```

See [Providers](docs/providers.md).

## Review An Existing Branch

Use `sikula review` when code already exists and you want an independent review/security gate.

```bash
sikula review \
  --branch feature/login \
  --base-branch main \
  --description-file pr.md
```

Report-only review exits `0` when approved and `1` when issues are found. Add `--fix` to let Sikula apply accepted fixes through the normal build/fix loop and commit them back to the reviewed branch.

See [Reviewing Branches](docs/review.md).

## Try An Example

The repository includes runnable examples with ready-to-run task files:

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

## Demo

This demo shows Sikula taking a task through analysis, implementation, independent review, test repair, and final validation on the JVM/Gradle Countries backend example.

![Sikula terminal demo showing analysis, implementation, independent review, test fix loop, and final branch status](docs/assets/sikula-demo.gif)

## Documentation

The main README is the documentation entry point. Deeper guides live under `docs/`:

| Need | Read |
|---|---|
| First setup and first task | [Quickstart](docs/quickstart.md) |
| Writing good task files | [Writing Sikula Tasks](docs/writing-tasks.md) |
| Contract-first delivery workflow | [Workflow](docs/workflow.md) |
| Project configuration | [Configuration](docs/configuration.md) |
| Provider setup and model config | [Providers](docs/providers.md) |
| Worktree isolation, sandboxing, and privacy | [Sandbox](docs/sandbox.md) |
| Reviewing existing branches | [Reviewing Branches](docs/review.md) |
| Internal architecture and state model | [ARCHITECTURE.md](ARCHITECTURE.md) |

Learn more at [sikula.ai](https://sikula.ai).

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
