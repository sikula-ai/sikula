# Add Name Search across API and UI

## Background

The app can list all countries and filter them by region.
There is no way to search the list by country name from either the API or the UI.

## Requirements

- Add an optional `name` query parameter to `GET /api/countries`.
- The `name` filter matches countries whose name contains the provided text, case-insensitively.
- The existing `region` filter continues to work exactly as before.
- The `name` and `region` filters can be combined; both filters must apply to the result.
- Add a labelled search input to the browser UI.
- When the name search is blank the API and UI behave as before.
- Show an empty state, not an error, when the filters match no countries.
- Preserve any already implemented country detail view, card navigation, back behaviour, and
  detail formatting.
- Treat the current checked-out project state as the source of truth. Preserve existing UI/API
  behaviour unless this task explicitly changes it.

## Expected behaviour

```text
GET /api/countries?name=many              -> Germany
GET /api/countries?name=LAND              -> countries with "land" in the name
GET /api/countries?region=Europe&name=fr  -> France
GET /api/countries?name=zzz               -> []
```

## Out of scope

- Searching fields other than country name
- Sorting or pagination
- Changing the `Country` response shape
