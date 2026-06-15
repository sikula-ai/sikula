# Countries

A demonstration Android app built with idiomatic Kotlin, used as an example project for [Sikula](https://github.com/sikula-ai/sikula).

## Stack

| Library | Purpose |
|---|---|
| Jetpack Compose (Material 3) | UI |
| Koin | Dependency injection |
| Retrofit 2 + OkHttp | HTTP client |
| Moshi | JSON deserialization |
| Coroutines | Async/reactive layer |
| Detekt | Static analysis |

## Usage

```bash
# Build debug APK
./gradlew assembleDebug

# Run unit tests
./gradlew testDebugUnitTest

# Run lint
./gradlew lintDebug

# Run detekt
./gradlew detekt
```

## Architecture

Multi-module project with clean architecture inside the `feature/countries` module.

```
app/                          — application module; DI wiring, navigation host
feature/
  countries/
    data/
      CountriesApi.kt         — Retrofit interface
      CountryDto.kt           — JSON deserialization model
      CountriesRepositoryImpl.kt
    domain/
      model/Country.kt        — domain model
      CountriesRepository.kt  — repository interface
      FetchCountriesUseCase.kt
    di/CountriesModule.kt     — Koin module
    navigation/               — nav graph and routes
    presentation/
      CountriesViewModel.kt
    system/
      CountriesScreen.kt      — Compose screen
library/
  network/                    — shared Retrofit/OkHttp setup
  presentation/               — shared ViewModel base
  ui/                         — shared Compose components
  testing/                    — shared test utilities
```

## Data

Data is loaded from [REST Countries](https://restcountries.com) (MPL 2.0) when available.
The app includes a small local fallback dataset with the same DTO shape so the example remains
usable when the public API is unavailable.

Dependency direction: `system` → `presentation` → `domain` ← `data`.
The domain layer has no knowledge of Retrofit, Moshi, or Compose.

## Sikula tasks

Ready-to-run tasks in `.sikula/tasks/`:

| Task file | Description |
|---|---|
| `add-country-detail-screen.md` | Country Detail Screen |
| `add-pull-to-refresh.md` | Pull-to-Refresh on Countries List |
| `format-population.md` | Format Population in Countries List |

Run from this directory: `sikula run .sikula/tasks/<task-file>`
