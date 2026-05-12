# System Architecture

## High-Level Components
- Telegram Bot (long polling)
- Local Bridge Service
- Session Discovery
- Session Registry
- Active Session Manager
- Agent Process Runner
- Output Parser/Summarizer
- Git Context Helper
- Safety/Authorization Layer

## Data Flow
1. Telegram update received via long polling.
2. Command router validates user/chat authorization.
3. Session commands hit discovery/registry/active manager.
4. Prompt forwarding invokes provider-specific runner.
5. Output is summarized and returned to Telegram.
6. State persisted in SQLite.

## Provider Adapters
- `CodexProvider`: session discovery, resume/new commands.
- `CopilotProvider`: session discovery, resume/new commands.
- Both normalize to shared `LocalSession` model.

## Extensibility
- Add providers by implementing the `SessionProvider` interface.
- Keep command layer provider-agnostic.
- Keep runner execution isolated from Telegram transport.
