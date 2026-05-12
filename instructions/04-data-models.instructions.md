# Data Models

## LocalSession
Canonical discovered session record:
- `index`
- `provider`: `codex | copilot`
- `session_id`
- `cwd`
- `repo_name`
- `branch`
- `last_active_at`
- `last_user_prompt`
- `last_final_answer`
- `resumable`
- `source_path`

## ActiveSession
Per-chat active binding:
- `telegram_chat_id`
- `provider`
- `session_id`
- `cwd`
- `process_id`
- `alias`
- `mode`: `resumed | new`
- `status`: `starting | ready | running | closed | error`
- `created_at`
- `last_message_at`

## Persistence Notes
- Store latest `/last` snapshot per chat to keep numbering stable.
- Store active session per chat for free-text routing.
- Persist lifecycle changes atomically.
