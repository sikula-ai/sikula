# Test writer extra rules — example-countries-rust

- Unit tests for pure functions must be in an inline `mod tests` block in the same file (`#[cfg(test)]`), not in a separate file under `tests/`.
- Every `assert_eq!` call must include a failure message as the third argument so that a failing test output is self-explanatory without reading the source.
- Do not read from external files or network in unit tests. Load fixture data inline as a `const` string or construct test values programmatically.
