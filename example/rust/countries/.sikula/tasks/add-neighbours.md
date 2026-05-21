# Show Neighbouring Countries in Info

## Background

The `info` command displays detailed information about a single country. The dataset
already contains a `borders` field for each country — a list of alpha-3 codes of
neighbouring countries — but it is not displayed anywhere. The borders lists are limited
to countries present in the dataset and are not geographically complete.

## Requirements

- Display a "Neighbours:" row in the `info` command output, after the existing "Currencies:" row.
- Resolve each border code to the country's common name. Display names as a comma-separated
  list, sorted alphabetically.
- Countries with no neighbours (e.g. island nations) display `"—"` in the Neighbours row.
- Border codes that do not resolve to a country in the dataset are silently skipped.

## Out of scope

- Showing neighbours in `list`, `search`, or `stats` commands
- Recursive or multi-hop neighbour lookup
- Any change to `data/countries.json`
