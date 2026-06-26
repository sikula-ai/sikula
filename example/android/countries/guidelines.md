# Development Guidelines

This document defines architectural rules and coding conventions for the Sikula Android Example project.
It is loaded as context by AI agents — follow every rule precisely.

---

## Architecture

Clean Architecture with MVVM presentation layer.

### Layers

Dependency direction is strict: **presentation → domain → data**. No layer may import from a higher layer.

| Layer | Package | Responsibilities |
|-------|---------|-----------------|
| Presentation | `system/`, `presentation/` | Composable screens, ViewModels, UI state |
| Domain | `domain/` | Use cases, domain models, repository interfaces |
| Data | `data/` | Repository implementations, API interfaces, DTOs |

### Module types

| Module | Purpose |
|--------|---------|
| `:app` | Application entry point — DI wiring, navigation graph, `MainActivity` |
| `:feature:<name>` | Self-contained vertical slice of a user-facing feature |
| `:library:network` | OkHttp + Retrofit setup, shared HTTP client |
| `:library:ui` | `AbstractViewModel`, shared Compose utilities |
| `:library:testing` | `AbstractTest` base class and shared test utilities — `testImplementation` only |

---

## Presentation Layer

### ViewModel

- Extend `AbstractViewModel<S>` from `:library:ui`.
- State is a `data class` nested inside the ViewModel as `State`, implementing `AbstractViewModel.State`.
- All public methods follow `on<Event>()` naming: `onRefresh()`, `onItemClick(id: String)`.
- Use `updateState { copy(...) }` for all state mutations — never replace `_states` directly.
- Use `launch { ... }` for all coroutines in a ViewModel — it delegates to `viewModelScope` and cancels when the ViewModel is cleared.

```kotlin
internal class CountriesViewModel(
    private val fetchCountries: FetchCountriesUseCase,
) : AbstractViewModel<CountriesViewModel.State>(State()) {

    data class State(
        val isLoading: Boolean = false,
        val countries: List<Country> = emptyList(),
        val error: String? = null,
    ) : AbstractViewModel.State

    init {
        onRefresh()
    }

    fun onRefresh() {
        launch {
            updateState { copy(isLoading = true, error = null) }
            fetchCountries()
                .onSuccess { updateState { copy(isLoading = false, countries = it) } }
                .onFailure { updateState { copy(isLoading = false, error = it.message) } }
        }
    }
}
```

### Compose screens

Always use the two-method pattern — split into a public entry point and a private implementation:

```kotlin
@Composable
fun CountriesScreen(onCountryClick: (String) -> Unit) {
    val viewModel: CountriesViewModel = koinViewModel()
    val state by viewModel.states.collectAsState()
    CountriesScreenImpl(
        state = state,
        onRefresh = viewModel::onRefresh,
        onCountryClick = onCountryClick,
    )
}

@Composable
private fun CountriesScreenImpl(
    state: CountriesViewModel.State,
    onRefresh: () -> Unit,
    onCountryClick: (String) -> Unit,
) {
    // Pure UI — no ViewModel, no coroutines, no side effects
}
```

Rules:
- The public function injects the ViewModel via `koinViewModel()` and passes state + lambdas to `Impl`.
- The private `Impl` function takes only `state` and lambdas — never a ViewModel reference.
- Never put business logic inside a Composable.
- Never access `NavController` inside a screen — use navigation lambdas passed from `:app`.

### Material 3 navigation UI

Use standard Material 3 navigation components for screen-to-screen flows:

- A `TopAppBar` back action must use `navigationIcon` with `IconButton` and a Material back
  arrow icon, such as `Icons.AutoMirrored.Filled.ArrowBack`. Do not use a text-only
  `TextButton("Back")` for the primary app bar back action.
- A clickable `ListItem` that opens a detail screen must include `trailingContent` with a
  standard Material navigation/disclosure icon, such as `Icons.AutoMirrored.Filled.KeyboardArrowRight`.
  Do not rely on click handling alone when the row needs to communicate navigation.
- This project includes `material-icons-core`; use it for standard navigation icons instead of
  replacing icons with text.
- Actionable icons must have localized `contentDescription` text from `res/values/strings.xml`.
  If the task does not specify a label for a standard action, add a conventional localized string
  such as `Back`, `Close`, `Search`, or `Retry`, then read it with `stringResource(...)`.
- Decorative icons, such as a trailing disclosure icon in a clickable row, may use
  `contentDescription = null` when the row itself communicates the action.

---

## Domain Layer

### Use cases

- Single public method: `operator fun invoke(...)`.
- Stateless — registered as `factoryOf(::UseCase)` in Koin.
- Return `Result<T>` for fallible operations; never throw from a use case.
- No Android framework dependencies — plain Kotlin only.

```kotlin
internal class FetchCountriesUseCase(
    private val repository: CountriesRepository,
) {
    suspend operator fun invoke(): Result<List<Country>> =
        repository.fetchCountries()
}
```

### Domain models

- Immutable `data class` in `domain/model/`.
- No serialization annotations — those belong in data layer DTOs.
- No Android dependencies.

### Repository interfaces

- Defined in `domain/` alongside the models they return.
- Return `Result<T>` for fallible operations.

```kotlin
internal interface CountriesRepository {
    suspend fun fetchCountries(): Result<List<Country>>
}
```

---

## Data Layer

### Repository implementations

- Suffix: `Impl` (e.g. `CountriesRepositoryImpl`).
- Implement the domain interface.
- Wrap all remote calls in `runCatching { ... }`.
- Map DTOs to domain models before returning.
- The REST Countries API is a public API dependency and can fail or return deprecation/error
  payloads. When a task touches country data fetching, keep the repository remote-first but add or
  reuse a local request-failure fallback in the data layer. The fallback must use the same DTO shape
  as the remote response (`name.common`, `cca2`, `capital`, `region`, `population`) so remote and
  fallback data share the same DTO-to-domain mapper.
- Reuse one fallback dataset for list and detail lookups. Do not duplicate fallback country values
  across separate list/detail implementations, and do not add persistent caching, local storage, or
  offline sync unless a task explicitly asks for it.

```kotlin
internal class CountriesRepositoryImpl(
    private val api: CountriesApi,
) : CountriesRepository {

    override suspend fun fetchCountries(): Result<List<Country>> =
        runCatching { api.fetchCountries().map { it.toDomain() } }
}
```

### Retrofit API interfaces

- Defined in `data/` of the feature module.
- Use suspend functions — no `Call<T>` or `Observable<T>`.
- Do not use Kotlin default parameter values in Retrofit interfaces — Retrofit does not honour them.

```kotlin
internal interface CountriesApi {
    @GET("all?fields=name,cca2,capital,region,population")
    suspend fun fetchCountries(): List<CountryDto>
}
```

### DTOs

- Suffix: `Dto` (e.g. `CountryDto`).
- Use Moshi annotations (`@Json`).
- Extension function `fun CountryDto.toDomain(): Country` for mapping — never put mapping logic in the DTO class itself.

---

## Dependency Injection (Koin)

- Each feature module exports a `val <feature>Module = module { ... }` in `di/<Feature>Module.kt`.
- ViewModels: `viewModel { ViewModel(get(), get()) }` or `viewModelOf(::ViewModel)`.
- Use cases: `factoryOf(::UseCase)` — stateless, new instance per injection site.
- Repositories: `single { RepositoryImpl(get()) } bind Repository::class` — single instance.
- API interfaces: created via `single { get<Retrofit>().createApi<CountriesApi>() }`.
- All feature modules are collected in `:app`'s `AppModule.kt` via `includes(networkModule, countriesModule, ...)`.
- `networkModule` is always present — it provides the shared `OkHttpClient` and `Retrofit` instance that feature modules depend on.

---

## Navigation

- `NavHost` lives in `:app/navigation/AppNavHost.kt`.
- Each feature exposes a `fun NavGraphBuilder.<feature>Graph(navController: NavController)` extension.
- Routes are `const val` strings in `<Feature>Routes.kt` inside the feature module.
- Screens never hold a reference to `NavController` — navigation actions are passed as lambdas.
- Encode dynamic route arguments with a JVM-testable helper before placing them in route paths or
  query parameters. Do not call Android framework APIs such as `android.net.Uri` from route-builder
  functions that should be covered by local JVM unit tests.
- Validate dynamic identifiers against the expected format before navigation or before calling a
  repository/API.
- Prefer query parameters for display-only values such as titles or names, and use stable
  identifiers for path parameters.
- Add a small unit test for route builders when a route contains dynamic values.

```kotlin
// feature/countries/navigation/CountriesRoutes.kt
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

object CountriesRoutes {
    const val LIST = "countries/list"
    const val DETAIL = "countries/detail/{code}"
    fun detail(code: String) = "countries/detail/${encodeRouteArgument(code)}"

    private fun encodeRouteArgument(value: String): String =
        URLEncoder.encode(value, StandardCharsets.UTF_8.name()).replace("+", "%20")
}

// feature/countries/navigation/CountriesGraph.kt
fun NavGraphBuilder.countriesGraph(navController: NavController) {
    composable(CountriesRoutes.LIST) {
        CountriesScreen(onCountryClick = { code ->
            navController.navigate(CountriesRoutes.detail(code))
        })
    }
    composable(CountriesRoutes.DETAIL) { backStackEntry ->
        CountryDetailScreen(
            code = backStackEntry.arguments?.getString("code").orEmpty(),
            onBack = { navController.popBackStack() },
        )
    }
}
```

---

## Async

- Coroutines + Flow only — no RxJava, no callbacks.
- `suspend fun` for one-shot operations (network calls, database reads).
- `Flow` for streams (real-time updates, database observations).
- Repository and data source methods always suspend — never block.
- Never use `GlobalScope` or `runBlocking` in production code.

---

## Visibility

- Implementation classes (`RepositoryImpl`, `ViewModel`, use cases) that are not part of a public API should be `internal`.
- Test classes and fixtures are always `internal`.
- Repository interfaces and domain models are `internal` unless consumed across module boundaries.

---

## Testing

### Example testing scope

- Use the existing unit/integration test setup for example tasks.
- Do not add new UI automation, screenshot, emulator, or simulator test infrastructure unless
  the task explicitly asks for it.

### Unit tests

- Framework: JUnit 5 (Jupiter) — annotate with `@Test`, not JUnit 4.
- Assertions: Kotest (`shouldBe`, `shouldContain`, `shouldThrow`).
- Mocking: MockK (`mockk<T>()`, `coEvery`, `coVerify`).
- Flow testing: Turbine (`flow.test { awaitItem() shouldBe ... }`).
- Coroutines: `runTest { ... }` from `kotlinx-coroutines-test`.
- Base class: extend `AbstractTest` from `:library:testing` — sets up `Main` dispatcher automatically.

```kotlin
internal class FetchCountriesUseCaseTest : AbstractTest() {

    private val repository = mockk<CountriesRepository>()
    private val useCase = FetchCountriesUseCase(repository)

    @Test
    fun `returns countries on success`() = runTest {
        val expected = listOf(countryFixture(name = "Germany"))
        coEvery { repository.fetchCountries() } returns Result.success(expected)

        val result = useCase()

        result.getOrThrow() shouldBe expected
    }

    @Test
    fun `propagates failure from repository`() = runTest {
        val error = RuntimeException("network error")
        coEvery { repository.fetchCountries() } returns Result.failure(error)

        val result = useCase()

        result.isFailure shouldBe true
    }
}
```

### Test data

- Fixture factories in `<feature>/src/test/.../model/Fixtures.kt`.
- Function name: `<model>Fixture(...)` with sensible defaults — override only what the test needs.

```kotlin
fun countryFixture(
    name: String = "Germany",
    capital: String = "Berlin",
    population: Long = 83_000_000,
    region: String = "Europe",
) = Country(name = name, capital = capital, population = population, region = region)
```

### Code quality (Detekt)

The project enforces Detekt static analysis on every build. Key rules:

- Max line length: 120 characters.
- No magic numbers — extract named constants or use `private val`.
- No swallowed exceptions — always log or rethrow.
- No `GlobalScope` — flagged by the `GlobalCoroutineUsage` rule.
- Composable function names are allowed to start with an uppercase letter (configured in `config/detekt.yml`).
- Cyclomatic complexity limit: 15 per method. Split complex logic into smaller functions or use cases.

### Coverage

- Target: ≥ 90% branch and line coverage on new and changed code.
- Every `Result.failure` path must have a dedicated test.
- Every nullable field must have a null-path test.
- Every ViewModel state transition must be tested.

---

## Naming Conventions

| Element | Pattern | Example |
|---------|---------|---------|
| ViewModel | `<Feature>ViewModel` | `CountriesViewModel` |
| ViewModel state | nested `State` data class | `CountriesViewModel.State` |
| Use case | `<Verb><Noun>UseCase` | `FetchCountriesUseCase` |
| Repository interface | `<Feature>Repository` | `CountriesRepository` |
| Repository impl | `<Feature>RepositoryImpl` | `CountriesRepositoryImpl` |
| Retrofit API | `<Feature>Api` | `CountriesApi` |
| DTO | `<Name>Dto` | `CountryDto` |
| DTO mapping | `fun Dto.toDomain()` | `fun CountryDto.toDomain()` |
| Koin module | `val <feature>Module` | `val countriesModule` |
| Screen composable | `<Feature>Screen` | `CountriesScreen` |
| Screen impl | `<Feature>ScreenImpl` (private) | `CountriesScreenImpl` |
| Routes object | `<Feature>Routes` | `CountriesRoutes` |
| Nav graph extension | `NavGraphBuilder.<feature>Graph` | `NavGraphBuilder.countriesGraph` |

---

## Package Structure (per feature module)

```
feature/<name>/src/main/kotlin/com/example/countries/feature/<name>/
├── di/
│   └── <Name>Module.kt              # Koin module definition
├── domain/
│   ├── model/
│   │   └── <Name>.kt                # Immutable domain model
│   ├── <Name>Repository.kt          # Repository interface
│   └── <Verb><Name>UseCase.kt       # Use case(s)
├── data/
│   ├── <Name>Api.kt                 # Retrofit interface
│   ├── <Name>Dto.kt                 # DTO + toDomain() extension
│   └── <Name>RepositoryImpl.kt      # Repository implementation
├── presentation/
│   └── <Name>ViewModel.kt           # ViewModel + nested State
├── navigation/
│   ├── <Name>Routes.kt              # Route constants
│   └── <Name>Graph.kt               # NavGraphBuilder extension
└── system/
    └── <Name>Screen.kt              # Composable screen(s)

feature/<name>/src/test/kotlin/com/example/countries/feature/<name>/
├── domain/
│   └── <Verb><Name>UseCaseTest.kt
├── presentation/
│   └── <Name>ViewModelTest.kt
└── model/
    └── Fixtures.kt
```

---

## Rules for AI Agents

- **Never** modify files outside the active write scope given in the agent prompt.
- **Never** break the layer rule: domain must not import from `data/` or `system/`/`presentation/`.
- **Never** put business logic in a Composable function.
- **Never** access `NavController` inside a feature screen — use navigation lambdas.
- **Never** use `GlobalScope` or `runBlocking` in production code.
- **Always** register a new ViewModel with `viewModel { }` or `viewModelOf()` in the feature's Koin module.
- **Always** register a new use case with `factoryOf()` in the feature's Koin module.
- **Always** register a new repository with `single { ... } bind Interface::class` in the feature's Koin module.
- **Always** write tests for every new use case and every ViewModel state transition.
- **Always** test both the success path and the failure path for every repository call.
- **Always** use `Result<T>` as the return type of repository methods — never throw from a repository.
- **Always** follow the two-method Compose pattern (`Screen` + private `ScreenImpl`).
- **Always** use `runCatching { }` when wrapping Retrofit calls in a repository implementation.
- **Always** mark implementation classes (`RepositoryImpl`, use cases, ViewModels) as `internal`.
- **Always** write Detekt-compliant code: no magic numbers, max line length 120, no swallowed exceptions.
