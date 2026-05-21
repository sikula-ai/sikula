# Development Guidelines

This document defines architectural rules and coding conventions for the Sikula Rust Example project.
It is loaded as context by AI agents — follow every rule precisely.

---

## Architecture

Three-layer architecture with a strict, unidirectional dependency rule.

### Layers

Dependency direction is strict: **commands → domain ← data**. The domain layer has no knowledge of commands or data.

| Layer | Path | Responsibilities |
|-------|------|-----------------|
| Commands | `src/commands/` | CLI handlers — parse args, call domain, format and print output |
| Domain | `src/domain/` | Pure business logic — models, filtering, statistics. No I/O, no clap, no serde side-effects at runtime |
| Data | `src/data/` | File I/O — reads JSON and deserializes into domain types |

`src/error.rs` defines `AppError` and is shared across all layers.

`src/cli.rs` and `src/main.rs` belong to the binary only — they are not part of the library crate.

### Crate split

The project uses both a `[lib]` and a `[[bin]]` target in `Cargo.toml`:

- `lib` (`src/lib.rs`) — exports `domain`, `data`, `error`. Used by integration tests.
- `bin` (`src/main.rs`) — wires the CLI (`src/cli.rs`) to the library via `src/commands/`.

Integration tests in `tests/` import from the library crate (`use countries::...`), not from `main.rs`.

---

## Domain Layer

### Models

- Defined in `src/domain/country.rs`.
- Structs are plain data — `#[derive(Debug, Clone)]`.
- `#[derive(Deserialize)]` is allowed on domain models because the project has no separate DTO layer; deserialization is invoked only by the data layer, so the domain model carrying the annotation does not violate the layer boundary.
- No business logic beyond simple accessor methods (`primary_capital()`, `language_names()`, `currency_summary()`).

```rust
#[derive(Debug, Clone, Deserialize)]
pub struct Country {
    pub name: CountryName,
    pub cca3: CountryCode,
    pub capital: Vec<String>,
    pub region: String,
    pub subregion: String,
    pub population: u64,
    pub area: f64,
    pub languages: HashMap<String, String>,
    pub currencies: HashMap<String, Currency>,
}
```

### Newtypes

Wrap validated primitives in newtypes to prevent misuse:

```rust
pub struct CountryCode(String); // always uppercase, always 3 ASCII letters
```

- Implement `FromStr` to validate on construction.
- Implement `Display` for user-facing output.
- Use `#[serde(try_from = "String")]` to reuse `FromStr` validation during deserialization.

### Filter functions

- Defined in `src/domain/filter.rs` as free functions, not methods on the model.
- Accept `&[Country]` and return `Vec<&Country>` — borrow, never clone.
- All string comparisons are case-insensitive (`.to_lowercase()`).

```rust
pub fn search_by_name<'a>(countries: &'a [Country], query: &str) -> Vec<&'a Country>
pub fn filter_by_region<'a>(countries: &'a [Country], region: &str) -> Vec<&'a Country>
pub fn find_by_code<'a>(countries: &'a [Country], code: &CountryCode) -> Option<&'a Country>
```

### Statistics

- `src/domain/stats.rs` exposes a single entry point: `pub fn compute(countries: &[Country]) -> Stats`.
- `Stats` and `RegionSummary` are plain data structs with no formatting logic.
- `f64` comparisons use `.partial_cmp(...).unwrap_or(std::cmp::Ordering::Equal)` — never `.unwrap()` directly on `partial_cmp`.

---

## Data Layer

- `src/data/loader.rs` exposes one public function: `pub fn load_countries(path: &Path) -> Result<Vec<Country>, AppError>`.
- All I/O errors and parse errors are mapped to typed `AppError` variants with the file path embedded.
- No printing, no clap, no anyhow in this layer.

```rust
pub fn load_countries(path: &Path) -> Result<Vec<Country>, AppError> {
    let contents = std::fs::read_to_string(path).map_err(|source| AppError::DataLoad {
        path: path.display().to_string(),
        source,
    })?;
    serde_json::from_str(&contents).map_err(|source| AppError::DataParse {
        path: path.display().to_string(),
        source,
    })
}
```

---

## Commands Layer

Each subcommand has its own file in `src/commands/`. A command handler:

- Has a single public entry point: `pub fn run(args: &XxxArgs, countries: &[Country]) -> Result<(), AppError>`.
- Calls domain functions — never reads files or parses JSON.
- Returns `Err(AppError::NoMatchingCountries)` when a filter produces an empty result.
- Formats and prints to stdout — all output lives here, not in the domain layer.
- Extracts format helpers into private functions within the same file.
- Display helpers needed by more than one command belong in `src/commands/shared.rs` — never duplicated across command files.
- Business logic needed by more than one command belongs in `src/domain/`.

```rust
pub fn run(args: &ListArgs, countries: &[Country]) -> Result<(), AppError> {
    let filtered = match &args.region {
        Some(region) => filter::filter_by_region(countries, region),
        None => countries.iter().collect(),
    };
    if filtered.is_empty() {
        return Err(AppError::NoMatchingCountries);
    }
    print_header();
    for country in filtered {
        print_row(country);
    }
    Ok(())
}
```

---

## Error Handling

### `AppError` (typed, library)

- Defined in `src/error.rs` using `#[derive(thiserror::Error)]`.
- One variant per distinct failure mode — never a generic `Other(String)`.
- Struct variants embed context (e.g. `path: String`, `#[source] source: std::io::Error`) so the error message is self-contained.

```rust
#[derive(Debug, Error)]
pub enum AppError {
    #[error("failed to load data file '{path}': {source}")]
    DataLoad { path: String, #[source] source: std::io::Error },

    #[error("failed to parse data file '{path}': {source}")]
    DataParse { path: String, #[source] source: serde_json::Error },

    #[error("country not found: '{0}'")]
    CountryNotFound(String),

    #[error("invalid country code '{0}': must be exactly 3 ASCII letters")]
    InvalidCountryCode(String),

    #[error("no countries match the given filter")]
    NoMatchingCountries,
}
```

### `anyhow` (binary only)

- Used only in `src/main.rs` — `fn main() -> anyhow::Result<()>`.
- `.with_context(|| ...)` adds human-readable context around `AppError` values.
- Never use `anyhow` in `src/domain/`, `src/data/`, or `src/commands/`.

---

## CLI (clap)

- Defined entirely in `src/cli.rs`.
- Use `#[derive(Parser)]` on `Cli`, `#[derive(Subcommand)]` on `Command`, `#[derive(Args)]` on argument structs.
- One `Args` struct per subcommand: `ListArgs`, `SearchArgs`, `InfoArgs`, `StatsArgs`.
- Global options (e.g. `--data`) live on `Cli` with `global = true`.
- The `env` feature must be listed in `Cargo.toml` to use `env = "..."` in `#[arg()]`.

```rust
#[derive(Debug, Parser)]
#[command(name = "countries", version, about, long_about = None)]
pub struct Cli {
    #[arg(long, global = true, default_value = "data/countries.json", env = "COUNTRIES_DATA")]
    pub data: PathBuf,
    #[command(subcommand)]
    pub command: Command,
}
```

- `src/cli.rs` and `src/commands/` are declared as `mod` inside `src/main.rs` — they are not part of the library crate and must not be added to `src/lib.rs`.
- Enums used as `--flag` values (`#[derive(ValueEnum)]`) that represent a domain concept (e.g. sort field, filter type) belong in `src/domain/` and are mapped from CLI args in the command handler. Enums that are purely a display/presentation concept (e.g. output format) belong in `src/cli.rs`.

---

## Testing

### Unit tests

- Live in `#[cfg(test)] mod tests` at the bottom of the same file as the code under test.
- Use inline fixture helpers (e.g. `fn make_country(...)`) — no external fixture files.
- Test both the happy path and failure cases for every public function.
- Test case-insensitivity and edge cases (empty slice, missing key, etc.).

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn make_country(common: &str, region: &str, code: &str) -> Country { ... }

    #[test]
    fn search_case_insensitive() {
        let countries = vec![make_country("Germany", "Europe", "DEU")];
        let results = search_by_name(&countries, "germany");
        assert_eq!(results.len(), 1);
    }
}
```

### Integration tests

- Live in `tests/integration_tests.rs`.
- Import from the library crate: `use countries::data::loader::load_countries`.
- Load the real `data/countries.json` using `env!("CARGO_MANIFEST_DIR")` — never hardcode a path.
- Group tests into `mod loader_tests`, `mod filter_tests`, `mod stats_tests`.
- Assert lower bounds (e.g. `>= 25 countries`) so tests don't break when new data entries are added.

```rust
fn data_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data/countries.json")
}
```

### Coverage targets

- Every `AppError` variant must have at least one test that produces it.
- Every filter function must have a no-match test.
- Every `Option`-returning function must have a `None` case test.

---

## Code Quality

### Clippy

Run with `-D warnings` — all warnings are errors:

```bash
cargo clippy -- -D warnings
```

Key rules enforced:
- No literal strings as the last argument to a format macro when they could be inlined (`print_literal`).
- No `.unwrap()` on `partial_cmp` — use `.unwrap_or(std::cmp::Ordering::Equal)`.

### Formatting

All code must pass `cargo fmt --check`. Run `cargo fmt` to apply.

### Verification before done

Run all three before declaring work complete:

```bash
cargo test
cargo clippy -- -D warnings
cargo fmt --check
```

### No magic numbers

Extract column widths and other repeated constants into named `const` values:

```rust
const NAME_W: usize = 30;
const CAPITAL_W: usize = 20;
```

---

## Naming Conventions

| Element | Pattern | Example |
|---------|---------|---------|
| Command args struct | `<Command>Args` | `ListArgs`, `InfoArgs` |
| Command handler | `pub fn run(args: &XxxArgs, countries: &[Country])` | `list::run`, `stats::run` |
| Domain filter fn | verb + noun, snake_case | `search_by_name`, `filter_by_region` |
| Domain compute fn | `compute(input) -> Output` | `stats::compute` |
| Data loader fn | `load_<noun>(path)` | `load_countries` |
| Newtype | singular noun | `CountryCode` |
| Error enum | `AppError` (single enum, one variant per case) | `AppError::CountryNotFound` |
| Test fixture helper | `make_<model>(...)` | `make_country(...)` |

---

## File Structure

```
src/
  lib.rs                    — re-exports domain, data, error (library crate root)
  main.rs                   — wires CLI to commands (binary crate root)
  cli.rs                    — clap structs
  error.rs                  — AppError via thiserror
  commands/
    mod.rs
    list.rs                 — countries list [--region]
    search.rs               — countries search --name
    shared.rs               — display helpers shared across commands
    info.rs                 — countries info <CODE>
    stats.rs                — countries stats [--region]
  domain/
    mod.rs
    country.rs              — Country, CountryCode, CountryName, Currency
    filter.rs               — search_by_name, filter_by_region, find_by_code
    stats.rs                — compute(countries) → Stats, RegionSummary
  data/
    mod.rs
    loader.rs               — load_countries(path) → Result<Vec<Country>, AppError>

tests/
  integration_tests.rs      — end-to-end tests against data/countries.json

data/
  countries.json            — static dataset (REST Countries API format, 25 entries)
```
