# Add Name Search to Countries List

## Background

The `GET /countries` endpoint can list all countries and filter them by region.
There is no way to search the list by country name.

## Requirements

- Add an optional `name` query parameter to `GET /countries`.
- The `name` filter matches countries whose name contains the provided text, case-insensitively.
- The existing `region` filter continues to work exactly as before.
- The `name` and `region` filters can be combined; both filters must apply to the result.
- When `name` is blank or absent the endpoint behaves as before.
- Return an empty list, not `404`, when the filters match no countries.

## Expected behaviour

```
GET /countries?name=many       → [{"code": "DEU", "name": "Germany", ...}]
GET /countries?name=LAND       → countries with "land" in the name
GET /countries?region=Europe&name=fr → France
GET /countries?name=zzz        → []
```

## Out of scope

- Sorting or pagination
- Searching fields other than country name
- Changing the response shape of `GET /countries`
