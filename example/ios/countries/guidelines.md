# Development Guidelines

This document defines architectural rules and coding conventions for the Sikula iOS Example project.
It is loaded as context by AI agents — follow every rule precisely.

---

## Architecture

Four-layer architecture with a strict, unidirectional dependency rule.

### Layers

Dependency direction is strict: **UI → Presentation → Domain ← Data**. The domain layer has no knowledge of UIKit, SwiftUI, URLSession, or any framework.

| Layer | Path | Responsibilities |
|-------|------|-----------------|
| App | `Countries/App/` | Entry point, dependency wiring |
| UI | `Countries/UI/` | SwiftUI views — read from ViewModel, forward actions to ViewModel |
| Presentation | `Countries/Presentation/` | `@Observable` ViewModels — hold UI state, call use cases |
| Domain | `Countries/Domain/` | Models, repository protocols, use cases. No UIKit, no URLSession, no SwiftUI |
| Data | `Countries/Data/` | Network client, DTOs, repository implementations |

Tests live in `CountriesTests/`.

---

## Domain Layer

### Models

- Defined in `Countries/Domain/Country.swift`.
- Plain value types: `struct` with `let` properties.
- `Identifiable` conformance uses `cca2` as the stable identifier.
- No framework imports — pure Swift only.

### Repository Protocol

- Defined in `Countries/Domain/CountriesRepository.swift`.
- `protocol CountriesRepository` with `func fetchAll() async throws -> [Country]`.
- Domain defines the protocol; Data layer provides the implementation.

### Use Cases

- Each use case is a `struct` in `Countries/Domain/` with a single `func execute(...)` method.
- Receives a repository via constructor injection — never instantiates network clients.
- Returns domain types only.

---

## Data Layer

### DTOs

- Defined in `Countries/Data/CountryDTO.swift` as `Decodable` structs.
- Use nested types for clarity (`CountryDTO.Name`, etc.).
- Optional fields are `Optional` in the DTO — the repository implementation provides defaults.
- Fetched fields: `name,cca2,capital,region,population`.

### API Client

- `CountriesAPIClient` in `Countries/Data/CountriesAPIClient.swift`.
- Uses `URLSession.shared` with `async/await`. No Combine, no third-party networking.
- Returns DTOs — no domain types.

### Repository Implementation

- `CountriesRepositoryImpl` in `Countries/Data/CountriesRepositoryImpl.swift`.
- Implements `CountriesRepository`.
- Maps DTOs to domain models in a `private extension CountryDTO { var asDomain: Country }`.
- All mapping logic lives in the extension, not in the struct initialiser.

---

## Presentation Layer

### ViewModels

- One `@Observable` `final class` per screen in `Countries/Presentation/`.
- State properties: `private(set) var` — readable from views, writable only inside the ViewModel.
- `@MainActor` on any method that mutates state.
- Receives use cases via constructor injection.
- No SwiftUI imports — `Observation` only.
- Always initialize `isLoading = true` — no exceptions, regardless of whether the screen's content type is `Optional` or a collection. SwiftUI's `@Observable` tracking is established during the first body evaluation. If all state conditions are false on that render (e.g. `isLoading = false`, `error = nil`, `country = nil` on a detail screen), the body produces `EmptyView` and `.task` may not fire. `isLoading = true` ensures the first render shows `ProgressView`, which establishes tracking and guarantees `.task` runs.

```swift
import Observation

@Observable
final class CountriesViewModel {
    private(set) var countries: [Country] = []
    private(set) var isLoading = true   // must be true — see isLoading rule above
    private(set) var error: String?

    private let useCase: FetchCountriesUseCase

    init(useCase: FetchCountriesUseCase) { self.useCase = useCase }

    @MainActor func load() async { ... }
}
```

---

## UI Layer

### Views

- SwiftUI `struct` conforming to `View` in `Countries/UI/`.
- Hold ViewModel as `@State var viewModel: SomeViewModel` — views own their ViewModel.
- Never call use cases or repositories directly — all data access goes through the ViewModel.
- Use `.task { await viewModel.load() }` for initial data fetch.
- Extract reusable sub-views into separate files in `Countries/UI/`.

### Strings

- All user-visible strings are defined in `Countries/Localizable.xcstrings` (String Catalog).
- Reference strings in code with `String(localized: "key")`.
- Keys follow the `<screen>_<element>` pattern (e.g. `countries_title`, `countries_retry`).

### Navigation

- Use `NavigationStack` at the root view.
- Use `NavigationLink` for push navigation.
- Pass only the data needed to the destination view, not the entire ViewModel.

### Pull-to-refresh

Attach `.refreshable` to the `List` directly — only when the list is visible, not while loading or showing an error. This ensures pull-to-refresh is only available when data is already loaded, which is the correct UX.

The ViewModel method must accept a `showSpinner` parameter. Pass `false` from `.refreshable` so the native SwiftUI refresh indicator is used instead of the app's `ProgressView`. The `@MainActor` annotation is required regardless of parameters.

```swift
// ViewModel
@MainActor func load(showSpinner: Bool = true) async {
    if showSpinner { isLoading = true }
    error = nil
    do { ... } catch { ... }
    if showSpinner { isLoading = false }
}

// View
List(viewModel.items) { item in ... }
    .refreshable {
        await viewModel.load(showSpinner: false)
    }
```

Never call `load(showSpinner: true)` from `.refreshable` — that would hide the list content and show `ProgressView` during refresh, which is jarring UX.

### Per-item navigation

When a list navigates to a per-item detail screen, the list view may hold the
detail use case and instantiate the detail ViewModel inside the `NavigationLink`.
This is the correct SwiftUI pattern — the view does not call the use case directly.

```swift
NavigationLink {
    CountryDetailView(
        viewModel: CountryDetailViewModel(cca2: country.cca2, useCase: detailUseCase)
    )
} label: {
    CountryRowView(country: country)
}
```

---

## Dependency Injection

No DI framework. Wire dependencies manually in `CountriesApp.swift`:

```swift
CountriesListView(
    viewModel: CountriesViewModel(
        useCase: FetchCountriesUseCase(
            repository: CountriesRepositoryImpl(
                client: CountriesAPIClient()
            )
        )
    )
)
```

For new screens, follow the same pattern: create ViewModel at the call site and pass it in.

---

## Error Handling

- Use cases and repository implementations throw typed errors or `URLError` — never swallow errors silently.
- ViewModels catch errors in `do/catch` and store `error: String?` for the view.
- Views show an inline error message and a retry button — no full-screen error screens unless specified.
- Never use `try!` or `try?` in production code.

---

## Testing

### Unit Tests

- Live in `CountriesTests/` and import the main module with `@testable import Countries`.
- One test class per unit under test (e.g. `FetchCountriesUseCaseTests`, `CountryModelTests`).
- Use `async throws` for async test methods.
- Mock protocols with private `struct` conformances inside the test file.
- Use `private extension Country { static func fixture(...) -> Country }` for test data — no external fixture files.

```swift
private struct MockCountriesRepository: CountriesRepository {
    let result: Result<[Country], Error>
    func fetchAll() async throws -> [Country] { try result.get() }
}
```

### Coverage Targets

- Every `protocol` must have at least one mock test that exercises both success and failure paths.
- Every computed property on domain models must have a test.
- Every `Optional`-returning accessor must have a nil/empty case test.
- Every ViewModel must have a test that verifies `isLoading == true` immediately after initialization, before any method is called.
- When a ViewModel method has multiple call sites with different loading behaviour (e.g. initial load vs. background refresh), each variant must have a test.

---

## Code Quality

### Swift Concurrency

- All async UI updates go through `@MainActor`.
- Never use `DispatchQueue.main.async` — use `@MainActor` or `await MainActor.run { }`.
- Avoid `Task.detached` unless there is a specific reason; prefer structured concurrency.

### Naming Conventions

| Element | Pattern | Example |
|---------|---------|---------|
| ViewModel | `<Screen>ViewModel` | `CountriesViewModel`, `CountryDetailViewModel` |
| View | `<Screen>View` or `<Component>View` | `CountriesListView`, `CountryRowView` |
| Use case | `<Verb><Noun>UseCase` | `FetchCountriesUseCase`, `FetchCountryDetailUseCase` |
| Repository protocol | `<Noun>Repository` | `CountriesRepository` |
| Repository impl | `<Noun>RepositoryImpl` | `CountriesRepositoryImpl` |
| API client | `<Noun>APIClient` | `CountriesAPIClient` |
| DTO | `<Noun>DTO` | `CountryDTO` |
| Domain model | singular noun | `Country` |
| Test fixture helper | `static func fixture(...)` | `Country.fixture()` |

### No Magic Numbers

Extract repeated constants:

```swift
private enum Layout {
    static let flagSize: CGFloat = 40
    static let spacing: CGFloat = 12
}
```

---

## File Structure

```
Countries/
  App/
    CountriesApp.swift          — @main entry point, dependency wiring
  Domain/
    Country.swift               — Country
    CountriesRepository.swift   — CountriesRepository protocol
    FetchCountriesUseCase.swift — use case
  Data/
    CountryDTO.swift            — Decodable DTOs
    CountriesAPIClient.swift    — URLSession client
    CountriesRepositoryImpl.swift — CountriesRepository implementation
  Presentation/
    CountriesViewModel.swift    — @Observable ViewModel
  UI/
    CountriesListView.swift     — countries list screen
    CountryRowView.swift        — single row in list
  Assets.xcassets/             — app icon, accent color

CountriesTests/
  CountriesTests.swift          — unit tests
```

---

## Verification Before Done

Build and test before declaring work complete:

```bash
xcodebuild build -project Countries.xcodeproj -scheme Countries \
    -destination 'generic/platform=iOS Simulator' -configuration Debug
xcodebuild test -project Countries.xcodeproj -scheme Countries \
    -destination 'platform=iOS Simulator,OS=latest,name=iPhone 16' -configuration Debug
```
