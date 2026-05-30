# Country Detail Screen

## Background

The app shows a list of countries. Tapping a country currently does nothing.

## Requirements

- Tapping a country opens a detail screen with its flag emoji, name, capital, region, and population
  (formatted with thousand separators).
- The user can navigate back to the list using both the platform's system back gesture/button
  and a familiar back control in the detail screen header.
- Follow the platform's native design guidelines.
- Fetch country details from `GET /v3.1/alpha/{code}?fields=name,cca2,capital,region,population`.
  Returns a single JSON object — verified against the live API.
  The response fields map to the existing DTO fields already present in the project.

## Layout

- Top of the screen: flag emoji centred, country name centred below it.
- Properties listed below: label on the left, value on the right (capital, region, population).
- Use generous spacing and typography appropriate for a detail screen — the content should not feel cramped.

## UI specs

- The screen title shows the country's common name.
- The selected country identity is visible immediately after navigation. Use information already
  available from the tapped list item for the initial screen title/name instead of waiting for
  the detail request to complete.
- Detail content may show a loading state while fresh detail data is fetched, but the title/header
  must not be blank or visibly jump after the first render.
- Each list item should feel like a standard navigation row that opens another screen.
- On error, show an inline error message (see Strings section below).

## Strings

- country_detail_capital: "Capital"
- country_detail_region: "Region"
- country_detail_population: "Population"
- country_detail_error: "Failed to load country"

## Out of scope

- Caching or offline support
- Sharing or bookmarking
- Flag image — use the flag emoji already available in the app
