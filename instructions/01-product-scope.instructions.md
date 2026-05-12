# Product Scope

## Vision
Telegram AI Bridge allows remote continuation of local AI coding sessions from Telegram while keeping execution, files, and credentials on the local machine.

## Primary User Outcomes
- See recent local sessions with `/last`.
- Bind Telegram chat to a local session via `/use N`.
- Send normal Telegram messages to continue a selected local session.
- Start new sessions with `/new` and `/new N`.
- Close or inspect lifecycle state with `/close` and `/status`.

## Non-Goals (MVP)
- No public webhooks.
- No remote file execution outside allowlisted roots.
- No destructive command approvals from Telegram in early phases.

## MVP Cut
- Phase 0
- Phase 1
- Phase 2
- Phase 3 (Codex and Copilot resume flows)
- Phase 5 basic (`/close`, `/status`)
- Phase 7 basic allowlist safety

## Success Criteria
- End-to-end flow works: `/last` -> `/use 1` -> free-text prompt -> useful final response.
- Unauthorized users/chats receive no useful data.
- Sessions are stable, resumable, and constrained by path policy.
