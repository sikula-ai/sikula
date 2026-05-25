# Country Detail View

## Background

The app shows a list of countries. Selecting a country currently does nothing.

## Requirements

- Selecting a country opens a detail view with its flag emoji, name, capital, region, population,
  and area.
- The user can navigate back to the list using browser back navigation and a visible in-app back
  control in the detail view header.
- The in-app back control should behave like browser back for this view: when a detail view was
  opened from the list, activating the control returns to the previous list history entry instead
  of only replacing component state.
- Follow common accessible web UI conventions.
- Use the country data already present in the local dataset.

## Layout

- Top of the detail view: flag emoji centred, country name centred below it.
- Properties listed below: label on the left, value on the right (capital, region, population, area).
- Use spacing and typography appropriate for a detail view — the content should not feel cramped.

## UI specs

- The detail view title shows the country's name.
- The in-app back control uses the `detail_back` string.
- Each list item should feel like a standard navigation row that opens another view.
- If the selected country cannot be found, show an inline error message.

## Strings

- detail_back: "Back"
- detail_capital: "Capital"
- detail_region: "Region"
- detail_population: "Population"
- detail_area: "Area"
- detail_not_found: "Country not found"

## Out of scope

- Fetching country details from a remote API
- Routing libraries
- Sharing or bookmarking
- Flag images — use the flag emoji already available in the data
