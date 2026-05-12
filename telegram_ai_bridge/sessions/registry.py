from __future__ import annotations

from ..db import Database
from ..models import LocalSession


class SessionRegistry:
    def __init__(self, db: Database) -> None:
        self.db = db

    def store_last(self, chat_id: int, sessions: list[LocalSession]) -> None:
        self.db.save_last_sessions(chat_id, sessions)

    def get_indexed(self, chat_id: int, index: int) -> LocalSession | None:
        sessions = self.db.load_last_sessions(chat_id)
        for s in sessions:
            if s.index == index:
                return s
        return None
