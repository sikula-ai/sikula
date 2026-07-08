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

Sikula has built-in CLI integrations for Codex, Claude, Gemini, OpenCode, and Antigravity. See [Providers](docs/providers.md) for authentication, model configuration, per-agent overrides, and data-boundary notes.

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

Generated configs use Codex by default. Claude, Gemini, OpenCode, and Antigravity are supported too; see [Providers](docs/providers.md).

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
# Inspect a task to see if it is ready for implementation
sikula contract check .sikula/tasks/my-task.md

# Let Sikula refine a rough product task into a cleaner task description
sikula task refine .sikula/tasks/my-task.md \
  --auto \
  --output .sikula/tasks/my-task.refined.md

# Prepare a project-aware implementation contract from the task
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --auto \
  --output .sikula/contracts/my-task.contract.md
```

Use this when you want to clarify a product task description into a formal implementation contract before any agents start changing code. `task refine` is an optional step to normalize a rough request, and `contract prepare` adds project-aware delivery constraints.

These commands also support interactive modes (`--interactive`) and answers-file injection for strict control. See [Writing Sikula Tasks](docs/writing-tasks.md) for full details on advanced contract workflows.

**Author and check a delivery plan**

```bash
sikula delivery prepare .sikula/tasks/my-task.md
sikula delivery prepare .sikula/tasks/my-task.md --json
sikula delivery check .sikula/delivery/my-task/plan.yaml
sikula delivery check .sikula/delivery/my-task/plan.yaml --json
sikula delivery status .sikula/delivery/my-task/plan.yaml
sikula delivery status .sikula/delivery/my-task/plan.yaml --json
sikula delivery run-next .sikula/delivery/my-task/plan.yaml --dry-run
sikula delivery run-next .sikula/delivery/my-task/plan.yaml
sikula delivery run-next .sikula/delivery/my-task/plan.yaml \
  --agent-provider implementer=antigravity
sikula delivery finalize .sikula/delivery/my-task/plan.yaml --dry-run
sikula delivery finalize .sikula/delivery/my-task/plan.yaml
```

Use this to author, validate, and inspect the tracked parent plan for larger work split into delivery units. By default, `delivery prepare` writes:

- `.sikula/delivery/<slug>/plan.yaml`
- `.sikula/delivery/<slug>/units/<unit-slug>.md`

`delivery prepare` is an authoring step only: it does not start implementation, create task state, run delivery units, mutate `.sikula/state/delivery/<plan-id>/`, or update branches. The follow-up path is to check the plan, dry-run the next eligible unit, run that unit through the normal `sikula run` pipeline, and finalize the completed plan. The current MVP checks plan structure, unit dependencies, task paths, optional planning and sizing metadata, single-repository scope, and privacy-safe parent progress. See [Delivery Plans](docs/delivery-plans.md).

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

When you are already on the branch you want Sikula to fix, use current-branch
fix mode:

```bash
sikula review \
  --fix \
  --current-branch \
  --base-branch main \
  --description-file pr.md
```

This keeps write-capable agent work in an isolated Sikula worktree, then
fast-forwards the originally current branch only after review, validation, and
delivery safety checks pass.

## Common First-Run Issues

- **`pipx: command not found`** - install `pipx` from <https://pipx.pypa.io/stable/installation/>.
- **Config, guidelines, or rules missing in the worktree** - commit `.sikula/config.yaml`, any generated `.sikula/guidelines.md`, and any `extra_rules` files used by enabled phases before the first isolated run. These prompt-context paths must be files in the worktree start ref. For `sikula review`, make sure the reviewed branch contains its configured guidelines/rules files.
- **Provider is not authenticated** - run `codex login` or see [Providers](docs/providers.md) for Claude, Gemini, OpenCode, and Antigravity.
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
The React example demonstrates how to provide physical delivery assets in a task description (see `add-search-by-name.md`).

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
| Checking large-work delivery plans | [Delivery Plans](docs/delivery-plans.md) |
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
