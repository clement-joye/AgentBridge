# Development Flow

## Branching
- Use focused feature branches per phase.
- Keep commits scoped by subsystem (bot, provider, runner, safety).

## Implementation Order
1. Bootstrap and authorization.
2. Discovery and selection.
3. Message forwarding and runner.
4. Lifecycle hardening.
5. Output quality and git commands.
6. Multi-session alias routing.
7. Installability and operations.

## Local Workflow
1. Update config.
2. Run bridge in polling mode.
3. Test Telegram command flows.
4. Inspect logs and SQLite state.
5. Iterate with focused fixes.

## Coding Practices
- Keep provider contracts explicit and stable.
- Prefer fail-safe behavior when metadata is ambiguous.
- Separate transport (Telegram) from process orchestration.
