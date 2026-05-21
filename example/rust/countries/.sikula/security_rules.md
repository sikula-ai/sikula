# Security reviewer extra rules — example-countries-rust

- Flag any code that constructs a file path from user-supplied input without using `Path::join` and explicit base-path validation. Path traversal via `..` segments must be blocked before any read or write.
- Flag `serde` deserialization of untrusted input that does not use `#[serde(deny_unknown_fields)]` when the struct represents a fixed protocol — unexpected fields silently dropped can mask version mismatches or injection attempts.
