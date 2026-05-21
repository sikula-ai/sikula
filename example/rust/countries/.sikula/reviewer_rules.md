# Reviewer extra rules — example-countries-rust

- Do not approve a new public function (`pub fn`) that lacks a `///` doc comment. The comment must describe what the function does, not just restate its name.
- Do not approve code that calls `.unwrap()` or `.expect()` on values that could realistically fail at runtime (e.g. regex compilation is fine; user-facing data lookups are not). Prefer `?`, `unwrap_or`, `unwrap_or_else`, or explicit error handling.
- When a `match` on an enum is added or modified, verify it is exhaustive and that no arm uses a catch-all `_` that silently discards a meaningful variant.
