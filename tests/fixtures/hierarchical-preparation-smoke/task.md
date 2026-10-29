# Inventory and order integration

## Goal
Build a small in-memory Python inventory and order library whose independent
parts integrate safely before a downstream public facade uses them.

## Current behavior
The repository has an importable `warehouse` package and a passing unittest
baseline. It has no inventory, reservation, or order behavior yet.

## Desired behavior
Inventory stores nonnegative integer quantities keyed by SKU. Stock can be added
and queried. Reservations reduce available stock without changing total stock.
A reservation can be released once or consumed once; repeating the same action
must not apply the quantity change again. Consuming stock reduces total stock.

Orders contain one or more SKU/quantity lines. Creating an order reserves all its
lines atomically. If any line cannot be reserved, every reservation made by that
attempt is rolled back. Cancelling a pending order releases its reservations.
Completing it consumes those reservations. Repeating cancellation or completion
is idempotent; attempting the opposite transition after completion/cancellation
returns `INVALID_ORDER_STATE` without changing stock.

The public facade connects inventory and orders. A client can add stock, place
an order, inspect available quantities, and cancel or complete an order through
that facade. Return structured results with stable error codes. Unknown SKUs use
`UNKNOWN_SKU`, invalid quantities use `INVALID_QUANTITY`, and insufficient stock
uses `INSUFFICIENT_STOCK`. Successful results must not be confused with errors.

## Acceptance criteria
- Two orders competing for the last item cannot both reserve it.
- A multi-line order with an unavailable final line leaves all stock unchanged.
- Cancelling an order restores exactly its reserved quantities, even if repeated.
- Completing an order consumes its quantities exactly once.
- Rejected transitions leave both order state and stock unchanged.
- Facade integration tests cover the complete add/place/inspect/cancel and
  add/place/complete flows, including failures across the inventory/order boundary.
- Shared quantity rules and exact error codes hold in every affected component.

## Out of scope
No database, persistence, network, web server, CLI, payment processing, background
workers, external packages, or replacement dependencies. Sequential in-process
calls are sufficient; multithreaded concurrency is outside this task.

## Security and privacy
Do not log order contents or customer data. Do not read environment files or
credentials. Implement within `warehouse/`; tests belong in `tests_proj/`.

## Reviewer focus
Check atomic rollback, idempotency, shared quantity rules, exact error codes,
and the actual connections between inventory, orders, and the public facade.

## Verification
- `python -m compileall -q warehouse/`
- `python -m unittest discover -s tests_proj -v`

## Preparation guidance
This is a smoke fixture for bounded hierarchical delivery preparation. Choose
the hierarchical preparation route, with independently checked local contracts
and at least one intermediate checkpoint before its downstream consumer. Choose
coherent grouping and dependencies yourself; do not add product functionality
or redundant integration units solely to create a hierarchy. Include a nested
checkpoint only if the actual work warrants one. Preserve the shared rules and
cross-group acceptance criteria in all affected contracts.
