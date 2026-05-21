# Development Guidelines

This document defines architectural rules and coding conventions for the Countries REST API.
It is loaded as context by AI agents — follow every rule precisely.

---

## Architecture

Three-layer architecture with a strict, unidirectional dependency rule.

### Layers

Dependency direction: **Controller → Service → Repository**. Each layer has one responsibility.

| Layer | Class | Responsibilities |
|-------|-------|-----------------|
| Controller | `CountryController` | HTTP request handling — parse params, call service, return responses |
| Service | `CountryService` | Business logic — filtering, validation, transformation. No HTTP, no JSON |
| Repository | `CountryRepository` | Data access — loads and caches country data from the classpath resource |

### Rules

- Controllers must not access `CountryRepository` directly — always go through the service.
- Services must not import Spring Web types (`ResponseEntity`, `@RequestMapping`, etc.).
- Repositories must not contain business logic — only data loading and simple lookups.
- No business logic in data classes.

---

## Data Model

`Country` is a plain data class — no annotations other than Jackson defaults.

```kotlin
data class Country(
    val code: String,       // ISO 3166-1 alpha-3 code, e.g. "DEU"
    val name: String,
    val capital: String,
    val region: String,     // "Europe", "Americas", "Asia", "Africa", "Oceania"
    val population: Long,
    val area: Double,       // km²
)
```

- Country codes are case-insensitive in lookups but stored uppercase.
- The data set lives in `src/main/resources/data/countries.json` and is loaded once at startup.

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/countries` | List all countries. Optional `?region=` filter |
| GET | `/countries/{code}` | Get a single country by ISO code. Returns 404 if not found |

### Response conventions

- Return `List<Country>` directly from list endpoints — no wrapper envelope.
- Return `ResponseEntity<Country>` from single-item endpoints to allow `404 Not Found`.
- Return an empty list (not 404) when a region filter matches nothing.
- Never return `null` from a list endpoint.

---

## Testing

### Unit tests (`CountryServiceTest`)

- Test the service in isolation using Mockito mocks for the repository.
- One test class per production class.
- Use descriptive backtick test names: `` `listAll returns all countries when no region filter` ``.
- Cover both the happy path and null/empty cases for every public method.

### Controller tests (`CountryControllerTest`)

- Use `@WebMvcTest(CountryController::class)` — loads only the web layer.
- Mock the service with `@MockBean`.
- Use `MockMvc.get(...)` with the DSL-style `andExpect {}` block.
- Assert HTTP status and at least one JSON field per test.

### No `@SpringBootTest`

- Full-context integration tests are out of scope for this example.
- Use `@WebMvcTest` for the controller and plain unit tests for the service.

---

## Code Style

- Prefer expression bodies for single-expression functions.
- Use named arguments when a function has more than two parameters of the same type.
- No mutable state in service or repository classes — all operations are pure lookups.
- Keep controller methods thin: one line calling the service, one line returning the response.
- Do not log inside service or repository methods — leave that to a future cross-cutting concern.

---

## File Structure

```
src/
  main/
    kotlin/com/example/countries/
      CountriesApplication.kt    — @SpringBootApplication entry point
      Country.kt                 — data class Country(...)
      CountryRepository.kt       — loads and caches data/countries.json
      CountryService.kt          — listAll(region?), findByCode(code)
      CountryController.kt       — GET /countries, GET /countries/{code}
    resources/
      application.properties
      data/countries.json        — static dataset, 15 countries
  test/
    kotlin/com/example/countries/
      CountryServiceTest.kt      — unit tests with Mockito mocks
      CountryControllerTest.kt   — @WebMvcTest with MockMvc
```
