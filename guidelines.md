# Development Guidelines

This document defines coding conventions and architectural rules for the Sikula project.
It is loaded as context by AI agents — follow every rule precisely.
For the execution model, state fields, and component map, see `ARCHITECTURE.md`.

---

## Architecture

Multi-agent orchestration pipeline. Each concern lives in exactly one layer:

| Layer | Package | Responsibility |
|-------|---------|---------------|
| Orchestration | `core/orchestrator.py` | Phase sequencing, loop control, state transitions |
| Agents | `agents/` | Single-concern LLM calls; mutate `TaskState` in place |
| LLM clients | `core/llm_client.py` | Subprocess wrappers for AI CLI tools |
| State | `core/state.py` | Single source of truth; persisted as JSON after every mutation |
| Tools | `tools/` | File I/O, git, build — all platform-specific logic lives here |
| Config | `.sikula/config.yaml` | Per-project settings; agents must not hardcode project-specific values |

Dependency direction: `orchestrator → agents → llm_client`, `orchestrator → tools`. Agents never import tools directly — they receive a `tools: dict[str, Any]` from the orchestrator.

---

## Self-Hosting Sikula With Sikula

Sikula is expected to run against its own repository for meaningful feature
work. Treat this as a first-class development workflow, not as a demo.

### Contract-first workflow

Fresh Sikula implementation work should start from a source task, then move
through refinement, contract preparation, contract readiness, and `sikula run
--require-contract-ready`. Direct manual edits are appropriate for small
bootstrap fixes, repository maintenance, documentation, configuration, and
explicit review work, but product behavior changes should be expressible as
Sikula tasks and prepared implementation contracts.

Write source tasks as product/behavior descriptions. Do not hand-author
file-by-file implementation plans unless the exact file, command, serialized
field, or migration behavior is itself a stable contract.

Good Sikula core tasks include:

- CLI command behavior, flags, exit/status semantics, and JSON output contracts;
- state, resume, cleanup/delete, worktree, review, review-fix, and delivery
  behavior when those surfaces are touched;
- provider, sandbox, privacy, prompt/audit, diagnostic, and local evidence
  boundaries when they are relevant;
- compatibility expectations for existing `TaskState` files and existing CLI
  workflows;
- documentation acceptance criteria for changes that alter public behavior,
  architecture, state fields, provider setup, task/contract preparation, or
  project governance;
- exact validation commands in backticks, shell code fences, or under a clear
  `Verification:` heading when they are acceptance criteria.

Do not optimize a source task for a better contract-check score by pasting
prepared-contract scaffolding or generated validation snapshots back into the
task. Answer delivery mechanics, reviewer focus, and validation coverage gaps
through the answers file or prepared contract.

### Editable install caveats

In development, the `sikula` command is commonly installed with `pipx install
--editable .` or a virtualenv editable install. The process that starts a run
may therefore import code from the same checkout that a task is changing.

Rules:

- Prefer the default isolated worktree flow for changes to `sikula.py`,
  `sikula_cli/`, `core/`, `agents/`, `tools/`, provider wrappers, state
  handling, worktree handling, or delivery finalization.
- Avoid `--no-isolate` for Sikula self-development unless the user explicitly
  requests it and understands that the running process and edited checkout are
  the same filesystem tree.
- Do not rely on the currently running `sikula` process to reload modified
  Python modules from a task worktree. Validation subprocesses and tests prove
  the modified files; manual end-to-end CLI verification should start a fresh
  `sikula` command after the change lands.
- Do not add validation commands that launch nested real `sikula run` or
  `sikula review --fix` delivery flows. Tests that exercise Sikula command
  paths must use fake LLM clients, temporary repositories, and isolated state.
- If package metadata, entrypoints, process-startup imports, or provider CLI
  setup changes, follow `CONTRIBUTING.md` and reinstall or restart the editable
  CLI before manual verification.
- `.sikula/config.yaml` is captured into task state when a run starts. A task
  that changes this config does not repair the active run's loaded validation
  coverage or sandbox policy; complete the config change and start a fresh run.

### Governance and runtime artifacts

`guidelines.md`, `AGENTS.md`, `.sikula/CONVENTIONS.md`,
`.sikula/config.yaml`, `.sikula/*_rules.md`, `ARCHITECTURE.md`,
`CONTRIBUTING.md`, and public docs are governance surfaces. They may be changed
by explicit governance/documentation/configuration tasks. Do not change them as
incidental implementation output.

Prompt-governing files (`guidelines.md`, `AGENTS.md`, `ARCHITECTURE.md`,
`.sikula/CONVENTIONS.md`, `.sikula/config.yaml`, and `.sikula/*_rules.md`) can
affect agent prompts and policy for later runs. Until Sikula snapshots all
prompt context for active runs, a task that changes these files must be
followed by a fresh Sikula review or run from the committed updated context
before treating the governance change as fully approved. Role-specific
`.sikula/*_rules.md` files are maintained outside normal agent write scope.

`.sikula/state/`, `.sikula/worktrees/`, `.sikula/contract-reports/`, caches,
coverage files, virtualenvs, and build outputs are runtime/debug artifacts. Do
not treat them as source files and do not make contracts depend on their current
contents unless the task explicitly targets state compatibility or runtime
artifact policy.

### Self-hosting review and testing expectations

Changes to Sikula's own workflow must preserve the task's prepared
implementation contract when one exists and keep public docs, architecture
notes, agent guidance, and `.sikula/config.yaml` consistent when they alter
behavior, state semantics, validation policy, provider setup, or task/contract
expectations.

Treat these as stable contracts when they are touched:

- CLI flags, exit codes, status semantics, and JSON/status/result fields;
- `TaskState` compatibility, schema migrations, status/show output, and resume;
- worktree creation, cleanup/delete, review-fix, current-branch delivery, and
  finalization;
- sandbox/write-scope behavior and provider workspace boundaries;
- provider diagnostics, prompt/state privacy, redaction, and local evidence
  handling.

Tests for Sikula command paths must use fake LLM clients, temporary
repositories, temporary `.sikula/state` directories, and explicit config files.
Do not require real provider credentials, git remotes, network access, package
publishing, deployment targets, or machine-specific absolute paths. Test stable
Sikula-owned fields, statuses, and error categories rather than incidental
provider or third-party diagnostic wording.

---

## Python Conventions

### Imports

Every production Python file must include this import after the optional module docstring
and before all other imports:

```python
from __future__ import annotations
```

Standard library imports, then third-party, then internal — one blank line between groups.

### Type hints

All function signatures and class attributes must have type hints. Use built-in generics
(`list[str]`, `dict[str, Any]`) — not `List`, `Dict` from `typing`. Use `X | None` for
optional values in new code; `Optional[X]` is acceptable in existing code for consistency.

```python
def run(self, state: TaskState) -> AgentResult:
    ...

def _cfg(self, key: str, default: int) -> int:
    ...
```

### Dataclasses

Use `@dataclass` for all data-carrying types. Use `field(default_factory=list)` for mutable defaults.

```python
@dataclass
class AgentResult:
    success: bool
    message: str
    data: dict = field(default_factory=dict)
```

### Module-level constants

Private constants use `_SCREAMING_SNAKE_CASE`. Prompt templates are always module-level constants:

```python
_MAX_DIFF_CHARS = 40_000
_DEFAULT_TIMEOUT = 300

_SYSTEM_REVIEW = """\
You are a senior software engineer...
"""

_USER_REVIEW = """\
Task description:
{task_description}
...
"""
```

### Logging

Use a module-level logger named `log`:

```python
log = logging.getLogger(__name__)
```

Log at `INFO` for significant actions, `WARNING` for recoverable anomalies, `ERROR` only
before returning failure. Do not use `print()` in agents, tools, or core orchestration
logic; use logging there. CLI command handlers in `sikula.py` may use `print()` for
intentional user-facing terminal output.

### Error handling

Return `AgentResult(success=False, ...)` or `ToolResult(success=False, ...)` on
technical/tool/provider failure. Reviewer-style agents may also use
`AgentResult(success=False, data={"issues": ...})` for a valid domain decision such as
"issues found"; orchestration must distinguish that from a failed agent invocation.
Catch `RuntimeError` from LLM clients. Do not catch broad `Exception` unless re-raising
or wrapping:

```python
try:
    output = self.llm.run_readonly_agent(prompt, cwd=root)
except RuntimeError as e:
    msg = str(e)
    state.record(self.name, "review_failed", msg[:500])
    return AgentResult(success=False, message=msg[:200])
```

---

## Agent Conventions

### Structure

Every agent subclasses `BaseAgent`, declares a `name` class attribute, and implements a
single public method `run(state) -> AgentResult`:

```python
class ReviewerAgent(BaseAgent):
    name = "reviewer"

    def run(self, state: TaskState) -> AgentResult:
        ...
```

Agents receive tools via `self.tools` (dict), LLM via `self.llm`, and project config via
`self.project_config`. Never import tools directly.

### Config access

Read project-YAML values through `self.project_config`, always with a default:

```python
def _cfg(self, key: str, default):
    return self.project_config.get("analyst", {}).get(key, default)
```

### Project-specific rules injection

Four agents support an `extra_rules` config key that appends a project-owned Markdown file to
the agent's system prompt as a `## Project-specific rules` section with an explicit priority
statement. Use `load_extra_rules()` from `agents/base_agent.py`:

```python
from agents.base_agent import load_extra_rules as _load_extra_rules

# In the agent's run() method, after building the system prompt:
system_prompt += _load_extra_rules(self.project_config, self.name, file_tool)
```

`load_extra_rules` reads the path from `project_config.get(agent_name, {}).get("extra_rules")`,
returns an empty string if the key is absent or the file cannot be read — always safe to call.

The four supported agents and their scope boundaries:

| Agent | `extra_rules` scope | What it must NOT contain |
|-------|---------------------|--------------------------|
| `reviewer` | Correctness checks, invariants, architecture constraints | Implementation instructions — reviewer is read-only |
| `security_reviewer` | Compliance rules, data classification, threat model | Anything that changes output format (BLOCKING/WARNING sections must stay intact) |
| `test_writer` | Testing conventions, required test doubles, naming patterns | Production code rules — test writer cannot touch production files |
| `planner` | Task-splitting conventions: what to split, what to keep atomic | Instructions for individual agents — planner only decides step boundaries |

**Do not add `extra_rules` support to `analyst`, `implementer`, or `fixer`.** The analyst's
configuration point is `guidelines.md` (loaded via `gather_guidelines()`). The implementer
and fixer follow the analyst's `implementation_prompt` — giving them independent rules risks
conflicts with the analyst's output.

### Prompt templates

Prompts are module-level string constants (`_SYSTEM_*`, `_USER_*`, `_AGENT_PROMPT`).
A system prompt defines agent role and rules. A user prompt carries the per-run data.
Use `.format()` substitution — no f-strings for prompts.

### LLM invocation — readonly vs. read-write

Use `self.llm.run_readonly_agent()` for agents that must not modify the filesystem
(reviewer, security reviewer, analyst). Use `self.llm.run_agent()` only for agents that
need to write files (implementer, fixer, test writer). Mixing these up breaks the
read-only invariant and the sandbox boundary.

| Agent | Method |
|-------|--------|
| `AnalystAgent`, `ReviewerAgent`, `SecurityReviewerAgent` | `run_readonly_agent()` |
| `ImplementerAgent`, `FixerAgent`, `TestWriterAgent` | `run_agent()` |

### CLI-backed LLM clients

When implementing or modifying a provider in `core/llm_client.py` that wraps a CLI,
prefer stdin or another provider-supported non-argv input channel for `generate()`,
`run_readonly_agent()`, and `run_agent()` prompts. Reviewer, analyst, and implementation
prompts can exceed operating-system argument length limits on large tasks; command
arguments should carry provider options only when the provider CLI supports it. If a
provider requires the prompt as an option value to enter headless/non-interactive mode,
preserve that provider contract.

For streaming write-agent subprocesses, start output readers before writing the prompt,
write stdin through a timeout-aware path, and tolerate early stdin pipe closure. A provider
can exit immediately or stop reading stdin on quota, authentication, or configuration
failures; Sikula must still enforce `agent_timeout` and drain/classify stdout/stderr
instead of hanging or surfacing a raw broken-pipe exception.

When adding or materially changing CLI-backed provider diagnostics, avoid surfacing raw
provider stderr, stdout, or log payloads in exceptions, retry records, or task state. Prefer
provider-specific safe diagnostic extraction and redact common secrets before returning
errors. Existing providers may have different legacy diagnostic behavior; do not broaden
raw diagnostic exposure when touching them.

### State recording

Call `state.record(self.name, action, result)` after every meaningful action.
The `action` string is a short verb phrase (`"analyze"`, `"review_failed"`, `"implement"`).
The `result` is a single line — path list, char count, or error excerpt:

```python
state.record(self.name, "analyze", f"prompt generated ({len(prompt)} chars)")
state.record(self.name, "review_failed", msg[:500])
```

### Structured observability records

Every agent that invokes an LLM must append one record per invocation to the appropriate
`TaskState` list before returning:

| Agent | List |
|-------|------|
| `AnalystAgent` | `state.analyst_cycle_records` |
| `ImplementerAgent` | `state.implement_cycle_records` |
| `FixerAgent` | `state.fix_cycle_records` |
| `ReviewerAgent` | `state.review_cycle_records` |
| `SecurityReviewerAgent` | `state.security_review_cycle_records` |
| `TestWriterAgent` | `state.test_write_records` |
| Orchestrator validation phases | `state.validation_cycle_records` |

`AnalystAgent` cycle records are content-free invocation indexes: store bounded outcome,
reason/error/disposition, and correlation metadata without duplicating its full prompt or output.
The primary accepted prompt and output remain in `state.analyst_prompt` and
`state.implementation_prompt`; rejected outputs and retry prompts remain append-only in
`state.analyst_retry_records`.

Prompts stored in `TaskState` are local audit artifacts. They should remain faithful to
what the agent received, including provider-added boundary instructions, and should not be
automatically redacted before persistence. Avoid adding secrets to prompts in the first
place, and do not surface stored prompts through ordinary terminal summaries, retry
messages, provider diagnostics, CI output, PR comments, or external reports. `sikula show`
is the explicit full-state audit/debug command and may print prompts; remind users to
review and redact its output before sharing it outside the local project context.

Except for the content-free `AnalystAgent` index described above, each agent record must
include at minimum: the agent's prompt, LLM output (`None` on exception), files written,
timestamp, and the correlation keys needed to locate the record within the pipeline:
`step`, `build_iteration` (`state.build_iterations`),
`review_iteration` (`state.review_iterations`), `security_review_iteration`
(`state.security_review_iterations`), and `scope` (`"task"`, `"step"`, or
`"final_full_task"` when the agent runs after all planned steps).

Validation records are orchestrator-owned and use a smaller shape: `phase`, `status`,
`build_iteration`, `step`, `timestamp`, optional `scope`, optional `elapsed_s`, optional
`check_name`, and diagnostic `error_excerpt` on failure. Error excerpts must preserve
failure-marker blocks from long command output instead of storing only the final tail; the
fixer needs the concrete compiler diagnostic, failing test, assertion, traceback, or tool error.

Records are append-only and must not drive pipeline control flow — stop/continue decisions
belong in dedicated state fields (`review_approved`, `security_approved`, `failed`, etc.).
The same rule applies to `state.active_operation`: it is a transient progress
heartbeat for status/CI visibility only and must not drive pipeline decisions.

```python
state.implement_cycle_records.append({
    "step": state.current_step,
    "build_iteration": state.build_iterations,
    "review_iteration": state.review_iterations,
    "security_review_iteration": state.security_review_iterations,
    "scope": state.active_scope or ("step" if state.plan else "task"),
    "implementer_prompt": prompt,
    "implementer_output": output,  # None on exception
    "files_written": changed,
    "timestamp": datetime.now(timezone.utc).isoformat(),
})
```

### State mutations

Agents mutate `state` in place. The key state fields written by agents:

| Field | Written by | Rule |
|-------|-----------|------|
| `analyst_prompt` | `AnalystAgent` | Full assembled prompt stored before the LLM call; enables post-run audit even if guidelines files change |
| `implementation_prompt` | `AnalystAgent` | Structured prompt fed to implementer; the analyst's key output |
| `analyst_retry_records` | `AnalystAgent` | Append-only records for rejected analyst outputs; never read for pipeline decisions |
| `review_issues` | `ReviewerAgent` | Issue list from last review; cleared on approval |
| `files_changed` | `ImplementerAgent`, `FixerAgent` | Append-only; never clear outside the orchestrator |
| `errors`, `test_errors`, `check_errors` | Orchestrator | Cleared by the fixer after a fix pass |
| `validation_artifact_records` | Orchestrator | Append-only audit of unexpected non-ignored repository changes produced during sync/build/test/check validation, plus sync outputs blocked from safe adoption |
| `history` | `state.record()` | Append-only audit log; never clear or modify past entries |
| `done`, `failed` | Orchestrator only | Never set these in an agent |

### Fix-pass pattern

`ImplementerAgent` is called both for the initial implementation and for review fix passes.
It detects the mode by checking `state.review_issues`: if non-empty, it appends the issues
to the prompt as a `REVIEW ISSUES` section. No separate mode flag exists — the agent is
stateless with respect to pass type. Any agent that participates in a fix loop should follow
the same pattern: read the relevant error/issue list from state, include it in the prompt,
and leave control flow to the orchestrator.

### Agent scope boundaries

Each agent has a fixed scope — crossing it silently breaks the pipeline:

- **Delivery child agents** must validate the persisted inherited-constraint
  context before every provider call. The Analyst, Implementer, Reviewer, and
  Security Reviewer receive the same authoritative unit constraints; dependency
  handoffs and the implementation prompt may not weaken or expand them.
- **Delivery boundary dispositions** are control data, not advisory prose.
  Reviewer and Security Reviewer prompts must receive the same current validated
  production scope enforced for that child invocation, including exact-file versus
  path-prefix semantics. A resumed run must not present the broader creation-time
  upper bound after current policy has narrowed it.
  A delivery-child approval uses the explicit `approved` disposition; ordinary
  non-delivery review retains the standalone `APPROVED` signal. `approved` is
  positive audit evidence only and must never enter terminal-stop or amendment
  failure evidence. A delivery Implementer may report `already_satisfied` only
  with a clean no-change result after inspecting the active task or step. Sikula
  must reject that value when any file changed, and an accepted value must still
  pass every configured review, security, test-writing, and validation gate. A
  changed-file `already_satisfied` result is a non-retryable invalid Implementer
  disposition; its writes remain inspectable but cannot be adopted by reset.
  Unstructured no-change output remains a failure for delivery children. A malformed
  Reviewer or Security Reviewer disposition may receive exactly one read-only
  protocol retry with the parser error included in its history. That retry must
  not consume a fix iteration or invoke a write agent, and a repeated malformed
  result must fail closed. Parsers may normalize one terminal Markdown JSON fence
  around the exact disposition object, but must reject trailing prose, multiple
  schema markers, embedded objects, and ambiguous envelopes.
  `fix_in_scope` may continue the bounded fix loop;
  `requires_scope_amendment` and `external_dependency_gap` must stop the child
  immediately, preserve its audit evidence, and prevent later write agents,
  validation, commit, handoff, and assembly.
- **Delivery constraint verification** must be actionable and bounded. An
  incomplete verifier result must identify concrete omitted constraints or
  missing unit assignments; a bare negative completeness claim is invalid. At
  most one constraints-only repair may run, it must preserve the complete unit
  draft and every existing constraint identity, and its result must pass a
  second independent verification before deterministic publication.
- **Stop-and-follow-up constraints are execution blockers.** A preserved
  `stop_and_follow_up` constraint means the required external decision or input
  remains unresolved; it must make generated unit readiness blocking. Delivery
  execution must recheck the selected unit under the progress lock and stop
  before child creation. It must not treat prompt compliance or
  `--reset-failed` as a recovery mechanism.
- **Delivery unit contracts** must be self-contained because child agents cannot
  read the parent source task. Source-defined identifiers and values required
  verbatim must appear in every affected unit task. Independent preparation
  verification may return only bounded complete source-task lines missing from
  named units; deterministic repair may append those lines to task Markdown but
  must not modify any other unit field, and the result must be reverified.
- **`AnalystAgent`** must not suggest or generate test file changes — test changes are exclusively the domain of `TestWriterAgent`.
- **`AnalystAgent`, `ReviewerAgent`, and `TestWriterAgent`** must preserve structured input contracts: parser, validator, expression engine, DSL, config, schema, and rule-engine changes need explicit accepted/rejected cases, materially different rejected input classes, expected-result-type handling when typed contexts exist, and a validation-vs-runtime failure-phase distinction when observable.
- **`TestWriterAgent`** must only write to paths within `sandbox.allowed_test_write_paths`; production source files are off-limits.
- **`TestWriterAgent`** must treat coverage targets as scoped to existing project test
  infrastructure. It must not synthesize runtime/framework harnesses that recreate render
  trees, selector or event systems, lifecycle schedulers, navigation/history stacks,
  dependency containers, device/emulator APIs, filesystems, servers, command runners, or
  similar missing infrastructure. A test/helper that combines several fake runtime
  subsystems is a synthetic runtime harness, not a collection of harmless small mocks; use
  project-standard seams, narrow stable contracts, or `TESTABILITY GAP` output instead.
  Skipped, disabled, ignored, expected-failure, assumption-gated, or environment-gated
  tests that Sikula's configured validation will not execute do not count as coverage for
  changed behaviour.
  The deterministic execution-gate audit must stay scoped to newly added gates in
  Sikula-modified test files and inline-test source files under configured test write paths
  so pre-existing project skips and legitimate stale-test fixes do not become false
  positives.
- **`ReviewerAgent`** must stay read-only and mode-aware for tests: in normal `sikula run`, changed tests are not standalone reviewer-owned output, but contract-bearing test deletion, relaxation, or replacement with a different invalid fixture is evidence to re-check and report the production contract issue. In `sikula review`, changed tests are branch output and may be reported directly.
- **`ReviewerAgent`** must treat task-described validation commands as coverage requirements for the configured validation pipeline, not as manual commands for agents to run. Commands are extracted only from explicit validation contexts: backticks, shell code fences, `$`-prompted lines, or command lists under validation-oriented headings/prefixes such as `Verification:` or `Run:`; Markdown blank separator lines after such headings are allowed. Prose that happens to start with a tool name is not a command, and bare tool names such as `cargo` or `npm` are not executable validation commands. If a `sikula run` task command is covered by effective build/test/check config, do not block review only because it has not run yet. A same-tool-family command with materially different flags, targets, scripts, packages, schemes, or paths is only a near match, not coverage; Gradle/Maven wrapper spelling for the same invocation (`./gradlew` vs `gradle`, `./mvnw` vs `mvn`), Python module forms (`python -m pytest` vs `pytest`, `python -m ruff` vs `ruff`), the npm `test` shortcut (`npm test` vs `npm run test`), and pnpm/Yarn package-script shorthands for common validation scripts (`pnpm typecheck` vs `pnpm run typecheck`, `yarn lint` vs `yarn run lint`) are accepted as coverage. If a `sikula run` command is not covered, report a validation coverage gap; this is not implementer-fixable inside the current task worktree, so the operator must update the Sikula config file used for the run (default `.sikula/config.yaml`, or the file passed with `--config`) or the task and rerun. In `sikula review` modes, commands found in PR/review text are informational branch-verification context; do not preflight-abort review/fix or report a validation coverage gap solely because such a command is not covered.
- **`FixerAgent`** write paths depend on error type: when `state.errors` is non-empty (build failure), only `allowed_write_paths` (production), unless the build/check diagnostics reference only test files or recognized test targets. Test failures and test-origin validation failures start with a test-only triage/fix pass limited to `allowed_test_write_paths`. The pass emits one `TEST FAILURE TRIAGE` block per failure (`chosen_fix` must be `production_code` or `test_code`, never `none`); if any block classifies a `production_defect` (and did not explicitly choose `test_code`), Sikula runs a separate production-enabled fixer pass with `allowed_write_paths` plus `allowed_test_write_paths`. Test-only writes made before that pass may be kept only when a separate `stale_test`/`malformed_test` triage explicitly authorizes `test_code`; otherwise a production-defect triage must not change files before the confirmed pass. Production writes during a test-only pass are rejected: Sikula restores that pass's writes and retries once; restore failure or a second scope violation fails the task. A production-confirmed pass must actually change production code. Mixed source/test file exceptions must go through the active `BuildTool.is_test_only_change()` hook and must fail closed by default; keep platform syntax rules out of `FixerAgent`.
- **`FixerAgent`** must not stabilize malformed generated tests by adding skipped/disabled
  execution gates for changed behaviour that configured validation cannot run. If missing
  runtime infrastructure prevents meaningful execution, the fixer may output a structured
  `TESTABILITY GAP`, including `covered_by` when existing-surface tests or seams cover the
  runnable portion, which Sikula records in task state. If the orchestrator reports a
  `TEST EXECUTION GATE AUDIT`, the fixer should remove the gate and either add real
  existing-seam coverage or leave a structured `TESTABILITY GAP`; it must not replace one
  passing placeholder gate with another. The fixer must also avoid repeatedly repairing a
  generated test/helper that combines several fake runtime subsystems; simplify to stable
  seam coverage or report the gap instead of fixing one fake subsystem at a time. When
  repeated generated-test fixes trigger `GENERATED TEST RE-TRIAGE`, preserve auditability by
  choosing a platform-neutral strategy rather than adding platform-specific conditions.
  Synthetic harness recovery belongs in the orchestrator: restore affected generated test
  files to the pre-agent snapshot, retry once with audit context, and record a
  `TESTABILITY GAP` if the retry recreates the broad harness. Do not add platform-specific
  hard gates for individual runtimes.
  Missing re-triage output after another generated-test edit should be recoverable by
  restoring that pass and retrying once before validation is run again; a repeated omission
  should remain auditable without blocking an otherwise valid task solely on prompt format.
- **`ReviewerAgent` and `SecurityReviewerAgent`** must never write files — use `run_readonly_agent()` only.
- **`SecurityReviewerAgent`** fail-safe: non-delivery output with no `APPROVED` signal, no `## Warnings` section, and no `## Security Issues` section is blocking. Delivery output always requires a valid disposition, including warning-only output; a missing disposition is a protocol error subject only to the bounded read-only retry. Never relax this — ambiguous output from the security reviewer must always fail closed.

### Platform neutrality

Agent prompts and logic must be platform-neutral. References to Gradle tasks, Kotlin idioms,
Android layer names, or any other platform-specific detail do not belong in agents. Platform
specifics belong only in `BuildTool` subclasses and project config YAMLs.

---

## Tool Conventions

### BaseTool

Subclass `BaseTool` for general-purpose tools. Always call `self.sandbox.check_write(path)`
before any write operation:

```python
class FileTool(BaseTool):
    def write(self, path: str, content: str) -> ToolResult:
        self.sandbox.check_write(Path(path))
        ...
```

### BuildTool

Subclass `BuildTool` for platform build systems. Implement all five required methods:
`sync()`, `compile_check()`, `run_tests()`, `run_check()`, `is_build_config_file()`.
Override `generate_sources()` only when `sync()` is too broad for the presync phase.
Override `env_files()` only when the platform needs gitignored files copied to new worktrees.
Override `is_sync_adoptable_file()` only to classify source-controlled, project-relative
outputs that `sync()` may intentionally update and that should be reviewed in the final diff
when they already exist as tracked files. Brand-new generated outputs require explicit
`build.sync_adopt_paths` opt-in.
Override `is_test_only_change()` only for syntax-aware mixed source/test file detection; the
default must remain conservative. If that detection requires before/after file bytes,
override `requires_test_only_change_content()` only for plausible mixed source/test files;
generated, binary, and oversized content must remain digest-only.

For Gradle-based platforms, subclass `GradleBaseTool` (`tools/gradle_tool.py`) instead of
`BuildTool` directly — it provides `_run()`, `_run_shell()`, `run_check()`, and
`is_build_config_file()` so you only need to implement `sync()`, `compile_check()`, and
`run_tests()`. See `AndroidGradleTool` and `JvmGradleTool` as reference implementations.

When adding a new platform, update these core files: `tools/<platform>_tool.py` (new tool),
`core/orchestrator.py` (`_build_tool()`), `sikula.py` (`_build_tool_class()`,
`_SUPPORTED_BUILD_TOOLS`), `sikula_cli/init.py` (`generate_config()`), and
`tools/scanner.py` (`_SIGNATURES` + path detection). Update
`tests/test_platform_onboarding.py` so the factory, scanner, and generated init-config
surfaces stay in sync. If the platform supports inline tests in source files with suffixes
not already covered, update `_TEST_GATE_AUDIT_SOURCE_SUFFIXES` in `core/orchestrator.py`.
If the platform introduces test framework
skip/disable/ignore/expected-failure/assumption idioms that are not already covered, also
update
`core/test_execution_gate_audit.py` and `tests/test_test_execution_gate_audit.py`. If the
platform introduces common fake runtime idioms not already covered, update
`core/synthetic_test_harness_audit.py` and `tests/test_synthetic_test_harness_audit.py`.
These are the audit registries allowed outside BuildTool code because the audit remains
platform-neutral and scoped to Sikula-modified tests. For source-controlled outputs updated
by sync, decide whether `is_sync_adoptable_file()` should classify them. For mixed
source/test files, leave `is_test_only_change()` conservative unless syntax-aware diff
analysis can prove the change is test-only.
If `generate_sources()` produces source/IDL files under gitignored build output paths that
read-only agents need for analysis or review, update Antigravity's read-only generated-source
preservation rules and regression tests while keeping gitignored secrets and env files out of
provider workspace copies.

All methods return `ToolResult`. Treat subprocess exit code 0 as success. Platform-specific
exit codes (e.g. pytest exit 5 = "no tests collected") may also be treated as success — document
why with an inline comment.

Keep platform-specific dependency resolution behavior in the relevant `BuildTool`, not in the
orchestrator. For example, Cargo's default sync may retry `cargo fetch` after a locked fetch
reports that `Cargo.lock` needs updating, while explicit `build.sync_command` values remain exact.
Keep sync-output policy platform-neutral: the orchestrator owns repository snapshots,
cleanup, adoption into `state.files_changed`, audit records, and stale review/security/test
gates. Platform tools only classify paths such as lockfiles or dependency verification
metadata through `is_sync_adoptable_file()`; do not put Cargo, Gradle, Node, or Xcode
path rules in agents or orchestration logic.

On failure, set `ToolResult.error` to the combined stdout+stderr output (not stderr alone) —
many tools (pytest, cargo test, ruff) write diagnostics to stdout:

```python
output = r.stdout + r.stderr
if r.returncode not in (0, 5):  # pytest exit 5 = no tests collected
    return ToolResult(success=False, output=output, error=output[-4000:])
return ToolResult(success=True, output=output)
```

---

## State Conventions

`TaskState` is the current persisted workflow state and the resume/audit source
of truth. The orchestrator persists it to JSON after every agent operation —
do not cache state values in local variables across agent calls.

`TaskState` JSON is local execution state and audit/debug storage. It is not a public
protocol for runner, console, or other external consumers. Public machine-readable output
must come from explicit projection code with stable fields, privacy rules, and tests.
Do not implement `--json`, status/result, or future runner/console output by serializing
raw `TaskState`, `sikula show`, prompt text, logs, or terminal output and trimming fields
after the fact.

When adding a new domain/result layer over the current state model, keep one decision
source of truth. Legacy state fields may be derived compatibility projections, but old and
new fields must not independently drive workflow decisions.

Runtime metadata, CLI version output, and other user-facing Sikula version labels must use
`core.version.sikula_version()` rather than calling `importlib.metadata.version("sikula")`
directly. This keeps task-state audit records aligned with `sikula --version`, including
development checkout suffixes.

### Schema migrations

Removing, renaming, or changing the type of an existing `TaskState` field requires a
`schema_version` migration in `core/state.py`. Omitting it silently breaks resume for
existing tasks — the field is either missing or has the wrong type on load.

```python
# core/state.py — in JsonStateStore.load(), before TaskState(**data):
SCHEMA_VERSION = 2  # bump when making breaking field changes

# --- schema migrations (run in version order before TaskState is constructed) ---
if data.get("schema_version", 1) < 2:
    data["new_field"] = data.pop("old_field", None)
# --- end migrations ---
```

Adding a new optional field with a default value does not require a migration.

### StateStore abstraction

Orchestrator and agents must depend only on the `StateStore` interface (`core/state.py`).
Never import `JsonStateStore` directly outside of `sikula.py` — `JsonStateStore` is
instantiated only there and injected via constructor.

| Field category | Rule |
|---|---|
| `analyst_prompt` | Written by `AnalystAgent` before the LLM call; never overwrite after first write |
| `implementation_prompt`, `review_issues` | Written by agents; read by subsequent agents |
| `files_changed` | Append-only; never clear outside the orchestrator |
| `errors`, `test_errors`, `check_errors` | Cleared by the fixer after a fix pass |
| `history` | Append-only via `state.record()`; never cleared; permanent audit log |
| `validation_cycle_records` | Append-only via the orchestrator; never used for pipeline control flow |
| `validation_artifact_records` | Append-only via the orchestrator; records sync/build/test/check artifact cleanup and blocked sync-output adoption for audit |
| `done`, `failed` | Set by the orchestrator only; never set in an agent |

---

## Naming Conventions

| Element | Pattern | Example |
|---------|---------|---------|
| Agent class | `<Role>Agent` | `ReviewerAgent`, `TestWriterAgent` |
| Tool class | `<Platform>Tool` | `AndroidGradleTool`, `PythonTool` |
| LLM client class | `<Provider>Client` | `ClaudeClient`, `GeminiClient` |
| System prompt constant | `_SYSTEM_<VERB>` | `_SYSTEM_REVIEW` |
| User prompt constant | `_USER_<VERB>` | `_USER_REVIEW` |
| Agent prompt constant | `_AGENT_PROMPT` (single) or `_AGENT_<VERB>` | `_AGENT_PROMPT` |
| Config helper method | `_cfg(key, default)` | inside agent classes |
| Module logger | `log` | `log = logging.getLogger(__name__)` |
| Private constants | `_SCREAMING_SNAKE` | `_MAX_DIFF_CHARS` |
| Private methods | `_verb_noun` | `_gather_guidelines`, `_build_tool` |
| Agent `name` attribute | `snake_case` | `name = "security_reviewer"` |

---

## Testing Conventions

Framework: **pytest**. Test files live under `tests/` (the only allowed test write path).

### Structure

Default unit-test structure is one test file per source module, flat under
`tests/`: `agents/analyst_agent.py` -> `tests/test_analyst_agent.py`.
Use subdirectories only for established suites such as `tests/e2e/` or when a
task explicitly introduces a new suite boundary. E2E tests live in
`tests/e2e/` and must use fake LLM clients, temporary repositories, and
isolated state; they must not require provider credentials, network access, git
remotes, or machine-specific absolute paths.

### Fixtures

Define fixtures in `tests/conftest.py` for shared setup (fake `TaskState`, stub `LLMClient`,
minimal `project_config`). Use `pytest.fixture` — not class-level `setUp`.

```python
@pytest.fixture
def state() -> TaskState:
    return TaskState(task_id="abc123", task_description="test task")

@pytest.fixture
def project_config() -> dict:
    return {"sandbox": {"allowed_write_paths": ["agents/"], "allowed_read_paths": ["."]}}
```

### Naming

Test functions: `test_<what>_<condition>` or `test_<what>_when_<condition>`:

```python
def test_run_returns_failure_when_no_implementation_prompt(state):
    ...

def test_run_sets_review_approved_on_approval(state):
    ...
```

### Agent tests

Use a stub `LLMClient` (do not call real LLMs in tests). Verify:
- `AgentResult.success` value
- Mutations on `TaskState` (`state.review_approved`, `state.history`, etc.)
- `state.record()` entries appended to `state.history`

### Coverage

Target: ≥ 90% branch and line coverage on new and changed code (configured via
`test_writer.coverage_target` in project YAML). Every technical/tool/provider failure path
that returns `AgentResult(success=False, ...)` must have a dedicated test. Valid review
outcomes that return `success=False` with structured issues must also be covered as normal
review-loop behavior, not treated as agent invocation failures.

---

## Code Quality

All code must pass `ruff check .` (lint) and `ruff format --check .` (formatting). These run
as CI checks on every task. Key rules enforced by ruff:

- No unused imports — remove them explicitly after every refactor.
- No bare `except:` — always name the exception type.
- `from __future__ import annotations` must appear immediately after the optional module
  docstring and before all other imports.

Run locally before committing:

```
python3 -m ruff check .
python3 -m ruff format .
```

---

## Rules for AI Agents

- **Never** modify files outside the active write scope given in the agent prompt.
- **Never** put platform-specific logic (Gradle tasks, Kotlin patterns, Android layer names) in agents, orchestrator, or LLM client code — it belongs only in `BuildTool` subclasses and `.sikula/config.yaml`.
- **Never** set `state.done` or `state.failed` in an agent — these are orchestrator-only fields.
- **Never** clear or modify past entries in `state.history` — it is a permanent audit log.
- **Never** use `state.implement_cycle_records`, `state.fix_cycle_records`, `state.review_cycle_records`, `state.security_review_cycle_records`, `state.test_write_records`, or `state.validation_artifact_records` to drive pipeline control flow — they are observability records only.
- **Never** expose raw `TaskState`, prompts, provider output, logs, source snippets, or terminal text through public JSON/status/result output. Add an explicit projection instead.
- **Never** trust a write-capable provider's reported file list as delivery-scope
  enforcement. Stabilize provider-owned project setup before the baseline, rejecting
  collisions with tracked provider configuration instead of overwriting or baselining
  them. Then audit actual full-worktree changes after every physical Implementer and
  Fixer provider attempt, including provider-internal retries and multiple calls inside
  one agent run. Bind each audit to the pre-attempt commit, Git directory, common Git
  directory, worktree root, and active Git-reference authority. Reject later Git discovery
  that retargets those locations and fail closed on any provider or deterministic-tool
  mutation of `HEAD`, its active ref/reflog, packed refs, or reftable metadata; Sikula alone
  owns commits and ref movement. The binding must detect a commit followed by a reset back
  to the captured baseline without trusting reflogs or final ancestry. Do not trust replace
  refs or a provider-mutated index. Bind the effective Git ignore authority before
  each mutation, neutralize repository `core.excludesFile` overrides, and fail closed if
  `info/exclude` or an effective `.gitignore` changes before candidate enumeration completes.
  Use Git-visible tracked, staged, and ordinary
  untracked paths as sparse candidates, traverse ignored paths unless the active `BuildTool`
  owns them as disposable dependency/build output, and never let that platform
  classification hide a Git-visible candidate. A failed attempt must finish its
  audit before another provider call is allowed. For a nested `project.root_path`, classify paths below it relative to
  the project and treat every sibling worktree change as terminal. A production path
  outside the effective unit scope is an evidence-preserving `unit_scope_violation`.
  Preserve bounded actual project-relative paths from successful audits for later
  amendment evidence, but retain outside-project paths separately so they cannot be
  mistaken for authorizable project scope. Persist a dedicated audit-pending control
  field before the baseline and provider call, retain it across every interruption, and
  clear it only after the audit result is saved; `active_operation` must remain
  visibility-only regardless of heartbeat configuration. Stream candidate hashes and retain
  full content only for bounded, non-binary mixed source/test candidates selected by the
  active platform. Preserve native filesystem path semantics during classification,
  including platform `is_ephemeral_build_path()` hooks: a POSIX backslash is a filename
  character, not a separator. Treat Windows reparse points
  as link-like entries. Revalidate each captured lexical-to-resolved root binding before
  every pre-mutation snapshot, including bindings reached through a link ancestor. Validate every
  symlink or link-like entry at or below an active write root before and after the provider
  call, and fail closed when its resolved target escapes that
  invocation's production and explicitly active test-write roots. Construct each Fixer
  provider-attempt policy from the separate production and test roots passed to that exact
  `_run_once`, persist that narrower policy for interruption recovery, and restore the
  whole-agent fallback only after its audit completes. A Fixer path is test-exempt only
  when the current invocation actually received that test-write root;
  apply the same effective production boundary to deterministic phases whose output may
  be retained in the delivery worktree (`presync`, dependency `sync`, and configured
  check `fix_command`), auditing before adoption, cleanup, revalidation, or another phase;
  persist those roots in the interruption marker so resume cannot recompute broader
  authority.
- **Never** treat a persisted delivery scope or terminal boundary stop as
  retryable authority. Current config may narrow a saved scope but cannot broaden
  it; an exact-file root cannot become a directory prefix after dependency
  assembly. The winning terminal failure code selects recovery, and
  `--reset-failed` must not bypass scope, amendment, or external-dependency stops.
- **Never** use real provider-backed nested `sikula run` or `sikula review --fix` delivery flows as tests or validation commands unless a task explicitly targets that behavior and isolates all state and providers.
- **Always** add `from __future__ import annotations` immediately after the optional
  module docstring and before all other imports.
- **Always** add type hints to every function signature.
- **Always** call `state.record(self.name, action, summary)` after every meaningful agent action.
- **Always** append one record to the appropriate `*_cycle_records` / `*_write_records` list per LLM invocation, including on exception. Non-Analyst records set `output` to `None`; Analyst records remain content-free as described above.
- **Always** store `state.analyst_prompt` before the LLM call in `AnalystAgent` — not after.
- **Always** set `ToolResult.error` to combined stdout+stderr (`output[-4000:]`), not stderr alone.
- **Always** return `AgentResult(success=False, message=...)` on failure — never raise from `run()`.
- **Always** write all code comments in English.
- **Always** run `ruff check .` after any code change; fix all reported issues before finishing.
- **Always** update `ARCHITECTURE.md`, `guidelines.md`, `AGENTS.md`, `.sikula/config.yaml`, or public docs when a change alters workflow behavior, state semantics, CLI behavior, provider setup, or task/contract expectations.
- **Always** follow the naming conventions table above for new agents, tools, constants, and methods.
- **Always** ensure LLM output parsers degrade safely and do not crash the orchestrator on hallucinated formatting.
