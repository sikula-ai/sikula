# Planner extra rules — example-countries-rust

- If the task touches only a single pure function (e.g. a formatter or a parser with no UI wiring), output SINGLE_PASS — splitting a one-function change adds no value.
- When a task adds both a data model change and a UI/display change, split them into two steps: model first, display second. This keeps each step independently reviewable and compilable.
- Serde struct changes (adding or removing fields) and the code that reads those fields must go in the same step — splitting them produces an uncompilable intermediate state.
