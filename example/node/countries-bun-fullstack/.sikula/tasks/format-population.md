# Format Population as Compact String

## Background

The countries list shows population formatted with comma separators
(for example `1,400,000,000`). Long numbers are hard to scan in a compact list.

## Requirements

- Replace country list population formatting with compact suffix notation:
  - >= 1,000,000,000 -> one decimal place + "B" suffix (for example 1,400,000,000 -> "1.4B")
  - >= 1,000,000 -> one decimal place + "M" suffix (for example 83,200,000 -> "83.2M")
  - >= 1,000 -> one decimal place + "K" suffix (for example 450,000 -> "450.0K")
  - < 1,000 -> raw number as string (for example 512 -> "512")
- Strip trailing ".0" from compact output (for example "83.0M" -> "83M", "1.0B" -> "1B").
- Keep full comma-separated population formatting wherever country detail is already available:
  - in backend country detail data
  - in an existing country detail screen, if the app already has one
- If the app does not have a country detail screen yet, do not create one for this task.
- Preserving backend country detail data is enough in that case.
- Add or update tests for all threshold and trailing-zero cases.

## Out of scope

- Formatting area or any other numeric field
- Changing the raw `population` field
- Sorting or filtering by population
