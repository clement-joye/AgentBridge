# Package Structure

## Root Package
`telegram_ai_bridge/`

## Core Modules
- `main.py`: CLI entrypoint and runtime startup.
- `config.py`: configuration loading and validation.
- `telegram_bot.py`: update handlers and command routing.
- `commands.py`: optional command composition layer.
- `models.py`: typed runtime models.
- `db.py`: SQLite persistence.

## Subpackages
- `providers/`
  - `base.py`
  - `codex.py`
  - `copilot.py`
- `sessions/`
  - `discovery.py`
  - `registry.py`
  - `active.py`
  - `runner.py`
- `utils/`
  - `git.py`
  - `paths.py`
  - `formatting.py`
  - `security.py`
  - `logging.py`

## Documentation Convention
All deep-dive docs live under `instructions/` using `*.instructions.md` naming.
