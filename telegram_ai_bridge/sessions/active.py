from __future__ import annotations

from datetime import datetime, timezone

from ..db import Database
from ..models import ActiveSession, LocalSession, ProviderName


class ActiveSessionManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    def bind(self, chat_id: int, session: LocalSession, alias: str = "default", make_default: bool = True) -> ActiveSession:
        active = ActiveSession(
            telegram_chat_id=str(chat_id),
            provider=session.provider,
            session_id=session.session_id,
            cwd=session.cwd,
            process_id=None,
            alias=alias,
            mode="resumed",
            status="ready",
            created_at=datetime.now(timezone.utc),
            last_message_at=datetime.now(timezone.utc),
            model=session.model,
        )
        self.db.upsert_binding(active)
        if make_default:
            self.db.set_default_alias(chat_id, alias)
        return active

    def bind_new(
        self,
        chat_id: int,
        provider: ProviderName,
        cwd: str,
        alias: str = "default",
        make_default: bool = True,
        model: str | None = None,
    ) -> ActiveSession:
        now = datetime.now(timezone.utc)
        active = ActiveSession(
            telegram_chat_id=str(chat_id),
            provider=provider,
            session_id=f"pending:{int(now.timestamp())}",
            cwd=cwd,
            process_id=None,
            alias=alias,
            mode="new",
            status="ready",
            created_at=now,
            last_message_at=now,
            model=model,
        )
        self.db.upsert_binding(active)
        if make_default:
            self.db.set_default_alias(chat_id, alias)
        return active

    def get(self, chat_id: int, alias: str | None = None) -> ActiveSession | None:
        if alias:
            return self.db.get_binding(chat_id, alias)
        default_alias = self.db.get_default_alias(chat_id)
        if default_alias:
            hit = self.db.get_binding(chat_id, default_alias)
            if hit:
                return hit
        bindings = self.db.list_bindings(chat_id)
        return bindings[0] if bindings else None

    def list_all(self) -> list[ActiveSession]:
        return self.db.list_all_bindings()

    def list_for_chat(self, chat_id: int) -> list[ActiveSession]:
        return self.db.list_bindings(chat_id)

    def set_default(self, chat_id: int, alias: str) -> None:
        self.db.set_default_alias(chat_id, alias)

    def get_default_alias(self, chat_id: int) -> str | None:
        return self.db.get_default_alias(chat_id)

    def close(self, chat_id: int, alias: str | None = None) -> None:
        if alias is None:
            current = self.get(chat_id)
            if current:
                self.db.delete_binding(chat_id, current.alias or "default")
            return
        self.db.delete_binding(chat_id, alias)
