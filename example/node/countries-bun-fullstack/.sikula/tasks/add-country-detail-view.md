# Country Detail View

## Background

The browser UI shows a list of countries. Selecting a country currently does nothing.
The backend already exposes `GET /api/countries/:code`.

## Requirements

- Selecting a country opens a detail view in the browser UI.
- Load detail data from `GET /api/countries/:code`; do not read the local data file from client code.
- The detail view shows name, alpha-3 code, capital, region, population, and area.
- The user can navigate back to the list using browser back navigation and a visible in-app back
  control in the detail view header.
- The in-app back control should behave like browser back for this view when possible.
- If the selected country cannot be found, show an inline error message.
- Keep existing list filtering behaviour unchanged, including region filtering and any already
  implemented name search.
- Treat the current checked-out project state as the source of truth. Preserve existing UI/API
  behaviour unless this task explicitly changes it.

## UI specs

- Preserve the existing responsive country grid on desktop; do not collapse the country list
  into a single full-width column unless the viewport naturally reaches the mobile layout.
- Each country item should remain a compact card-like grid item, but the entire item must
  become a real accessible navigation control.
- Preserve the country card content structure: population and area stay as separate
  label/value facts, not flattened into one inline sentence that wraps unpredictably.
- If wrapping each country card in a button or link, keep the existing inner card hierarchy
  and adapt styles for the interactive wrapper instead of replacing the card content layout.
- Keep the existing visual density of the list: country cards should still use the available
  horizontal space on wider screens.
- Use accessible buttons or links for country items; do not make non-interactive elements clickable.
- Detail rows use a label on the left and value on the right.

## Strings

- detail_back: "Back"
- detail_capital: "Capital"
- detail_region: "Region"
- detail_population: "Population"
- detail_area: "Area"
- detail_not_found: "Country not found"

## Out of scope

- Routing libraries
- Remote data fetching beyond the existing local API
- Sharing or bookmarking
- Adding dependencies
