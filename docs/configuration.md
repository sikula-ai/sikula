# Configuration

Sikula reads project configuration from `.sikula/config.yaml`. Commands auto-discover this file by walking up from the current directory. Use `--config /path/to/config.yaml` to override discovery.

## Generate A Config

```bash
sikula init
```

`sikula init` detects the build tool, language/platform, source and test paths, Node package manager/scripts, shared Xcode scheme when present, and existing guidance files.

If detection is incomplete, the generated config includes `TODO` comments. Review them before running a task.

## Important Sections

```yaml
project:
  name: my-project
  root_path: .
  build_tool: python
  language: Python

sandbox:
  allowed_write_paths:
    - src/
  allowed_test_write_paths:
    - tests/
  allowed_read_paths:
    - .
  max_iterations: 10
  max_review_iterations: 3
  max_security_review_iterations: 3

llm:
  provider: codex
  model: gpt-5.3-codex
  agent_timeout: 1800

run_planner: true
run_review: true
run_security_review: true
run_test_writing: true
run_build: true
run_tests: true
run_checks: true

guidelines:
  context_files:
    - AGENTS.md
    - README.md
  max_file_chars: 30000
```

## Build Tools

| `project.build_tool` | Stack |
|---|---|
| `gradle-android` | Android / Gradle |
| `gradle-jvm` | JVM backend / Gradle |
| `maven` | JVM backend / Maven |
| `node` | Node.js / TypeScript / JavaScript, including npm, pnpm, Yarn, and Bun |
| `xcodebuild` | iOS / Xcode |
| `python` | Python |
| `cargo` | Rust / Cargo |

Build tool details live in `tools/` and are described fully in [ARCHITECTURE.md](../ARCHITECTURE.md).

## Phase Flags

Every `run_*` key can be overridden for a single run:

```bash
sikula run .sikula/tasks/my-task.md --no-build
sikula run .sikula/tasks/my-task.md --no-planner
sikula run .sikula/tasks/my-task.md --security-review
```

Supported run flags:

| Flag | Config key |
|---|---|
| `--build` / `--no-build` | `run_build` |
| `--presync` / `--no-presync` | `run_presync` |
| `--presync-clean` / `--no-presync-clean` | `build.presync_clean` |
| `--planner` / `--no-planner` | `run_planner` |
| `--review` / `--no-review` | `run_review` |
| `--security-review` / `--no-security-review` | `run_security_review` |
| `--test-writing` / `--no-test-writing` | `run_test_writing` |
| `--tests` / `--no-tests` | `run_tests` |
| `--build-per-step` / `--no-build-per-step` | `run_build_per_step` |
| `--checks` / `--no-checks` | `run_checks` |

## Per-Agent LLM Overrides

Each agent can use a different provider, model, or timeout. Keep provider-specific setup, YAML examples, CLI override examples, and valid agent names in [Providers](providers.md).

## Guidelines

Guidelines are one of the strongest quality controls. They tell agents the architecture, naming conventions, testing patterns, and project-specific constraints to follow.

```bash
sikula init --guidelines --provider codex --model <model-your-provider-supports>
```

If `.sikula/config.yaml` already exists, this command preserves the config, writes `.sikula/guidelines.md`, and adds it to `guidelines.context_files` when missing.

## Project-Specific Agent Rules

Use `extra_rules` when one specific agent should follow project rules that do not need to reach every agent.

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

`extra_rules` files are plain Markdown paths relative to the project root. Sikula appends the file content to the selected agent prompt under `## Project-specific rules`.
For isolated worktrees, `extra_rules` files used by enabled agent phases must exist as files in the worktree start ref, be tracked by git, and be clean before Sikula creates the worktree. Otherwise the agent would run with stale or missing rules. For `sikula review`, this means the reviewed branch must already contain the rule files it is configured to use.

Use them for:

- `planner.extra_rules` - task splitting rules.
- `reviewer.extra_rules` - correctness, architecture, and invariants.
- `security_reviewer.extra_rules` - compliance, threat model, and data handling.
- `test_writer.extra_rules` - testing conventions, required doubles, and naming patterns.

Rules apply only when the corresponding agent runs:

- `reviewer` and `security_reviewer` apply in `sikula run`, `sikula review`, and `sikula review --fix`.
- `test_writer` applies in `sikula run` and `sikula review --fix`.
- `planner` applies in `sikula run`; review mode does not run the planner.

`guidelines.context_files` are broad project context. `extra_rules` are targeted per-agent instructions. `extra_rules` do not reach the implementer or fixer.

## Security Context

Use `security.context` to tell the security reviewer what the application does and what threats matter.

```yaml
security:
  context: "Backend API. Handles user accounts and auth tokens. Main concerns are authorization, PII logging, injection, and token handling."
```

Use `security_reviewer.extra_rules` for mandatory project-specific security rules.

## Full Reference

The full config reference and state snapshot contract are maintained in [ARCHITECTURE.md](../ARCHITECTURE.md).
