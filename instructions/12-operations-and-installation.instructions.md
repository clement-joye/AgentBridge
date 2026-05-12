# Operations and Installation

## Deliverables
- `README.md`
- `config.example.toml`
- Install script
- `systemd` user service
- `launchd` plist (macOS)
- Logging setup

## Runtime Commands
- `telegram-ai-bridge init`
- `telegram-ai-bridge run`
- `telegram-ai-bridge doctor`
- `telegram-ai-bridge install-service`

## Doctor Checks
- Telegram token is configured.
- Codex CLI exists in `PATH`.
- Copilot CLI exists in `PATH`.
- Session directories are readable.
- SQLite DB path is writable.
- Allowed roots and blocked paths are valid.
- Effective user/chat authorization policy is coherent.

## Ops Requirements
- Automatic startup on login/boot.
- Logs are persistent and searchable.
- Graceful shutdown on service stop.
