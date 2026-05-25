# Add Name Search to Countries List

## Background

The countries page can list all countries and filter them by region.
There is no way to search the list by country name.

## Requirements

- Add a text search control for country name.
- The name filter matches countries whose name contains the provided text, case-insensitively.
- The existing region filter continues to work exactly as before.
- The name and region filters can be combined; both filters must apply to the result.
- When the name search is blank the page behaves as before.
- Show an empty state, not an error, when the filters match no countries.

## Expected behaviour

```
name = "many"       → Germany
name = "LAND"       → countries with "land" in the name
region = Europe
name = "fr"         → France
name = "zzz"        → no matching countries
```

## Out of scope

- Sorting or pagination
- Searching fields other than country name
- Changing the country data shape

