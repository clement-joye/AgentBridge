from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ProviderName = Literal["codex", "copilot"]
ActiveMode = Literal["resumed", "new"]
ActiveStatus = Literal["starting", "ready", "running", "closed", "error"]


@dataclass(slots=True)
class LocalSession:
    index: int
    provider: ProviderName
    session_id: str
    cwd: str
    repo_name: str | None
    branch: str | None
    last_active_at: datetime
    last_user_prompt: str | None
    last_final_answer: str | None
    resumable: bool
    source_path: str | None
    git_dirty_count: int = 0
    model: str | None = None


@dataclass(slots=True)
class ActiveSession:
    telegram_chat_id: str
    provider: ProviderName
    session_id: str
    cwd: str
    process_id: int | None
    alias: str | None
    mode: ActiveMode
    status: ActiveStatus
    created_at: datetime
    last_message_at: datetime
    model: str | None = None
