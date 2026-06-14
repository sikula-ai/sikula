# Writing Sikula Tasks

Sikula is contract-first. A task description should become an implementation
contract: a two-way handshake between you and Sikula where you bring the intent,
Sikula checks whether it is clear and deliverable, asks for missing context when
needed, and the result becomes scope, acceptance criteria, risks, tests, and
validation.

Sikula does not require a strict task template. The task should be clear enough
that the analyst can produce reviewable implementation instructions without
guessing.

Runnable examples live in each example project's `.sikula/tasks/` directory.
Those files are real tasks, not mandatory templates. Use this guide when you are
unsure how much detail to include.

## Check Readiness

Before running a task, you can ask Sikula to inspect whether the task file is
specific enough to act as an implementation contract:

```bash
sikula contract check .sikula/tasks/my-task.md
sikula contract check .sikula/tasks/my-task.md --json
sikula contract check .sikula/tasks/my-task.md --write-report
sikula contract improve .sikula/tasks/my-task.md \
  --answers .sikula/contracts/my-task.answers.yaml \
  --output .sikula/tasks/my-task.v2.md
```

The check is read-only unless `--write-report` is passed. It does not edit the
task, create a branch, or start the agent pipeline. The output highlights
missing scope boundaries, acceptance criteria, security/privacy notes,
validation coverage, and follow-up questions with stable IDs. `--write-report`
creates `.sikula/contracts/*.check.json` and `.answers.yaml` artifacts for
review or follow-up answers. Filled answers apply only to the exact task hash in
the template. After you fill the YAML answers, `sikula contract improve`
deterministically writes a stronger Markdown task file, keeps unanswered items
under `Open questions`, and runs the contract check on the output. It refuses
stale answer hashes and accidental overwrites; for plain-text `.txt` inputs,
write the improved contract to a new Markdown file with `--output`. After the
task file changes, old filled answers are moved to `previous_answers` and the
active answers are reset for the new hash. If the project has a configured
Sikula build/test/check pipeline, the task does not need to repeat those
commands unless it requires additional project-specific validation. The
readiness score is a preflight signal, not a guarantee that the task will
succeed.
Normal `sikula run TASK_FILE` records the same check as a compact, warning-only
state snapshot and prints a one-line summary before agents start; it does not
write `.sikula/contracts` artifacts.

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
