# Countries

A demonstration Spring Boot REST API built with Kotlin, used as an example project for [Sikula](https://github.com/sikula-ai/sikula).

## Stack

| Library | Purpose |
|---|---|
| Spring Boot 3.3 | Application framework |
| Kotlin 1.9 | Language |
| Java 21 | JVM toolchain |
| Spring MVC | REST controllers |
| Jackson | JSON serialization |
| Mockito Kotlin 5 | Unit test mocking |

## Usage

```bash
# Run the application (starts on port 8080)
./gradlew bootRun

# Compile
./gradlew classes

# Run unit tests
./gradlew test
```

## API

```
GET /countries              — list all countries (optional: ?region=Europe)
GET /countries/{code}       — get a single country by ISO 3166-1 alpha-3 code
```

Example:

```bash
curl http://localhost:8080/countries
curl http://localhost:8080/countries?region=Europe
curl http://localhost:8080/countries/DEU
```

## Architecture

```
src/main/kotlin/com/example/countries/
  CountriesApplication.kt   — Spring Boot entry point
  Country.kt                — domain data class (name, capital, region, population, area, code)
  CountryRepository.kt      — loads data/countries.json from classpath; in-memory store
  CountryService.kt         — filtering logic (by region)
  CountryController.kt      — REST endpoints
src/test/kotlin/com/example/countries/
  CountryServiceTest.kt     — unit tests (Mockito mocks)
  CountryControllerTest.kt  — @WebMvcTest slice tests
src/main/resources/
  data/countries.json       — static dataset sourced from REST Countries (MPL 2.0)
```

## Running tests

```bash
./gradlew test
```

## Sikula tasks

Ready-to-run tasks in `.sikula/tasks/`:

| Task file | Description |
|---|---|
| `add-search-by-name.md` | Add name search to the list endpoint |
| `add-sorting.md` | Add sort and order query parameters |
| `add-population-stats.md` | Add a population statistics endpoint |

Run from this directory: `sikula run .sikula/tasks/<task-file>`
