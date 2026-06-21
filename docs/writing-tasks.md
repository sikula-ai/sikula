# Writing Sikula Tasks

Sikula is contract-first. A product task description says what should change for
users or the business. Before agents change code, that description should become
an implementation contract: the delivery artifact Sikula can run, with scope,
acceptance criteria, constraints, risks, tests, and validation.

The public CLI keeps this flow explicit. `sikula contract check` inspects a
task or contract file. `sikula task refine` can turn a rough product request
into a cleaner product task description. `sikula contract prepare` writes the
project-aware Markdown implementation contract from that refined description and
answers. `sikula run` then runs the file you pass to it; it records a
warning-only readiness snapshot, but it does not rewrite a product brief into a
contract during the run.

Sikula does not require a strict task template. The task should be clear enough
that the analyst can produce reviewable implementation instructions without
guessing.

Runnable examples live in each example project's `.sikula/tasks/` directory.
Those files are real tasks, not mandatory templates. Use this guide when you are
unsure how much detail to include.

Generated configs define `tasks.task_description_dir` for product task descriptions,
`tasks.contract_dir` for prepared implementation contracts, and
`tasks.task_asset_dir` for local task assets such as screenshots, icons, or
spec excerpts. `tasks.contract_report_dir` stores check reports, answers YAML,
and sidecar metadata. The task, contract, and task asset directories are meant
to be source-controlled when they contain project inputs; `.sikula/contract-reports/`
is generated working state and is ignored by `sikula init`.

## Check Readiness

Before running a task, you can ask Sikula to inspect whether the task or
contract file is specific enough to act as an implementation contract:

```bash
# Inspect the original product task.
sikula contract check .sikula/tasks/my-task.md
sikula contract check .sikula/tasks/my-task.md --json

# Option A: let a read-only LLM normalize the product task and answer supported
# product-level questions.
sikula task refine .sikula/tasks/my-task.md \
  --auto \
  --output .sikula/tasks/my-task.refined.md

# Option B: answer product-level refinement questions in the terminal.
sikula task refine .sikula/tasks/my-task.md \
  --interactive \
  --output .sikula/tasks/my-task.refined.md

# Option C: generate a product-refinement answers template, edit it, then apply it.
sikula task refine .sikula/tasks/my-task.md
# edit .sikula/contract-reports/my-task.task-refine.answers.yaml
sikula task refine .sikula/tasks/my-task.md \
  --answers .sikula/contract-reports/my-task.task-refine.answers.yaml \
  --output .sikula/tasks/my-task.refined.md

# Optional: inspect the refined task as an implementation-contract candidate
# and write reusable report/answers artifacts.
sikula contract check .sikula/tasks/my-task.refined.md --write-report

# Option A: let a read-only LLM answer only delivery questions supported by the
# task, repository, guidelines, or Sikula config.
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --auto \
  --output .sikula/contracts/my-task.contract.md

# Option B: answer contract-preparation questions in the terminal.
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --interactive \
  --output .sikula/contracts/my-task.contract.md

# Option C: generate a contract-preparation answers template, edit it, then
# apply it.
sikula contract prepare .sikula/tasks/my-task.refined.md
# edit .sikula/contract-reports/my-task.contract-prepare.answers.yaml
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --answers .sikula/contract-reports/my-task.contract-prepare.answers.yaml \
  --output .sikula/contracts/my-task.contract.md

# Or use the answers template produced by contract check --write-report.
# edit .sikula/contract-reports/my-task.refined.answers.yaml
sikula contract prepare .sikula/tasks/my-task.refined.md \
  --answers .sikula/contract-reports/my-task.refined.answers.yaml \
  --output .sikula/contracts/my-task.contract.md
```

The check is read-only unless `--write-report` is passed. It does not edit the
task, create a branch, or start the agent pipeline. The output highlights
missing scope boundaries, acceptance criteria, security/privacy notes,
validation coverage, and follow-up questions with stable IDs. `--write-report`
creates `.sikula/contract-reports/*.check.json` and `.answers.yaml` artifacts
for review or follow-up answers. Filled answers apply only to the exact task
hash in the template.

`sikula task refine` is for the product-level description. It should preserve
the product intent and avoid project-specific implementation details so the
refined task can still be reused across platforms. `sikula task refine --auto`
uses the read-only `task_preparer` LLM agent to normalize a rough or non-English
request into clean English product-task Markdown. When deterministic
product-question handling still finds open product questions, `--auto` may
also propose answers that are directly supported by the task or project
guidelines, then reruns the same deterministic product-question pass. The LLM
does not answer delivery questions and does not write files directly. Its
prompts and raw responses are recorded in
`.sikula/contract-reports/*.auto-llm.jsonl` for local audit before the
normalized task is applied, including provider failures or malformed responses
that cannot be parsed. Task refine only resolves product
task-description questions; `sikula contract prepare` may still ask delivery
questions about privacy, tests, validation, reviewer focus, or other
implementation-contract readiness gaps. If a non-interactive refine run finds
open product questions and no answers were supplied, it writes an answers
template under `.sikula/contract-reports/` and does not write the refined
Markdown output yet. Use `--interactive` to answer immediately, or fill the
answers YAML and rerun with `--answers`. In `--auto` mode, Sikula writes the
normalized refined Markdown first and scopes any generated answers template to
that new refined file.

### Direct Prepare Vs Refine First

`sikula task refine` is optional. Use it when you want a cleaner product task
description that stays reusable across projects or platforms. You can also run
`sikula contract prepare` directly on the original task description:

```bash
sikula contract prepare .sikula/tasks/my-task.md \
  --interactive \
  --output .sikula/contracts/my-task.contract.md
```

In that direct path, Sikula evaluates the input as an implementation contract.
It may therefore ask both product-level questions and delivery-readiness
questions about security/privacy, tests, validation, reviewer focus, or other
implementation risks. If you refine first, product questions are handled in the
refined task description and `contract prepare` can focus on the project-aware
delivery contract.

`sikula contract prepare` is for the delivery artifact. It applies answers,
adds project context and effective validation commands, keeps unanswered items
under `Open questions`, and runs the contract check on the output. It refuses
stale answer hashes and accidental overwrites. The visible Markdown output stays
clean; Sikula stores working reports, answers, and generated-answer metadata
under `.sikula/contract-reports`. If a non-interactive prepare run finds open
questions and no answers were supplied, it writes an answers template under
`.sikula/contract-reports/` and does not write the implementation contract yet.
Use `--interactive` to answer immediately, or fill the answers YAML and rerun
with `--answers`.

`sikula contract prepare --auto` uses a read-only `task_preparer` LLM agent
to propose answers for currently open preparation questions when the answer is
supported by the task description, repository, project guidelines, or Sikula
config. Sikula still applies those answers through the deterministic contract
prepare core and re-runs the readiness check; the LLM does not write the
contract Markdown directly. Its prompt and raw response are recorded in
`.sikula/contract-reports/*.auto-llm.jsonl` before auto answers are applied, and
provider failures or malformed responses are still recorded before the command
fails. Existing answers templates are preserved: auto answers only fill empty
entries, including answers supplied with `--answers`. The command refuses an
existing output path before creating the LLM client. If a default answers
template already contains filled values, pass it explicitly with `--answers` so
Sikula treats those values as authored input. If
product, security, privacy, or validation policy still needs a human answer,
Sikula writes the normal answers YAML with any auto-applied answers prefilled
and does not write the contract yet.

`--interactive` is a convenience mode for terminal use: it creates or reuses the
answers template, prompts for follow-up answers, saves the answers YAML, and
then writes the refined task or prepared contract. Blank interactive answers
remain open questions. If the
project has a configured
Sikula build/test/check pipeline and those phases are enabled for a normal
`sikula run`, the task does not need to repeat those commands unless it requires
additional project-specific validation. Disabled validation phases do not count
as contract readiness coverage. The readiness score is a preflight signal, not a
guarantee that the task will succeed.
Normal `sikula run TASK_FILE` assumes `TASK_FILE` is the delivery task or
implementation contract you want to execute. It records the same check as a
compact, warning-only state snapshot and prints a one-line summary before
agents start; it does not write `.sikula/contract-reports` artifacts or prepare the
file automatically.

If a team wants task readiness to be enforced before agents start, fresh task
runs can opt into strict gates:

```bash
sikula run .sikula/tasks/my-task.md --require-contract-ready
sikula run .sikula/tasks/my-task.md --min-contract-score 80
```

Those gates save the same state snapshot and fail before creating a worktree or
running agents when the threshold is not met. Because no delivery worktree exists
yet, a gate-failed state is for audit only and cannot be resumed with
`--reset-failed`; prepare the implementation contract and start a fresh
task-file run. The
gates do not apply to `resume`, `review`, or `review --fix`. Review mode uses the
existing branch diff as its primary source of truth; any future review-context
readiness check should be a separate review-specific gate, not this delivery task
contract gate.

The examples use Markdown because it is easier to structure and review, but
plain-text `.txt` task files are supported too.

## Principles

- Describe the user-visible goal and expected behaviour.
- Include constraints the codebase cannot infer: API contracts, business rules,
  third-party service behaviour, required copy, and explicit out-of-scope items.
- Keep implementation details out unless they are true requirements.
- Do not ask for tests just to remind Sikula to write tests; the test writer
  handles that when test writing is enabled.
- Make the task self-contained. The analyst cannot fetch Jira, Figma, GitHub, or
  web URLs.
- Mention files, screenshots, specs, or mockups only if they are committed in the
  project and readable from the task worktree.

## Task Assets

Use local project files for screenshots, mockups, icons, specs, fixtures, or
other non-text inputs. The recommended directory is `.sikula/task-assets/` or a
per-task subdirectory under it:

```text
.sikula/task-assets/team-invites/login-spacing-bug.png
.sikula/task-assets/team-invites/success-check.svg
```

Reference assets are read-only context. They can guide the analyst,
implementer, reviewer, and test writer, but they must not be copied into
production assets unless the task also lists them as delivery assets.

Delivery assets are files that should become part of the branch output, such as
icons, images, fonts, or fixtures. Delivery assets need explicit purpose,
source/license/provenance, and optionally a target path or target subsystem when
the product/design request already knows one. If no target is specified, Sikula's
analyst should choose the correct platform location from the project structure
and conventions.

Prefer an explicit section:

```md
## Assets

### Reference assets

- `.sikula/task-assets/login-spacing-bug.png`
  - Use as reference only.
  - Shows the expected spacing on the login form.
  - Do not copy this screenshot into app assets.

### Delivery assets

- `.sikula/task-assets/success-check.svg`
  - Use as the new success state icon.
  - Target: `app/src/main/res/drawable/success_check.svg`
  - Source/license: provided by product team for this project.
```

Put per-asset metadata as nested bullets under that asset item. Same-level
bullets are treated as separate task requirements, not as metadata for the
previous asset.

Sikula treats asset paths deterministically during contract readiness checks:
referenced local files are resolved inside the project boundary, hashed, and
reported with lightweight metadata. Missing files, paths outside the project,
or delivery assets without provenance are readiness blockers. Untracked,
ignored, or dirty/staged assets are warnings because isolated runs may not
receive the same inputs. Sikula does not fetch URLs during delivery runs; put
the file in the project first and use URLs only as provenance or background
context.

When `sikula contract prepare` sees verified reference or delivery assets, it
adds an `Asset manifest` section to the implementation contract with the
resolved project path, hash, asset intent, and any explicit target or
source/license details from the task. Missing, ambiguous, or out-of-project
assets remain follow-up questions instead of being silently added to the
contract. If you answer an `assets.local_files` question, provide supported
local project paths, one per unresolved asset. Free text such as `n/a` does
not resolve a missing asset reference.

## Feature Example

```md
# Add country search

## Goal

Users should be able to filter the countries list by name.

## Desired behaviour

- Add a search field above the countries list.
- Typing into the field filters the list by country name.
- Matching should be case-insensitive.
- Clearing the field shows the full list again.
- Empty and loading states should keep working as they do today.

## Out of scope

- Do not add server-side search.
- Do not change country sorting.
- Do not redesign the list item layout.
```

## Bug Fix Example

```md
# Fix stale countries after refresh failure

## Problem

When refreshing the countries list fails, the screen keeps showing stale data
without making it clear that the refresh failed.

## Expected behaviour

- If refresh fails and existing data is already visible, keep the existing list.
- Show a non-blocking error message so the user knows the refresh failed.
- The user should be able to retry by refreshing again.

## Out of scope

- Do not change the initial loading error screen.
- Do not change the API client.
```

## UI Change Example

```md
# Improve country detail navigation

## Goal

The country detail screen should feel like a normal drill-down page from the
countries list.

## Desired behaviour

- The countries list should clearly show that each row opens a detail screen.
- The detail screen should provide a clear way to return to the list.
- Keep the existing country information and layout hierarchy.

## Out of scope

- Do not add new country fields.
- Do not change networking or persistence.
- Do not change the visual style of unrelated screens.
```

## API Contract Example

If the task requires a new API call, include the response contract:

````md
# Add population stats endpoint

## Desired behaviour

Add an endpoint that returns aggregate population statistics for all countries.

## API contract

`GET /countries/population-stats`

Response: single JSON object

```json
{
  "totalPopulation": 7850000000,
  "averagePopulation": 39250000,
  "countryCount": 200
}
```

Fields:
- `totalPopulation`: integer
- `averagePopulation`: integer
- `countryCount`: integer
````

## Common Mistakes

- Too vague: "Improve the country screen."
- Too implementation-specific: "Create `CountryDetailViewModel` with these exact
  methods" unless those names are required by an existing API or convention.
- External-only context: "Implement the Figma design here: <url>" without adding
  the relevant design file or screenshot to the repo.
- Review context that mentions an attachment without committing it locally. In
  `sikula review`, files named in `--description` or `--description-file` are only
  inlined into the review prompt when Sikula can find them in the review worktree.
- Hidden business rules: "Use the normal eligibility logic" without describing
  what that means or where it already exists in the codebase.
