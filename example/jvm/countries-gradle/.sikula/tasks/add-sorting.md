# Add Sorting to Countries List

## Background

The `GET /countries` endpoint returns countries in the order they appear in the
dataset. Clients cannot choose the order of the list.

## Requirements

- Add optional `sort` and `order` query parameters to `GET /countries`.
- `sort` accepts `name`, `population`, and `area`. Default is `name`.
- `order` accepts `asc` and `desc`. Default is `asc`.
- Sorting is applied after the existing `region` filter.
- Invalid `sort` or `order` values return `400 Bad Request` with a JSON error message.

## Error response

Invalid `sort` values return:

```json
{
  "message": "Invalid sort parameter '<value>'. Allowed values: name, population, area."
}
```

Invalid `order` values return:

```json
{
  "message": "Invalid order parameter '<value>'. Allowed values: asc, desc."
}
```

## Expected behaviour

```
GET /countries?sort=population&order=desc  → sorted by population, largest first
GET /countries?region=Europe&sort=area     → European countries sorted by area ascending
GET /countries?sort=invalid                → 400 Bad Request
```

## Out of scope

- Adding search, pagination, or new filters
- Sorting endpoints other than `GET /countries`
- Changing the response shape of `GET /countries`
