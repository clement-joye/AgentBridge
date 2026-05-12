# Telegram AI Bridge (MVP)

Local Telegram bridge for continuing local AI coding sessions.

## Install

```bash
./scripts/install.sh
```

## Configure

```bash
telegram-ai-bridge init --config config.toml
# edit config.toml with real bot token and allowlist IDs/paths
```

## Doctor

```bash
telegram-ai-bridge doctor --config config.toml
```

## Run

```bash
telegram-ai-bridge run --config config.toml
```

## Install as Service

Linux (systemd user service):

```bash
telegram-ai-bridge install-service --config config.toml
systemctl --user daemon-reload
systemctl --user enable --now telegram-ai-bridge.service
```

macOS (launchd):

```bash
telegram-ai-bridge install-service --config config.toml
launchctl load -w ~/Library/LaunchAgents/com.telegram-ai-bridge.plist
```

## Notes

- Codex session discovery reads `~/.codex/sessions/**/*.json{,l}` using best-effort parsing.
- Copilot session discovery reads `~/.copilot/session-state/*/events.jsonl` with `workspace.yaml` context.
- Runner uses `pexpect` when available; otherwise a subprocess fallback is used.

## Troubleshooting

### Polling works but bot does not reply

If you see HTTP polling traffic but get no Telegram replies:

1. Verify token and allowlists in `config.toml`.
   - `telegram_bot_token` must be the real BotFather token.
   - `allowed_user_ids` must include your Telegram user ID.
   - `allowed_chat_ids` must include the chat ID you are messaging from.

2. Confirm chat context.
   - If you message in a private chat, `allowed_chat_ids` should include that private chat ID.
   - If you message in a group, include the group chat ID (usually negative, e.g. `-100...`).

3. Run in verbose mode and inspect logs.
   - Start with:
     - `./.venv/bin/telegram-ai-bridge run --config config.toml --verbose`
   - Check `Update route=... user_id=... chat_id=...` log lines.
   - If you see updates but no responses, IDs are usually mismatched with allowlists.

4. Test with basic commands first.
   - `/ping`
   - `/help`

5. If testing in groups, check BotFather privacy mode.
   - With privacy mode enabled, bots may only receive limited messages in groups.
   - Use private chat for initial verification.
