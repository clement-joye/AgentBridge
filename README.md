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

The `install-service` command must be run with the virtual environment activated so
it can resolve the correct binary path for the service unit.

Linux (systemd user service):

```bash
source .venv/bin/activate
telegram-ai-bridge install-service --config config.toml
systemctl --user daemon-reload
systemctl --user enable --now telegram-ai-bridge.service
```

macOS (launchd):

```bash
source .venv/bin/activate
telegram-ai-bridge install-service --config config.toml
launchctl load -w ~/Library/LaunchAgents/com.telegram-ai-bridge.plist
```

---

## CLI Reference

### Commands

| Command | Description |
|---|---|
| `init` | Copy `config.example.toml` to the given path. Prints next steps. No-op if the file already exists. |
| `run` | Start the bot and begin polling Telegram for updates. Default command when none is given. |
| `doctor` | Validate config, check CLI tools, verify session directories, and report each check as OK or FAIL. |
| `install-service` | Write a platform service unit (`systemd` on Linux, `launchd` on macOS) and print activation instructions. Requires the virtual environment to be active. |

### Flags

| Flag | Description |
|---|---|
| `--config <path>` | Path to the TOML config file. Defaults to `config.toml`. |
| `--verbose` | Enable DEBUG-level logging for all modules. |
| `--io-logs` | Show only bridge input/output log lines plus warnings/errors from dependencies. Useful for monitoring prompts and responses without noise. |

---

## Configuration Reference

All settings live in a TOML file (default `config.toml`). Run `telegram-ai-bridge init` to create one from the bundled example.

| Key | Type | Description |
|---|---|---|
| `telegram_bot_token` | string | BotFather token. **Required.** |
| `allowed_user_ids` | list of int | Telegram user IDs that may interact with the bot. |
| `allowed_chat_ids` | list of int | Telegram chat IDs the bot will respond in (private or group). |
| `security_alert_chat_ids` | list of int | Admin chat IDs that receive security alert notifications. |
| `allowed_repo_roots` | list of path | Directories the agent is allowed to work in. Session paths and worktree paths must be under one of these. |
| `blocked_paths` | list of path | Absolute paths that are always denied, regardless of `allowed_repo_roots`. |
| `runner_timeout_seconds` | int | Maximum seconds to wait for an agent response. Minimum 30. Defaults to 180. |
| `state_dir` | path | Directory for the SQLite database and persisted state. Defaults to `~/.local/state/telegram-ai-bridge`. |
| `[safety] audit_enabled` | bool | Write every significant event to the audit log in the database. Defaults to `true`. |
| `[safety] deny_destructive_prompts` | bool | Block prompts that contain destructive command patterns before forwarding to the agent. Defaults to `true`. |
| `[codex] enabled` | bool | Allow creating and resuming Codex sessions. Defaults to `true`. |
| `[copilot] enabled` | bool | Allow creating and resuming Copilot sessions. Defaults to `true`. |

---

## Telegram Bot Commands

### Utility

| Command | Description |
|---|---|
| `/start` | Confirm the bridge is running. |
| `/help` | Show the full command list. |
| `/ping` | Reply with the host name of the machine running the bridge. |

### Session Discovery

| Command | Description |
|---|---|
| `/last` | List the 10 most recent sessions from all enabled providers. Assigns temporary indices for use with `/use`, `/new`, and `/worktree`. |
| `/last_codex` | Same as `/last` but limited to Codex sessions. |
| `/last_copilot` | Same as `/last` but limited to Copilot sessions. |

### Session Management

| Command | Description |
|---|---|
| `/use <index> [as <alias>]` | Bind a listed session (from the last `/last` call) as the active session. Optionally give it a named alias. |
| `/new [<index>]` | Start a brand-new agent session. Without an index, reuses the provider and path of the current active session. With an index, copies the provider and path from the listed session. |
| `/active` | Show the provider, path, mode, status, and model of the current default session. |
| `/sessions` | List all saved session bindings for this chat, including alias, provider, path, status, runner state, and model. |
| `/close [<alias\|index>]` | Close a session binding and terminate its runner process. Cleans up any managed worktree. Without an argument, closes the default session. |
| `/abort` | Send an interrupt signal to the active runner process, stopping the current task while keeping the session binding. |
| `/status` | Show full status: provider, mode, runner state, model, path, and kill-switch state. |
| `/reload` | Hot-reload `config.toml` without restarting the bridge. Reports which fields changed. |

### Model Control

| Command | Description |
|---|---|
| `/model` | Show the current model override for the active session. |
| `/model <name>` | Set a model override for the active session (e.g. `/model o3`). |
| `/model clear` | Remove the model override, reverting to the provider default. |

### Safety Controls

| Command | Description |
|---|---|
| `/killswitch` | Show the current kill-switch state with on/off buttons. |
| `/killswitch on` | Enable the kill switch: all running sessions are terminated and new prompt forwarding is blocked. |
| `/killswitch off` | Disable the kill switch, restoring normal operation. |
| `/trace` | Show the current trace state with on/off buttons. |
| `/trace on` | Attach a debug trace block (provider, mode, session ID, alias) to each agent response. |
| `/trace off` | Disable trace output. |

### Git

| Command | Description |
|---|---|
| `/git` | Show repository name, current branch, root path, and number of modified files for the active session. |
| `/files` | List up to 200 files with uncommitted changes in the active session's repository. |
| `/diff` | Show the full `git diff` output for the active session's repository. |
| `/worktree <index> [as <alias>] [branch <name>]` | Create a new git worktree from a listed session's repository, bind a fresh agent session to it, and set it as the default. The worktree is automatically removed when you run `/close <alias>`. |

### History

| Command | Description |
|---|---|
| `/history [n]` | Show the last *n* prompt/response pairs for the current session alias. Defaults to 5, maximum 20. |

---

## Sending Prompts

Any plain text message (non-command) is forwarded to the active session's agent. While the agent is working, a typing indicator is shown and live progress messages are streamed back as they arrive.

**Multi-session routing** — prefix a message with `@<alias>` to route it to a specific named session instead of the default:

```
@work refactor the auth module
```

---

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
   - Optional: `security_alert_chat_ids` can include one or more admin chat IDs to receive security alerts.

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
