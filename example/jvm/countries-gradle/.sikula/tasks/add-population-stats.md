# Add Population Statistics Endpoint

## Background

The API exposes individual countries and a list endpoint. There is no endpoint for
aggregate population statistics across the dataset.

## Requirements

- Add a new `GET /countries/stats` endpoint.
- Without filters, the endpoint returns aggregate statistics across all countries.
- With `?region=Europe`, the endpoint returns statistics for European countries only.
- Return `404 Not Found` when the region filter matches no countries.
- The existing `GET /countries` and `GET /countries/{code}` endpoints must keep their current behaviour.

## Response shape

```json
{
  "count": 15,
  "totalPopulation": 4567890123,
  "averagePopulation": 304526008,
  "mostPopulous": {"code": "CHN", "name": "China", ...},
  "leastPopulous": {"code": "AUS", "name": "Australia", ...},
  "largestByArea": {"code": "CAN", "name": "Canada", ...}
}
```

## Edge cases

- For one matching country, that country is both `mostPopulous`, `leastPopulous`, and `largestByArea`.
- `averagePopulation` is the average population across the matching countries, rounded down to a whole number.

## Out of scope

- New filters other than `region`
- Persisting or caching computed statistics
- Changing the response shape of existing endpoints
