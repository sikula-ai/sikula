# Countries

A demonstration iOS app built with idiomatic Swift, used as an example project for [Sikula](https://github.com/sikula-ai/sikula).

## Stack

| Technology | Purpose |
|---|---|
| SwiftUI (iOS 17+) | UI |
| `@Observable` | State management |
| URLSession + async/await | HTTP client |
| XCTest | Unit tests |

No external dependencies — pure Apple frameworks only.

## Data

Data is loaded from [REST Countries](https://restcountries.com) (MPL 2.0) when available.
The app includes a small local fallback dataset with the same DTO shape so the example remains
usable when the public API is unavailable.

## Usage

```bash
# Build for simulator
xcodebuild build -project Countries.xcodeproj -scheme Countries \
    -destination 'generic/platform=iOS Simulator' -configuration Debug

# Run unit tests
xcodebuild test -project Countries.xcodeproj -scheme Countries \
    -destination 'platform=iOS Simulator,OS=latest,name=iPhone 16' -configuration Debug
```

## Architecture

Single-module project with clean four-layer architecture.

```
Countries/
  App/
    CountriesApp.swift            — @main entry point, dependency wiring
  Domain/
    Country.swift                 — domain model
    CountriesRepository.swift     — repository protocol
    FetchCountriesUseCase.swift   — use case
  Data/
    CountryDTO.swift              — Decodable DTOs
    CountriesAPIClient.swift      — URLSession client
    CountriesRepositoryImpl.swift — CountriesRepository implementation
  Presentation/
    CountriesViewModel.swift      — @Observable ViewModel
  UI/
    CountriesListView.swift       — countries list screen
    CountryRowView.swift          — single row in list

CountriesTests/
  CountriesTests.swift            — unit tests
```

Dependency direction: `UI` → `Presentation` → `Domain` ← `Data`.
The domain layer has no knowledge of URLSession, SwiftUI, or any framework.

## Sikula tasks

Ready-to-run tasks in `.sikula/tasks/`:

| Task file | Description |
|---|---|
| `add-country-detail-screen.md` | Country Detail Screen |
| `add-pull-to-refresh.md` | Pull-to-Refresh on Countries List |
| `format-population.md` | Format Population in Countries List |

Run from this directory: `sikula run .sikula/tasks/<task-file>`
