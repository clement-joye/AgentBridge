# Testing Strategy

## Unit Tests
- Config parsing and defaults.
- Authorization and path policy checks.
- Provider parsing normalization.
- Session sorting and index stability.
- Message formatting and chunking logic.

## Integration Tests
- Telegram command handlers with mocked updates.
- SQLite persistence for `/last` and `/use` flows.
- Runner invocation using test doubles for providers.

## End-to-End Smoke
- `/ping` returns hostname for authorized user.
- `/last` shows merged sessions.
- `/use N` binds active session.
- Free-text message routes to active session.

## Reliability Tests
- Timeout handling.
- Dead process cleanup.
- Restart behavior with persisted active state.
