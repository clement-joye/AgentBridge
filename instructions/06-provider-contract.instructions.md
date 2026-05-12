# Provider Contract

## SessionProvider Interface
- `discover_sessions(limit: int) -> list[LocalSession]`
- `resume_command(session: LocalSession) -> list[str]`
- `new_command(cwd: str) -> list[str]`

## Codex Provider Expectations
- Discover local Codex sessions.
- Extract stable session ID.
- Extract cwd, last prompt/answer, and activity time where possible.
- Resume command shape: `codex resume --no-alt-screen <session_id>`.

## Copilot Provider Expectations
- Discover Copilot CLI sessions with best-effort parsing.
- Normalize fields to `LocalSession`.
- Parse `~/.copilot/session-state/*/events.jsonl` and `workspace.yaml`.
- Resume command shape: `copilot --resume <session_id>`.

## Error Handling
- Discovery failures should skip malformed entries, not fail whole list.
- Unknown formats should degrade gracefully.
