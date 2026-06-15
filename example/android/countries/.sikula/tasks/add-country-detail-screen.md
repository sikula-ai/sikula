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
  The expected successful response is a single JSON object, not a list or array.
  The response fields map to the existing DTO fields already present in the project.

## Data availability

- Keep the app remote-first: use REST Countries when the request succeeds.
- The base app already has a local DTO-shaped fallback dataset for the countries list when REST
  Countries is unavailable, returns an error/deprecation payload, or cannot decode.
- Reuse the existing fallback dataset and DTO-to-domain mapper for the new detail lookup by `cca2`.
  Do not create a second copy of fallback country values for the detail screen.
- This is not persistent caching or offline support: do not add local storage, cache invalidation,
  background sync, or new settings.

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

## Existing behavior

- The countries list remains the entry point to the app.
- Existing list loading, row content, error, and loading states continue to work.
- Country data shown in the list and detail screen stays consistent.

## Reviewer focus

- Remote-first country loading with request-failure fallback.
- Shared fallback dataset and DTO-to-domain mapping across list and detail flows.
- Existing countries list loading, row content, error, and loading states after navigation is added.

## Out of scope

- Persistent caching or offline support beyond the request-failure fallback described above
- Sharing or bookmarking
- Flag image — use the flag emoji already available in the app
