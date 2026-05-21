# Format Population as Compact String

## Background

The `list` and `search` commands display population as a raw number with comma separators
(e.g. `1,400,000,000`). Long numbers are hard to scan in a table. A compact suffix notation
(e.g. `1.4B`) is more readable and fits better in fixed-width columns.

## Requirements

- Replace the population display in the `list` and `search` commands with a compact format:
  - ≥ 1 000 000 000 → one decimal place + "B" suffix (e.g. 1 400 000 000 → "1.4B")
  - ≥ 1 000 000 → one decimal place + "M" suffix (e.g. 83 200 000 → "83.2M")
  - ≥ 1 000 → one decimal place + "K" suffix (e.g. 450 000 → "450.0K")
  - < 1 000 → raw number as string (e.g. 512 → "512")
- Trailing ".0" must be stripped (e.g. "83.0M" → "83M", "1.0B" → "1B").

## Out of scope

- Changing the `info` command output — it uses comma thousands separators and must stay unchanged
- Formatting area or any other numeric field
- Any change to `Country` struct fields or `data/countries.json`
