# Countries

A demonstration CLI application built with idiomatic Rust, used as an example project for [Sikula](https://github.com/sikula-ai/sikula).

## Stack

| Crate | Purpose |
|---|---|
| `clap` v4 (derive) | CLI parsing |
| `serde` + `serde_json` | JSON deserialization |
| `thiserror` v2 | Typed domain errors |
| `anyhow` v1 | Application-level error propagation |

## Usage

```bash
# Build
cargo build --release

# List all countries
./target/release/countries list

# Filter by region
./target/release/countries list --region Europe

# Search by name (case-insensitive, partial)
./target/release/countries search --name "Czech"

# Show details for a country
./target/release/countries info CZE

# Global stats
./target/release/countries stats

# Stats for a region
./target/release/countries stats --region Asia
```

Use a custom data file:

```bash
./target/release/countries --data /path/to/my.json list
# or via env var
COUNTRIES_DATA=/path/to/my.json ./target/release/countries list
```

## Data

Static dataset (`data/countries.json`) sourced from [REST Countries](https://restcountries.com) (MPL 2.0).

## Architecture

```
src/
  lib.rs            — re-exports domain, data, error modules
  main.rs           — wires CLI to commands
  cli.rs            — clap structs (Parser, Subcommand, Args)
  error.rs          — AppError via thiserror
  commands/         — thin CLI handlers; format and print results
  domain/
    country.rs      — Country, CountryCode (newtype + FromStr), CountryName
    filter.rs       — search_by_name, filter_by_region, find_by_code
    stats.rs        — compute(countries) → Stats
  data/
    loader.rs       — load_countries(path) → Result<Vec<Country>, AppError>
```

The `domain/` layer contains pure business logic — no I/O, no clap, no serde side-effects at runtime.

## Running tests

```bash
cargo test
```

## Sikula tasks

Ready-to-run tasks in `.sikula/tasks/`:

| Task file | Description |
|---|---|
| `add-neighbours.md` | Show Neighbouring Countries in Info |
| `format-population.md` | Format Population as Compact String |
| `sort-list.md` | Sort Countries List |

Run from this directory: `sikula run .sikula/tasks/<task-file>`
