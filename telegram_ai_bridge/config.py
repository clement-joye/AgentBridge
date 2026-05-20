from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(slots=True)
class ProviderConfig:
    enabled: bool = True


@dataclass(slots=True)
class BridgeConfig:
    telegram_bot_token: str
    allowed_user_ids: set[int]
    allowed_chat_ids: set[int]
    security_alert_chat_ids: set[int]
    allowed_repo_roots: list[Path]
    blocked_paths: list[Path]
    codex: ProviderConfig
    copilot: ProviderConfig
    state_dir: Path
    runner_timeout_seconds: int
    audit_enabled: bool
    deny_destructive_prompts: bool


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "telegram-ai-bridge"


def load_config(path: str | Path) -> BridgeConfig:
    raw = tomllib.loads(Path(path).read_text())
    token = raw.get("telegram_bot_token", "").strip()
    if not token:
        raise ValueError("telegram_bot_token is required")

    def to_path_list(key: str) -> list[Path]:
        values = raw.get(key, [])
        return [Path(v).expanduser().resolve() for v in values]

    state_dir = Path(raw.get("state_dir", str(DEFAULT_STATE_DIR))).expanduser().resolve()
    runner_timeout_seconds = int(raw.get("runner_timeout_seconds", 180))
    safety = raw.get("safety", {})
    return BridgeConfig(
        telegram_bot_token=token,
        allowed_user_ids=set(int(v) for v in raw.get("allowed_user_ids", [])),
        allowed_chat_ids=set(int(v) for v in raw.get("allowed_chat_ids", [])),
        security_alert_chat_ids=set(int(v) for v in raw.get("security_alert_chat_ids", [])),
        allowed_repo_roots=to_path_list("allowed_repo_roots"),
        blocked_paths=to_path_list("blocked_paths"),
        codex=ProviderConfig(enabled=bool(raw.get("codex", {}).get("enabled", True))),
        copilot=ProviderConfig(enabled=bool(raw.get("copilot", {}).get("enabled", True))),
        state_dir=state_dir,
        runner_timeout_seconds=max(30, runner_timeout_seconds),
        audit_enabled=bool(safety.get("audit_enabled", True)),
        deny_destructive_prompts=bool(safety.get("deny_destructive_prompts", True)),
    )
