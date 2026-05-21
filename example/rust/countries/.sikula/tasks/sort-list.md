# Sort Countries List

## Background

The `list` command outputs countries in the order they appear in `data/countries.json`.
There is no way for the user to control the order. A `--sort` flag would make the output
more useful for exploration and comparison.

## Requirements

- Add a `--sort` flag to the `list` command with three accepted values: `name`, `population`, `area`.
- Default sort is `name`.
- Example usage: `countries list --sort population`
- Sorting rules:
  - `name` — ascending alphabetical by common name
  - `population` — descending (most populous first)
  - `area` — descending (largest first)
- The `--sort` and `--region` flags are independent and composable:
  `countries list --region Europe --sort population` must work correctly.

## Out of scope

- Sorting in the `search` command
- Reverse-sort flag
- Any change to `Country` struct fields or `data/countries.json`
