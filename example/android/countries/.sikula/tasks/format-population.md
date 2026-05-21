# Format Population in Countries List

## Background

The countries list shows each country's region and capital in the row subtitle.
The `Country` model already contains a `population` field, but it is not
displayed anywhere in the UI.

## Requirements

- Add a `formattedPopulation` property to `Country` that formats the
  raw population number into a compact, human-readable string:
  - ≥ 1 000 000 000 → one decimal place + "B" suffix (e.g. 1 400 000 000 → "1.4B")
  - ≥ 1 000 000 → one decimal place + "M" suffix (e.g. 83 200 000 → "83.2M")
  - ≥ 1 000 → one decimal place + "K" suffix (e.g. 450 000 → "450.0K")
  - < 1 000 → raw number as string (e.g. 512 → "512")
- Trailing ".0" must be stripped from the output (e.g. "83.0M" → "83M", "1.0B" → "1B").
- Display the formatted population in the subtitle of each row in the countries list,
  appended after the existing region · capital text.

## Out of scope

- Population on any screen other than the countries list
- Sorting or filtering by population
- Any change to the `Country` model or the network layer
