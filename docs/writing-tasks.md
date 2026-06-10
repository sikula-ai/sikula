# Writing Sikula Tasks

Sikula does not require a strict task template. A task description should be a
clear, human-readable request that gives the analyst enough context to produce
reviewable implementation instructions without guessing.

Runnable examples live in each example project's `.sikula/tasks/` directory.
Those files are real tasks, not mandatory templates. Use this guide when you are
unsure how much detail to include.

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
